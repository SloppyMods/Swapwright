using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Swapwright.Core;

/// <summary>Result of one swapd call: either a value or a plain error message.</summary>
public sealed record SwapdResult<T>(T? Value, string? Error, int StatusCode)
{
    public bool Ok => Error is null;
    public static SwapdResult<T> Success(T value, int status) => new(value, null, status);
    public static SwapdResult<T> Fail(string error, int status) => new(default, error, status);
}

/// <summary>
/// HTTP client for swapd (CONTRACT.md). Every call except /ping sends X-Swapwright-Token.
/// On 401 the token is re-read once and the call retried. Never call from ImGui Draw.
/// </summary>
public sealed class SwapdApi : IDisposable
{
    public const string TokenHeader = "X-Swapwright-Token";

    private static readonly JsonSerializerOptions Json = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };

    /// <summary>Per-request timeout for every call except /install.</summary>
    public static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(30);

    /// <summary>/install copies a whole mod (cp -a + diff -r) before answering, so it gets longer.</summary>
    public static readonly TimeSpan InstallTimeout = TimeSpan.FromMinutes(10);

    private readonly HttpClient _http;
    private readonly Func<string?> _readToken;
    private readonly TimeSpan _defaultTimeout;
    private readonly TimeSpan _installTimeout;
    private string? _token;

    public SwapdApi(Uri baseAddress, Func<string?> readToken, HttpMessageHandler? handler = null,
        TimeSpan? defaultTimeout = null, TimeSpan? installTimeout = null)
    {
        _http = handler is null ? new HttpClient() : new HttpClient(handler);
        _http.BaseAddress = baseAddress;
        _http.Timeout = Timeout.InfiniteTimeSpan; // enforced per request in SendAsync
        _defaultTimeout = defaultTimeout ?? DefaultTimeout;
        _installTimeout = installTimeout ?? InstallTimeout;
        _readToken = readToken;
        _token = SafeRead();
    }

    public async Task<SwapdResult<bool>> PingAsync(CancellationToken ct = default)
    {
        var r = await SendAsync<PingResponse>(HttpMethod.Get, "/ping", null, auth: false, ct).ConfigureAwait(false);
        return r.Ok
            ? SwapdResult<bool>.Success(r.Value?.Ok == true, r.StatusCode)
            : SwapdResult<bool>.Fail(r.Error!, r.StatusCode);
    }

    public Task<SwapdResult<List<ModEntry>>> GetModsAsync(CancellationToken ct = default)
        => SendAsync<List<ModEntry>>(HttpMethod.Get, "/mods", null, true, ct);

    public Task<SwapdResult<List<ModEntry>>> GetUsedAsync(CancellationToken ct = default)
        => SendAsync<List<ModEntry>>(HttpMethod.Get, "/used", null, true, ct);

    public Task<SwapdResult<JobStarted>> BuildAsync(string dir, string emote, string? fromKey = null, CancellationToken ct = default)
        => SendAsync<JobStarted>(HttpMethod.Post, "/build",
            new Dictionary<string, object?> { ["dir"] = dir, ["emote"] = emote, ["from_emote"] = null, ["from_key"] = fromKey, ["no_duck"] = false }, true, ct);

    public Task<SwapdResult<JobStatus>> GetJobAsync(string job, CancellationToken ct = default)
        => SendAsync<JobStatus>(HttpMethod.Get, $"/job/{Uri.EscapeDataString(job)}", null, true, ct);

    public Task<SwapdResult<RedirectsResult>> GetRedirectsAsync(string job, IReadOnlyDictionary<string, List<string>> settings, CancellationToken ct = default)
        => SendAsync<RedirectsResult>(HttpMethod.Post, $"/job/{Uri.EscapeDataString(job)}/redirects",
            new Dictionary<string, object?> { ["settings"] = settings }, true, ct);

    public Task<SwapdResult<InstallResult>> InstallAsync(string job, CancellationToken ct = default)
        => SendAsync<InstallResult>(HttpMethod.Post, "/install", new Dictionary<string, object?> { ["job"] = job }, true, ct, _installTimeout);

    public Task<SwapdResult<OkResponse>> ResultAsync(string folder, string outcome, string note = "", CancellationToken ct = default)
        => SendAsync<OkResponse>(HttpMethod.Post, "/result",
            new Dictionary<string, object?> { ["folder"] = folder, ["outcome"] = outcome, ["note"] = note }, true, ct);

    private async Task<SwapdResult<T>> SendAsync<T>(HttpMethod method, string path, object? body, bool auth, CancellationToken ct,
        TimeSpan? timeout = null)
    {
        var limit = timeout ?? _defaultTimeout;
        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(limit);
        var rt = timeoutCts.Token;
        try
        {
            var (status, text) = await SendOnceAsync(method, path, body, auth, rt).ConfigureAwait(false);
            if (status == HttpStatusCode.Unauthorized && auth)
            {
                _token = SafeRead(); // token may have been recreated by a swapd restart
                (status, text) = await SendOnceAsync(method, path, body, auth, rt).ConfigureAwait(false);
            }

            var code = (int)status;
            if (code is < 200 or > 299)
                return SwapdResult<T>.Fail(ErrorText(code, text), code);

            var value = JsonSerializer.Deserialize<T>(text, Json);
            return value is null
                ? SwapdResult<T>.Fail($"swapd returned an empty body for {path}", code)
                : SwapdResult<T>.Success(value, code);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException)
        {
            return SwapdResult<T>.Fail($"swapd did not answer {path} within {limit.TotalSeconds:0} s", 0);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException)
        {
            return SwapdResult<T>.Fail(e is JsonException ? $"Bad JSON from swapd: {e.Message}" : e.Message, 0);
        }
    }

    private async Task<(HttpStatusCode, string)> SendOnceAsync(HttpMethod method, string path, object? body, bool auth, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(method, path);
        if (auth && !string.IsNullOrEmpty(_token))
            req.Headers.TryAddWithoutValidation(TokenHeader, _token);
        if (body is not null)
        {
            req.Content = new StringContent(JsonSerializer.Serialize(body, Json), Encoding.UTF8);
            req.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json") { CharSet = "utf-8" };
        }
        using var resp = await _http.SendAsync(req, ct).ConfigureAwait(false);
        var text = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        return (resp.StatusCode, text);
    }

    private static string ErrorText(int code, string body)
    {
        try
        {
            using var doc = JsonDocument.Parse(body);
            if (doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("error", out var err) && err.ValueKind == JsonValueKind.String)
                return $"{err.GetString()} (HTTP {code})";
        }
        catch (JsonException) { }
        return code == 401 ? "swapd rejected the token (HTTP 401)" : $"HTTP {code}";
    }

    private string? SafeRead()
    {
        try { return _readToken()?.Trim(); }
        catch { return null; }
    }

    public void Dispose() => _http.Dispose();

    public sealed class PingResponse
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("version")] public string? Version { get; set; }
    }

    public sealed class OkResponse
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
    }
}
