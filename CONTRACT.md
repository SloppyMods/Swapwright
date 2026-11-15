# Swapwright plugin <-> swapd contract (v1)

Plan: /home/you/html-artifacts/2026-11-04_swapwright-plugin-plan.html

## Transport
- swapd: Python 3 stdlib `http.server` (ThreadingHTTPServer), binds 127.0.0.1 only, port 47815 (override `SWAPD_PORT`).
- Auth: every request except `GET /ping` carries header `X-Swapwright-Token: <token>`. swapd creates the token on first start at `~/.local/share/swapwright/token` (mode 600, 32 hex chars). Plugin reads it at `Z:\home\SloppyMods\.local\share\swapwright\token` (Wine maps Z:\ to /). Missing/wrong token -> 401.
- All bodies JSON, UTF-8. Errors: HTTP 4xx/5xx with `{"error": "<plain message>"}`.

## Paths
- Library root: read `ModDirectory` from `~/.xlcore/pluginConfigs/Penumbra/config/penumbra.json` on every request that needs it. Value is a Windows path (`E:\Penumbra`); map `E:\` -> `/mnt/STORAGE/` and `Z:\` -> `/`. Refuse to run if the mapped path does not exist.
- Build output: `/tmp/claude-1000/<slug>_out` (slug = lowercase ascii of mod+emote, unsafe chars -> `_`). Never `$TMPDIR`.
- Registry: `~/.local/share/swapwright/swaps.json`.
- Tool: `/home/you/Projects/ffxiv-tools/_shared/tools/emote_swap/emote_swap.py` (do NOT modify).

## Endpoints
`GET /ping` -> `{"ok": true, "version": "1"}`

`GET /mods` -> `[{"dir": "<folder name>", "name": "<meta.json Name>", "keys": ["emote/dance16_loop", ...], "dances": ["emote/dance16_loop"], "has_scd": true}]`
`dances` = one loop key per distinct dance in the mod (`x_loop`, `x_start`, `x_no_target` count as one). Two or more means the mod carries several dancers and a build must name one.
Only mods with at least one redirect to a body emote PAP: game path matching `chara/human/c\d{4}/animation/a0001/bt_common/(<key>).pap` where key starts with `emote/` or `emote_sp/`. Read DefaultData + every group option (file v4 embeds groups in meta.json; older mods use `default_mod.json` + `group_*.json`). Cache per mod by meta/group json mtime.

`GET /used` -> `[{"dir", "name", "keys": [...], "status": "worked|dance_only|failed|awaiting|unknown", "is_swap": true}]`
Same scan as /mods (same list in fact); `status` from swaps.json matched by folder `dir`, else `unknown`. `is_swap` = name ends with `test]`. The PLUGIN maps keys -> Emote rows (Emote.ActionTimeline[0..1].Key), swapd does not.

`POST /build` `{"dir": "<mod folder>", "emote": "<Emote name as in game>", "from_emote": null, "no_duck": false}` -> `{"job": "<id>"}`
`from_key` (optional, string or null) = the donor game path of the dance to keep, e.g. `"emote_sp/sp12_loop"` (case-insensitive, no `.pap`); passed to `emote_swap.py --from-key`. A mod with 2+ `dances` and neither `from_emote` nor `from_key` -> 409 naming the dances. A `from_key` that is not one of the mod's dances, or sent together with `from_emote`, -> 400. The plugin offers the choice only for exactly two dances; mods with more are built by hand.
Runs emote_swap.py `--src <lib>/<dir> --emote <emote> --out /tmp/claude-1000/<slug>_out` (removing a stale out dir it owns first), then verify_swap.py on the output. One job at a time; second build while running -> 409.

`GET /job/<id>` -> `{"state": "running|built|failed", "log": "<tool stdout+stderr tail, max 8000 chars>", "out_dir": "...", "folder_name": "<meta Name of output>", "report": {"paps_patched": N, "races": N, "unmapped": ["emote/j_laugh", ...], "scd": [{"file": "sound/x.scd", "action": "patched|already_ducked|skipped", "detail": "..."}]}, "verify": {"pass": bool, "checks": [{"name": "...", "pass": bool, "detail": "..."}]}}`
`report` fields are parsed from tool output where available; fields that cannot be determined are null, never guessed. `unmapped` = the target emote's ActionTimeline slot keys that have a body PAP (`cbem_` name) for at least one race, per emote_swap's built Lumina lookup helper (`lookup/bin/Debug/net10.0/lookup.dll`, run read-only), minus the body keys the output meta.json maps; null if the helper is missing, stale (older than its Program.cs) or fails.

`POST /job/<id>/redirects` `{"settings": {"<group name>": ["<selected option name>", ...]}}` -> `{"files": {"<game path>": "<Windows path to file in out_dir>"}, "unsupported": ["<plain note>", ...]}`
For the reversible TEST (decision D8). Refuse unless state=built and verify.pass. Flattens the BUILD OUTPUT's meta.json: DefaultData files + files of the given selected options (the plugin sends the original mod's current settings from Penumbra; for a group not in `settings` use its DefaultSettings). Matches Penumbra: a higher-priority group wins over a lower one and, at equal group Priority, the later group in file order wins; DefaultData loses to every group. Inside a Multi group the selected options apply by option Priority descending with the first added winning, so the higher-priority option wins and at equal priority the earlier option (lower index) wins. Game-path keys are lowercased (Penumbra paths are case-insensitive); if two keys in one option collide after lowercasing with different targets, the first is kept and the collision is listed in `unsupported`. Windows path = `Z:` + absolute Linux path with `/` -> `\`. File swaps and meta manipulations are not converted: list them in `unsupported`, never drop silently.

`POST /install` `{"job": "<id>"}` -> `{"folder": "<new folder name>", "diff_ok": true}`
Refuse (409) unless state=built and verify.pass. Folder name = output meta.json Name sanitized for the filesystem. Create `<lib>/<folder>` atomically with `mkdir` (exists -> 409), then `cp -a <out_dir>/. <lib>/<folder>/`, then `diff -r`; if the copy or diff fails report it (500 / `diff_ok: false`) and DO NOT delete anything. Add swaps.json entry status `awaiting`.

`POST /result` `{"folder": "<dir>", "outcome": "worked|dance_only|failed", "note": ""}` -> `{"ok": true}`
Update swaps.json and append a dated line to the "Plugin results" section at the end of `_shared/tools/emote_swap/HANDOFF.md` (create the section if absent; never edit other rows).

## D8: Test vs Install (user decision 2026-11-04)
- **Test** (button) = build + verify, then the PLUGIN loads the result as a Penumbra *temporary mod* (IPC `AddTemporaryMod`, tag `Swapwright test`, player's current collection, priority 99) from `/job/<id>/redirects`. Nothing is written to the library. **Stop test** calls `RemoveTemporaryMod`; the plugin also removes it on dispose. Temporary mods also vanish on game restart.
- **Install** (button) = same as before: a NEW library folder named `<mod> [<Emote> test]`, original untouched. After install the plugin enables the new mod, disables the original and every other mod using the same target emote in the player's collection (reversible via the emote checkbox), removes any active test temp mod, and queues a refresh even if one is already running. Before a Test build the plugin removes the active test temp mod first (the build replaces the out dir it points into).

## Hard rules
- Never delete, rename, or write inside an existing library folder. Only /install creates a new one.
- Plugin never calls Penumbra `DeleteMod`. Plugin only uses GetModList, GetModDirectory, AddMod, ReloadMod, TrySetMod, GetCurrentModSettings, AddTemporaryMod, RemoveTemporaryMod, GetCollectionForObject (or equivalent current-collection call), GetEnabledState, and the Initialized and Disposed events (read-only; user decision 2026-11-04).
