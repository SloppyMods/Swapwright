namespace Swapwright.Core;

/// <summary>The two Penumbra temporary-mod calls the test needs. The plugin implements it over IPC.</summary>
public interface ITempModHost
{
    /// <summary>AddTemporaryMod(tag, collection, files, "", priority). True on success.</summary>
    bool AddTemporaryMod(string tag, Guid collection, IReadOnlyDictionary<string, string> files, int priority, out string error);

    /// <summary>RemoveTemporaryMod(tag, collection, priority). True on success or nothing to remove.</summary>
    bool RemoveTemporaryMod(string tag, Guid collection, int priority, out string error);
}

/// <summary>The temp mod that is live right now.</summary>
public sealed record ActiveTest(Guid Collection, string JobId, string Label, int FileCount);

/// <summary>
/// Decision D8 lifecycle: at most one test temp mod at a time. Starting a new test removes the old one first;
/// install, stop and dispose remove it. If Penumbra goes away its temp mods go with it, so we only forget.
/// </summary>
public sealed class TestSession
{
    public const string Tag = "Swapwright test";
    public const int Priority = 99;

    private readonly ITempModHost _host;

    public TestSession(ITempModHost host) => _host = host;

    public ActiveTest? Active { get; private set; }

    public string? LastError { get; private set; }

    public bool Start(Guid collection, string jobId, string label, IReadOnlyDictionary<string, string> files)
    {
        LastError = null;
        if (!Stop()) return false; // could not remove the old one: do not stack a second temp mod

        if (files.Count == 0)
        {
            LastError = "The build produced no file redirects to load.";
            return false;
        }

        if (!_host.AddTemporaryMod(Tag, collection, files, Priority, out var error))
        {
            LastError = $"AddTemporaryMod failed: {error}";
            return false;
        }

        Active = new ActiveTest(collection, jobId, label, files.Count);
        return true;
    }

    /// <summary>Removes the active test, if any. Returns false (and keeps it) only if Penumbra refused.</summary>
    public bool Stop()
    {
        if (Active is null) return true;
        if (!_host.RemoveTemporaryMod(Tag, Active.Collection, Priority, out var error))
        {
            LastError = $"RemoveTemporaryMod failed: {error}";
            return false;
        }
        Active = null;
        return true;
    }

    /// <summary>Install replaces the test with the real library mod.</summary>
    public bool OnInstalled() => Stop();

    public void Dispose() => Stop();

    /// <summary>Penumbra was disposed; its temporary mods are gone already.</summary>
    public void Forget() => Active = null;
}
