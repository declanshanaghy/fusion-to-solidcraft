"""Tests for convert/convert.py's parameter restoring, with solidcraft-cli stubbed out."""

import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "convert"))
sys.path.insert(0, ROOT)
import convert  # noqa: E402


def param(name, expression, unit="mm"):
    return {"name": name, "expression": expression, "unit": unit, "comment": ""}


class ParamOrderTest(unittest.TestCase):
    def test_dependencies_come_first(self):
        order = [p["name"] for p in convert.param_order(
            [param("c", "b * 2"), param("b", "a + 1 mm"), param("a", "3 mm"), param("d", "round(c / 7)", "")])]
        self.assertEqual(order, ["a", "b", "c", "d"])

    def test_cycle_does_not_recurse_forever(self):
        order = [p["name"] for p in convert.param_order([param("a", "b"), param("b", "a")])]
        self.assertEqual(sorted(order), ["a", "b"])


class RestoreParamsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sc = os.path.join(self.tmp.name, "d.sc")
        open(self.sc, "w").close()
        self.addCleanup(setattr, convert, "sc_param_names", convert.sc_param_names)
        self.addCleanup(setattr, convert.subprocess, "run", convert.subprocess.run)

    def manifest(self, params):
        with open(os.path.join(self.tmp.name, "manifest.json"), "w") as f:
            json.dump({"parameters": params}, f)

    def stub_cli(self, refuse):
        """solidcraft-cli run that refuses the first parameter named in refuse, like the real one."""
        def run(cmd, **kw):
            with open(cmd[cmd.index("run") + 1]) as f:
                script = json.load(f)
            for i, c in enumerate(script["commands"], 1):
                if c["params"]["name"] in refuse:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr=(
                        "error: step %d (parameters.add): expression: unknown parameter `d1`" % i))
            open(cmd[cmd.index("--save") + 1], "w").close()
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        convert.subprocess.run = run

    def test_adds_only_missing_parameters(self):
        self.manifest([param("a", "3 mm"), param("b", "a * 2")])
        convert.sc_param_names = lambda sc, t: {"a"}
        self.stub_cli(refuse=set())
        self.assertEqual(convert.restore_params(self.tmp.name, self.sc, 10), {"added": ["b"], "skipped": {}})

    def test_refused_parameter_and_its_users_are_skipped(self):
        self.manifest([param("a", "d1 * 2"), param("b", "a + 1 mm"), param("c", "5 mm")])
        convert.sc_param_names = lambda sc, t: set()
        self.stub_cli(refuse={"a"})
        r = convert.restore_params(self.tmp.name, self.sc, 10)
        self.assertEqual(r["added"], ["c"])
        self.assertEqual(sorted(r["skipped"]), ["a", "b"])
        self.assertIn("unknown parameter", r["skipped"]["a"])

    def test_no_manifest_means_nothing_to_do(self):
        self.assertIsNone(convert.restore_params(self.tmp.name, self.sc, 10))

    def test_mesh_and_failed_reports_are_not_restored(self):
        self.assertFalse(convert.needs_params({"ok": True, "f2s": {"method": "mesh"}}))
        self.assertFalse(convert.needs_params({"ok": False, "f2s": {"method": "archive"}}))
        self.assertFalse(convert.needs_params({"ok": True, "f2s": {"method": "archive", "params": None}}))
        self.assertTrue(convert.needs_params({"ok": True, "f2s": {"method": "archive"}}))


class UpToDateTest(unittest.TestCase):
    def test_design_with_nothing_in_fusion_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, report = os.path.join(tmp, "d.f3d"), os.path.join(tmp, "d.dutchman.json")
            open(archive, "w").close()
            with open(report, "w") as f:
                json.dump({"ok": False, "error": {"kind": "decode"}, "f2s": {"empty": True}}, f)
            os.utime(archive, (0, 0))
            self.assertTrue(convert.up_to_date(archive, os.path.join(tmp, "d.sc"), report))
            with open(report, "w") as f:
                json.dump({"ok": False, "error": {"kind": "decode"}, "f2s": {}}, f)
            self.assertFalse(convert.up_to_date(archive, os.path.join(tmp, "d.sc"), report))


if __name__ == "__main__":
    unittest.main()
