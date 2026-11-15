using System.Net;
using System.Text;
using Swapwright.Core;
using Xunit;

namespace Swapwright.Tests;

public class TestSessionTests
{
    private sealed class FakeHost : ITempModHost
    {
        public readonly List<string> Calls = new();
        public bool FailAdd, FailRemove;
        public readonly HashSet<(string, Guid, int)> Live = new();

        public bool AddTemporaryMod(string tag, Guid collection, IReadOnlyDictionary<string, string> files, int priority, out string error)
        {
            Calls.Add($"add {collection} {priority} {files.Count}");
            error = FailAdd ? "InvalidGamePath" : string.Empty;
            if (FailAdd) return false;
            Live.Add((tag, collection, priority));
            return true;
        }

        public bool RemoveTemporaryMod(string tag, Guid collection, int priority, out string error)
        {
            Calls.Add($"remove {collection} {priority}");
            error = FailRemove ? "refused" : string.Empty;
            if (FailRemove) return false;
            Live.Remove((tag, collection, priority));
            return true;
        }
    }

    private static readonly Guid C1 = Guid.NewGuid(), C2 = Guid.NewGuid();
    private static Dictionary<string, string> Files(int n = 1) =>
        Enumerable.Range(0, n).ToDictionary(i => $"chara/x{i}.pap", i => $"Z:\\tmp\\x{i}.pap");

    [Fact]
    public void Start_AddsWithTagAndPriority99()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        Assert.True(s.Start(C1, "j1", "A -> Pray", Files(2)));
        Assert.Equal(new[] { $"add {C1} 99 2" }, host.Calls);
        Assert.Contains((TestSession.Tag, C1, 99), host.Live);
        Assert.Equal("j1", s.Active!.JobId);
        Assert.Equal("Swapwright test", TestSession.Tag);
    }

    [Fact]
    public void Start_ReplacesPreviousTest()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        s.Start(C1, "j1", "a", Files());
        s.Start(C2, "j2", "b", Files());
        Assert.Equal(new[] { $"add {C1} 99 1", $"remove {C1} 99", $"add {C2} 99 1" }, host.Calls);
        Assert.Single(host.Live);
        Assert.Equal(C2, s.Active!.Collection);
    }

    [Fact]
    public void Start_DoesNotStackIfOldCannotBeRemoved()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        s.Start(C1, "j1", "a", Files());
        host.FailRemove = true;
        Assert.False(s.Start(C1, "j2", "b", Files()));
        Assert.Equal("j1", s.Active!.JobId);
        Assert.Contains("RemoveTemporaryMod", s.LastError);
    }

    [Fact]
    public void Start_FailedAdd_LeavesNoActive()
    {
        var host = new FakeHost { FailAdd = true };
        var s = new TestSession(host);
        Assert.False(s.Start(C1, "j1", "a", Files()));
        Assert.Null(s.Active);
        Assert.Contains("AddTemporaryMod", s.LastError);
    }

    [Fact]
    public void Start_NoFiles_Refuses()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        Assert.False(s.Start(C1, "j1", "a", new Dictionary<string, string>()));
        Assert.Empty(host.Calls);
    }

    [Fact]
    public void StopInstallDispose_RemoveOnce()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        s.Start(C1, "j1", "a", Files());
        Assert.True(s.OnInstalled());
        Assert.Null(s.Active);
        s.Stop();
        s.Dispose();
        Assert.Equal(1, host.Calls.Count(c => c.StartsWith("remove")));
    }

    [Fact]
    public void Dispose_RemovesActive()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        s.Start(C1, "j1", "a", Files());
        s.Dispose();
        Assert.Empty(host.Live);
    }

    [Fact]
    public void Forget_DoesNotCallPenumbra()
    {
        var host = new FakeHost();
        var s = new TestSession(host);
        s.Start(C1, "j1", "a", Files());
        s.Forget();
        s.Dispose();
        Assert.DoesNotContain(host.Calls, c => c.StartsWith("remove"));
    }
}

public class SwapdApiTests
{
    private sealed class Handler : HttpMessageHandler
    {
        public readonly List<(string Method, string Path, string? Token, string? Body)> Seen = new();
        public Func<HttpRequestMessage, int, (HttpStatusCode, string)> Respond = (_, _) => (HttpStatusCode.OK, "{}");

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            request.Headers.TryGetValues(SwapdApi.TokenHeader, out var tok);
            var body = request.Content is null ? null : await request.Content.ReadAsStringAsync(ct);
            Seen.Add((request.Method.Method, request.RequestUri!.AbsolutePath, tok?.FirstOrDefault(), body));
            var (code, text) = Respond(request, Seen.Count);
            return new HttpResponseMessage(code) { Content = new StringContent(text, Encoding.UTF8, "application/json") };
        }
    }

    private static readonly Uri Base = new("http://127.0.0.1:47815");

    private sealed class SlowHandler : HttpMessageHandler
    {
        public TimeSpan Delay;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            await Task.Delay(Delay, ct);
            var text = request.RequestUri!.AbsolutePath == "/install" ? "{\"folder\":\"f\",\"diff_ok\":true}" : "[]";
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(text, Encoding.UTF8, "application/json") };
        }
    }

    [Fact]
    public void Timeouts_InstallTenMinutes_Others30s()
    {
        Assert.Equal(TimeSpan.FromMinutes(10), SwapdApi.InstallTimeout);
        Assert.Equal(TimeSpan.FromSeconds(30), SwapdApi.DefaultTimeout);
    }

    [Fact]
    public async Task InstallUsesItsOwnLongerTimeout()
    {
        var h = new SlowHandler { Delay = TimeSpan.FromMilliseconds(400) };
        using var api = new SwapdApi(Base, () => "t", h, defaultTimeout: TimeSpan.FromMilliseconds(100), installTimeout: TimeSpan.FromSeconds(10));
        var mods = await api.GetModsAsync();
        Assert.False(mods.Ok);
        Assert.Contains("did not answer /mods", mods.Error);
        var ins = await api.InstallAsync("j");
        Assert.True(ins.Ok, ins.Error);
        Assert.Equal("f", ins.Value!.Folder);
    }

    [Fact]
    public async Task CallerCancellation_StillThrows()
    {
        var h = new SlowHandler { Delay = TimeSpan.FromSeconds(5) };
        using var api = new SwapdApi(Base, () => "t", h);
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(50));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => api.InstallAsync("j", cts.Token));
    }

    [Fact]
    public async Task Ping_HasNoToken()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, "{\"ok\": true, \"version\": \"1\"}") };
        using var api = new SwapdApi(Base, () => "tok", h);
        var r = await api.PingAsync();
        Assert.True(r.Ok && r.Value);
        Assert.Null(h.Seen[0].Token);
    }

    [Fact]
    public async Task On401_RereadsTokenAndRetriesOnce()
    {
        var reads = 0;
        var h = new Handler
        {
            Respond = (req, n) => req.Headers.GetValues(SwapdApi.TokenHeader).First() == "new"
                ? (HttpStatusCode.OK, "[]")
                : (HttpStatusCode.Unauthorized, "{\"error\": \"bad token\"}"),
        };
        using var api = new SwapdApi(Base, () => ++reads == 1 ? "old" : "new", h);
        var r = await api.GetUsedAsync();
        Assert.True(r.Ok);
        Assert.Equal(new[] { "old", "new" }, h.Seen.Select(s => s.Token));
    }

    [Fact]
    public async Task On401Twice_ReportsError_NoThirdTry()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.Unauthorized, "{\"error\": \"bad token\"}") };
        using var api = new SwapdApi(Base, () => "x", h);
        var r = await api.GetModsAsync();
        Assert.False(r.Ok);
        Assert.Equal(401, r.StatusCode);
        Assert.Contains("bad token", r.Error);
        Assert.Equal(2, h.Seen.Count);
    }

    [Fact]
    public async Task Build_SendsContractBody()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, "{\"job\": \"7\"}") };
        using var api = new SwapdApi(Base, () => "t", h);
        var r = await api.BuildAsync("PM Drama", "Psych Up");
        Assert.Equal("7", r.Value!.Job);
        Assert.Equal("/build", h.Seen[0].Path);
        Assert.Contains("\"dir\":\"PM Drama\"", h.Seen[0].Body);
        Assert.Contains("\"emote\":\"Psych Up\"", h.Seen[0].Body);
        Assert.Contains("\"from_emote\":null", h.Seen[0].Body);
        Assert.Contains("\"from_key\":null", h.Seen[0].Body);
        Assert.Contains("\"no_duck\":false", h.Seen[0].Body);
    }

    [Fact]
    public async Task Build_SendsFromKey()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, "{\"job\": \"8\"}") };
        using var api = new SwapdApi(Base, () => "t", h);
        await api.BuildAsync("PM Wife", "Cheer Jump", "emote_sp/sp12_loop");
        Assert.Contains("\"from_key\":\"emote_sp/sp12_loop\"", h.Seen[0].Body);
    }

    [Fact]
    public async Task Mods_ParsesDances()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, "[{\"dir\":\"A\",\"name\":\"A\",\"keys\":[\"emote_sp/sp10_loop\"],\"dances\":[\"emote_sp/sp10_loop\",\"emote_sp/sp12_loop\"]},{\"dir\":\"B\",\"name\":\"B\",\"keys\":[]}]") };
        using var api = new SwapdApi(Base, () => "t", h);
        var r = await api.GetModsAsync();
        Assert.Equal(new[] { "emote_sp/sp10_loop", "emote_sp/sp12_loop" }, r.Value![0].Dances);
        Assert.Empty(r.Value![1].Dances);
    }

    [Fact]
    public async Task Job_ParsesFullStatus()
    {
        const string json = """
        {"state":"built","log":"ok","out_dir":"/tmp/claude-1000/x_out","folder_name":"X [Pray test]",
         "report":{"paps_patched":7,"races":18,"unmapped":["emote/j_laugh"],"scd":[{"file":"sound/x.scd","action":"patched","detail":"d"}]},
         "verify":{"pass":true,"checks":[{"name":"meta","pass":true,"detail":""}]}}
        """;
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, json) };
        using var api = new SwapdApi(Base, () => "t", h);
        var j = (await api.GetJobAsync("7")).Value!;
        Assert.True(j.IsInstallable);
        Assert.Equal(7, j.Report!.PapsPatched);
        Assert.Equal("patched", j.Report.Scd![0].Action);
        Assert.Equal("X [Pray test]", j.FolderName);
    }

    [Fact]
    public async Task Job_NullReportFields_StayNull()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, "{\"state\":\"failed\",\"report\":{\"paps_patched\":null,\"races\":null,\"unmapped\":null,\"scd\":null},\"verify\":null}") };
        using var api = new SwapdApi(Base, () => "t", h);
        var j = (await api.GetJobAsync("1")).Value!;
        Assert.Null(j.Report!.PapsPatched);
        Assert.False(j.IsInstallable);
    }

    [Fact]
    public async Task Redirects_SendsSettings_ParsesUnsupported()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.OK, "{\"files\":{\"chara/a.pap\":\"Z:\\\\tmp\\\\a.pap\"},\"unsupported\":[\"file swap x\"]}") };
        using var api = new SwapdApi(Base, () => "t", h);
        var r = await api.GetRedirectsAsync("7", new Dictionary<string, List<string>> { ["Music"] = new() { "On" } });
        Assert.Equal("/job/7/redirects", h.Seen[0].Path);
        Assert.Contains("\"settings\":{\"Music\":[\"On\"]}", h.Seen[0].Body);
        Assert.Equal("Z:\\tmp\\a.pap", r.Value!.Files["chara/a.pap"]);
        Assert.Equal("file swap x", r.Value.Unsupported[0]);
    }

    [Fact]
    public async Task Error409_ReturnsPlainMessage()
    {
        var h = new Handler { Respond = (_, _) => (HttpStatusCode.Conflict, "{\"error\": \"a build is running\"}") };
        using var api = new SwapdApi(Base, () => "t", h);
        var r = await api.InstallAsync("1");
        Assert.Equal(409, r.StatusCode);
        Assert.StartsWith("a build is running", r.Error);
    }

    [Fact]
    public async Task Unreachable_IsError_NotThrow()
    {
        using var api = new SwapdApi(new Uri("http://127.0.0.1:1"), () => "t");
        var r = await api.PingAsync();
        Assert.False(r.Ok);
    }
}

public class JobPollerTests
{
    private static SwapdResult<JobStatus> S(string state) => SwapdResult<JobStatus>.Success(new JobStatus { State = state }, 200);

    [Fact]
    public async Task PollsUntilNotRunning()
    {
        var seq = new Queue<SwapdResult<JobStatus>>(new[] { S("running"), S("running"), S("built") });
        var seen = new List<string>();
        var r = await JobPoller.WaitAsync(_ => Task.FromResult(seq.Dequeue()), j => seen.Add(j.State), TimeSpan.Zero, default);
        Assert.Equal("built", r.Value!.State);
        Assert.Equal(new[] { "running", "running", "built" }, seen);
    }

    [Fact]
    public async Task ClientError_StopsImmediately()
    {
        var calls = 0;
        var r = await JobPoller.WaitAsync(_ => { calls++; return Task.FromResult(SwapdResult<JobStatus>.Fail("no such job", 404)); },
            _ => { }, TimeSpan.Zero, default);
        Assert.False(r.Ok);
        Assert.Equal(1, calls);
    }

    [Fact]
    public async Task TransportErrors_RetryThenGiveUp()
    {
        var calls = 0;
        var r = await JobPoller.WaitAsync(_ => { calls++; return Task.FromResult(SwapdResult<JobStatus>.Fail("refused", 0)); },
            _ => { }, TimeSpan.Zero, default, maxErrors: 3);
        Assert.False(r.Ok);
        Assert.Equal(3, calls);
    }
}
