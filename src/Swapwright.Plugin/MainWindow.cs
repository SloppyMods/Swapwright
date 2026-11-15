using System.Numerics;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Windowing;
using Dalamud.Plugin.Services;
using Dalamud.Interface.Components;
using Dalamud.Interface;
using FFXIVClientStructs.FFXIV.Client.System.String;
using FFXIVClientStructs.FFXIV.Client.UI;
using FFXIVClientStructs.FFXIV.Client.Game.UI;
using Emote = Lumina.Excel.Sheets.Emote;
using Swapwright.Core;

namespace Swapwright.Plugin;

/// <summary>
/// The Swapwright window. Draw never does HTTP: every swapd call runs on a background task that writes
/// results into fields Draw reads. Penumbra IPC runs on the framework thread (Draw or RunOnFrameworkThread).
/// </summary>
public sealed class MainWindow : Window, IDisposable
{
    private static readonly Vector4 Red = new(1f, 0.35f, 0.35f, 1f);
    private static readonly Vector4 Green = new(0.4f, 0.9f, 0.4f, 1f);
    private static readonly Vector4 Amber = new(1f, 0.75f, 0.3f, 1f);
    private const string ChooserPopup = "Choose which mod###SwapwrightChooser";

    private sealed record EmoteRow(uint Id, string Name, string NameLower, IReadOnlyList<string> Keys, string? Command, bool IsFacial);

    private readonly Configuration _config;
    private readonly Action _save;
    private readonly SwapdApi _swapd;
    private readonly PenumbraGateway _penumbra;
    private readonly TestSession _test;
    private readonly IFramework _framework;
    private readonly IPluginLog _log;
    private readonly CancellationTokenSource _cts = new();
    private readonly List<EmoteRow> _emotes;

    // --- written by background tasks, read by Draw (reference assignments only) ---
    private volatile bool _swapdUp;
    private volatile string? _swapdError;
    private volatile bool _disposed;
    private readonly RefreshGate _refresh = new();
    private bool _refreshing => _refresh.IsBusy;
    private volatile bool _busy;
    private volatile string? _busyText;
    private volatile string? _actionError;
    private volatile string? _actionInfo;
    private volatile List<ModEntry> _mods = new();
    private volatile List<ModEntry> _used = new();
    private volatile Dictionary<uint, List<ModEntry>> _usedByEmote = new();
    private volatile Dictionary<string, bool> _enabled = new();
    private volatile string? _jobId;
    private volatile JobStatus? _job;
    private volatile RedirectsResult? _redirects;
    private volatile InstallResult? _install;

    // --- Draw-thread only ---
    private bool _penumbraUp;
    private (Guid Id, string Name)? _collection;
    private DateTime _nextPenumbraCheck;
    private string _modFilter = string.Empty;
    private string _emoteFilter = string.Empty;
    private uint? _targetEmote;
    private string? _fromKey;      // dance (donor PAP key) chosen for a mod that carries two
    private string? _fromKeyDir;   // the mod folder that choice belongs to
    private string? _jobModDir;
    private string? _jobEmoteName;
    private uint? _jobEmoteId;
    private string? _resultFolder;
    private uint? _chooserEmote;
    private string? _chooserDir;
    private bool _openChooser;

    public MainWindow(Configuration config, Action save, SwapdApi swapd, PenumbraGateway penumbra, TestSession test,
        IFramework framework, IDataManager data, IPluginLog log)
        : base("Swapwright###SwapwrightMain")
    {
        _config = config;
        _save = save;
        _swapd = swapd;
        _penumbra = penumbra;
        _test = test;
        _framework = framework;
        _log = log;
        Size = new Vector2(720, 760);
        SizeCondition = ImGuiCond.FirstUseEver;

        _emotes = data.GetExcelSheet<Emote>()
            .Select(e => (Row: e, Name: e.Name.ExtractText()))
            .Where(e => !string.IsNullOrWhiteSpace(e.Name))
            .Select(e =>
            {
                var keys = EmoteMatcher.EmoteKeys(new[] { TimelineKey(e.Row, 0), TimelineKey(e.Row, 1) });
                return new EmoteRow(e.Row.RowId, e.Name, e.Name.ToLowerInvariant(), keys, TextCommand(e.Row), EmoteMatcher.IsFacialOnly(keys));
            })
            .OrderBy(e => e.Name, StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    private static string? TextCommand(Emote emote)
    {
        if (!emote.TextCommand.IsValid) return null;
        var c = emote.TextCommand.Value.Command.ExtractText();
        return string.IsNullOrWhiteSpace(c) ? null : c.Trim();
    }

    private static string? TimelineKey(Emote emote, int index)
    {
        var tl = emote.ActionTimeline[index];
        return tl.IsValid ? tl.Value.Key.ExtractText() : null;
    }

    public override void OnOpen() => RequestRefresh();

    // ------------------------------------------------------------------ background work

    /// <summary>
    /// Re-reads swapd (/ping, /mods, /used) then the Penumbra enabled states. Safe to call from any thread.
    /// While one is running a normal request is dropped; <paramref name="force"/> queues one more run after it
    /// (used after Install, whose new folder the running refresh may have missed).
    /// </summary>
    public void RequestRefresh() => RequestRefresh(false);

    public void RequestRefresh(bool force)
    {
        if (_disposed) return;
        if (_refresh.Request(force)) _ = Task.Run(RefreshLoopAsync);
    }

    private async Task RefreshLoopAsync()
    {
        do await RefreshOnceAsync();
        while (_refresh.Finish(stop: _disposed)); // a queued request keeps the loop going
    }

    private async Task RefreshOnceAsync()
    {
        try
        {
            var ct = _cts.Token;
            var ping = await _swapd.PingAsync(ct);
            _swapdUp = ping.Ok && ping.Value;
            _swapdError = ping.Ok ? null : ping.Error;
            if (!_swapdUp) return;

            var mods = await _swapd.GetModsAsync(ct);
            if (mods.Ok) _mods = mods.Value!.OrderBy(m => m.DisplayName, StringComparer.OrdinalIgnoreCase).ToList();
            else _swapdError = $"/mods: {mods.Error}";

            var used = await _swapd.GetUsedAsync(ct);
            if (used.Ok) SetUsed(used.Value!);
            else _swapdError = $"/used: {used.Error}";

            await _framework.RunOnFrameworkThread(RefreshEnabledStates);
        }
        catch (OperationCanceledException) { }
        catch (ObjectDisposedException) when (_disposed) { }
        catch (Exception e) { _log.Warning(e, "Swapwright refresh failed"); _swapdError = e.Message; }
    }

    private void SetUsed(List<ModEntry> used)
    {
        var byEmote = new Dictionary<uint, List<ModEntry>>();
        foreach (var e in _emotes)
        {
            var users = EmoteMatcher.ModsUsing(used, e.Keys);
            if (users.Count > 0) byEmote[e.Id] = users.ToList();
        }
        _used = used;
        _usedByEmote = byEmote;
    }

    /// <summary>Framework thread: enabled state of every /used mod in the player's collection.</summary>
    private void RefreshEnabledStates()
    {
        _penumbraUp = _penumbra.IsAvailable;
        _collection = _penumbraUp ? _penumbra.GetPlayerCollection() : null;
        var map = new Dictionary<string, bool>();
        if (_collection is { } c)
            foreach (var m in _used)
                map[m.Dir] = _penumbra.GetSettings(c.Id, m.Dir)?.Enabled ?? false;
        _enabled = map;
    }

    private void RunAction(string label, Func<CancellationToken, Task> work)
    {
        if (_busy) return;
        _busy = true;
        _busyText = label;
        _actionError = null;
        _actionInfo = null;
        _ = Task.Run(async () =>
        {
            try { await work(_cts.Token); }
            catch (OperationCanceledException) { }
            catch (Exception e) { _log.Warning(e, "Swapwright {Action} failed", label); _actionError = e.Message; }
            finally { _busy = false; _busyText = null; }
        });
    }

    /// <summary>D8 Test: build + verify, then load the output as a Penumbra temporary mod.</summary>
    private void StartTest(string modDir, string modName, uint emoteId, string emoteName, string? fromKey)
    {
        _jobModDir = modDir;
        _jobEmoteName = emoteName;
        _jobEmoteId = emoteId;
        _jobId = null;
        _job = null;
        _redirects = null;
        _install = null;
        RunAction($"Testing {modName} on {emoteName}", async ct =>
        {
            // The build replaces the out dir the active temp mod points into: drop that temp mod first.
            var stopped = await _framework.RunOnFrameworkThread(() => _disposed || _test.Stop());
            if (_disposed) return;
            if (!stopped) { _actionError = $"Could not remove the previous test, not building: {_test.LastError}"; return; }

            var started = await _swapd.BuildAsync(modDir, emoteName, fromKey, ct);
            if (!started.Ok) { _actionError = $"Build refused: {started.Error}"; return; }
            var jobId = started.Value!.Job;
            _jobId = jobId;

            var final = await JobPoller.WaitAsync(c => _swapd.GetJobAsync(jobId, c), j => _job = j, TimeSpan.FromSeconds(1), ct);
            if (!final.Ok) { _actionError = $"Job status: {final.Error}"; return; }
            var job = final.Value!;
            if (job.State != JobStatus.Built) { _actionError = "Build failed; see the log below."; return; }
            if (job.Verify is not { Pass: true }) { _actionError = "Built, but verification failed; not loading the test."; return; }

            // The original mod's current option choices decide which build-output files get loaded.
            var (collection, settings) = await _framework.RunOnFrameworkThread(() =>
            {
                var coll = _penumbra.IsAvailable ? _penumbra.GetPlayerCollection() : null;
                var s = coll is { } c ? _penumbra.GetSettings(c.Id, modDir)?.Options : null;
                return (coll, s ?? new Dictionary<string, List<string>>());
            });
            if (collection is null) { _actionError = "Penumbra or the player collection is not available; built but not loaded."; return; }

            var redirects = await _swapd.GetRedirectsAsync(jobId, settings, ct);
            if (!redirects.Ok) { _actionError = $"/redirects: {redirects.Error}"; return; }
            _redirects = redirects.Value!;

            var label = $"{modName} -> {emoteName}";
            var ok = await _framework.RunOnFrameworkThread(() =>
                !_disposed && !ct.IsCancellationRequested // never add a temp mod after Dispose removed ours
                && _test.Start(collection.Value.Id, jobId, label, redirects.Value!.Files));
            if (_disposed) return;
            if (!ok) { _actionError = _test.LastError ?? "Could not load the test."; return; }

            // Ask Penumbra where the player's own collection sends each body PAP, and redraw so the game drops cached animations.
            var (resolved, total, sample, redrawn) = await _framework.RunOnFrameworkThread(() => CheckPlayerResolves(redirects.Value!.Files));
            var msg = $"Test loaded: {label} ({redirects.Value!.Files.Count} files, priority {TestSession.Priority}). ";
            if (total == 0) msg += "No body animation paths to check. ";
            else if (resolved == total) msg += $"Your character resolves {resolved}/{total} body animation paths to the test files. ";
            else { _actionError = $"The test is loaded, but your character resolves only {resolved}/{total} body animation paths to it. {sample}"; }
            msg += redrawn ? "Redrawn; play the emote." : "Redraw failed; zone or /penumbra redraw self, then play the emote.";
            _actionInfo = msg;
            _log.Information("Swapwright test {Label}: collection {Collection}, {Files} files, player resolves {Resolved}/{Total} body paths; {Sample}",
                label, collection.Value.Name, redirects.Value!.Files.Count, resolved, total, sample);
        });
    }

    /// <summary>Framework thread. Counts body PAP redirects the local player's collection actually resolves to the test file, then redraws.</summary>
    private (int Resolved, int Total, string Sample, bool Redrawn) CheckPlayerResolves(IReadOnlyDictionary<string, string> files)
    {
        static string Norm(string p) => p.Replace('\\', '/').ToLowerInvariant();
        int total = 0, resolved = 0;
        string sample = "";
        foreach (var (gp, file) in files)
        {
            if (!gp.Contains("/bt_common/", StringComparison.Ordinal) || !gp.EndsWith(".pap", StringComparison.Ordinal)) continue;
            total++;
            var r = _penumbra.ResolvePlayerPath(gp);
            if (r is not null && Norm(r) == Norm(file)) resolved++;
            else if (sample.Length == 0) sample = $"{gp} -> {r ?? "(IPC failed)"} (wanted {file})";
        }
        if (sample.Length == 0 && total > 0) sample = "all resolved";
        return (resolved, total, sample, _penumbra.RedrawPlayer());
    }

    /// <summary>Install: new library folder, enable it, disable the original and every other mod on the target emote, drop the test temp mod.</summary>
    private void StartInstall(string jobId, string originalDir, uint? emoteId)
    {
        RunAction("Installing", async ct =>
        {
            var r = await _swapd.InstallAsync(jobId, ct);
            if (!r.Ok) { _actionError = $"Install refused: {r.Error}"; return; }
            _install = r.Value!;
            if (!r.Value!.DiffOk)
            {
                _actionError = $"Copied to '{r.Value.Folder}' but diff -r against the build FAILED. The mod was not added or enabled; check the folder by hand.";
                return;
            }

            var folder = r.Value.Folder;
            var msg = await _framework.RunOnFrameworkThread(() =>
            {
                // The test temp mod points into the build output: always try to drop it, whatever happens next.
                var removed = _test.OnInstalled();
                var removeNote = removed ? "" : $" The test temp mod could not be removed ({_test.LastError}); use Stop test.";
                var (ec, how) = _penumbra.AddOrReload(folder);
                if (ec != Penumbra.Api.Enums.PenumbraApiEc.Success) return $"Penumbra could not load '{folder}': {how}.{removeNote}";
                var coll = _penumbra.GetPlayerCollection();
                if (coll is null) return $"Added '{folder}', but no player collection to enable it in.{removeNote}";
                var targetUsers = emoteId is { } id && _usedByEmote.TryGetValue(id, out var u) ? u : new List<ModEntry>();
                var plan = TogglePlanner.PlanInstall(targetUsers, folder, originalDir);
                var res = InstallSteps.Apply(plan, t =>
                {
                    var tec = _penumbra.SetEnabled(coll.Value.Id, t.Dir, t.Enable);
                    return (tec is Penumbra.Api.Enums.PenumbraApiEc.Success or Penumbra.Api.Enums.PenumbraApiEc.NothingChanged, tec.ToString());
                });
                _resultFolder = folder;
                if (!res.Enabled)
                    return $"Installed '{folder}' ({how}), but enabling it failed ({res.EnableDetail}); nothing else was disabled.{removeNote}";
                return $"Installed '{folder}' ({how}); enabled: {res.EnableDetail}; disabled {res.Disabled.Count} other mod(s) on this emote and the original"
                       + (res.Failed.Count > 0 ? $"; could not disable {string.Join("; ", res.Failed)}" : "") + "." + removeNote;
            });
            _actionInfo = msg;
            RequestRefresh(force: true);
        });
    }

    private void PostResult(string folder, string outcome)
    {
        RunAction("Saving result", async ct =>
        {
            var r = await _swapd.ResultAsync(folder, outcome, string.Empty, ct);
            if (r.Ok) _actionInfo = $"Marked '{folder}' as {outcome}.";
            else _actionError = $"/result: {r.Error}";
            RequestRefresh();
        });
    }

    // ------------------------------------------------------------------ drawing

    public override unsafe void Draw()
    {
        if (DateTime.UtcNow >= _nextPenumbraCheck)
        {
            _penumbraUp = _penumbra.IsAvailable;
            _collection = _penumbraUp ? _penumbra.GetPlayerCollection() : null;
            _nextPenumbraCheck = DateTime.UtcNow.AddSeconds(2);
        }

        DrawStatus();
        var readOnly = !_swapdUp;
        ImGui.Separator();

        if (readOnly) ImGui.BeginDisabled();
        DrawModPicker();
        if (readOnly) ImGui.EndDisabled();

        DrawEmoteList(readOnly);
        DrawChooserPopup();
        ImGui.Separator();
        DrawActions(readOnly);
        DrawJob();
        DrawResultButtons(readOnly);
    }

    private void DrawStatus()
    {
        if (_swapdUp)
            ImGui.TextColored(Green, "swapd: running");
        else
            ImGui.TextColored(Red, "swapd is not running: start it with python3 ~/Projects/ffxiv-tools/Swapwright/swapd/swapd.py");
        ImGui.SameLine();
        ImGui.TextUnformatted("|");
        ImGui.SameLine();
        if (!_penumbraUp) ImGui.TextColored(Red, "Penumbra: not available");
        else ImGui.TextColored(Green, $"Penumbra: {(_collection is { } c ? c.Name : "no player collection")}");
        ImGui.SameLine();
        if (ImGui.Button(_refreshing ? "Refreshing..." : "Refresh")) RequestRefresh();
        if (_swapdError is { } err) ImGui.TextColored(Amber, err);
        if (_test.Active is { } t) ImGui.TextColored(Amber, $"Test active: {t.Label} ({t.FileCount} files)");
    }

    private void DrawModPicker()
    {
        var mods = _mods;
        var current = mods.FirstOrDefault(m => m.Dir == _config.LastModDir);
        ImGui.TextUnformatted("Mod:");
        ImGui.SameLine();
        ImGui.SetNextItemWidth(-1);
        if (!ImGui.BeginCombo("##mod", current?.DisplayName ?? (mods.Count == 0 ? "(no dance mods found)" : "(pick a dance mod)")))
            return;

        if (ImGui.IsWindowAppearing()) ImGui.SetKeyboardFocusHere();
        ImGui.SetNextItemWidth(-1);
        ImGui.InputTextWithHint("##modfilter", "Search mods", ref _modFilter, 128);
        foreach (var m in mods)
        {
            if (_modFilter.Length > 0
                && !m.DisplayName.Contains(_modFilter, StringComparison.OrdinalIgnoreCase)
                && !m.Dir.Contains(_modFilter, StringComparison.OrdinalIgnoreCase))
                continue;
            if (ImGui.Selectable($"{m.DisplayName}##{m.Dir}", m.Dir == _config.LastModDir))
            {
                _config.LastModDir = m.Dir;
                _save();
            }
            if (ImGui.IsItemHovered()) ImGui.SetTooltip($"{m.Dir}\n{m.Keys.Count} emote PAP key(s){(m.HasScd == true ? ", has music" : "")}");
        }
        ImGui.EndCombo();
    }

    private unsafe void DrawEmoteList(bool readOnly)
    {
        ImGui.TextUnformatted("Emotes (unlocked only)");
        ImGui.SameLine();
        var onlyUsed = _config.ShowOnlyUsed;
        if (ImGui.Checkbox("show only used", ref onlyUsed))
        {
            _config.ShowOnlyUsed = onlyUsed;
            _save();
        }
        ImGui.SameLine();
        var hideFacial = _config.HideFacial;
        if (ImGui.Checkbox("hide facial", ref hideFacial))
        {
            _config.HideFacial = hideFacial;
            _save();
            // Don't leave a row that just vanished as the Test/Install target.
            if (hideFacial && _emotes.Any(e => e.Id == _targetEmote && e.IsFacial)) _targetEmote = null;
        }
        if (ImGui.IsItemHovered()) ImGui.SetTooltip("Hide face-only emotes (the game's Expressions list). Dance mods can't go on them.");
        ImGui.SameLine();
        ImGui.SetNextItemWidth(-1);
        ImGui.InputTextWithHint("##emotefilter", "Search emotes", ref _emoteFilter, 64);

        var uiState = UIState.Instance();
        var byEmote = _usedByEmote;
        var enabled = _enabled;
        var filter = _emoteFilter.Trim().ToLowerInvariant();

        const ImGuiTableFlags flags = ImGuiTableFlags.RowBg | ImGuiTableFlags.BordersInnerV | ImGuiTableFlags.ScrollY | ImGuiTableFlags.SizingStretchProp;
        if (!ImGui.BeginTable("##emotes", 5, flags, new Vector2(0, 300))) return;
        ImGui.TableSetupScrollFreeze(0, 1);
        ImGui.TableSetupColumn("On", ImGuiTableColumnFlags.WidthFixed, 28);
        ImGui.TableSetupColumn("Play", ImGuiTableColumnFlags.WidthFixed, 30);
        ImGui.TableSetupColumn("Emote", ImGuiTableColumnFlags.WidthStretch, 1.2f);
        ImGui.TableSetupColumn("Mod(s)", ImGuiTableColumnFlags.WidthStretch, 2.5f);
        ImGui.TableSetupColumn("Status", ImGuiTableColumnFlags.WidthStretch, 1f);
        ImGui.TableHeadersRow();

        foreach (var e in _emotes)
        {
            if (hideFacial && e.IsFacial) continue;
            if (filter.Length > 0 && !e.NameLower.Contains(filter)) continue;
            byEmote.TryGetValue(e.Id, out var users);
            if (onlyUsed && users is null) continue;
            if (uiState == null || !uiState->IsEmoteUnlocked((ushort)e.Id)) continue;

            ImGui.TableNextRow();
            ImGui.PushID((int)e.Id);

            ImGui.TableNextColumn();
            var isOn = users is not null && TogglePlanner.IsChecked(users, d => enabled.TryGetValue(d, out var v) && v);
            var canToggle = users is not null && !readOnly && _penumbraUp && _collection is not null;
            if (!canToggle) ImGui.BeginDisabled();
            var check = isOn;
            if (ImGui.Checkbox("##on", ref check) && users is not null)
                OnToggle(e.Id, users, check);
            if (!canToggle) ImGui.EndDisabled();

            ImGui.TableNextColumn();
            if (e.Command is null) ImGui.BeginDisabled();
            if (ImGuiComponents.IconButton("##play", FontAwesomeIcon.Play)) PlayEmote(e);
            if (e.Command is null) ImGui.EndDisabled();
            if (ImGui.IsItemHovered(ImGuiHoveredFlags.AllowWhenDisabled)) ImGui.SetTooltip(e.Command is null ? "No text command for this row" : $"Play emote ({e.Command})");

            ImGui.TableNextColumn();
            if (ImGui.Selectable(e.Name, _targetEmote == e.Id)) _targetEmote = e.Id;
            if (ImGui.IsItemHovered()) ImGui.SetTooltip($"Click to make this the Test/Install target.\nKeys: {string.Join(", ", e.Keys)}");

            ImGui.TableNextColumn();
            if (users is null) ImGui.TextDisabled("-");
            else
            {
                for (var i = 0; i < users.Count; i++)
                {
                    if (i > 0) { ImGui.SameLine(0, 0); ImGui.TextUnformatted(", "); ImGui.SameLine(0, 0); }
                    var on = enabled.TryGetValue(users[i].Dir, out var v) && v;
                    if (on) ImGui.TextUnformatted(users[i].DisplayName);
                    else ImGui.TextDisabled(users[i].DisplayName);
                }
            }

            ImGui.TableNextColumn();
            if (users is null) ImGui.TextDisabled("-");
            else ImGui.TextUnformatted(string.Join(", ", users.Select(u => StatusText(u.Status)).Distinct()));

            ImGui.PopID();
        }
        ImGui.EndTable();
    }

    // Same as typing the emote's text command (e.g. /thumbsup) into the chat box. Draw runs on the framework thread.
    // The game itself refuses if the emote can't be used right now (mounted, in combat, and so on).
    private unsafe void PlayEmote(EmoteRow e)
    {
        if (e.Command is null) return;
        var ui = UIModule.Instance();
        if (ui == null) return;
        var mes = Utf8String.FromSequence(System.Text.Encoding.UTF8.GetBytes(e.Command));
        ui->ProcessChatBoxEntry(mes);
        mes->Dtor(true);
        _log.Debug("Play emote {0}: sent {1}", e.Name, e.Command);
    }

    private static string StatusText(string? status) => status switch
    {
        "awaiting" => "awaiting test",
        "dance_only" => "dance ok, music not cut",
        null or "" => "unknown",
        _ => status,
    };

    private void OnToggle(uint emoteId, List<ModEntry> users, bool turnOn)
    {
        if (turnOn && TogglePlanner.NeedsChoice(users))
        {
            _chooserEmote = emoteId;
            _chooserDir = TogglePlanner.DefaultChoice(users)?.Dir;
            _openChooser = true;
            return;
        }
        ApplyPlan(turnOn ? TogglePlanner.PlanOn(users) : TogglePlanner.PlanOff(users));
    }

    /// <summary>Draw thread (= framework thread): TrySetMod for each toggle in the player's collection.</summary>
    private void ApplyPlan(IReadOnlyList<ModToggle> plan)
    {
        if (_collection is not { } c) { _actionError = "No player collection."; return; }
        var map = new Dictionary<string, bool>(_enabled);
        var failures = new List<string>();
        foreach (var t in plan)
        {
            var ec = _penumbra.SetEnabled(c.Id, t.Dir, t.Enable);
            if (ec is Penumbra.Api.Enums.PenumbraApiEc.Success or Penumbra.Api.Enums.PenumbraApiEc.NothingChanged)
                map[t.Dir] = t.Enable;
            else failures.Add($"{t.Dir}: {ec}");
        }
        _enabled = map;
        _actionError = failures.Count > 0 ? "TrySetMod failed for " + string.Join("; ", failures) : null;
    }

    private void DrawChooserPopup()
    {
        if (_openChooser)
        {
            ImGui.OpenPopup(ChooserPopup);
            _openChooser = false;
        }
        if (!ImGui.BeginPopup(ChooserPopup)) return;

        if (_chooserEmote is { } id && _usedByEmote.TryGetValue(id, out var users))
        {
            ImGui.TextUnformatted("Several mods use this emote. Enable which one?");
            ImGui.TextDisabled("The others are switched off.");
            foreach (var u in users)
                if (ImGui.RadioButton($"{u.DisplayName} ({StatusText(u.Status)})##{u.Dir}", _chooserDir == u.Dir))
                    _chooserDir = u.Dir;
            if (ImGui.Button("Enable"))
            {
                ApplyPlan(TogglePlanner.PlanOn(users, _chooserDir));
                ImGui.CloseCurrentPopup();
            }
            ImGui.SameLine();
            if (ImGui.Button("Cancel")) ImGui.CloseCurrentPopup();
        }
        else ImGui.CloseCurrentPopup();
        ImGui.EndPopup();
    }

    /// <summary>A mod with two dances needs one picked; the choice is returned (null when there is nothing chosen or nothing to choose).
    /// More than two is not offered here: build those by hand with emote_swap.py --from-key.</summary>
    private string? DrawDancePicker(ModEntry? mod)
    {
        if (mod is null || mod.Dances.Count < 2) return null;
        if (mod.Dances.Count > 2)
        {
            ImGui.TextColored(Amber, $"This mod has {mod.Dances.Count} dances. Build one by hand: emote_swap.py --from-key <key>.");
            return null;
        }

        string Label(string key)
        {
            var names = EmoteMatcher.DanceLabel(key, _emotes.Select(e => (e.Name, (IReadOnlyCollection<string>)e.Keys)));
            return names is null ? key : $"{names}  ({key})";
        }

        var chosen = _fromKeyDir == mod.Dir && _fromKey is { } k && mod.Dances.Contains(k) ? k : null;
        ImGui.TextUnformatted("Dance:");
        ImGui.SameLine();
        ImGui.SetNextItemWidth(-1);
        if (ImGui.BeginCombo("##dance", chosen is null ? "(this mod has two dances: pick one)" : Label(chosen)))
        {
            foreach (var d in mod.Dances)
                if (ImGui.Selectable($"{Label(d)}##{d}", d == chosen))
                {
                    _fromKey = d;
                    _fromKeyDir = mod.Dir;
                    chosen = d;
                }
            ImGui.EndCombo();
        }
        if (ImGui.IsItemHovered()) ImGui.SetTooltip("The mod animates two dancers. Only the dance you pick is moved onto the target emote.");
        return chosen;
    }

    private void DrawActions(bool readOnly)
    {
        var mod = _mods.FirstOrDefault(m => m.Dir == _config.LastModDir);
        var emote = _targetEmote is { } id ? _emotes.FirstOrDefault(e => e.Id == id) : null;
        ImGui.TextUnformatted($"Target: {mod?.DisplayName ?? "(pick a mod)"}  ->  {emote?.Name ?? "(click an emote name)"}");

        var fromKey = DrawDancePicker(mod);
        var needsDance = mod is { Dances.Count: > 1 } && fromKey is null;

        var job = _job;
        var canTest = !readOnly && !_busy && mod is not null && emote is not null && _penumbraUp && !needsDance;
        if (!canTest) ImGui.BeginDisabled();
        if (ImGui.Button("Test")) StartTest(mod!.Dir, mod.DisplayName, emote!.Id, emote.Name, fromKey);
        if (!canTest) ImGui.EndDisabled();
        if (ImGui.IsItemHovered(ImGuiHoveredFlags.AllowWhenDisabled))
            ImGui.SetTooltip("Build + verify, then load the result as a Penumbra temporary mod (nothing is written to your library).");

        ImGui.SameLine();
        var canStop = _test.Active is not null;
        if (!canStop) ImGui.BeginDisabled();
        if (ImGui.Button("Stop test"))
        {
            if (_test.Stop()) _actionInfo = "Test removed.";
            else _actionError = _test.LastError;
        }
        if (!canStop) ImGui.EndDisabled();

        ImGui.SameLine();
        var canInstall = !readOnly && !_busy && _jobId is not null && job is { IsInstallable: true } && _jobModDir is not null && _install is null;
        if (!canInstall) ImGui.BeginDisabled();
        if (ImGui.Button("Install")) StartInstall(_jobId!, _jobModDir!, _jobEmoteId);
        if (!canInstall) ImGui.EndDisabled();
        if (ImGui.IsItemHovered(ImGuiHoveredFlags.AllowWhenDisabled))
            ImGui.SetTooltip("Copy the verified build into a NEW library folder, enable it and disable the original and every other mod on this emote. Needs a passed Test.");

        if (_busy) { ImGui.SameLine(); ImGui.TextColored(Amber, (_busyText ?? "Working") + "..."); }
        if (_actionError is { } err) ImGui.TextColored(Red, err);
        if (_actionInfo is { } info) ImGui.TextWrapped(info);

        if (_install is { } ins)
        {
            ImGui.TextUnformatted($"Installed folder: {ins.Folder}");
            ImGui.SameLine();
            if (ins.DiffOk) ImGui.TextColored(Green, "diff_ok: true");
            else ImGui.TextColored(Red, "diff_ok: FALSE - copy does not match the build. Mod not enabled.");
        }
    }

    private void DrawJob()
    {
        var job = _job;
        if (job is null) return;

        ImGui.Separator();
        var stateColor = job.State switch { JobStatus.Built => Green, JobStatus.Failed => Red, _ => Amber };
        ImGui.TextColored(stateColor, $"Job {_jobId}: {job.State}{(job.FolderName is { } f ? $"  ({f})" : "")}  [{_jobEmoteName}]");

        if (job.Report is { } r)
        {
            ImGui.TextUnformatted($"PAPs patched: {N(r.PapsPatched)}   races: {N(r.Races)}");
            if (r.Unmapped is { Count: > 0 } um)
                ImGui.TextWrapped($"Not mapped ({um.Count}): {string.Join(", ", um)}");
            else if (r.Unmapped is not null) ImGui.TextUnformatted("Not mapped: none");
            if (r.Scd is { } scd)
                foreach (var s in scd)
                    ImGui.TextWrapped($"Music {s.File}: {s.Action}{(string.IsNullOrEmpty(s.Detail) ? "" : $" ({s.Detail})")}");
        }

        if (job.Verify is { } v)
        {
            if (v.Pass) ImGui.TextColored(Green, "Verify: PASS");
            else ImGui.TextColored(Red, "Verify: FAIL");
            foreach (var c in v.Checks)
            {
                ImGui.TextColored(c.Pass ? Green : Red, c.Pass ? "  pass" : "  FAIL");
                ImGui.SameLine();
                ImGui.TextWrapped($"{c.Name}{(string.IsNullOrEmpty(c.Detail) ? "" : $": {c.Detail}")}");
            }
        }

        if (_redirects is { } rd)
        {
            ImGui.TextUnformatted($"Test redirects: {rd.Files.Count} file(s)");
            if (rd.Unsupported.Count > 0)
            {
                ImGui.TextColored(Amber, $"Not loaded in the test ({rd.Unsupported.Count}):");
                foreach (var u in rd.Unsupported) ImGui.TextWrapped($"  - {u}");
            }
        }

        if (!string.IsNullOrEmpty(job.Log) && ImGui.CollapsingHeader("Log tail"))
        {
            ImGui.BeginChild("##log", new Vector2(0, 160), true, ImGuiWindowFlags.HorizontalScrollbar);
            ImGui.TextUnformatted(job.Log);
            if (job.IsRunning) ImGui.SetScrollHereY(1f);
            ImGui.EndChild();
        }
    }

    private static string N(int? v) => v?.ToString() ?? "?";

    private void DrawResultButtons(bool readOnly)
    {
        ImGui.Separator();
        // Installed swap copies; narrowed to the target emote when one is selected.
        IEnumerable<ModEntry> swaps = _used.Where(u => u.IsSwap);
        if (_targetEmote is { } id && _usedByEmote.TryGetValue(id, out var forEmote))
            swaps = forEmote.Where(u => u.IsSwap);
        var list = swaps.ToList();
        if (_resultFolder is not null && list.All(s => s.Dir != _resultFolder) && _used.All(u => u.Dir != _resultFolder))
            list.Add(new ModEntry { Dir = _resultFolder, Name = _resultFolder, IsSwap = true, Status = "awaiting" });

        ImGui.TextUnformatted("After testing in game:");
        ImGui.SameLine();
        ImGui.SetNextItemWidth(320);
        var current = list.FirstOrDefault(s => s.Dir == _resultFolder);
        if (ImGui.BeginCombo("##resultswap", current is null ? "(pick an installed swap)" : $"{current.DisplayName} ({StatusText(current.Status)})"))
        {
            foreach (var s in list)
                if (ImGui.Selectable($"{s.DisplayName} ({StatusText(s.Status)})##{s.Dir}", s.Dir == _resultFolder))
                    _resultFolder = s.Dir;
            ImGui.EndCombo();
        }

        var can = !readOnly && !_busy && _resultFolder is not null;
        if (!can) ImGui.BeginDisabled();
        if (ImGui.Button("Worked")) PostResult(_resultFolder!, Outcome.Worked);
        ImGui.SameLine();
        if (ImGui.Button("Dance OK, music not cut")) PostResult(_resultFolder!, Outcome.DanceOnly);
        ImGui.SameLine();
        if (ImGui.Button("Failed")) PostResult(_resultFolder!, Outcome.Failed);
        if (!can) ImGui.EndDisabled();
    }

    public void Dispose()
    {
        _disposed = true;
        _cts.Cancel();
        _cts.Dispose();
    }
}
