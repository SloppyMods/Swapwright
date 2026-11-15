# swapd

Local HTTP service for the Swapwright plugin. Spec: `../CONTRACT.md`. Python 3 stdlib only.

## Run

    python3 swapd.py                      # 127.0.0.1:47815, token at ~/.local/share/swapwright/token
    python3 seed_registry.py              # one-time: swaps.json from HANDOFF.md (--force to overwrite)
    python3 verify_swap.py --src <original mod> --out <build dir>   # JSON report, exit 0/1

`swapd.service` is a systemd --user unit (not installed).

Env overrides: `SWAPD_PORT`, `SWAPD_LIBRARY` (skip penumbra.json), `SWAPD_PENUMBRA_CONFIG`, `SWAPD_REGISTRY`,
`SWAPD_TOKEN_FILE`, `SWAPD_HANDOFF`, `SWAPD_TMP_ROOT`, `SWAPD_TOOL`, `SWAPD_VERIFY`, `SWAPD_QUIET`.

## Test

    python3 -m unittest discover -s tests -t tests -v

Tests use temp folders only, a fake emote_swap tool, and synthetic PAP/SCD bytes.

## Endpoints (all but /ping need `X-Swapwright-Token`)

| method | path | does |
|---|---|---|
| GET | /ping | `{"ok": true, "version": "1"}` |
| GET | /mods | mods with body emote PAP redirects: dir, name, keys, has_scd |
| GET | /used | same list + status from swaps.json, is_swap |
| POST | /build | run emote_swap.py then verify_swap.py in a background job; 409 if one is running |
| GET | /job/<id> | state, log tail, out_dir, folder_name, report, verify |
| POST | /job/<id>/redirects | flattened game path -> `Z:\...` file map for a temporary mod (D8) |
| POST | /install | copy a verified build into the library as a new folder, diff -r it |
| POST | /result | record in-game outcome in swaps.json and HANDOFF.md "Plugin results" |

Notes: jobs live in memory (lost on restart). swapd only deletes build dirs it created (tracked in
`<tmp root>/.swapd_owned.json`). `report.unmapped` is always null: emote_swap.py does not print the target's unmapped
slots. `report.races` is the number of race codes mapped to the printed target keys in the output meta.json.
