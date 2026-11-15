"""Shared fixtures for swapd tests: synthetic mods, a fake emote_swap tool, env isolation. Never touches the real library."""
import json, os, struct, sys, tempfile, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWAPD_DIR = HERE.parent
EMOTE_SWAP_DIR = SWAPD_DIR.parents[1] / "_shared" / "tools" / "emote_swap"
sys.path.insert(0, str(SWAPD_DIR))
sys.path.insert(0, str(EMOTE_SWAP_DIR))
import scd_duck  # read-only use of its parser/patcher

GP = "chara/human/{race}/animation/a0001/bt_common/{key}.pap"
LP = "chara\\human\\c0101\\animation\\a0001\\bt_common\\emote\\dance16_loop.pap"
FACE_GP = "chara/human/c0801/animation/f0001/nonresident/face/cfxl_lip.pap"
FACE_LP = "chara\\human\\c0801\\animation\\face\\cfxl_lip.pap"
KDB_LP = "chara\\human\\c0801\\skeleton\\lip.kdb"
AUDIO = bytes(range(256)) * 3


def fake_pap(name):
    """Tiny stand-in PAP: the internal name twice (name field + C009 path), like the real donors."""
    return b"pap \x01\x00\x02\x00\x01\x00" + b"\0" * 16 + name.ljust(32, b"\0") + b"TMLB....C009" + name + b"\0" + b"HAVOKDATA" * 4


def pad16(b):
    return b + b"\0" * (-len(b) % 16)


def fake_scd():
    """One-sound SCD in scd_duck's verified layout, no ducking (mirrors test_scd_duck.build, built independently)."""
    t_track, t_audio, t_layout, t_attr = 0x60, 0x70, 0x80, 0x90
    pos = 0xA0
    layout, sound = pad16(b"L" * 16), pad16(struct.pack("<BBBBIfhBb", 1, 1, 0xB4, 1, 0x0001, 0.5, 0xC9, 0, 0) + struct.pack("<HH", 0, 0))
    track, attr = pad16(b"T" * 20), pad16(b"A" * 20)
    p_layout, p_sound = pos, pos + len(layout)
    p_track = p_sound + len(sound)
    p_attr = p_track + len(track)
    p_audio = p_attr + len(attr)
    body = layout + sound + track + attr + AUDIO
    total = pos + len(body)
    head = scd_duck.MAGIC + struct.pack("<iBBhi", 3, 0, 4, 0x30, total) + b"\0" * 28
    head += struct.pack("<4h", 1, 1, 1, 0x270F) + struct.pack("<6i", t_track, t_audio, t_layout, 0, t_attr, 0)
    head += b"\0" * (0x50 - len(head))
    tables = b"".join(struct.pack("<i", v).ljust(16, b"\0") for v in (p_sound, p_track, p_audio, p_layout, p_attr))
    return head + tables + body


def write(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def make_v4_mod(root, name="Fake Dance", with_face=True, with_scd=True, races=("c0101", "c0801")):
    """FileVersion 4 mod: donor dance16_loop in DefaultData and an Animation group; music in a Multi group."""
    root = Path(root)
    files = {GP.format(race=r, key="emote/dance16_loop"): LP for r in races}
    lip = dict(files)
    if with_face:
        lip[FACE_GP] = FACE_LP
        lip["chara/human/c0801/skeleton/lip.kdb"] = KDB_LP
        write(root / "chara/human/c0801/animation/face/cfxl_lip.pap", fake_pap(b"cfxl_lip_sho2"))
        write(root / "chara/human/c0801/skeleton/lip.kdb", b"KDB-bytes")
    groups = [{"Name": "Animation", "Type": "Single", "Priority": 0, "DefaultSettings": 0,
               "Options": [{"Name": "w/ ears", "Files": files}, {"Name": "w/o ears", "Files": lip}]}]
    if with_scd:
        groups.append({"Name": "Music", "Type": "Multi", "Priority": 0, "DefaultSettings": 1,
                       "Options": [{"Name": "On", "Files": {"sound/x.scd": "sound\\x.scd"}}]})
        write(root / "sound/x.scd", fake_scd())
    meta = {"FileVersion": 4, "Identifier": str(uuid.uuid4()), "Name": name, "DefaultData": {"Files": dict(files)}, "Groups": groups}
    write(root / "meta.json", json.dumps(meta, indent=1))
    write(root / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop.pap", fake_pap(b"cbem_dance16_2lp"))
    return root


def make_two_dance_mod(root, name="Two Dancers"):
    """make_v4_mod plus a second dancer (emote_sp/sp12_loop) in its own group, like a duo mod."""
    root = make_v4_mod(root, name=name)
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    lp2 = "chara\\human\\c0101\\animation\\a0001\\bt_common\\emote_sp\\sp12_loop.pap"
    meta["Groups"].append({"Name": "Dancer 2", "Type": "Single", "Priority": 0, "DefaultSettings": 0,
                           "Options": [{"Name": "on", "Files": {GP.format(race="c0101", key="emote_sp/sp12_loop"): lp2}}]})
    write(root / "meta.json", json.dumps(meta, indent=1))
    write(root / "chara/human/c0101/animation/a0001/bt_common/emote_sp/sp12_loop.pap", fake_pap(b"cbem_sp12_2lp"))
    return root


def make_old_mod(root, name="Old Style"):
    """FileVersion 3 style: meta.json without groups, default_mod.json + group_*.json."""
    root = Path(root)
    write(root / "meta.json", json.dumps({"FileVersion": 3, "Name": name}))
    write(root / "default_mod.json", json.dumps({"Files": {"chara/equipment/e0001/x.mdl": "x.mdl"}}))
    write(root / "group_001_dance.json", json.dumps({"Name": "Dance", "Type": "Single", "Options": [
        {"Name": "A", "Files": {GP.format(race="c0801", key="emote_sp/sp12_loop"): "a.pap"}}]}))
    write(root / "group_002_music.json", json.dumps({"Name": "Music", "Type": "Multi", "Options": [
        {"Name": "On", "Files": {"sound/y.scd": "y.scd"}}]}))
    return root


FAKE_TOOL = r'''
import argparse, json, os, shutil, sys, time, uuid
from pathlib import Path
sys.path.insert(0, os.environ["EMOTE_SWAP_DIR"])
import scd_duck
ap = argparse.ArgumentParser()
ap.add_argument("--src"); ap.add_argument("--emote"); ap.add_argument("--out"); ap.add_argument("--from-emote"); ap.add_argument("--from-key"); ap.add_argument("--no-duck", action="store_true")
a = ap.parse_args()
time.sleep(float(os.environ.get("FAKE_TOOL_SLEEP", "0")))
if os.environ.get("FAKE_TOOL_FAIL"):
    sys.exit("error: fake failure")
if os.environ.get("FAKE_TOOL_EXISTS"):  # someone else created --out just before the tool looked
    Path(a.out).mkdir(parents=True)
    (Path(a.out) / "theirs.txt").write_text("not swapd's")
    sys.exit(f"error: {a.out} already exists")
src, out = Path(a.src), Path(a.out)
shutil.copytree(src, out)
key = "emote/" + a.emote.lower()
meta = json.loads((out / "meta.json").read_text())
pap = out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop.pap"
new = pap.with_name("dance16_loop__" + a.emote.lower() + "_loop.pap")
name = ("cbem_" + a.emote.lower()).encode()
if os.environ.get("FAKE_TOOL_BAD_PAP"):
    name = b"cbem_dance16_2lp"
new.write_bytes(pap.read_bytes().replace(b"cbem_dance16_2lp", name.ljust(16, b"\0")))
lp = "chara\\human\\c0101\\animation\\a0001\\bt_common\\emote\\" + new.name
def conv(files):
    res = {}
    for gp, l in files.items():
        if "dance16_loop" in gp:
            res[gp.replace("emote/dance16_loop", key)] = lp
        else:
            res[gp] = l
    return res
meta["DefaultData"]["Files"] = conv(meta["DefaultData"]["Files"])
for g in meta["Groups"]:
    for o in g["Options"]:
        o["Files"] = conv(o["Files"])
meta["Name"] += " [" + a.emote + " test]"
meta["Identifier"] = str(uuid.uuid4())
(out / "meta.json").write_text(json.dumps(meta))
scd = out / "sound/x.scd"
if scd.exists() and not a.no_duck:
    scd.write_bytes(scd_duck.patch(scd.read_bytes())[0])
    print("  sound\\x.scd: ducking added (bus 1 -> 4, duck bus 1, 1200 ms)")
    print("SCD ducking: 1 patched, 0 already correct, 0 left alone")
print("Wrote " + str(out))
if a.from_key: print("from-key: " + a.from_key)
print("target slots: {'loop': '" + key + "'}")
print("1 PAP(s) patched. Not tested in game.")
'''


class Env:
    """Temp dirs for library/registry/handoff/tmp-root/token; sets SWAPD_* env vars; restore() undoes them."""

    def __init__(self):
        self.td = tempfile.TemporaryDirectory(dir="/tmp/claude-1000" if Path("/tmp/claude-1000").is_dir() else None)
        t = Path(self.td.name)
        self.root = t
        self.lib = t / "lib"
        self.lib.mkdir()
        self.tmp = t / "tmpout"
        self.handoff = t / "HANDOFF.md"
        self.handoff.write_text("# h\n\n## Status\n\n| test | result |\n|---|---|\n| a | **Worked in game** |\n", encoding="utf-8")
        self.tool = t / "fake_tool.py"
        self.tool.write_text(FAKE_TOOL)
        self.vars = {"SWAPD_LIBRARY": str(self.lib), "SWAPD_REGISTRY": str(t / "state/swaps.json"), "SWAPD_TOKEN_FILE": str(t / "state/token"),
                     "SWAPD_HANDOFF": str(self.handoff), "SWAPD_TMP_ROOT": str(self.tmp), "SWAPD_TOOL": str(self.tool),
                     "SWAPD_PENUMBRA_CONFIG": str(t / "no_such_penumbra.json"), "EMOTE_SWAP_DIR": str(EMOTE_SWAP_DIR), "SWAPD_QUIET": "1",
                     "SWAPD_LOOKUP": str(t / "no_such_lookup")}
        self.saved = {k: os.environ.get(k) for k in list(self.vars) + ["FAKE_TOOL_SLEEP", "FAKE_TOOL_FAIL", "FAKE_TOOL_BAD_PAP", "FAKE_TOOL_EXISTS"]}
        os.environ.update(self.vars)

    def fake_lookup(self, slots):
        """Executable stand-in for emote_swap's lookup helper printing the given slots; sets SWAPD_LOOKUP."""
        p = self.root / "fake_lookup"
        p.write_text("#!" + sys.executable + "\nimport json, sys\nprint(json.dumps({'emote': sys.argv[1], 'slots': " + repr(slots) + "}))\n")
        p.chmod(0o755)
        os.environ["SWAPD_LOOKUP"] = str(p)
        return p

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.td.cleanup()
