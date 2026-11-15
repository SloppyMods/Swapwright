#!/usr/bin/env python3
"""swapd: local HTTP service for the Swapwright plugin (contract: ../CONTRACT.md).

    python3 swapd.py            # 127.0.0.1:47815 (SWAPD_PORT)

Env overrides (tests): SWAPD_PORT, SWAPD_LIBRARY (skip penumbra.json), SWAPD_PENUMBRA_CONFIG, SWAPD_REGISTRY,
SWAPD_TOKEN_FILE, SWAPD_HANDOFF, SWAPD_TMP_ROOT, SWAPD_TOOL, SWAPD_VERIFY, SWAPD_LOOKUP.
"""
import hmac, json, os, re, secrets, shutil, subprocess, sys, threading, traceback, uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify_swap as vs

VERSION = "1"
STATE = Path.home() / ".local/share/swapwright"
TOOL_DEFAULT = Path("/home/you/Projects/ffxiv-tools/_shared/tools/emote_swap/emote_swap.py")


def env_path(name, default):
    return Path(os.environ.get(name) or default)


def token_file(): return env_path("SWAPD_TOKEN_FILE", STATE / "token")
def registry_file(): return env_path("SWAPD_REGISTRY", STATE / "swaps.json")
def handoff_file(): return env_path("SWAPD_HANDOFF", TOOL_DEFAULT.parent / "HANDOFF.md")
def tmp_root(): return env_path("SWAPD_TMP_ROOT", "/tmp/claude-1000")
def tool_path(): return env_path("SWAPD_TOOL", TOOL_DEFAULT)
def verify_path(): return env_path("SWAPD_VERIFY", HERE / "verify_swap.py")
def penumbra_config(): return env_path("SWAPD_PENUMBRA_CONFIG", Path.home() / ".xlcore/pluginConfigs/Penumbra/config/penumbra.json")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ---------- paths ----------

DRIVE_MAP = {"E": "/mnt/STORAGE/", "Z": "/"}


def map_windows_path(win):
    """E:\\Penumbra -> /mnt/STORAGE/Penumbra, Z:\\x\\y -> /x/y. Other drives raise ValueError."""
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", win or "")
    if not m or m.group(1).upper() not in DRIVE_MAP:
        raise ValueError(f"cannot map Windows path {win!r} (only E:\\ and Z:\\ are known)")
    rest = m.group(2).replace("\\", "/").strip("/")
    return Path(DRIVE_MAP[m.group(1).upper()] + rest) if rest else Path(DRIVE_MAP[m.group(1).upper()])


def to_windows(p):
    """Absolute Linux path -> Z:\\... (Wine's Z: is /)."""
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")


def library_root():
    if os.environ.get("SWAPD_LIBRARY"):
        root = Path(os.environ["SWAPD_LIBRARY"])
    else:
        try:
            cfg = json.loads(penumbra_config().read_text(encoding="utf-8-sig"))
            root = map_windows_path(cfg["ModDirectory"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ApiError(503, f"cannot read Penumbra ModDirectory: {e}")
    if not root.is_dir():
        raise ApiError(503, f"library path {root} does not exist; refusing to run")
    return root


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:80] or "mod"


MOD_SLUG_BUDGET, EMOTE_SLUG_MAX = 80, 150


def build_slug(mod, emote):
    """<mod>_<emote> slug. Only the mod part is shortened (to keep the whole near MOD_SLUG_BUDGET); the emote stays."""
    e = re.sub(r"[^a-z0-9]+", "_", emote.lower()).strip("_")[:EMOTE_SLUG_MAX].strip("_")
    m = re.sub(r"[^a-z0-9]+", "_", mod.lower()).strip("_")
    m = m[:max(MOD_SLUG_BUDGET - len(e) - 1, 16)].strip("_") or "mod"
    return f"{m}_{e}" if e else m


def sanitize_folder(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")
    if s in ("", ".", ".."):
        raise ApiError(400, f"unusable folder name {name!r}")
    return s


def lib_child(lib, d):
    if not isinstance(d, str) or not d or "/" in d or "\\" in d or d in (".", ".."):
        raise ApiError(400, "dir must be a single folder name in the library")
    p = lib / d
    if not (p / "meta.json").is_file():
        raise ApiError(404, f"no mod folder {d!r} with meta.json in the library")
    return p


# ---------- token ----------

def ensure_token():
    f = token_file()
    if f.is_file():
        tok = f.read_text().strip()
        if re.fullmatch(r"[0-9a-f]{32}", tok):
            return tok
        raise SystemExit(f"token file {f} is malformed; fix or remove it")
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tok = secrets.token_hex(16)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as h:
        h.write(tok + "\n")
    os.chmod(f, 0o600)
    return tok


# ---------- registry ----------

REG_LOCK = threading.Lock()


def load_registry():
    f = registry_file()
    if not f.is_file():
        return {"version": 1, "swaps": {}}
    data = json.loads(f.read_text(encoding="utf-8"))
    data.setdefault("swaps", {})
    return data


def save_registry(data):
    f = registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def set_status(folder, status, **extra):
    with REG_LOCK:
        reg = load_registry()
        e = reg["swaps"].setdefault(folder, {})
        e.update(extra)
        e["status"] = status
        e["updated"] = datetime.now().isoformat(timespec="seconds")
        save_registry(reg)


# ---------- scan ----------

_CACHE, _CACHE_LOCK = {}, threading.Lock()

_DANCE_BASE = re.compile(r"_(?:loop|start|no_target)$")
FROM_KEY_RE = re.compile(r"^emote(?:_sp)?/[a-z0-9_]+$")


def dance_keys(keys):
    """The distinct dances in a mod's body PAP keys: one key per dance, the loop. `x_loop`, `x_start` and `x_no_target` are one dance.
    A group that has only a start or no-target key has no loop to retarget and is not a dance of its own."""
    groups = {}
    for k in keys:
        groups.setdefault(_DANCE_BASE.sub("", k), []).append(k)
    out = []
    for base in sorted(groups):
        loops = sorted(k for k in groups[base] if not k.endswith(("_start", "_no_target")))
        if loops:
            out.append(loops[0])
    return out


def scan_mod(mod):
    files = vs.mod_json_files(mod)
    stamp = tuple((f.name, f.stat().st_mtime_ns) for f in files)
    with _CACHE_LOCK:
        hit = _CACHE.get(str(mod))
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        meta, default, groups = vs.load_mod(mod)
        redirects = vs.all_redirects(default, groups)
    except (OSError, ValueError, AttributeError, TypeError):
        res = None
    else:
        keys = sorted({k for *_, gp, _ in redirects if (k := vs.body_key(gp))})
        has_scd = any(str(gp).lower().endswith(".scd") or str(lp).lower().endswith(".scd") for *_, gp, lp in redirects)
        res = {"dir": mod.name, "name": str(meta.get("Name", mod.name)), "keys": keys, "dances": dance_keys(keys), "has_scd": has_scd} if keys else None
    with _CACHE_LOCK:
        _CACHE[str(mod)] = (stamp, res)
    return res


def scan_library():
    lib = library_root()
    out = []
    for d in sorted(lib.iterdir(), key=lambda p: p.name):
        if d.is_dir() and (d / "meta.json").is_file():
            r = scan_mod(d)
            if r:
                out.append(dict(r))
    return out


def used():
    reg = load_registry()["swaps"]
    mods = scan_library()
    for m in mods:
        m["status"] = (reg.get(m["dir"]) or {}).get("status", "unknown")
        m["is_swap"] = m["name"].endswith("test]")
        m.pop("has_scd", None)
    return mods


# ---------- jobs ----------

JOBS, JOB_LOCK = {}, threading.Lock()
BUILD_RUNNING = threading.Lock()
OWNED_NAME = ".swapd_owned.json"


def owned_dirs():
    f = tmp_root() / OWNED_NAME
    try:
        return set(json.loads(f.read_text()))
    except (OSError, ValueError):
        return set()


OWNER_SUFFIX = ".swapd-owned"


def owner_marker(p):
    """Sibling of the out dir (never inside it, so it cannot reach the library or disturb diff -r / verify)."""
    p = Path(p)
    return p.with_name(p.name + OWNER_SUFFIX)


def _fingerprint(p):
    st = os.lstat(p)
    return {"dev": st.st_dev, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns}


def write_owner_marker(p):
    """Record the out dir's identity once the tool is done with it. Best effort: no marker means no deletion later."""
    try:
        owner_marker(p).write_text(json.dumps(_fingerprint(p)))
    except OSError:
        pass


def clear_owner_marker(p):
    try:
        owner_marker(p).unlink()
    except OSError:
        pass


def owns_out_dir(p):
    """True only if swapd claimed the path AND the dir is still the one swapd's tool left (marker matches)."""
    p = Path(p)
    if p.is_symlink() or str(p) not in owned_dirs():
        return False
    try:
        return json.loads(owner_marker(p).read_text()) == _fingerprint(p)
    except (OSError, ValueError):
        return False


def mark_owned(p, owned=True):
    s = owned_dirs()
    (s.add if owned else s.discard)(str(p))
    if not owned:
        clear_owner_marker(p)
    tmp_root().mkdir(parents=True, exist_ok=True)
    (tmp_root() / OWNED_NAME).write_text(json.dumps(sorted(s)))


TOOL_TIMEOUT = 600    # seconds: emote_swap.py (dotnet lookups, PAP patching)
VERIFY_TIMEOUT = 120  # seconds: verify_swap.py


def _text(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")


SCD_OK = re.compile(r"^\s+(.+?\.scd): ducking added \((.*)\)\s*$", re.I)
SCD_SAME = re.compile(r"^\s+(.+?\.scd): already ducked with the same values", re.I)
SCD_WARN = re.compile(r"^WARNING: \.scd LEFT (?:UNPATCHED|AS IS): (.+?\.scd)(?::\s*|\s+)(.*)$", re.I)


def parse_report(log, out_dir):
    rep = {"paps_patched": None, "races": None, "unmapped": None, "scd": None}
    m = re.search(r"^(\d+) PAP\(s\) patched", log, re.M)
    if m:
        rep["paps_patched"] = int(m.group(1))
    m = re.search(r"^target slots: (\{.*\})\s*$", log, re.M)
    if m:  # races = distinct race codes mapped to the printed target keys in the output meta.json
        keys = set(re.findall(r"'([a-z0-9_]+/[a-z0-9_/]+)'", m.group(1)))
        try:
            _, d, g = vs.load_mod(out_dir)
            races = {mm.group(1) for *_, gp, _ in vs.all_redirects(d, g) if (mm := vs.BODY_GP.match(gp)) and mm.group(2).lower() in keys}
            rep["races"] = len(races) if keys else None
        except (OSError, ValueError):
            pass
    if "no .scd in this mod" in log:
        rep["scd"] = []
    elif "SCD ducking skipped (--no-duck)" not in log and re.search(r"^SCD ducking: ", log, re.M):
        scd = []
        for line in log.splitlines():
            for rx, action in ((SCD_OK, "patched"), (SCD_SAME, "already_ducked"), (SCD_WARN, "skipped")):
                mm = rx.match(line)
                if mm:
                    detail = mm.group(2).strip() if mm.lastindex and mm.lastindex >= 2 else "already ducked with the same values"
                    scd.append({"file": mm.group(1).strip().replace("\\", "/"), "action": action, "detail": detail})
                    break
        rep["scd"] = scd
    return rep


def lookup_path():
    return vs.lookup_path()


def lookup_slots(emote):
    """Target emote ActionTimeline slots from emote_swap's built Lumina lookup helper, or None (see verify_swap.lookup_slots).
    Slots of a same-named no-target row (Dote, All Saints' Charm, Snowball) carry variant == "no_target", so a no-target key that
    the build could not map shows up in unmapped_keys like any other unmapped key."""
    return vs.lookup_slots(emote)


def unmapped_keys(emote, out):
    """Every non-empty target slot key with at least one race PAP, minus the body keys the output maps. None if unknown."""
    slots = lookup_slots(emote)
    if slots is None:
        return None
    try:
        _, d, g = vs.load_mod(out)
    except (OSError, ValueError):
        return None
    mapped = {k for *_, gp, _ in vs.all_redirects(d, g) if (k := vs.body_key(gp))}
    keys = []
    for s in slots:
        k = str(s.get("key") or "").lower()
        if k and s.get("races") and k not in mapped and k not in keys:
            keys.append(k)
    return keys


def run_job(job, src, cmd, out):
    log = ""
    try:
        if out.exists() or out.is_symlink():  # appeared after start_build removed/checked it: not ours
            mark_owned(out, False)
            job["log"] = f"swapd: {out} appeared before the tool ran; not created by swapd, left alone"
            job["state"] = "failed"
            return
        env = {**os.environ, "TMPDIR": str(tmp_root())}  # emote_swap keeps its dotnet scratch in tempfile.gettempdir()
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            log = _text(e.output)
            job["log"] = (log + f"\nswapd: the swap tool did not finish within {TOOL_TIMEOUT} seconds and was stopped")[-8000:]
            job["state"] = "failed"
            if out.exists():  # the tool made it (partial): stays ours, now with its identity recorded
                write_owner_marker(out)
            return
        log = r.stdout
        job["log"] = log[-8000:]
        if r.returncode != 0 or not (out / "meta.json").is_file():
            # The tool refuses an existing --out ("<out> already exists"): then someone else made it, so drop our claim.
            if not out.exists() or f"{out} already exists" in log:
                mark_owned(out, False)
            else:
                write_owner_marker(out)
            job["state"] = "failed"
            return
        write_owner_marker(out)
        try:
            name = json.loads((out / "meta.json").read_text(encoding="utf-8")).get("Name")
            job["folder_name"] = name if isinstance(name, str) else None
        except (OSError, ValueError, AttributeError):
            job["folder_name"] = None
        job["report"] = parse_report(log, out)
        job["report"]["unmapped"] = unmapped_keys(job["emote"], out)
        try:
            v = subprocess.run([sys.executable, str(verify_path()), "--src", str(src), "--out", str(out)], capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            job["log"] = (log + f"\nswapd: verification did not finish within {VERIFY_TIMEOUT} seconds and was stopped")[-8000:]
            job["state"] = "failed"
            return
        try:
            job["verify"] = json.loads(v.stdout)
        except ValueError:
            job["verify"] = {"pass": False, "checks": [{"name": "verify_swap", "pass": False, "detail": (v.stderr or v.stdout)[-2000:]}]}
        job["state"] = "built"
    except Exception as e:  # never leave a job running
        job["log"] = (log + f"\nswapd error: {e!r}")[-8000:]
        job["state"] = "failed"
    finally:
        BUILD_RUNNING.release()


def start_build(body):
    d, emote = body.get("dir"), body.get("emote")
    if not isinstance(emote, str) or not emote.strip():
        raise ApiError(400, "emote is required")
    from_emote, no_duck = body.get("from_emote"), body.get("no_duck", False)
    from_key = body.get("from_key")
    if from_emote is not None and not isinstance(from_emote, str):
        raise ApiError(400, "from_emote must be a string or null")
    if from_key is not None and (not isinstance(from_key, str) or not FROM_KEY_RE.fullmatch(from_key.lower())):
        raise ApiError(400, "from_key must be a donor key like emote/dance04_loop or emote_sp/sp12_loop, or null")
    if from_emote and from_key:
        raise ApiError(400, "send from_emote or from_key, not both")
    if not isinstance(no_duck, bool):
        raise ApiError(400, "no_duck must be true or false")
    src = lib_child(library_root(), d)
    dances = (scan_mod(src) or {}).get("dances") or []
    if len(dances) > 1 and not from_emote and not from_key:
        raise ApiError(409, f"this mod has {len(dances)} dances ({', '.join(dances)}); choose one with from_key")
    if from_key and dances and from_key.lower() not in dances:
        raise ApiError(400, f"from_key {from_key!r} is not one of this mod's dances: {', '.join(dances)}")
    if not BUILD_RUNNING.acquire(blocking=False):
        raise ApiError(409, "a build is already running")
    try:
        root = tmp_root()
        root.mkdir(parents=True, exist_ok=True)
        out = root / f"{build_slug(d, emote)}_out"
        if out.exists() or out.is_symlink():
            if not owns_out_dir(out):
                raise ApiError(409, f"{out} exists and is not the folder swapd's tool left there; refusing to remove it")
        with JOB_LOCK:  # one locked section: no install may be mid-flight, and none can start once these are failed
            if any(j["out_dir"] == str(out) and j.get("installing") for j in JOBS.values()):
                raise ApiError(409, f"{out} is being installed")
            for j in JOBS.values():  # an older job's output is about to be replaced
                if j["out_dir"] == str(out) and j["state"] == "built":
                    j["state"], j["log"] = "failed", (j["log"] + "\n[swapd: output replaced by a newer build]")[-8000:]
        if out.exists() or out.is_symlink():
            shutil.rmtree(out)
        clear_owner_marker(out)
        mark_owned(out)
        cmd = [sys.executable, str(tool_path()), "--src", str(src), "--emote", emote, "--out", str(out)]
        if from_emote:
            cmd += ["--from-emote", from_emote]
        if from_key:
            cmd += ["--from-key", from_key.lower()]
        if no_duck:
            cmd.append("--no-duck")
        jid = uuid.uuid4().hex[:12]
        job = {"state": "running", "log": "", "out_dir": str(out), "folder_name": None, "report": None, "verify": None,
               "src": str(src), "dir": d, "emote": emote}
        with JOB_LOCK:
            JOBS[jid] = job
        threading.Thread(target=run_job, args=(job, src, cmd, out), daemon=True).start()
        return {"job": jid}
    except BaseException:
        BUILD_RUNNING.release()
        raise


def public_job(job):
    return {k: job[k] for k in ("state", "log", "out_dir", "folder_name", "report", "verify")}


def get_job(jid):
    if not isinstance(jid, str):
        raise ApiError(400, "job must be a string")
    with JOB_LOCK:
        job = JOBS.get(jid)
    if not job:
        raise ApiError(404, f"no job {jid}")
    return job


def require_verified(job):
    if job["state"] != "built" or not (job.get("verify") or {}).get("pass"):
        raise ApiError(409, f"job is {job['state']} and verify {'passed' if (job.get('verify') or {}).get('pass') else 'did not pass'}; refused")


# ---------- redirects (D8) ----------

def _selected_options(group, chosen, notes):
    gname, gtype = group.get("Name", "?"), str(group.get("Type", "Single"))
    opts = group.get("Options") or []
    names = [o.get("Name") for o in opts]
    if gtype not in ("Single", "Multi"):
        notes.append(f"group {gname!r}: type {gtype} is not converted")
        return []
    if chosen is None:
        ds = group.get("DefaultSettings", 0) or 0
        if gtype == "Single":
            idx = [ds] if 0 <= ds < len(opts) else []
        else:
            idx = [i for i in range(len(opts)) if ds >> i & 1]
    else:
        idx = []
        for n in chosen:
            if n in names:
                idx.append(names.index(n))
            else:
                notes.append(f"group {gname!r}: option {n!r} not in the build output; ignored")
        if gtype == "Single" and len(idx) > 1:
            notes.append(f"group {gname!r}: Single group got {len(idx)} options; using the first, {names[idx[0]]!r}")
            idx = idx[:1]
    sel = [(i, opts[i]) for i in idx]
    if gtype == "Multi":
        # Penumbra MultiModGroup.AddData: options by Priority descending (stable), TryAdd, so the first added wins:
        # higher priority wins, equal priority -> earlier index wins. Our layers overwrite (later wins), so the
        # winner must come last: priority ascending, then index descending.
        sel.sort(key=lambda t: (t[1].get("Priority", 0) or 0, -t[0]))
    return sel


def _unsupported(where, opt, notes):
    swaps = opt.get("FileSwaps") or {}
    for gp, to in swaps.items():
        notes.append(f"{where}: file swap {gp} -> {to} not converted")
    manips = opt.get("Manipulations") or []
    if manips:
        kinds = sorted({str(m.get("Type", "?")) for m in manips if isinstance(m, dict)})
        notes.append(f"{where}: {len(manips)} meta manipulation(s) not converted ({', '.join(kinds)})")


def redirects(job, body):
    require_verified(job)
    settings = body.get("settings", {}) if isinstance(body, dict) else None
    if not isinstance(settings, dict) or not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in settings.values()):
        raise ApiError(400, "settings must be an object of group name -> list of option names")
    out = Path(job["out_dir"])
    _, default, groups = vs.load_mod(out)
    notes, layers = [], [("DefaultData", default)]
    known = {g.get("Name") for g in groups}
    for n in settings:
        if n not in known:
            notes.append(f"settings group {n!r} not in the build output; ignored")
    order = sorted(range(len(groups)), key=lambda i: (groups[i].get("Priority", 0) or 0, i))
    for i in order:
        g = groups[i]
        for _, o in _selected_options(g, settings.get(g.get("Name")), notes):
            layers.append((f"{g.get('Name')}/{o.get('Name')}", o))
    files = {}
    for where, opt in layers:
        _unsupported(where, opt, notes)
        layer = {}  # Penumbra lowercases game paths and keeps the first of an option's case-colliding keys
        for gp, lp in (opt.get("Files") or {}).items():
            key = str(gp).lower()
            if key in layer:
                if str(layer[key][1]).replace("\\", "/").lower() != str(lp).replace("\\", "/").lower():
                    notes.append(f"{where}: {gp} -> {lp} collides with {layer[key][0]} -> {layer[key][1]} after lowercasing; kept the first")
                continue
            layer[key] = (gp, lp)
        for key, (gp, lp) in layer.items():
            p, kind = vs.resolve_local(out, lp)
            if p is None:
                notes.append(f"{where}: {gp} -> {lp}: local file {kind}; not redirected")
                continue
            files[key] = to_windows(p)
    return {"files": files, "unsupported": notes}


# ---------- install / result ----------

def install(body):
    job = get_job(body.get("job"))
    with JOB_LOCK:
        require_verified(job)
        if job.get("installing") or job.get("installed"):
            raise ApiError(409, "job already installed or installing")
        job["installing"] = True
    try:
        lib = library_root()
        out = Path(job["out_dir"])
        name = json.loads((out / "meta.json").read_text(encoding="utf-8")).get("Name")
        if not isinstance(name, str):
            raise ApiError(409, "output meta.json has no Name")
        folder = sanitize_folder(name)
        dst = lib / folder
        try:  # atomic claim: never copy into a folder that already exists, never delete on failure
            os.mkdir(dst)
        except FileExistsError:
            raise ApiError(409, f"library folder {folder!r} already exists")
        r = subprocess.run(["cp", "-a", "--", f"{out}/.", f"{dst}/"], capture_output=True, text=True)
        if r.returncode != 0:
            raise ApiError(500, f"cp -a into {dst} failed (nothing deleted; the folder may be partial): {r.stderr.strip()}")
        d = subprocess.run(["diff", "-r", "--", str(out), str(dst)], capture_output=True, text=True)
        diff_ok = d.returncode == 0
        set_status(folder, "awaiting", source=job["dir"], emote=job["emote"], installed=date.today().isoformat())
        job["installed"] = folder
        return {"folder": folder, "diff_ok": diff_ok}
    finally:
        job["installing"] = False


HANDOFF_SECTION = "## Plugin results"


def compose_handoff(line):
    """Read HANDOFF.md and return (path, new text) with `line` added to the Plugin results section. Line endings are
    kept exactly (newline="" on read and write); the new line uses the file's own ending."""
    f = handoff_file()
    with open(f, encoding="utf-8", newline="") as h:
        text = h.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split("\n")
    idx = next((i for i, l in enumerate(lines) if l.strip() == HANDOFF_SECTION), None)
    if idx is None:
        new = text.rstrip("\r\n") + f"{eol}{eol}{HANDOFF_SECTION}{eol}{eol}{line}{eol}"
    else:
        end = next((j for j in range(idx + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        while end > idx + 1 and lines[end - 1].strip() == "":
            end -= 1
        lines[end:end] = [line + ("\r" if eol == "\r\n" else "")]
        new = "\n".join(lines)
        if not new.endswith("\n"):
            new += eol
    return f, new


def write_handoff(f, new):
    tmp = f.with_name(f".{f.name}.swapd.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as h:
        h.write(new)
    os.replace(tmp, f)


def append_handoff(line):
    write_handoff(*compose_handoff(line))


BAD_FOLDER_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")


def result(body):
    folder, outcome, note = body.get("folder"), body.get("outcome"), body.get("note", "") or ""
    if not isinstance(folder, str) or not folder or folder in (".", "..") or BAD_FOLDER_CHARS.search(folder):
        raise ApiError(400, "folder must be a single library folder name without path separators or control characters")
    if not (library_root() / folder).is_dir():
        raise ApiError(400, f"no library folder {folder!r}")
    if outcome not in ("worked", "dance_only", "failed"):
        raise ApiError(400, "outcome must be worked, dance_only or failed")
    if not isinstance(note, str):
        raise ApiError(400, "note must be a string")
    note = " ".join(note.split())
    words = {"worked": "worked in game", "dance_only": "dance only (music/other parts did not work)", "failed": "failed in game"}
    line = f"- {date.today().isoformat()} `{folder}`: {words[outcome]} (reported via Swapwright plugin)" + (f". Note: {note}" if note else "")
    with REG_LOCK:
        write_handoff(*compose_handoff(line))  # a HANDOFF failure raises here, before the registry is touched
    set_status(folder, outcome, note=note)
    return {"ok": True}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "swapd/" + VERSION
    token = None

    def log_message(self, format, *args):
        if os.environ.get("SWAPD_QUIET"):
            return
        sys.stderr.write("swapd %s - %s\n" % (self.address_string(), format % args))

    def _send(self, code, obj, headers=()):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        """stdlib-generated errors (bad request line, oversized header, ...) as JSON too."""
        short = self.responses.get(code, ("error",))[0]
        self.close_connection = True
        self._send(code, {"error": message or short}, headers=(("Connection", "close"),))

    def _body(self):
        raw_len = (self.headers.get("Content-Length") or "").strip()
        if not raw_len:
            n = 0
        elif re.fullmatch(r"-\d+", raw_len):
            raise ApiError(400, "negative Content-Length")
        elif not re.fullmatch(r"\d+", raw_len):
            raise ApiError(400, "Content-Length is not an integer")
        else:
            n = int(raw_len)
        if n > 1 << 20:
            raise ApiError(413, "body too large (max 1 MiB)")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body is not valid UTF-8 JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if method == "GET" and path == "/ping":
                return self._send(200, {"ok": True, "version": VERSION})
            given = self.headers.get("X-Swapwright-Token") or ""
            if not hmac.compare_digest(given.encode(), (self.token or "").encode()) or not given:
                return self._send(401, {"error": "missing or wrong X-Swapwright-Token"})
            if method not in ("GET", "POST"):
                return self._send(405, {"error": f"method {method} not allowed; use GET or POST"}, headers=(("Allow", "GET, POST"),))
            if method == "GET" and path == "/mods":
                return self._send(200, scan_library())
            if method == "GET" and path == "/used":
                return self._send(200, used())
            m = re.fullmatch(r"/job/([0-9a-f]+)", path)
            if method == "GET" and m:
                return self._send(200, public_job(get_job(m.group(1))))
            m = re.fullmatch(r"/job/([0-9a-f]+)/redirects", path)
            if method == "POST" and m:
                job = get_job(m.group(1))
                return self._send(200, redirects(job, self._body()))
            if method == "POST" and path == "/build":
                return self._send(200, start_build(self._body()))
            if method == "POST" and path == "/install":
                return self._send(200, install(self._body()))
            if method == "POST" and path == "/result":
                return self._send(200, result(self._body()))
            return self._send(404, {"error": f"no endpoint {method} {path}"})
        except ApiError as e:
            return self._send(e.code, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"internal error: {e}"})

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PUT(self): self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")
    def do_PATCH(self): self._dispatch("PATCH")
    def do_HEAD(self): self._dispatch("HEAD")
    def do_OPTIONS(self): self._dispatch("OPTIONS")


def make_server(port=None):
    Handler.token = ensure_token()
    port = int(os.environ.get("SWAPD_PORT", 47815)) if port is None else port
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    srv = make_server()
    print(f"swapd {VERSION} on 127.0.0.1:{srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
