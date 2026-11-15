import json, os, shutil, sys, time, unittest, urllib.error, urllib.request, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as fx
import swapd, verify_swap as vs, seed_registry


def run_fake(src, out, emote="Welcome", **env):
    """Build an output with the fake tool synchronously (no server)."""
    import subprocess
    e = {**os.environ, **env}
    subprocess.run([sys.executable, str(os.environ["SWAPD_TOOL"]), "--src", str(src), "--emote", emote, "--out", str(out)],
                   check=True, env=e, capture_output=True)
    return out


def check(res, name):
    return next(c for c in res["checks"] if c["name"] == name)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = fx.Env()
        swapd._CACHE.clear()
        swapd.JOBS.clear()

    def tearDown(self):
        deadline = time.time() + 10
        while swapd.BUILD_RUNNING.locked() and time.time() < deadline:
            time.sleep(0.05)
        self.env.restore()


class PathTests(unittest.TestCase):
    def test_map_windows_path(self):
        self.assertEqual(swapd.map_windows_path("E:\\Penumbra"), Path("/mnt/STORAGE/Penumbra"))
        self.assertEqual(swapd.map_windows_path("e:/Penumbra/Sub"), Path("/mnt/STORAGE/Penumbra/Sub"))
        self.assertEqual(swapd.map_windows_path("Z:\\mnt\\fedora\\Penumbra"), Path("/mnt/fedora/Penumbra"))
        with self.assertRaises(ValueError):
            swapd.map_windows_path("C:\\Penumbra")

    def test_to_windows(self):
        self.assertEqual(swapd.to_windows("/tmp/a b/c.pap"), "Z:\\tmp\\a b\\c.pap")

    def test_slug_and_folder(self):
        self.assertEqual(swapd.slugify("PM~D.Holic - Chewy (no ears wiggle) Welcome"), "pm_d_holic_chewy_no_ears_wiggle_welcome")
        self.assertEqual(swapd.slugify("에스파 'Drama' Psych"), "drama_psych")
        self.assertEqual(swapd.sanitize_folder("A: b/c [X test]"), "A_ b_c [X test]")

    def test_library_from_penumbra_config(self):
        env = fx.Env()
        try:
            os.environ.pop("SWAPD_LIBRARY")
            cfg = env.root / "penumbra.json"
            cfg.write_text(json.dumps({"ModDirectory": "Z:" + str(env.lib).replace("/", "\\")}))
            os.environ["SWAPD_PENUMBRA_CONFIG"] = str(cfg)
            self.assertEqual(swapd.library_root(), env.lib)
            cfg.write_text(json.dumps({"ModDirectory": "Z:\\no\\such\\dir"}))
            with self.assertRaises(swapd.ApiError) as cm:
                swapd.library_root()
            self.assertEqual(cm.exception.code, 503)
        finally:
            env.restore()


class ScanTests(Base):
    def test_v4_and_old_style_keys(self):
        fx.make_v4_mod(self.env.lib / "Dance A")
        fx.make_old_mod(self.env.lib / "Old B")
        fx.write(self.env.lib / "Gear C/meta.json", json.dumps({"FileVersion": 4, "Name": "Gear", "DefaultData": {"Files": {"chara/x.mdl": "x.mdl"}}, "Groups": []}))
        (self.env.lib / "not a mod").mkdir()
        mods = {m["dir"]: m for m in swapd.scan_library()}
        self.assertEqual(set(mods), {"Dance A", "Old B"})
        self.assertEqual(mods["Dance A"], {"dir": "Dance A", "name": "Fake Dance", "keys": ["emote/dance16_loop"], "dances": ["emote/dance16_loop"], "has_scd": True})
        self.assertEqual(mods["Old B"]["keys"], ["emote_sp/sp12_loop"])
        self.assertTrue(mods["Old B"]["has_scd"])

    def test_cache_refreshes_on_mtime(self):
        m = fx.make_v4_mod(self.env.lib / "Dance A")
        self.assertEqual(swapd.scan_library()[0]["name"], "Fake Dance")
        meta = json.loads((m / "meta.json").read_text())
        meta["Name"] = "Renamed"
        (m / "meta.json").write_text(json.dumps(meta))
        os.utime(m / "meta.json", ns=(time.time_ns() + 10**9,) * 2)
        self.assertEqual(swapd.scan_library()[0]["name"], "Renamed")

    def test_used_status(self):
        fx.make_v4_mod(self.env.lib / "Dance A")
        fx.make_v4_mod(self.env.lib / "Dance A [Wave test]", name="Fake Dance [Wave test]")
        swapd.set_status("Dance A [Wave test]", "worked")
        u = {m["dir"]: m for m in swapd.used()}
        self.assertEqual((u["Dance A"]["status"], u["Dance A"]["is_swap"]), ("unknown", False))
        self.assertEqual((u["Dance A [Wave test]"]["status"], u["Dance A [Wave test]"]["is_swap"]), ("worked", True))


class VerifyTests(Base):
    def setUp(self):
        super().setUp()
        self.src = fx.make_v4_mod(self.env.root / "src")
        self.out = run_fake(self.src, self.env.root / "out")

    def test_good_build_passes_all(self):
        res = vs.verify(self.src, self.out)
        self.assertTrue(res["pass"], res)
        self.assertEqual([c["name"] for c in res["checks"]], ["meta", "local_files", "no_donor_leftovers", "pap_names", "face_kdb_unchanged", "scd_audio"])
        self.assertIn("1 +16 bytes", check(res, "scd_audio")["detail"])

    def test_cli_exit_codes(self):
        import subprocess
        r = subprocess.run([sys.executable, str(fx.SWAPD_DIR / "verify_swap.py"), "--src", str(self.src), "--out", str(self.out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(json.loads(r.stdout)["pass"])
        (self.out / "sound/x.scd").write_bytes(b"x")
        r = subprocess.run([sys.executable, str(fx.SWAPD_DIR / "verify_swap.py"), "--src", str(self.src), "--out", str(self.out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["pass"])

    def _meta(self, **kw):
        m = json.loads((self.out / "meta.json").read_text())
        m.update(kw)
        (self.out / "meta.json").write_text(json.dumps(m))
        return m

    def test_meta_fails(self):
        src_id = json.loads((self.src / "meta.json").read_text())["Identifier"]
        self._meta(Identifier=src_id)
        self.assertFalse(check(vs.verify(self.src, self.out), "meta")["pass"])
        self._meta(Identifier="new", Name="no suffix")
        self.assertFalse(check(vs.verify(self.src, self.out), "meta")["pass"])

    def test_local_files_missing_and_case(self):
        p = self.out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop__welcome_loop.pap"
        p.rename(p.with_name("DANCE16_loop__welcome_loop.pap"))
        c = check(vs.verify(self.src, self.out), "local_files")
        self.assertTrue(c["pass"], c)
        self.assertIn("1 case-only", c["detail"])
        (self.out / "sound/x.scd").unlink()
        c = check(vs.verify(self.src, self.out), "local_files")
        self.assertFalse(c["pass"])
        self.assertIn("MISSING", c["detail"])

    def test_leftover_donor_redirect_fails(self):
        m = json.loads((self.out / "meta.json").read_text())
        m["Groups"][0]["Options"][1]["Files"][fx.GP.format(race="c0801", key="emote/dance16_loop")] = fx.LP
        (self.out / "meta.json").write_text(json.dumps(m))
        c = check(vs.verify(self.src, self.out), "no_donor_leftovers")
        self.assertFalse(c["pass"])
        self.assertIn("LEFTOVER", c["detail"])

    def test_pap_name_fails(self):
        p = self.out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop__welcome_loop.pap"
        data = p.read_bytes()
        p.write_bytes(data.replace(b"cbem_welcome\0\0\0\0", b"cbem_dance16_2lp", 1))  # one name left as the donor
        c = check(vs.verify(self.src, self.out), "pap_names")
        self.assertFalse(c["pass"])
        self.assertIn("appears 1x", c["detail"])
        self.assertIn("donor name cbem_dance16_2lp still appears 1x", c["detail"])

    def test_loop_name_derivation(self):
        self.assertEqual(vs.internal_name("emote/dance16_loop"), "cbem_dance16_2lp")
        self.assertEqual(vs.internal_name("emote/loop_emot31_start"), "cbem_loop_emot31_1")
        self.assertEqual(vs.internal_name("emote_sp/sp77_loop"), "cbem_sp77_2lp")
        self.assertEqual(vs.internal_name("emote/welcome"), "cbem_welcome")

    def test_face_and_kdb_changed_fail(self):
        (self.out / "chara/human/c0801/skeleton/lip.kdb").write_bytes(b"changed")
        c = check(vs.verify(self.src, self.out), "face_kdb_unchanged")
        self.assertFalse(c["pass"])
        self.assertIn("lip.kdb", c["detail"])

    def test_face_pap_changed_fails(self):
        (self.out / "chara/human/c0801/animation/face/cfxl_lip.pap").write_bytes(fx.fake_pap(b"cfxl_other"))
        self.assertFalse(check(vs.verify(self.src, self.out), "face_kdb_unchanged")["pass"])

    def test_scd_wrong_size_and_audio_change_fail(self):
        scd = self.out / "sound/x.scd"
        good = scd.read_bytes()
        scd.write_bytes(good + b"\0" * 16)
        self.assertFalse(check(vs.verify(self.src, self.out), "scd_audio")["pass"])
        bad = bytearray(good)
        bad[-1] ^= 0xFF  # last audio byte, same +16 size
        scd.write_bytes(bytes(bad))
        c = check(vs.verify(self.src, self.out), "scd_audio")
        self.assertFalse(c["pass"])
        self.assertIn("audio section changed", c["detail"])
        scd.write_bytes((self.src / "sound/x.scd").read_bytes())
        self.assertTrue(check(vs.verify(self.src, self.out), "scd_audio")["pass"])

    def test_skips_only_when_not_applicable(self):
        src = fx.make_v4_mod(self.env.root / "src2", with_face=False, with_scd=False)
        out = run_fake(src, self.env.root / "out2")
        res = vs.verify(src, out)
        self.assertTrue(res["pass"], res)
        self.assertTrue(check(res, "scd_audio")["detail"].startswith("SKIPPED:"))
        self.assertTrue(check(res, "face_kdb_unchanged")["detail"].startswith("SKIPPED:"))
        self.assertFalse(check(res, "pap_names")["detail"].startswith("SKIPPED:"))

    def test_no_patched_pap_fails(self):
        src = fx.make_v4_mod(self.env.root / "src3")
        out = run_fake(src, self.env.root / "out3", FAKE_TOOL_BAD_PAP="1")
        res = vs.verify(src, out)
        self.assertFalse(res["pass"])
        self.assertFalse(check(res, "pap_names")["pass"])

    def _point_targets_at(self, lp):
        m = json.loads((self.out / "meta.json").read_text())
        for files in [m["DefaultData"]["Files"]] + [o["Files"] for g in m["Groups"] for o in g["Options"]]:
            for gp in list(files):
                if "emote/welcome" in gp:
                    files[gp] = lp
        (self.out / "meta.json").write_text(json.dumps(m))

    def test_target_redirect_at_unpatched_donor_pap_fails(self):
        self._point_targets_at(fx.LP)  # the patched variant stays on disk but nothing references it any more
        c = check(vs.verify(self.src, self.out), "pap_names")
        self.assertFalse(c["pass"], c)
        self.assertIn("unpatched donor PAP", c["detail"])
        self.assertIn("not referenced by any body emote redirect", c["detail"])

    def test_target_redirect_at_donor_copy_fails_without_variant(self):
        variant = self.out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop__welcome_loop.pap"
        variant.unlink()
        self._point_targets_at(fx.LP)
        c = check(vs.verify(self.src, self.out), "pap_names")
        self.assertFalse(c["pass"])
        self.assertIn("unpatched donor PAP", c["detail"])

    def test_unreferenced_patched_pap_fails(self):
        extra = self.out / "chara/human/c0101/animation/a0001/bt_common/emote/extra__welcome_loop.pap"
        extra.write_bytes((self.out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop__welcome_loop.pap").read_bytes())
        c = check(vs.verify(self.src, self.out), "pap_names")
        self.assertFalse(c["pass"], c)
        self.assertIn("extra__welcome_loop.pap: patched PAP not referenced", c["detail"])


class ParseReportTests(Base):
    def test_parse(self):
        src = fx.make_v4_mod(self.env.root / "src")
        out = run_fake(src, self.env.root / "out")
        log = ("  skip x: target has no PAP for c0301\n  sound\\x.scd: ducking added (bus 1 -> 4, duck bus 1, 1200 ms)\n"
               "  sound\\y.scd: already ducked with the same values, left as is\n"
               "WARNING: .scd LEFT AS IS: sound\\z.scd already has other values: bus 16\n"
               "WARNING: .scd LEFT UNPATCHED: sound\\w.scd: refused: routing\n"
               "SCD ducking: 1 patched, 1 already correct, 2 left alone\n"
               "target slots: {'loop': 'emote/welcome'}\n3 PAP(s) patched. Not tested in game.\n")
        r = swapd.parse_report(log, out)
        self.assertEqual(r["paps_patched"], 3)
        self.assertEqual(r["races"], 2)
        self.assertIsNone(r["unmapped"])
        self.assertEqual([(s["file"], s["action"]) for s in r["scd"]],
                         [("sound/x.scd", "patched"), ("sound/y.scd", "already_ducked"), ("sound/z.scd", "skipped"), ("sound/w.scd", "skipped")])

    def test_unknowns_are_null(self):
        r = swapd.parse_report("SCD ducking skipped (--no-duck)\n", self.env.root)
        self.assertEqual(r, {"paps_patched": None, "races": None, "unmapped": None, "scd": None})
        self.assertEqual(swapd.parse_report("no .scd in this mod; no music ducking applied\n", self.env.root)["scd"], [])


class HttpBase(Base):
    def setUp(self):
        super().setUp()
        self.srv = swapd.make_server(port=0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.token = Path(os.environ["SWAPD_TOKEN_FILE"]).read_text().strip()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, token=True):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   data=None if body is None else json.dumps(body).encode())
        if token:
            r.add_header("X-Swapwright-Token", self.token if token is True else token)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def build(self, d="Dance A", emote="Welcome", **env):
        os.environ.update(env)
        code, b = self.req("POST", "/build", {"dir": d, "emote": emote})
        self.assertEqual(code, 200, b)
        jid = b["job"]
        for _ in range(200):
            code, j = self.req("GET", f"/job/{jid}")
            if j["state"] != "running":
                return jid, j
            time.sleep(0.05)
        self.fail("job did not finish")


class AuthTests(HttpBase):
    def test_token(self):
        self.assertEqual(self.req("GET", "/ping", token=False), (200, {"ok": True, "version": "1"}))
        self.assertEqual(self.req("GET", "/mods", token=False)[0], 401)
        self.assertEqual(self.req("GET", "/mods", token="0" * 32)[0], 401)
        code, body = self.req("GET", "/used", token=False)
        self.assertEqual((code, set(body)), (401, {"error"}))
        self.assertEqual(self.req("POST", "/build", {}, token=False)[0], 401)
        self.assertEqual(self.req("GET", "/mods")[0], 200)
        f = Path(os.environ["SWAPD_TOKEN_FILE"])
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertRegex(self.token, r"^[0-9a-f]{32}$")


class BuildInstallTests(HttpBase):
    def setUp(self):
        super().setUp()
        fx.make_v4_mod(self.env.lib / "Dance A")

    def test_build_reports_and_verifies(self):
        jid, j = self.build()
        self.assertEqual(j["state"], "built", j)
        self.assertTrue(j["verify"]["pass"], j["verify"])
        self.assertEqual(j["folder_name"], "Fake Dance [Welcome test]")
        self.assertEqual(j["out_dir"], str(self.env.tmp / "dance_a_welcome_out"))
        self.assertEqual(j["report"]["paps_patched"], 1)
        self.assertEqual(j["report"]["scd"][0]["action"], "patched")
        # rebuild removes the stale out dir swapd owns
        jid2, j2 = self.build()
        self.assertEqual(j2["state"], "built")
        self.assertEqual(self.req("GET", f"/job/{jid}")[1]["state"], "failed")  # its output was replaced

    def test_refuses_foreign_out_dir(self):
        (self.env.tmp / "dance_a_welcome_out").mkdir(parents=True)
        code, b = self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome"})
        self.assertEqual(code, 409, b)
        self.assertTrue((self.env.tmp / "dance_a_welcome_out").is_dir())

    def test_second_build_while_running_409(self):
        os.environ["FAKE_TOOL_SLEEP"] = "1"
        code, b = self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome"})
        self.assertEqual(code, 200)
        self.assertEqual(self.req("POST", "/build", {"dir": "Dance A", "emote": "Wave"})[0], 409)

    def test_bad_dir(self):
        self.assertEqual(self.req("POST", "/build", {"dir": "../x", "emote": "Welcome"})[0], 400)
        self.assertEqual(self.req("POST", "/build", {"dir": "Nope", "emote": "Welcome"})[0], 404)

    def test_tool_failure(self):
        jid, j = self.build(FAKE_TOOL_FAIL="1")
        self.assertEqual(j["state"], "failed")
        self.assertIn("fake failure", j["log"])
        self.assertEqual(self.req("POST", "/install", {"job": jid})[0], 409)

    def test_install_and_refuse_existing(self):
        jid, j = self.build()
        code, b = self.req("POST", "/install", {"job": jid})
        self.assertEqual((code, b), (200, {"folder": "Fake Dance [Welcome test]", "diff_ok": True}))
        dst = self.env.lib / "Fake Dance [Welcome test]"
        self.assertTrue((dst / "meta.json").is_file())
        reg = json.loads(Path(os.environ["SWAPD_REGISTRY"]).read_text())
        self.assertEqual(reg["swaps"]["Fake Dance [Welcome test]"]["status"], "awaiting")
        self.assertEqual(self.req("POST", "/install", {"job": jid})[0], 409)  # same job again
        jid2, _ = self.build()
        before = sorted(p.name for p in dst.rglob("*"))
        code, b = self.req("POST", "/install", {"job": jid2})
        self.assertEqual(code, 409, b)
        self.assertIn("exists", b["error"])
        self.assertEqual(sorted(p.name for p in dst.rglob("*")), before)

    def test_install_refuses_failed_verify(self):
        jid, j = self.build(FAKE_TOOL_BAD_PAP="1")
        self.assertEqual(j["state"], "built")
        self.assertFalse(j["verify"]["pass"])
        code, b = self.req("POST", "/install", {"job": jid})
        self.assertEqual(code, 409, b)
        self.assertEqual(sorted(p.name for p in self.env.lib.iterdir()), ["Dance A"])
        self.assertEqual(self.req("POST", f"/job/{jid}/redirects", {"settings": {}})[0], 409)

    def test_unknown_job(self):
        self.assertEqual(self.req("GET", "/job/abc123")[0], 404)
        self.assertEqual(self.req("POST", "/install", {"job": "abc123"})[0], 404)

    def test_install_copies_contents_into_new_folder(self):
        jid, j = self.build()
        self.assertEqual(self.req("POST", "/install", {"job": jid})[0], 200)
        dst = self.env.lib / "Fake Dance [Welcome test]"
        self.assertEqual(sorted(p.name for p in dst.iterdir()), sorted(p.name for p in Path(j["out_dir"]).iterdir()))

    def test_install_refuses_existing_symlink_and_keeps_it(self):
        jid, _ = self.build()
        target = self.env.root / "elsewhere"
        target.mkdir()
        (self.env.lib / "Fake Dance [Welcome test]").symlink_to(target)
        code, b = self.req("POST", "/install", {"job": jid})
        self.assertEqual(code, 409, b)
        self.assertTrue((self.env.lib / "Fake Dance [Welcome test]").is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_install_cp_failure_deletes_nothing(self):
        from unittest import mock
        jid, _ = self.build()
        real = swapd.subprocess.run
        def fake_run(cmd, *a, **k):
            if cmd[0] == "cp":
                return swapd.subprocess.CompletedProcess(cmd, 1, "", "boom")
            return real(cmd, *a, **k)
        with mock.patch.object(swapd.subprocess, "run", fake_run):
            code, b = self.req("POST", "/install", {"job": jid})
        self.assertEqual(code, 500, b)
        self.assertIn("nothing deleted", b["error"])
        self.assertTrue((self.env.lib / "Fake Dance [Welcome test]").is_dir())  # the claimed folder is left in place

    def test_tool_refusing_foreign_out_unmarks_it(self):
        out = self.env.tmp / "dance_a_welcome_out"
        jid, j = self.build(FAKE_TOOL_EXISTS="1")
        self.assertEqual(j["state"], "failed")
        self.assertTrue((out / "theirs.txt").is_file())
        self.assertNotIn(str(out), swapd.owned_dirs())
        os.environ.pop("FAKE_TOOL_EXISTS")
        code, b = self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome"})
        self.assertEqual(code, 409, b)  # not ours, so never removed
        self.assertTrue((out / "theirs.txt").is_file())

    def test_failed_tool_partial_out_stays_owned(self):
        out = self.env.tmp / "dance_a_welcome_out"
        tool = Path(os.environ["SWAPD_TOOL"])
        tool.write_text(tool.read_text().replace('print("Wrote "', 'sys.exit("error: died late") if os.environ.get("FAKE_TOOL_LATE") else None\nprint("Wrote "'))
        jid, j = self.build(FAKE_TOOL_LATE="1")
        os.environ.pop("FAKE_TOOL_LATE")
        self.assertEqual(j["state"], "failed")
        self.assertTrue(out.is_dir())
        self.assertIn(str(out), swapd.owned_dirs())  # the tool made it, so a rebuild may clean it up
        _, j2 = self.build()
        self.assertEqual(j2["state"], "built")

    def test_unmapped_from_lookup(self):
        self.assertIsNone(self.build()[1]["report"]["unmapped"])  # no helper -> unknown, never guessed
        self.env.fake_lookup([{"index": 0, "key": "emote/welcome", "races": {"c0101": "cbem_welcome"}},
                              {"index": 2, "key": "emote/j_welcome", "races": {"c0101": "cbem_j_welcome"}},
                              {"index": 3, "key": "emote/j_welcome", "races": {"c0101": "cbem_j_welcome"}},
                              {"index": 4, "key": "emote/u_welcome", "races": {}},
                              {"index": 5, "key": "", "races": {"c0101": "x"}}])
        _, j = self.build()
        self.assertEqual(j["report"]["unmapped"], ["emote/j_welcome"])


class RawHttpTests(HttpBase):
    def raw(self, length):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("POST", "/result")
            c.putheader("X-Swapwright-Token", self.token)
            c.putheader("Content-Length", length)
            c.endheaders()
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()

    def test_content_length_validation(self):
        self.assertEqual(self.raw("-1")[0], 400)
        self.assertEqual(self.raw("abc")[0], 400)
        self.assertEqual(self.raw("1.5")[0], 400)
        self.assertEqual(self.raw(str((1 << 20) + 1))[0], 413)
        self.assertEqual(self.raw("0")[0], 400)  # empty body -> {} -> folder is required


class RedirectTests(HttpBase):
    def setUp(self):
        super().setUp()
        fx.make_v4_mod(self.env.lib / "Dance A")
        self.jid, self.j = self.build()
        self.out = Path(self.j["out_dir"])
        self.welcome = "chara/human/c0101/animation/a0001/bt_common/emote/welcome.pap"

    def win(self, rel):
        return "Z:" + str((self.out / rel).resolve()).replace("/", "\\")

    def test_defaults(self):
        code, r = self.req("POST", f"/job/{self.jid}/redirects", {"settings": {}})
        self.assertEqual(code, 200, r)
        # Animation default is option 0 (w/ ears, no face files); Music DefaultSettings=1 -> On
        self.assertEqual(set(r["files"]), {self.welcome, "chara/human/c0801/animation/a0001/bt_common/emote/welcome.pap", "sound/x.scd"})
        self.assertEqual(r["files"]["sound/x.scd"], self.win("sound/x.scd"))
        self.assertTrue(r["files"][self.welcome].startswith("Z:\\"))
        self.assertEqual(r["unsupported"], [])

    def test_selected_options_and_unknowns(self):
        code, r = self.req("POST", f"/job/{self.jid}/redirects", {"settings": {"Animation": ["w/o ears"], "Music": [], "Ghost": ["x"]}})
        self.assertEqual(code, 200, r)
        self.assertIn(fx.FACE_GP, r["files"])
        self.assertNotIn("sound/x.scd", r["files"])
        self.assertTrue(any("Ghost" in n for n in r["unsupported"]))

    def test_priority_and_unsupported(self):
        m = json.loads((self.out / "meta.json").read_text())
        m["Groups"][1]["Priority"] = -5  # Music first, then Animation overrides it
        m["Groups"][1]["Options"][0]["Files"][self.welcome] = "sound\\x.scd"
        m["Groups"].append({"Name": "Late", "Type": "Single", "Priority": 0, "DefaultSettings": 0,
                            "Options": [{"Name": "only", "Files": {"sound/x.scd": "chara\\human\\c0801\\skeleton\\lip.kdb"}}]})
        m["DefaultData"]["FileSwaps"] = {"a/b.tex": "c/d.tex"}
        m["DefaultData"]["Manipulations"] = [{"Type": "Eqp", "Manipulation": {}}]
        (self.out / "meta.json").write_text(json.dumps(m))
        r = self.req("POST", f"/job/{self.jid}/redirects", {"settings": {}})[1]
        self.assertTrue(r["files"][self.welcome].endswith("dance16_loop__welcome_loop.pap"))  # Animation (prio 0) beat Music (-5)
        self.assertTrue(r["files"]["sound/x.scd"].endswith("lip.kdb"))  # same priority: later group wins
        self.assertTrue(any("file swap a/b.tex" in n for n in r["unsupported"]))
        self.assertTrue(any("meta manipulation" in n and "Eqp" in n for n in r["unsupported"]))

    def test_bad_settings(self):
        self.assertEqual(self.req("POST", f"/job/{self.jid}/redirects", {"settings": {"Animation": "w/o ears"}})[0], 400)

    def _multi(self, prios):
        m = json.loads((self.out / "meta.json").read_text())
        targets = ["sound\\x.scd", "chara\\human\\c0801\\skeleton\\lip.kdb", "meta.json"]
        m["Groups"].append({"Name": "Layers", "Type": "Multi", "Priority": 0, "DefaultSettings": 7,
                            "Options": [{"Name": f"o{i}", "Priority": p, "Files": {"a/shared.tex": targets[i]}} for i, p in enumerate(prios)]})
        (self.out / "meta.json").write_text(json.dumps(m))
        return self.req("POST", f"/job/{self.jid}/redirects", {"settings": {}})[1]["files"]["a/shared.tex"]

    def test_multi_options_match_penumbra(self):
        self.assertTrue(self._multi([0, 5, 5]).endswith("lip.kdb"))  # highest priority, earliest index among equals
        self.assertTrue(self._multi([0, 0, 0]).endswith("x.scd"))    # all equal: first option wins
        self.assertTrue(self._multi([1, 0, 2]).endswith("meta.json"))

    def test_game_paths_lowercased_and_collisions_reported(self):
        m = json.loads((self.out / "meta.json").read_text())
        m["Groups"].append({"Name": "Case", "Type": "Single", "Priority": 9, "DefaultSettings": 0, "Options": [{"Name": "c", "Files": {
            "Sound/Y.scd": "sound\\x.scd", "sound/y.SCD": "chara\\human\\c0801\\skeleton\\lip.kdb", "SOUND/y.scd": "Sound/X.SCD"}}]})
        (self.out / "meta.json").write_text(json.dumps(m))
        r = self.req("POST", f"/job/{self.jid}/redirects", {"settings": {}})[1]
        self.assertTrue(all(k == k.lower() for k in r["files"]))
        self.assertTrue(r["files"]["sound/y.scd"].endswith("x.scd"))  # the first key kept
        coll = [n for n in r["unsupported"] if "after lowercasing" in n]
        self.assertEqual(len(coll), 1, r["unsupported"])  # same target in another case is not a collision
        self.assertIn("sound/y.SCD", coll[0])


class ResultTests(HttpBase):
    def test_result_appends_section_then_line(self):
        for d in ("Fake [Wave test]", "Other"):  # /result now requires an existing library folder
            (self.env.lib / d).mkdir()
        orig = self.env.handoff.read_text(encoding="utf-8")
        self.assertEqual(self.req("POST", "/result", {"folder": "Fake [Wave test]", "outcome": "worked", "note": "fine\nreally"}), (200, {"ok": True}))
        t = self.env.handoff.read_text(encoding="utf-8")
        self.assertTrue(t.startswith(orig.rstrip("\n")))
        self.assertIn("## Plugin results", t)
        self.assertEqual(self.req("POST", "/result", {"folder": "Other", "outcome": "dance_only", "note": ""})[0], 200)
        t2 = self.env.handoff.read_text(encoding="utf-8")
        tail = t2.split("## Plugin results", 1)[1].strip().splitlines()
        self.assertEqual(len(tail), 2)
        self.assertIn("`Fake [Wave test]`: worked in game", tail[0])
        self.assertIn("Note: fine really", tail[0])
        self.assertIn("`Other`: dance only", tail[1])
        self.assertEqual(t2.count("## Plugin results"), 1)
        self.assertTrue(t2.startswith(orig.rstrip("\n")))
        reg = json.loads(Path(os.environ["SWAPD_REGISTRY"]).read_text())
        self.assertEqual(reg["swaps"]["Other"]["status"], "dance_only")

    def test_result_validation(self):
        (self.env.lib / "x").mkdir()
        self.assertEqual(self.req("POST", "/result", {"folder": "x", "outcome": "great"})[0], 400)
        self.assertEqual(self.req("POST", "/result", {"outcome": "worked"})[0], 400)


class SeedTests(Base):
    TEXT = """# x

## Status

| test | result |
|---|---|
| A on /x | **Worked in game**, confirmed. Installed as `A [X test]`. |
| B on /y | **Built, awaiting in-game test**. suggested folder `B [Y test]`. |
| C on /z | **Worked in game**. Installed as `C [Z test]`. |
| D | **NOT TESTED in game**. Installed as `D [Q test]`. |
| E (`E [E test]`) | **Worked in game**. |
| Curly (`PM ～ BAND - ‘SONG’`) on Hand  to Heart | **Worked in game**, confirmed. |
| Spaced (`PM~Other - Tune`) on Wave | **Built, awaiting in-game test**. |
| Nostatus (`N mod`) on Pray | Built. |
| Twice (`T mod`) on Kneel | **Worked in game**. |
| Twice again (`T mod`) on Kneel | **Built, awaiting in-game test**. |

## Other
| `F [F test]` | **Worked in game** |
"""

    def test_seed(self):
        dirs = ("A [X test]", "B [Y test]", "D [Q test]", "E [E test]", "PM ~ BAND - 'SONG' [Hand to Heart test]",
                "PM ~ Other - Tune [Wave test]", "N mod [Pray test]", "T mod [Kneel test]", "F [F test]", "G [G test]", "Not a swap")
        for d in dirs:
            (self.env.lib / d).mkdir()
        data = seed_registry.seed(self.TEXT, self.env.lib)
        self.assertEqual({k: v["status"] for k, v in data["swaps"].items()},
                         {"A [X test]": "worked", "B [Y test]": "awaiting", "D [Q test]": "awaiting", "E [E test]": "worked",
                          "PM ~ BAND - 'SONG' [Hand to Heart test]": "worked", "PM ~ Other - Tune [Wave test]": "awaiting"})
        self.assertEqual(sorted(u["folder"] for u in data["unmatched"]), ["F [F test]", "G [G test]", "N mod [Pray test]", "T mod [Kneel test]"])
        self.assertIn("2 rows", next(u for u in data["unmatched"] if u["folder"] == "T mod [Kneel test]")["reason"])
        self.assertEqual(data["swaps"]["PM ~ BAND - 'SONG' [Hand to Heart test]"]["matched_by"], "mod+emote")

    def test_norm(self):
        self.assertEqual(seed_registry.norm("PM ～ BABYMONSTER - ‘FOREVER’"), seed_registry.norm("PM~BABYMONSTER - 'FOREVER'"))
        self.assertNotEqual(seed_registry.norm("A [Wave test]"), seed_registry.norm("A [Cheer Wave test]"))

    def test_no_overwrite_without_force(self):
        reg = Path(os.environ["SWAPD_REGISTRY"])
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("{}")
        h = self.env.root / "h.md"
        h.write_text(self.TEXT)
        self.assertEqual(seed_registry.main(["--handoff", str(h)]), 2)
        self.assertEqual(reg.read_text(), "{}")
        self.assertEqual(seed_registry.main(["--handoff", str(h), "--force"]), 0)
        self.assertIn("swaps", json.loads(reg.read_text()))


class ReviewFixTests(HttpBase):
    """Second-round review findings (items 1-10)."""

    def setUp(self):
        super().setUp()
        fx.make_v4_mod(self.env.lib / "Dance A")

    # 1
    def test_resolve_local_refuses_escapes(self):
        root = self.env.root / "mod"
        root.mkdir()
        (self.env.root / "secret.txt").write_text("s")
        (root / "ok.txt").write_text("o")
        (root / "OK2.TXT").write_text("o")
        for lp in ("..\\secret.txt", "../secret.txt", "a\\..\\..\\secret.txt", str(self.env.root / "secret.txt"), "C:\\x\\secret.txt", "\\secret.txt"):
            self.assertEqual(vs.resolve_local(root, lp), (None, "outside"), lp)
        (root / "link.txt").symlink_to(self.env.root / "secret.txt")
        self.assertEqual(vs.resolve_local(root, "link.txt"), (None, "outside"))
        self.assertEqual(vs.resolve_local(root, "ok.txt")[1], "exact")
        self.assertEqual(vs.resolve_local(root, "ok2.txt")[1], "case")

    def test_escaping_redirect_fails_verify_and_is_unsupported(self):
        jid, j = self.build()
        out = Path(j["out_dir"])
        (out.parent / "secret.txt").write_text("s")
        m = json.loads((out / "meta.json").read_text())
        m["DefaultData"]["Files"]["x/y.txt"] = "..\\secret.txt"
        (out / "meta.json").write_text(json.dumps(m))
        _, d, g = vs.load_mod(out)
        c = check({"checks": [vs.check_local_files(out, vs.all_redirects(d, g))]}, "local_files")
        self.assertFalse(c["pass"], c)
        self.assertIn("OUTSIDE", c["detail"])
        r = self.req("POST", f"/job/{jid}/redirects", {"settings": {}})[1]
        self.assertNotIn("x/y.txt", r["files"])
        self.assertTrue(any("x/y.txt" in n and "outside" in n for n in r["unsupported"]), r["unsupported"])

    # 2
    def test_replaced_out_dir_is_not_deleted(self):
        jid, j = self.build()
        out = Path(j["out_dir"])
        shutil.rmtree(out)
        out.mkdir()
        (out / "precious.txt").write_text("mine")
        code, b = self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome"})
        self.assertEqual(code, 409, b)
        self.assertTrue((out / "precious.txt").is_file())

    def test_marker_is_a_sibling_not_inside_out_or_library(self):
        jid, j = self.build()
        out = Path(j["out_dir"])
        self.assertTrue(swapd.owner_marker(out).is_file())
        self.assertEqual(swapd.owner_marker(out).parent, out.parent)
        self.assertEqual(sorted(p.name for p in out.iterdir() if p.name.endswith(".swapd-owned")), [])
        self.assertEqual(self.req("POST", "/install", {"job": jid})[1]["diff_ok"], True)
        self.assertEqual(sorted(p.name for p in self.env.lib.iterdir()), ["Dance A", "Fake Dance [Welcome test]"])
        self.assertTrue(self.build()[1]["state"] == "built")  # same dir rebuilt: marker matches

    # 3
    def test_older_jobs_failed_before_rmtree(self):
        from unittest import mock
        jid, j = self.build()
        seen = []
        real = shutil.rmtree
        def spy(p, *a, **k):
            seen.append(self.req("GET", f"/job/{jid}")[1]["state"])
            return real(p, *a, **k)
        with mock.patch.object(swapd.shutil, "rmtree", spy):
            self.assertEqual(self.build()[1]["state"], "built")
        self.assertEqual(seen, ["failed"])

    def test_rebuild_refused_while_installing_keeps_job(self):
        jid, j = self.build()
        swapd.JOBS[jid]["installing"] = True
        code, b = self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome"})
        self.assertEqual(code, 409, b)
        self.assertEqual(self.req("GET", f"/job/{jid}")[1]["state"], "built")
        self.assertTrue((Path(j["out_dir"]) / "meta.json").is_file())

    # 4
    def test_tool_timeout_fails_job_and_keeps_claim(self):
        from unittest import mock
        os.environ["FAKE_TOOL_SLEEP"] = "5"
        with mock.patch.object(swapd, "TOOL_TIMEOUT", 1):
            jid, j = self.build()
        self.assertEqual(j["state"], "failed")
        self.assertIn("did not finish within 1 seconds", j["log"])
        os.environ.pop("FAKE_TOOL_SLEEP")
        self.assertEqual(self.build()[1]["state"], "built")

    def test_verify_timeout_fails_job(self):
        from unittest import mock
        v = self.env.root / "slow_verify.py"
        v.write_text("import time; time.sleep(5)")
        os.environ["SWAPD_VERIFY"] = str(v)
        try:
            with mock.patch.object(swapd, "VERIFY_TIMEOUT", 1):
                jid, j = self.build()
        finally:
            os.environ.pop("SWAPD_VERIFY")
        self.assertEqual(j["state"], "failed")
        self.assertIn("verification did not finish within 1 seconds", j["log"])
        self.assertEqual(self.req("POST", "/install", {"job": jid})[0], 409)

    # 5
    def test_lookup_non_dict_json_is_none(self):
        for payload in ("[1]", "5", "null", '"x"'):
            p = self.env.root / "fake_lookup2"
            p.write_text("#!" + sys.executable + "\nprint(" + repr(payload) + ")\n")
            p.chmod(0o755)
            os.environ["SWAPD_LOOKUP"] = str(p)
            self.assertIsNone(swapd.lookup_slots("Welcome"), payload)

    # 6
    def test_input_validation(self):
        for bad in ("no", 1, None, [], 0):
            self.assertEqual(self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome", "no_duck": bad})[0], 400, bad)
        for bad in ([], 5, {}, None):
            self.assertEqual(self.req("POST", "/install", {"job": bad})[0], 400, bad)
        self.assertEqual(self.req("POST", "/build", {"dir": "Dance A", "emote": "Welcome", "no_duck": True})[0], 200)

    def test_folder_name_null_without_name(self):
        tool = Path(os.environ["SWAPD_TOOL"])
        tool.write_text(tool.read_text().replace('meta["Name"] += " [" + a.emote + " test]"', 'del meta["Name"]'))
        jid, j = self.build()
        self.assertIsNone(j["folder_name"])
        self.assertEqual(self.req("POST", "/install", {"job": jid})[0], 409)

    # 7
    def test_result_folder_validation_and_handoff_first(self):
        (self.env.lib / "Real").mkdir()
        reg = Path(os.environ["SWAPD_REGISTRY"])
        for bad in ("../Real", "a/b", "a\\b", "Real\r", "Real\n", "Re\x07al", ".", "..", "", "Ghost", 5, None, ["Real"]):
            self.assertEqual(self.req("POST", "/result", {"folder": bad, "outcome": "worked"})[0], 400, repr(bad))
        self.assertFalse(reg.exists())
        self.env.handoff.unlink()  # HANDOFF read fails -> registry must stay unchanged
        self.assertEqual(self.req("POST", "/result", {"folder": "Real", "outcome": "worked"})[0], 500)
        self.assertFalse(reg.exists())

    # 8
    def test_handoff_preserves_line_endings(self):
        (self.env.lib / "Real").mkdir()
        crlf = b"# h\r\n\r\n## Plugin results\r\n\r\n- old\r\n\r\n## Other\r\n| a | b |\r\n"
        self.env.handoff.write_bytes(crlf)
        self.assertEqual(self.req("POST", "/result", {"folder": "Real", "outcome": "worked"})[0], 200)
        data = self.env.handoff.read_bytes()
        self.assertEqual(data.replace(b"\r\n", b"\n").count(b"\r"), 0)
        self.assertTrue(data.startswith(b"# h\r\n\r\n## Plugin results\r\n\r\n- old\r\n- "))
        self.assertTrue(data.endswith(b"\r\n\r\n## Other\r\n| a | b |\r\n"))
        mixed = b"# h\n\nrow\r\n"  # no section yet, lone \r kept in other rows
        self.env.handoff.write_bytes(mixed)
        self.req("POST", "/result", {"folder": "Real", "outcome": "failed"})
        self.assertTrue(self.env.handoff.read_bytes().startswith(mixed.rstrip(b"\n")))

    # 9
    def test_build_slug_keeps_emote(self):
        long_mod = "Some Very Long Mod Name " * 10
        a, b = swapd.build_slug(long_mod, "Hand to Heart"), swapd.build_slug(long_mod, "Hand to Heart")
        self.assertEqual(a, b)
        self.assertTrue(a.endswith("_hand_to_heart"), a)
        self.assertLessEqual(len(a), 80)
        self.assertEqual(swapd.build_slug("Dance A", "Welcome"), "dance_a_welcome")
        self.assertNotEqual(swapd.build_slug(long_mod, "Wave"), swapd.build_slug(long_mod, "Pray"))
        self.assertEqual(swapd.build_slug("", "Wave"), "mod_wave")

    # 10
    def test_other_methods_json_and_authed(self):
        for m in ("PUT", "DELETE", "PATCH"):
            code, b = self.req(m, "/mods", {} if m != "DELETE" else None, token=False)
            self.assertEqual((code, set(b)), (401, {"error"}), m)
            code, b = self.req(m, "/mods", {} if m != "DELETE" else None)
            self.assertEqual((code, set(b)), (405, {"error"}), m)

    def test_stdlib_errors_are_json(self):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"GET /x HTTP/9.9\r\n\r\n")
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b"505", head.split(b"\r\n")[0])
        self.assertIn(b"application/json", head)
        self.assertIn("error", json.loads(body))


class DanceTests(HttpBase):
    def setUp(self):
        super().setUp()
        fx.make_v4_mod(self.env.lib / "One Dance")
        fx.make_two_dance_mod(self.env.lib / "Two Dancers")

    def test_dance_keys(self):
        k = swapd.dance_keys
        self.assertEqual(k(["emote/dance16_loop"]), ["emote/dance16_loop"])
        self.assertEqual(k(["emote_sp/sp05_loop", "emote_sp/sp05_start"]), ["emote_sp/sp05_loop"])
        self.assertEqual(k(["emote/dance_no_target", "emote/dance"]), ["emote/dance"])
        self.assertEqual(k(["emote_sp/sp12_loop", "emote_sp/sp10_loop"]), ["emote_sp/sp10_loop", "emote_sp/sp12_loop"])
        self.assertEqual(k(["emote_sp/sp04", "emote_sp/sp12_loop"]), ["emote_sp/sp04", "emote_sp/sp12_loop"])
        self.assertEqual(k(["emote_sp/sp05_start"]), [])
        self.assertEqual(k([]), [])

    def test_mods_list_dances(self):
        mods = {m["dir"]: m for m in self.req("GET", "/mods")[1]}
        self.assertEqual(mods["One Dance"]["dances"], ["emote/dance16_loop"])
        self.assertEqual(mods["Two Dancers"]["dances"], ["emote/dance16_loop", "emote_sp/sp12_loop"])

    def test_two_dances_need_a_choice(self):
        code, b = self.req("POST", "/build", {"dir": "Two Dancers", "emote": "Welcome"})
        self.assertEqual(code, 409, b)
        self.assertIn("emote_sp/sp12_loop", b["error"])
        self.assertFalse((self.env.tmp / "two_dancers_welcome_out").exists())  # refused before anything ran

    def test_bad_from_key(self):
        for key in ("not a key", "emote_sp/sp99_loop", 5):
            code, b = self.req("POST", "/build", {"dir": "Two Dancers", "emote": "Welcome", "from_key": key})
            self.assertEqual(code, 400, (key, b))
        code, b = self.req("POST", "/build", {"dir": "Two Dancers", "emote": "Welcome", "from_key": "emote/dance16_loop", "from_emote": "Joy"})
        self.assertEqual(code, 400, b)

    def test_from_key_reaches_the_tool(self):
        code, b = self.req("POST", "/build", {"dir": "Two Dancers", "emote": "Welcome", "from_key": "EMOTE_SP/sp12_loop"})
        self.assertEqual(code, 200, b)
        for _ in range(200):
            j = self.req("GET", f"/job/{b['job']}")[1]
            if j["state"] != "running":
                break
            time.sleep(0.05)
        self.assertIn("from-key: emote_sp/sp12_loop", j["log"])  # lower-cased on the way to the tool

    def test_single_dance_mod_needs_no_choice(self):
        jid, j = self.build("One Dance")
        self.assertEqual(j["state"], "built", j)


if __name__ == "__main__":
    unittest.main()


NT_KEY = "emote/welcome_no_target"
LOOP_GP = "chara/human/{race}/animation/a0001/bt_common/emote/welcome.pap"


class NoTargetVerifyTests(Base):
    """Emotes with a same-named no-target row: verify_swap checks the no-target PAPs and flags a missing mapping."""

    def setUp(self):
        super().setUp()
        self.src = fx.make_v4_mod(self.env.root / "src")
        self.out = run_fake(self.src, self.env.root / "out")
        self.slots = [{"index": 0, "key": "emote/welcome", "races": {"c0101": "cbem_welcome", "c0801": "cbem_welcome"}},
                      {"index": 0, "key": NT_KEY, "races": {"c0101": "cbem_welcome_no_target", "c0801": "cbem_welcome_no_target"},
                       "variant": "no_target", "row": 9}]

    def option_files(self, meta):
        return [meta["DefaultData"]["Files"]] + [o["Files"] for g in meta["Groups"] for o in g["Options"]]

    def add_nt(self, name=b"cbem_welcome_no_target", skip=(), only=None):
        """Write a no-target PAP and map NT_KEY next to every loop mapping. skip: (option index, race) pairs left out;
        only: map just these option indexes."""
        mdir = self.out / "chara/human/c0101/animation/a0001/bt_common/emote"
        (mdir / "dance16_loop__welcome_no_target.pap").write_bytes(fx.fake_pap(name))
        lp = "chara\\human\\c0101\\animation\\a0001\\bt_common\\emote\\dance16_loop__welcome_no_target.pap"
        meta = json.loads((self.out / "meta.json").read_text())
        for i, files in enumerate(self.option_files(meta)):
            for race in ("c0101", "c0801"):
                if LOOP_GP.format(race=race) in files and (only is None or i in only) and (i, race) not in skip:
                    files[LOOP_GP.format(race=race).replace("welcome.pap", "welcome_no_target.pap")] = lp
        (self.out / "meta.json").write_text(json.dumps(meta))

    def verify(self, slots="default"):
        return vs.verify(self.src, self.out, slots=self.slots if slots == "default" else slots)

    def test_complete_no_target_mapping_passes(self):
        self.add_nt()
        res = self.verify()
        self.assertTrue(res["pass"], res)
        self.assertEqual(check(res, "no_target")["pass"], True, res)
        self.assertIn(NT_KEY, check(res, "no_target")["detail"])

    def test_no_target_key_alone_is_not_a_donor_or_a_target(self):
        """Good Thing: the source already maps a (hand-added) no-target key; the correct rebuild must still pass."""
        meta = json.loads((self.src / "meta.json").read_text())
        meta["Groups"][0]["Options"][0]["Files"][LOOP_GP.format(race="c0801").replace("welcome.pap", "welcome_no_target.pap")] = fx.LP
        (self.src / "meta.json").write_text(json.dumps(meta))
        self.add_nt()
        res = self.verify()
        self.assertTrue(res["pass"], res)
        self.assertTrue(check(res, "no_donor_leftovers")["pass"], res)

    def test_missing_everywhere_passes_only_as_reported_unmapped(self):
        res = self.verify()
        c = check(res, "no_target")
        self.assertTrue(c["pass"], res)
        self.assertIn("UNMAPPED", c["detail"])
        self.assertIn(NT_KEY, c["detail"])
        self.env.fake_lookup(self.slots)
        self.assertEqual(swapd.unmapped_keys("Welcome", self.out), [NT_KEY])  # the report lists exactly what verify says

    def test_missing_in_one_option_fails(self):
        self.add_nt(only=[0, 1])
        c = check(self.verify(), "no_target")
        self.assertFalse(c["pass"], c)

    def test_missing_race_fails(self):
        self.add_nt(skip={(1, "c0801")})
        c = check(self.verify(), "no_target")
        self.assertFalse(c["pass"], c)
        self.assertIn("c0801", c["detail"])

    def test_wrong_internal_name_fails(self):
        for bad in (b"cbem_dance16_2lp", b"cbem_welcome"):
            self.add_nt(name=bad)
            res = self.verify()
            self.assertFalse(res["pass"], bad)
            self.assertFalse(check(res, "no_target")["pass"], bad)

    def test_name_must_appear_exactly_twice(self):
        self.add_nt()
        p = self.out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop__welcome_no_target.pap"
        p.write_bytes(p.read_bytes() + b"cbem_welcome_no_target\0")
        self.assertFalse(check(self.verify(), "no_target")["pass"])

    def test_same_internal_name_shares_the_loop_pap(self):
        """Snowball: the no-target internal name equals the loop's, so no separate PAP exists."""
        loop_lp = "chara\\human\\c0101\\animation\\a0001\\bt_common\\emote\\dance16_loop__welcome_loop.pap"
        meta = json.loads((self.out / "meta.json").read_text())
        for files in self.option_files(meta):
            for race in ("c0101", "c0801"):
                if LOOP_GP.format(race=race) in files:
                    files[LOOP_GP.format(race=race).replace("welcome.pap", "welcome_no_target.pap")] = loop_lp
        (self.out / "meta.json").write_text(json.dumps(meta))
        slots = [dict(s, races={r: "cbem_welcome" for r in s["races"]}) for s in self.slots]
        res = self.verify(slots)
        self.assertTrue(res["pass"], res)
        self.assertTrue(check(res, "pap_names")["pass"], res)

    def test_without_lookup_data_structure_is_checked_and_completeness_is_skipped_loudly(self):
        self.add_nt(only=[0])
        res = self.verify(slots=None)  # no SWAPD_LOOKUP helper in the test env, so the target's rows are unknown
        c = check(res, "no_target")
        self.assertTrue(c["pass"], res)
        self.assertIn("SKIPPED", c["detail"])
        self.assertIn("lookup", c["detail"])

    def test_emote_comes_from_the_output_name_suffix_when_not_given(self):
        self.add_nt()
        seen = self.env.root / "seen_emote"
        p = self.env.root / "fake_lookup_seen"
        p.write_text("#!" + sys.executable + "\nimport json, sys\nopen(" + repr(str(seen)) + ", 'w').write(sys.argv[1])\n"
                     "print(json.dumps({'emote': sys.argv[1], 'slots': " + repr(self.slots) + "}))\n")
        p.chmod(0o755)
        os.environ["SWAPD_LOOKUP"] = str(p)
        res = vs.verify(self.src, self.out)
        self.assertEqual(seen.read_text(), "Welcome")
        self.assertTrue(check(res, "no_target")["pass"], res)

    def test_cli_takes_emote_and_exits_1_on_a_missing_race(self):
        import subprocess
        self.add_nt(skip={(0, "c0801")})
        meta = json.loads((self.out / "meta.json").read_text())
        meta["Name"] = "Fake Dance [Something Else test]"  # the suffix names another emote, so only --emote can pick Welcome
        (self.out / "meta.json").write_text(json.dumps(meta))
        seen = self.env.root / "seen_cli"
        p = self.env.root / "fake_lookup_cli"
        p.write_text("#!" + sys.executable + "\nimport json, sys\nopen(" + repr(str(seen)) + ", 'w').write(sys.argv[1])\n"
                     "print(json.dumps({'emote': sys.argv[1], 'slots': " + repr(self.slots) + "}))\n")
        p.chmod(0o755)
        os.environ["SWAPD_LOOKUP"] = str(p)
        r = subprocess.run([sys.executable, str(fx.SWAPD_DIR / "verify_swap.py"), "--src", str(self.src), "--out", str(self.out), "--emote", "Welcome"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(seen.read_text(), "Welcome")
        self.assertFalse(check(json.loads(r.stdout), "no_target")["pass"])

    def test_loop_name_comes_from_the_lookup_when_derivation_would_be_wrong(self):
        self.add_nt()
        for p in self.out.rglob("*welcome_loop.pap"):
            p.write_bytes(p.read_bytes().replace(b"cbem_welcome", b"cbem_oddname"))
        slots = [dict(self.slots[0], races={"c0101": "cbem_oddname", "c0801": "cbem_oddname"}), self.slots[1]]
        res = self.verify(slots)
        self.assertTrue(check(res, "pap_names")["pass"], res)
        self.assertFalse(check(self.verify(None), "pap_names")["pass"])  # without lookup data it can only derive cbem_welcome

    def test_shared_loop_file_without_lookup_data_is_not_flagged(self):
        loop_lp = "chara\\human\\c0101\\animation\\a0001\\bt_common\\emote\\dance16_loop__welcome_loop.pap"
        meta = json.loads((self.out / "meta.json").read_text())
        for files in self.option_files(meta):
            for race in ("c0101", "c0801"):
                if LOOP_GP.format(race=race) in files:
                    files[LOOP_GP.format(race=race).replace("welcome.pap", "welcome_no_target.pap")] = loop_lp
        (self.out / "meta.json").write_text(json.dumps(meta))
        res = self.verify(None)  # Snowball shape, lookup unavailable: the no-target name cannot be derived from its key
        self.assertTrue(check(res, "pap_names")["pass"], res)

    def test_loop_without_c0101_needs_no_target_only_for_its_own_races(self):
        """Good Thing's Animation option: no c0101 loop donor, so the tool fills no extra races; only the mapped race needs a no-target entry."""
        self.add_nt()
        meta = json.loads((self.out / "meta.json").read_text())
        for files in self.option_files(meta)[1:2]:
            for k in [k for k in files if "/c0101/" in k and "emote/welcome" in k]:
                del files[k]
        (self.out / "meta.json").write_text(json.dumps(meta))
        self.assertTrue(check(self.verify(), "no_target")["pass"], self.verify())

    def test_no_target_redirect_on_an_emote_without_a_no_target_row_fails(self):
        self.add_nt()
        res = self.verify(self.slots[:1])
        self.assertFalse(check(res, "no_target")["pass"], res)
        self.assertIn("no no-target row", check(res, "no_target")["detail"])

    def test_variant_pap_name_finds_its_source_counterpart(self):
        self.add_nt()
        f = self.out / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop__welcome_no_target.pap"
        cp = vs.src_counterpart(self.src, self.out, f)
        self.assertEqual(cp, self.src / "chara/human/c0101/animation/a0001/bt_common/emote/dance16_loop.pap")

    def test_report_races_and_unmapped_with_a_no_target_slot(self):
        self.add_nt()
        log = "target slots: {'loop': 'emote/welcome', 'no_target': 'emote/welcome_no_target'}\n3 PAP(s) patched. Not tested in game.\n"
        r = swapd.parse_report(log, self.out)
        self.assertEqual((r["paps_patched"], r["races"]), (3, 2))
        self.env.fake_lookup(self.slots)
        self.assertEqual(swapd.unmapped_keys("Welcome", self.out), [])
