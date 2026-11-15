#!/usr/bin/env python3
"""Seed ~/.local/share/swapwright/swaps.json from the emote_swap HANDOFF.md status table.

    seed_registry.py [--force] [--handoff PATH] [--library DIR] [--registry PATH]

Folder-driven: for every library folder whose name ends in 'test]' (`<mod> [<Emote> test]`), find the status-table
row it belongs to: a row that names the whole folder in backticks, else a row that names the mod in backticks and the
emote in its test cell. Names are compared after normalizing curly quotes, full-width forms (～), case and spacing.
Status: 'worked' if the row says 'Worked in game', 'awaiting' if it says 'awaiting' or 'NOT TESTED'. Folders with no
row, several equally good rows, or no status keyword are listed under "unmatched" and printed, never guessed.
The library is only listed, never written.
"""
import argparse, json, re, sys, unicodedata
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FOLDER_SPLIT = re.compile(r"^(.*?)\s*\[([^\[\]]+?)\s+test\]$")
TICKS = re.compile(r"`([^`]+)`")
QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u00b4": "'", "`": "'",
                        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
                        "\u301c": "~", "\u223c": "~", "\u02dc": "~"})


def norm(s):
    """Comparison key: NFKC (full-width ～ -> ~), straight quotes, lowercase, no whitespace."""
    s = unicodedata.normalize("NFKC", s).translate(QUOTES).lower()
    return re.sub(r"\s+", "", s)


def parse_rows(text):
    """(test cell, result cell) for each data row of the table under '## Status'."""
    rows, in_status = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_status = line.strip() == "## Status"
            continue
        if not in_status or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: ") or cells[0].lower() == "test":
            continue
        rows.append((cells[0], " | ".join(cells[1:])))
    return rows


def row_status(res):
    low = res.lower()
    if "worked in game" in low:
        return "worked"
    if "awaiting" in low or "not tested" in low:
        return "awaiting"
    return None


def match_rows(folder, rows):
    """Rows for one folder, best match kind first: ('folder', rows) if a row names the whole folder, else ('mod+emote', rows)."""
    m = FOLDER_SPLIT.match(folder)
    nf = norm(folder)
    full = [r for r in rows if any(norm(t) == nf for t in TICKS.findall(r[0] + " " + r[1]))]
    if full or not m:
        return "folder", full
    mod, emote = norm(m.group(1)), norm(m.group(2))
    return "mod+emote", [r for r in rows if any(norm(t) == mod for t in TICKS.findall(r[0])) and emote in norm(r[0])]


def seed(text, library, today=None):
    rows = parse_rows(text)
    folders = sorted(p.name for p in Path(library).iterdir() if p.is_dir() and p.name.endswith("test]")) if Path(library).is_dir() else []
    swaps, unmatched = {}, []
    for folder in folders:
        how, hits = match_rows(folder, rows)
        if len(hits) != 1:
            unmatched.append({"folder": folder, "reason": "no HANDOFF status row names this folder or its mod + emote" if not hits
                              else f"{len(hits)} rows match by {how}: " + "; ".join(h[0][:60] for h in hits)})
            continue
        test, res = hits[0]
        status = row_status(res)
        if not status:
            unmatched.append({"folder": folder, "reason": f"row {test[:60]!r} says neither 'Worked in game' nor 'awaiting'/'NOT TESTED'"})
            continue
        swaps[folder] = {"status": status, "source": "HANDOFF.md", "row": test, "matched_by": how,
                         "updated": today or datetime.now().isoformat(timespec="seconds")}
    return {"version": 1, "swaps": swaps, "unmatched": unmatched}


def main(argv=None):
    import swapd
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="overwrite an existing swaps.json")
    ap.add_argument("--handoff", type=Path, default=None)
    ap.add_argument("--library", type=Path, default=None)
    ap.add_argument("--registry", type=Path, default=None)
    a = ap.parse_args(argv)
    handoff = a.handoff or swapd.handoff_file()
    registry = a.registry or swapd.registry_file()
    library = a.library or swapd.library_root()
    if registry.exists() and not a.force:
        print(f"{registry} exists; not overwriting (use --force)", file=sys.stderr)
        return 2
    data = seed(handoff.read_text(encoding="utf-8"), library)
    registry.parent.mkdir(parents=True, exist_ok=True)
    tmp = registry.with_name(registry.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(registry)
    print(f"wrote {registry}: {len(data['swaps'])} matched")
    for k, v in data["swaps"].items():
        print(f"  {v['status']:9} {k}  (by {v['matched_by']})")
    print(f"{len(data['unmatched'])} folders unmatched:")
    for u in data["unmatched"]:
        print(f"  {u['folder']}: {u['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
