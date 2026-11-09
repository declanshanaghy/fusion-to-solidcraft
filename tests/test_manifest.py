"""Regression tests for the Fusion manifest code, run outside Fusion with a stub adsk API.

Covers the failures seen in real Fusion runs: joint lists being Python vectors
(no .count), direct-modelling designs raising on userParameters, and mesh bodies
(which only reach SolidCraft as STL).
"""

import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Coll:
    def __init__(self, items):
        self.items = list(items)
        self.count = len(self.items)

    def item(self, i):
        return self.items[i]


def stub_adsk():
    adsk = types.ModuleType("adsk")
    core = types.ModuleType("adsk.core")
    fusion = types.ModuleType("adsk.fusion")
    fusion.DesignTypes = types.SimpleNamespace(ParametricDesignType=1, DirectDesignType=0)
    adsk.core, adsk.fusion = core, fusion
    adsk.doEvents = lambda: None
    sys.modules.update({"adsk": adsk, "adsk.core": core, "adsk.fusion": fusion})


class Point:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def body(name, vol_cm3=1.0):
    bb = types.SimpleNamespace(minPoint=Point(0, 0, 0), maxPoint=Point(1, 2, 3))
    return types.SimpleNamespace(name=name, isSolid=True, isVisible=True, volume=vol_cm3,
                                 area=6.0, boundingBox=bb)


class Raises:
    def __get__(self, obj, owner):
        raise RuntimeError("3 : this is not a parametric design")


def design(parametric, meshes=()):
    root = types.SimpleNamespace(name="root", bRepBodies=Coll([body("Body1", 2.0)]),
                                 meshBodies=Coll(meshes),
                                 allOccurrences=Coll([]),
                                 allJoints=[object(), object()],  # JointVector: a list, no .count
                                 allAsBuiltJoints=[object()])
    comp = types.SimpleNamespace(name="root", sketches=Coll([]))
    attrs = {"rootComponent": root, "allComponents": Coll([comp]),
             "designType": 1 if parametric else 0, "unitsManager": None}
    if parametric:
        p = types.SimpleNamespace(name="h", expression="20 mm", unit="", value=2.0, comment="")
        attrs["userParameters"] = Coll([p])
        attrs["timeline"] = Coll([])
        return types.SimpleNamespace(**attrs)
    cls = type("DirectDesign", (), {"userParameters": Raises(), "timeline": Raises()})
    d = cls()
    d.__dict__.update(attrs)
    return d


class FakeExportManager:
    """Records STL exports and writes a placeholder file, as Fusion would."""

    def __init__(self):
        self.written = []

    def createSTLExportOptions(self, geometry, path):
        return types.SimpleNamespace(geometry=geometry, path=path, isBinaryFormat=False)

    def execute(self, opts):
        with open(opts.path, "wb") as f:
            f.write(b"solid")
        self.written.append(opts.path)
        return True


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stub_adsk()
        sys.path.insert(0, os.path.join(ROOT, "fusion", "lib"))
        import f2s_export
        cls.m = f2s_export

    df = types.SimpleNamespace(id="urn:x", name="Part", versionNumber=3)

    def test_direct_design_does_not_fail(self):
        man = self.m.build_manifest(self.df, design(parametric=False), "Part.f3d")
        self.assertFalse(man["fusion"]["parametric"])
        self.assertEqual(man["parameters"], [])
        self.assertEqual(man["errors"], [])
        self.assertAlmostEqual(man["totals"]["volume_mm3_all_solids"], 2000.0)

    def test_joint_vectors_are_counted(self):
        man = self.m.build_manifest(self.df, design(parametric=True), "Part.f3d")
        self.assertEqual(man["totals"]["joints"], 3)
        self.assertEqual([p["name"] for p in man["parameters"]], ["h"])

    def test_mesh_bodies_are_listed_and_exported(self):
        mesh = types.SimpleNamespace(name="Ball/Nut", isVisible=True)
        hidden = types.SimpleNamespace(name="old", isVisible=False)
        d = design(parametric=False, meshes=[mesh, hidden])
        man = self.m.build_manifest(self.df, d, "Part.f3d")
        self.assertEqual(man["totals"]["mesh_bodies"], 2)
        with tempfile.TemporaryDirectory() as out:
            errors = []
            files = self.m.export_meshes(FakeExportManager(), d.rootComponent, out, errors)
            self.assertEqual(files, ["meshes/01-Ball_Nut.stl"])  # hidden meshes skipped, name made safe
            self.assertTrue(os.path.exists(os.path.join(out, files[0])))
            self.assertEqual(errors, [])

    def test_write_manifest_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            man = self.m.write_manifest(d, self.df, object(), "Part.f3d", False)
            self.assertTrue(os.path.exists(os.path.join(d, "manifest.json")))
            self.assertTrue(man["errors"])



if __name__ == "__main__":
    unittest.main()
