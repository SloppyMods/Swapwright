using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using Penumbra.Api.Enums;
using Penumbra.Api.IpcSubscribers;
using Swapwright.Core;

namespace Swapwright.Plugin;

/// <summary>
/// Penumbra access over IPC only. Uses GetModList, GetModDirectory, AddMod, ReloadMod, TrySetMod,
/// GetCurrentModSettings, AddTemporaryMod, RemoveTemporaryMod, GetCollectionForObject, GetEnabledState.
/// Never calls DeleteMod or any delete/rename API.
/// </summary>
public sealed class PenumbraGateway : ITempModHost, IDisposable
{
    private readonly IPluginLog _log;
    private readonly GetEnabledState _getEnabled;
    private readonly GetModList _getModList;
    private readonly GetModDirectory _getModDirectory;
    private readonly GetCollectionForObject _getCollectionForObject;
    private readonly GetCurrentModSettings _getSettings;
    private readonly TrySetMod _trySetMod;
    private readonly AddMod _addMod;
    private readonly ReloadMod _reloadMod;
    private readonly AddTemporaryMod _addTemp;
    private readonly RemoveTemporaryMod _removeTemp;
    private readonly ResolvePlayerPath _resolvePlayer;
    private readonly RedrawObject _redraw;
    private readonly IDisposable _initialized;
    private readonly IDisposable _disposed;

    public PenumbraGateway(IDalamudPluginInterface pi, IPluginLog log, Action onInitialized, Action onDisposed)
    {
        _log = log;
        _getEnabled = new GetEnabledState(pi);
        _getModList = new GetModList(pi);
        _getModDirectory = new GetModDirectory(pi);
        _getCollectionForObject = new GetCollectionForObject(pi);
        _getSettings = new GetCurrentModSettings(pi);
        _trySetMod = new TrySetMod(pi);
        _addMod = new AddMod(pi);
        _reloadMod = new ReloadMod(pi);
        _addTemp = new AddTemporaryMod(pi);
        _removeTemp = new RemoveTemporaryMod(pi);
        _resolvePlayer = new ResolvePlayerPath(pi);
        _redraw = new RedrawObject(pi);
        _initialized = Initialized.Subscriber(pi, onInitialized);
        _disposed = Disposed.Subscriber(pi, onDisposed);
    }

    public bool IsAvailable
    {
        get
        {
            try { return _getEnabled.Invoke(); }
            catch { return false; }
        }
    }

    public string? GetModDirectory()
    {
        try { return _getModDirectory.Invoke(); }
        catch { return null; }
    }

    public IReadOnlyDictionary<string, string> GetModList()
    {
        try { return _getModList.Invoke(); }
        catch { return new Dictionary<string, string>(); }
    }

    /// <summary>The local player's effective collection (object index 0).</summary>
    public (Guid Id, string Name)? GetPlayerCollection()
    {
        try
        {
            var (valid, _, effective) = _getCollectionForObject.Invoke(0);
            return valid ? (effective.Id, effective.Name) : null;
        }
        catch (Exception e) { _log.Warning(e, "GetCollectionForObject failed"); return null; }
    }

    /// <summary>Saved settings of a mod in the collection (inheritance applied); null if missing.</summary>
    public (bool Enabled, Dictionary<string, List<string>> Options)? GetSettings(Guid collection, string modDir)
    {
        try
        {
            var (ec, settings) = _getSettings.Invoke(collection, modDir);
            if (ec != PenumbraApiEc.Success || settings is null) return null;
            var (enabled, _, options, _) = settings.Value;
            return (enabled, options ?? new Dictionary<string, List<string>>());
        }
        catch (Exception e) { _log.Warning(e, "GetCurrentModSettings failed for {Mod}", modDir); return null; }
    }

    public PenumbraApiEc SetEnabled(Guid collection, string modDir, bool enabled)
    {
        try
        {
            // The subscriber names the bool "inherit", but it is forwarded as IPenumbraApiModSettings.TrySetMod(..., bool enabled).
            return _trySetMod.Invoke(collection, modDir, enabled);
        }
        catch (Exception e) { _log.Warning(e, "TrySetMod failed for {Mod}", modDir); return PenumbraApiEc.UnknownError; }
    }

    /// <summary>Registers a new library folder; falls back to ReloadMod when Penumbra already knows it.</summary>
    public (PenumbraApiEc Result, string How) AddOrReload(string folder)
    {
        try
        {
            var ec = _addMod.Invoke(folder);
            if (ec == PenumbraApiEc.Success) return (ec, "AddMod");
            var reload = _reloadMod.Invoke(folder);
            return (reload, $"AddMod returned {ec}; ReloadMod returned {reload}");
        }
        catch (Exception e) { _log.Warning(e, "AddMod/ReloadMod failed for {Mod}", folder); return (PenumbraApiEc.UnknownError, e.Message); }
    }

    public bool AddTemporaryMod(string tag, Guid collection, IReadOnlyDictionary<string, string> files, int priority, out string error)
    {
        try
        {
            var ec = _addTemp.Invoke(tag, collection, new Dictionary<string, string>(files), string.Empty, priority);
            error = ec.ToString();
            return ec == PenumbraApiEc.Success;
        }
        catch (Exception e) { error = e.Message; return false; }
    }

    public bool RemoveTemporaryMod(string tag, Guid collection, int priority, out string error)
    {
        try
        {
            var ec = _removeTemp.Invoke(tag, collection, priority);
            error = ec.ToString();
            return ec is PenumbraApiEc.Success or PenumbraApiEc.NothingChanged or PenumbraApiEc.CollectionMissing;
        }
        catch (Exception e) { error = e.Message; return false; }
    }

    /// <summary>Where Penumbra sends this game path for the local player's collection (the game path itself when nothing redirects it); null on IPC failure.</summary>
    public string? ResolvePlayerPath(string gamePath)
    {
        try { return _resolvePlayer.Invoke(gamePath); }
        catch (Exception e) { _log.Warning(e, "ResolvePlayerPath failed for {Path}", gamePath); return null; }
    }

    /// <summary>Redraws the local player so the game re-reads animation files. Returns false if the IPC call failed.</summary>
    public bool RedrawPlayer()
    {
        try { _redraw.Invoke(0, RedrawType.Redraw); return true; }
        catch (Exception e) { _log.Warning(e, "RedrawObject failed"); return false; }
    }

    public void Dispose()
    {
        _initialized.Dispose();
        _disposed.Dispose();
    }
}
