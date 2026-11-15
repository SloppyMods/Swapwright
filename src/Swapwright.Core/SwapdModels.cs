using System.Text.Json.Serialization;

namespace Swapwright.Core;

/// <summary>One entry of GET /mods and GET /used (CONTRACT.md). /mods omits status/is_swap.</summary>
public sealed class ModEntry
{
    [JsonPropertyName("dir")] public string Dir { get; set; } = string.Empty;
    [JsonPropertyName("name")] public string Name { get; set; } = string.Empty;
    [JsonPropertyName("keys")] public List<string> Keys { get; set; } = new();
    /// <summary>The mod's distinct dances, one loop key each. Two or more means the build needs a choice (from_key).</summary>
    [JsonPropertyName("dances")] public List<string> Dances { get; set; } = new();
    [JsonPropertyName("has_scd")] public bool? HasScd { get; set; }
    [JsonPropertyName("status")] public string? Status { get; set; }
    [JsonPropertyName("is_swap")] public bool IsSwap { get; set; }

    public string DisplayName => string.IsNullOrWhiteSpace(Name) ? Dir : Name;
}

public sealed class JobStarted
{
    [JsonPropertyName("job")] public string Job { get; set; } = string.Empty;
}

public sealed class ScdAction
{
    [JsonPropertyName("file")] public string? File { get; set; }
    [JsonPropertyName("action")] public string? Action { get; set; }
    [JsonPropertyName("detail")] public string? Detail { get; set; }
}

public sealed class JobReport
{
    [JsonPropertyName("paps_patched")] public int? PapsPatched { get; set; }
    [JsonPropertyName("races")] public int? Races { get; set; }
    [JsonPropertyName("unmapped")] public List<string>? Unmapped { get; set; }
    [JsonPropertyName("scd")] public List<ScdAction>? Scd { get; set; }
}

public sealed class VerifyCheck
{
    [JsonPropertyName("name")] public string Name { get; set; } = string.Empty;
    [JsonPropertyName("pass")] public bool Pass { get; set; }
    [JsonPropertyName("detail")] public string? Detail { get; set; }
}

public sealed class VerifyResult
{
    [JsonPropertyName("pass")] public bool Pass { get; set; }
    [JsonPropertyName("checks")] public List<VerifyCheck> Checks { get; set; } = new();
}

/// <summary>GET /job/&lt;id&gt;.</summary>
public sealed class JobStatus
{
    public const string Running = "running";
    public const string Built = "built";
    public const string Failed = "failed";

    [JsonPropertyName("state")] public string State { get; set; } = string.Empty;
    [JsonPropertyName("log")] public string? Log { get; set; }
    [JsonPropertyName("out_dir")] public string? OutDir { get; set; }
    [JsonPropertyName("folder_name")] public string? FolderName { get; set; }
    [JsonPropertyName("report")] public JobReport? Report { get; set; }
    [JsonPropertyName("verify")] public VerifyResult? Verify { get; set; }

    public bool IsRunning => State == Running;

    /// <summary>Install and the test redirects are only allowed in this state (CONTRACT /install, /redirects).</summary>
    public bool IsInstallable => State == Built && Verify is { Pass: true };
}

/// <summary>POST /job/&lt;id&gt;/redirects.</summary>
public sealed class RedirectsResult
{
    [JsonPropertyName("files")] public Dictionary<string, string> Files { get; set; } = new();
    [JsonPropertyName("unsupported")] public List<string> Unsupported { get; set; } = new();
}

/// <summary>POST /install.</summary>
public sealed class InstallResult
{
    [JsonPropertyName("folder")] public string Folder { get; set; } = string.Empty;
    [JsonPropertyName("diff_ok")] public bool DiffOk { get; set; }
}

public static class Outcome
{
    public const string Worked = "worked";
    public const string DanceOnly = "dance_only";
    public const string Failed = "failed";
}
