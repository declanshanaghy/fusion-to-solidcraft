"""Tests for verify/check.py's measuring helpers, with solidcraft-cli stubbed out."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "verify"))
sys.path.insert(0, ROOT)
import check  # noqa: E402


def body(name, volume, lo=(0, 0, 0), hi=(1, 1, 1)):
    return {"name": name, "volume_mm3": volume, "bbox": {"min": list(lo), "max": list(hi)}}


class VerifyHelpersTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, check, "sc_eval", check.sc_eval)

    def test_null_volume_counts_as_zero(self):
        # SolidCraft reports volume_mm3: null for open bodies (surfaces); this crashed
        # the first full verify run on Prusa Mini Assembly.
        self.assertEqual(check.vol({"volume_mm3": None}), 0.0)
        self.assertEqual(check.vol({}), 0.0)

    def test_step_totals_from_step_eval(self):
        check.sc_eval = lambda path, timeout, script=None: (
            {"body_count": 2, "bodies": [body("a", 10.0, hi=(5, 5, 5)), body("b", None, lo=(-1, 0, 0))]}, None)
        totals, err = check.step_totals("x.step", 1)
        self.assertIsNone(err)
        self.assertEqual(totals["solid_bodies"], 2)
        self.assertEqual(totals["volume_mm3_all_solids"], 10.0)
        self.assertEqual(totals["bbox_mm"], {"min": [-1, 0, 0], "max": [5, 5, 5]})

    def test_partial_design_history_bodies_are_excluded(self):
        final = body("Final", 100.0)
        history = body("Body1", 120.0)
        ev = {"bodies": [final, history],
              "timeline": [{"type": "Sketch"}, {"type": "RevolveFeature"}, {"type": "BaseFeature"}]}
        check.sc_eval = lambda path, timeout, script=None: ({"bodies": [history]}, None)
        bodies, note = check.final_bodies("x.sc", ev, 1)
        self.assertEqual(([b["name"] for b in bodies], note), (["Final"], ""))

    def test_empty_when_fusion_has_nothing(self):
        self.assertTrue(check.nothing_in_fusion({"totals": {"bodies": 0, "solid_bodies": 0}, "bodies": [], "meshes": []}))
        self.assertFalse(check.nothing_in_fusion({"totals": {}, "bodies": [], "meshes": [{"visible": True}]}))


if __name__ == "__main__":
    unittest.main()
