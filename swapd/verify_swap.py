#!/usr/bin/env python3
"""Read-only checks on an emote_swap build output against its original mod.

    verify_swap.py --src <original mod dir> --out <build dir>

Prints JSON {"pass": bool, "checks": [{"name", "pass", "detail"}]} and exits 0 (all pass) or 1.
Neither folder is written. Also importable: swapd uses the mod-reading helpers below.
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path, PureWindowsPath

EMOTE_SWAP_DIR = Path(__file__).resolve().parents[2] / "_shared" / "tools" / "emote_swap"
BODY_GP = re.compile(r"^chara/human/(c\d{4})/animation/a0001/bt_common/((?:emote|emote_sp)/[a-z0-9_/]+)\.pap$", re.I)
NAME_RE = re.compile(rb"cbem_[a-z0-9_]+")
NO_TARGET = "no_target"
_UNSET = object()


# ---------- mod reading (shared with swapd) ----------

def _read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def mod_json_files(mod):
    """The json files whose mtimes define a mod's redirect state."""
    mod = Path(mod)
    files = [mod / "meta.json", mod / "default_mod.json"] + sorted(mod.glob("group_*.json"))
    return [f for f in files if f.is_file()]


def load_mod(mod):
    """Return (meta, default_data, groups). File v4 embeds DefaultData/Groups in meta.json; older mods use
    default_mod.json + group_*.json. Raises OSError/ValueError on unreadable json."""
    mod = Path(mod)
    meta = _read_json(mod / "meta.json")
    if not isinstance(meta, dict):
        raise ValueError("meta.json is not an object")
    default = meta.get("DefaultData")
    if default is None and (mod / "default_mod.json").is_file():
        default = _read_json(mod / "default_mod.json")
    groups = meta.get("Groups")
    if groups is None:
        groups = [_read_json(f) for f in sorted(mod.glob("group_*.json"))]
    return meta, default or {}, groups or []


def option_sets(default, groups):
    """[(group name, option name, option dict)] for DefaultData then every option of every group."""
    out = [("DefaultData", "Default", default)]
    for g in groups:
        for o in g.get("Options") or []:
            out.append((g.get("Name", "?"), o.get("Name", "?"), o))
    return out


def all_redirects(default, groups):
    """[(group, option, game path, local path)] over DefaultData and all options."""
    return [(g, o, gp, lp) for g, o, opt in option_sets(default, groups) for gp, lp in (opt.get("Files") or {}).items()]


def body_key(gp):
    m = BODY_GP.match(gp.replace("\\", "/"))
    return m.group(2).lower() if m else None


def _inside(root, p):
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def resolve_local(root, lp):
    """Resolve a mod-relative (maybe backslashed, maybe wrong-case) path. Returns (path or None, kind) where kind is
    'exact', 'case', 'ambiguous', 'missing' or 'outside' (absolute, drive, '..' or a symlink leaving the root)."""
    root = Path(root)
    win = PureWindowsPath(str(lp))
    if win.drive or win.root or ".." in win.parts or "\0" in str(lp):
        return None, "outside"
    parts = win.parts
    exact = root.joinpath(*parts)
    if exact.is_file():
        return (exact, "exact") if _inside(root, exact) else (None, "outside")
    p = root
    for part in parts:
        hit = [c for c in p.iterdir() if c.name.lower() == part.lower()] if p.is_dir() else []
        if len(hit) > 1:
            return None, "ambiguous"
        if not hit:
            return None, "missing"
        p = hit[0]
    if not p.is_file():
        return None, "missing"
    return (p, "case") if _inside(root, p) else (None, "outside")


def internal_name(key):
    """emote/dance16_loop -> cbem_dance16_2lp, x_start -> cbem_x_1, else cbem_<basename>."""
    base = key.rsplit("/", 1)[-1]
    if base.endswith("_loop"):
        base = base[:-5] + "_2lp"
    elif base.endswith("_start"):
        base = base[:-6] + "_1"
    return "cbem_" + base


def name_count(data, name):
    return sum(1 for n in NAME_RE.findall(data) if n == name.encode())


def is_nt_key(key):
    return NO_TARGET in key


def emote_from_name(name):
    """'<Mod> [<Emote> test]' -> '<Emote>' (the suffix emote_swap gives its output), else None."""
    m = re.search(r" \[([^\[\]]+) test\]$", name or "")
    return m.group(1) if m else None


def slot_name(slot):
    """The one internal name emote_swap patches in for this slot: c0101's, else the first race's."""
    races = slot.get("races") or {}
    return races.get("c0101") or next(iter(races.values()), None)


def key_names(slots):
    """{slot key: real internal name} from the lookup (empty when unknown). Derivation cannot be right for every key, e.g.
    Snowball's no-target row reads `cbem_throw_snow`, not `cbem_throw_snow_no_target`."""
    return {s["key"].lower(): n for s in slots or [] if s.get("key") and (n := slot_name(s))}


# ---------- target lookup (emote_swap's Lumina helper; shared with swapd) ----------

def lookup_path():
    return Path(os.environ.get("SWAPD_LOOKUP") or EMOTE_SWAP_DIR / "lookup/bin/Debug/net10.0/lookup.dll")


def lookup_slots(emote):
    """Target emote ActionTimeline slots from emote_swap's built Lumina lookup helper (read-only use), or None.
    A .dll is run with ~/.dotnet/dotnet; anything else is executed directly (tests). A dll older than its
    Program.cs is stale (emote_swap itself uses `dotnet run`, which would rebuild it) and is not used.
    Slots of a same-named no-target row carry variant == "no_target"."""
    p = lookup_path()
    if not p.is_file():
        return None
    if p.suffix == ".dll":
        prog = EMOTE_SWAP_DIR / "lookup/Program.cs"
        if prog.is_file() and prog.stat().st_mtime > p.stat().st_mtime:
            return None
        cmd = [str(Path.home() / ".dotnet/dotnet"), str(p), emote]
    else:
        cmd = [str(p), emote]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env={**os.environ, "DOTNET_NOLOGO": "1"})
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout.strip().splitlines()[-1])
        return info["slots"] if isinstance(info, dict) and isinstance(info.get("slots"), list) else None
    except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return None


# ---------- checks ----------

def _check(name, ok, detail):
    return {"name": name, "pass": bool(ok), "detail": detail}


def check_meta(src, out):
    try:
        sm, om = _read_json(Path(src) / "meta.json"), _read_json(Path(out) / "meta.json")
    except (OSError, ValueError) as e:
        return _check("meta", False, f"meta.json unreadable: {e}")
    probs = []
    if not om.get("Identifier"):
        probs.append("out has no Identifier")
    elif om.get("Identifier") == sm.get("Identifier"):
        probs.append("out Identifier equals src Identifier")
    if not str(om.get("Name", "")).endswith("test]"):
        probs.append(f"Name {om.get('Name')!r} does not end with 'test]'")
    return _check("meta", not probs, "; ".join(probs) or f"Name {om.get('Name')!r}, new Identifier {om.get('Identifier')}")


def check_local_files(out, redirects):
    missing, case, amb, n = [], [], [], 0
    seen = set()
    for g, o, gp, lp in redirects:
        if lp in seen:
            continue
        seen.add(lp)
        n += 1
        _, kind = resolve_local(out, lp)
        if kind == "missing":
            missing.append(f"{g}/{o}: {lp}")
        elif kind == "outside":
            missing.append(f"{g}/{o}: {lp} (OUTSIDE the mod folder)")
        elif kind == "ambiguous":
            amb.append(f"{g}/{o}: {lp}")
        elif kind == "case":
            case.append(lp)
    ok = not missing and not amb
    detail = f"{n} unique local files; {len(case)} case-only matches"
    if case:
        detail += " (" + ", ".join(sorted(case)[:10]) + (" ..." if len(case) > 10 else "") + ")"
    if missing:
        detail += f"; MISSING {len(missing)}: " + ", ".join(missing[:10])
    if amb:
        detail += f"; AMBIGUOUS {len(amb)}: " + ", ".join(amb[:10])
    return _check("local_files", ok, detail)


def donor_and_target_keys(src_redirects, out_redirects):
    # No-target keys are neither donors nor targets: emote_swap drops a source mapping on one and rebuilds the target's own
    # from the loop mapping, so a source that already maps one (a hand-added edit) must not turn it into a "donor key".
    src_keys = {k for *_, gp, _ in src_redirects if (k := body_key(gp)) and not is_nt_key(k)}
    out_keys = {k for *_, gp, _ in out_redirects if (k := body_key(gp)) and not is_nt_key(k)}
    targets = out_keys - src_keys
    donors = src_keys - targets
    return donors, targets


def check_no_leftover(out_redirects, donors, targets):
    if not donors:
        return _check("no_donor_leftovers", True, "SKIPPED: src has no body emote PAP redirects")
    if not targets:
        return _check("no_donor_leftovers", False, f"out has no new target emote keys; donor keys {sorted(donors)} still in use")
    left = sorted({f"{g}/{o}: {gp}" for g, o, gp, _ in out_redirects if body_key(gp) in donors})
    return _check("no_donor_leftovers", not left,
                  f"donor keys {sorted(donors)}, target keys {sorted(targets)}" + (f"; LEFTOVER {len(left)}: " + ", ".join(left[:10]) if left else ""))


_VARIANT = re.compile(r"^(.*)__.+_(?:loop|start|no_target)$")


def src_counterpart(src, out, f):
    """Same relative path in src; else, for emote_swap's `<stem>__<emote>_<kind>.pap`, the src `<stem>.pap`."""
    rel = f.relative_to(out)
    cand = Path(src) / rel
    if cand.is_file():
        return cand
    m = _VARIANT.match(f.stem)
    if m:
        cand = Path(src) / rel.with_name(m.group(1) + f.suffix)
        if cand.is_file():
            return cand
    return None


def donor_pap_bytes(src, src_redirects, donors):
    """Bytes of every src PAP that a donor-key redirect points at."""
    blobs = set()
    for _, _, gp, lp in src_redirects or []:
        if body_key(gp) in donors:
            p, _ = resolve_local(src, lp)
            if p:
                blobs.add(p.read_bytes())
    return blobs


def check_pap_names(src, out, out_redirects, donors, targets=(), src_redirects=None, names=None):
    names = names or {}
    by_file = {}
    probs, notes, patched = [], [], 0
    donor_blobs = donor_pap_bytes(src, src_redirects, donors)
    unpatched = set()
    for g, o, gp, lp in out_redirects:
        k = body_key(gp)
        if not k:
            continue
        p, _ = resolve_local(out, lp)
        if p:
            by_file.setdefault(p.resolve(), set()).add(k)
            if k in targets and p.read_bytes() in donor_blobs:
                unpatched.add(f"{g}/{o}: {gp} -> {lp}")
    for u in sorted(unpatched)[:10]:
        probs.append(f"target redirect points at an unpatched donor PAP ({u})")
    if len(unpatched) > 10:
        probs.append(f"... {len(unpatched) - 10} more target redirects at unpatched donor PAPs")
    donor_names = {internal_name(k) for k in donors}
    for f in sorted(Path(out).rglob("*")):
        if not f.is_file() or f.suffix.lower() != ".pap":
            continue
        data = f.read_bytes()
        cp = src_counterpart(src, out, f)
        if cp is not None and f.relative_to(out) == cp.relative_to(src) and cp.read_bytes() == data:
            continue  # unchanged copy
        if cp is None and not NAME_RE.search(data):
            continue  # no cbem_ name at all and no counterpart: not a body PAP we patched
        patched += 1
        rel = f.relative_to(out).as_posix()
        keys = by_file.get(f.resolve(), set())
        if not keys:
            probs.append(f"{rel}: patched PAP not referenced by any body emote redirect")
            continue
        plain = {k for k in keys if not is_nt_key(k)}
        tnames = {names.get(k) or internal_name(k) for k in plain}
        for k in keys - plain:  # a no-target key can share the loop's file (same internal name); its name is only derivable alone
            if k in names or not plain:
                tnames.add(names.get(k) or internal_name(k))
        for t in sorted(tnames):
            c = name_count(data, t)
            if c != 2:
                probs.append(f"{rel}: {t} appears {c}x (want 2)")
        dn = set(donor_names)
        if cp is not None:  # confirm the donor name against the src bytes
            found = {n.decode() for n in NAME_RE.findall(cp.read_bytes())}
            unconfirmed = sorted(dn - found)
            extra = found - tnames
            if extra and not (found & dn):
                notes.append(f"{rel}: src name(s) {sorted(found)} differ from derived {sorted(dn)}; checking both")
            dn |= extra
            if unconfirmed and found & dn:
                notes.append(f"{rel}: derived {unconfirmed} not in src bytes")
        for d in sorted(dn - tnames):
            c = name_count(data, d)
            if c:
                probs.append(f"{rel}: donor name {d} still appears {c}x")
    if not patched:
        return _check("pap_names", False, "no patched PAP found in out" + ("; " + "; ".join(probs) if probs else ""))
    return _check("pap_names", not probs, f"{patched} patched PAP(s)" + ("; " + "; ".join(probs) if probs else "")
                  + ("; notes: " + "; ".join(notes) if notes else ""))


def check_no_target(out, od, og, slots):
    """The target's same-named no-target row: its PAPs carry its internal name exactly twice and nothing else, and it is mapped
    for every race it has wherever the loop is (a key missing everywhere passes only as UNMAPPED, which swapd's report lists)."""
    nt = next((s for s in slots or [] if s.get("variant") == NO_TARGET and s.get("races")), None)
    sets = option_sets(od, og)
    present = {k for _, _, opt in sets for gp in (opt.get("Files") or {}) if (k := body_key(gp)) and is_nt_key(k)}
    if nt is None:
        if not present:
            return None
        return _check("no_target", slots is None, f"no-target redirect(s) {sorted(present)}: " + (
            "SKIPPED: no lookup data for the target emote, so which race/option should have one is not checked" if slots is None
            else "the target emote has no no-target row"))
    key, want = nt["key"].lower(), slot_name(nt)
    own = {s["key"].lower() for s in slots if s.get("races") and s.get("variant") != NO_TARGET and not s["key"].startswith("facial/")}
    nt_races = {r.lower() for r in nt["races"]}
    probs, files, redirects = [], {}, 0
    for g, o, opt in sets:
        loop_races, mapped = set(), {}
        for gp, lp in (opt.get("Files") or {}).items():
            m = BODY_GP.match(gp.replace("\\", "/"))
            if not m:
                continue
            race, k = m.group(1).lower(), m.group(2).lower()
            if k in own:
                loop_races.add(race)
            elif k == key:
                mapped[race] = lp
                redirects += 1
                files.setdefault(lp, f"{g}/{o}")
        if loop_races:  # same rule as the tool: the loop's c0101 mapping fills every race the no-target row has, else only the races mapped
            need = nt_races if "c0101" in loop_races else nt_races & loop_races
            gone = sorted(need - set(mapped))
            if gone and (present or mapped):
                probs.append(f"{g}/{o}: no-target mapping missing for {', '.join(gone)}")
    if not present:
        return _check("no_target", True, f"UNMAPPED: {key} has no mapping in this build (swapd lists it in report.unmapped)")
    for lp, where in sorted(files.items()):
        p, kind = resolve_local(out, lp)
        if p is None:
            probs.append(f"{where}: {lp} ({kind})")
            continue
        found = NAME_RE.findall(p.read_bytes())
        if found != [want.encode()] * 2:
            probs.append(f"{lp}: internal names {[n.decode() for n in found]}, want exactly {want} twice")
    return _check("no_target", not probs, f"{key} ({want}): {redirects} redirect(s), {len(files)} file(s)" + ("; " + "; ".join(probs[:10]) if probs else ""))


def check_face_kdb(src, out):
    n, probs = 0, []
    for f in sorted(Path(out).rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(out).as_posix()
        low = rel.lower()
        if not (low.endswith(".kdb") or (low.endswith(".pap") and ("/face/" in "/" + low or "cfx" in low))):
            continue
        n += 1
        s = Path(src) / f.relative_to(out)
        if not s.is_file():
            probs.append(f"{rel}: no src counterpart")
        elif s.read_bytes() != f.read_bytes():
            probs.append(f"{rel}: differs from src")
    if not n:
        return _check("face_kdb_unchanged", True, "SKIPPED: no face PAP or .kdb in out")
    return _check("face_kdb_unchanged", not probs, f"{n} face PAP/KDB files" + ("; " + "; ".join(probs[:10]) if probs else ", all byte-identical"))


def check_scd(src, out):
    files = sorted(f for f in Path(out).rglob("*") if f.is_file() and f.suffix.lower() == ".scd")
    if not files:
        return _check("scd_audio", True, "SKIPPED: no .scd in out")
    sys.path.insert(0, str(EMOTE_SWAP_DIR))
    import scd_duck
    probs, same, grown = [], 0, 0
    for f in files:
        rel = f.relative_to(out).as_posix()
        s = Path(src) / f.relative_to(out)
        if not s.is_file():
            probs.append(f"{rel}: no src counterpart")
            continue
        a, b = s.read_bytes(), f.read_bytes()
        if a == b:
            same += 1
            continue
        if len(b) != len(a) + 16:
            probs.append(f"{rel}: size {len(a)} -> {len(b)} (want identical or +16)")
            continue
        try:
            sa, sb = scd_duck.Scd(a), scd_duck.Scd(b)
        except scd_duck.ScdError as e:
            probs.append(f"{rel}: unparsable: {e}")
            continue
        if sa.audio_hash != sb.audio_hash or a[sa.audio_start:] != b[sb.audio_start:]:
            probs.append(f"{rel}: audio section changed")
            continue
        grown += 1
    return _check("scd_audio", not probs, f"{same} identical, {grown} +16 bytes with audio unchanged" + ("; " + "; ".join(probs) if probs else ""))


def verify(src, out, slots=_UNSET, emote=None):
    """slots: the target emote's lookup slots; by default looked up for `emote` or the emote named in the output's `[<Emote> test]` suffix."""
    src, out = Path(src), Path(out)
    checks = [check_meta(src, out)]
    try:
        _, sd, sg = load_mod(src)
        om, od, og = load_mod(out)
    except (OSError, ValueError) as e:
        checks.append(_check("load", False, f"cannot read mod json: {e}"))
        return {"pass": False, "checks": checks}
    sr, orr = all_redirects(sd, sg), all_redirects(od, og)
    donors, targets = donor_and_target_keys(sr, orr)
    if slots is _UNSET:
        emote = emote or emote_from_name(om.get("Name") if isinstance(om, dict) else None)
        slots = lookup_slots(emote) if emote else None
    nt_check = check_no_target(out, od, og, slots)
    checks += [check_local_files(out, orr), check_no_leftover(orr, donors, targets),
               check_pap_names(src, out, orr, donors, targets, sr, key_names(slots)), *([nt_check] if nt_check else []),
               check_face_kdb(src, out), check_scd(src, out)]
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--emote", help="target emote (default: the emote named in the output's 'Name [<Emote> test]' suffix)")
    a = ap.parse_args(argv)
    for p in (a.src, a.out):
        if not (Path(p) / "meta.json").is_file():
            print(json.dumps({"pass": False, "checks": [_check("meta", False, f"no meta.json in {p}")]}))
            return 1
    res = verify(a.src, a.out, emote=a.emote)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
