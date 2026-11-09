#!/usr/bin/env python3
"""Convert every exported Fusion design into a SolidCraft .sc beside it.

For each design folder it writes <design>.sc, the machine report <design>.dutchman.json
(with an "f2s" block saying which method produced the .sc and why) and, for Dutchman
conversions, the full text report <design>.dutchman.txt.

Methods, in order:
  archive         Dutchman on the Fusion archive, with the requested preset
  archive-<p>     the same with a lighter preset (standard, then fast) when the
                  requested one needs more memory than is free
  mesh            the design's mesh bodies (meshes/*.stl from the export) inserted as
                  mesh bodies, when the archive holds only meshes Dutchman can't decode
  step            Fusion's STEP export through Dutchman (geometry only), when the archive
                  has no readable geometry but the STEP does, e.g. a design that
                  references other designs, whose archive leaves their geometry out
Designs whose .sc is newer than the archive are skipped unless --force, so a run
after a tool upgrade should use --force.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import common  # noqa: E402

LIGHTER = {"full": ["standard", "fast"], "standard": ["fast"], "fast": []}
STEP_SOLIDS = (b"MANIFOLD_SOLID_BREP", b"BREP_WITH_VOIDS", b"SHELL_BASED_SURFACE_MODEL")


def up_to_date(archive, sc, report):
    """Whether the last conversion still stands: its .sc is newer than the archive, or the
    archive had no geometry at all (no .sc to make, so re-running would only repeat that)."""
    if not os.path.exists(report) or os.path.getmtime(report) < os.path.getmtime(archive):
        return False
    if os.path.exists(sc):
        return os.path.getmtime(sc) >= os.path.getmtime(archive)
    r = common.load_json(report)
    return (r.get("error") or {}).get("kind") == "no_geometry" or (r.get("f2s") or {}).get("empty", False)


def dutchman(src, sc, preset, log, timeout):
    """Run one Dutchman conversion; return (ok, report dict, stderr)."""
    cmd = ["dutchman", "convert", src, sc, "--preset", preset, "--yes", "--quiet", "--json", "--log", log]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", "timed out after %ss" % timeout, -1
    try:
        report = json.loads(out) if out.strip().startswith("{") else None
    except ValueError:
        report = None
    if report is None:
        report = {"ok": False, "exit": rc, "error": {"message": (err.strip().splitlines() or ["no output"])[-1]}}
    return rc == 0 and report.get("ok", False), report, err


def error_of(report):
    e = report.get("error") or {}
    return e.get("kind") or "", e.get("message") or ""


def step_has_solids(step):
    """Whether a STEP file holds any solid or surface model (streamed, files can be large)."""
    if not os.path.exists(step):
        return False
    tail = b""
    with open(step, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return False
            if any(k in tail + chunk for k in STEP_SOLIDS):
                return True
            tail = chunk[-64:]


def mesh_files(design_dir):
    manifest = os.path.join(design_dir, "manifest.json")
    listed = []
    if os.path.exists(manifest):
        listed = common.load_json(manifest).get("fusion", {}).get("mesh_files") or []
    files = [os.path.join(design_dir, f) for f in listed] or sorted(glob.glob(os.path.join(design_dir, "meshes", "*.stl")))
    return [f for f in files if os.path.exists(f)]


def from_meshes(files, sc, timeout):
    """Build a .sc holding the STL files as mesh bodies; return (ok, error)."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "meshes.json")
        common.write_json(script, {"commands": [
            {"command": "file.insert_mesh", "params": {"path": f}} for f in files]})
        try:
            p = subprocess.run(["solidcraft-cli", "run", script, "--save", sc, "--quiet"],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timed out after %ss" % timeout
        return p.returncode == 0, (p.stderr.strip().splitlines() or [""])[-1]


IDENT = re.compile(r"[A-Za-z_]\w*")


def param_order(params):
    """Fusion user parameters ordered so each comes after the parameters its expression uses."""
    by_name = {p["name"]: p for p in params}
    done, order = set(), []

    def visit(name, path):
        if name in done or name in path:
            return
        path.add(name)
        for ref in IDENT.findall(by_name[name].get("expression") or ""):
            if ref in by_name and ref != name:
                visit(ref, path)
        done.add(name)
        order.append(by_name[name])

    for p in params:
        visit(p["name"], set())
    return order


def sc_param_names(sc, timeout):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "eval.json")
        try:
            p = subprocess.run(["solidcraft-cli", "eval", sc, "--json-out", out],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode != 0 or not os.path.exists(out):
            return None
        return {q["name"] for q in common.load_json(out).get("params", [])}


def restore_params(design_dir, sc, timeout):
    """Add the Fusion user parameters a conversion dropped back into the .sc.

    Dutchman leaves out parameters it can't decode, and every parameter of a design whose
    history stops early. Fusion's manifest has them all, with their expressions. They are
    added as user parameters (they don't drive the imported final shape), in dependency
    order; one that SolidCraft refuses is skipped, along with the parameters that use it.
    Returns {"added": [...], "skipped": {name: reason}} or None when there's nothing to do.
    """
    manifest = os.path.join(design_dir, "manifest.json")
    if not os.path.exists(manifest):
        return None
    params = common.load_json(manifest).get("parameters") or []
    have = sc_param_names(sc, timeout) if params else set()
    if have is None:
        return {"added": [], "skipped": {"*": "the .sc could not be evaluated"}}
    todo = [p for p in param_order(params) if p["name"] not in have]
    result = {"added": [], "skipped": {}}
    while todo:
        cmds = [{"command": "parameters.add", "params": {
            "name": p["name"], "expression": p["expression"], "unit": p.get("unit") or "",
            **({"comment": p["comment"]} if p.get("comment") else {})}} for p in todo]
        with tempfile.TemporaryDirectory() as tmp:
            script, out = os.path.join(tmp, "params.json"), os.path.join(tmp, "out.sc")
            common.write_json(script, {"commands": cmds})
            try:
                r = subprocess.run(["solidcraft-cli", "run", script, "--in", sc, "--save", out, "--quiet"],
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                result["skipped"].update({p["name"]: "timed out" for p in todo})
                break
            if r.returncode == 0 and os.path.exists(out):
                os.replace(out, sc)
                result["added"] = [p["name"] for p in todo]
                break
        err = (r.stderr.strip().splitlines() or ["refused"])[-1]
        m = re.search(r"step (\d+)", err)
        bad = todo[int(m.group(1)) - 1]["name"] if m and 0 < int(m.group(1)) <= len(todo) else None
        if bad is None:
            result["skipped"].update({p["name"]: err for p in todo})
            break
        result["skipped"][bad] = err.split(": ", 2)[-1]
        dropped = {bad}
        kept = []
        for p in todo:  # todo is dependency-ordered, so users come after what they use
            if p["name"] in dropped:
                continue
            if dropped & set(IDENT.findall(p.get("expression") or "")):
                dropped.add(p["name"])
                result["skipped"][p["name"]] = "uses %s, which was skipped" % bad
                continue
            kept.append(p)
        todo = kept
    return result


def needs_params(report):
    return report.get("ok") and "params" not in (report.get("f2s") or {}) \
        and (report.get("f2s") or {}).get("method") != "mesh"


def empty_in_fusion(design_dir):
    manifest = os.path.join(design_dir, "manifest.json")
    return os.path.exists(manifest) and common.nothing_in_fusion(common.load_json(manifest))


def convert(design_dir, archive, preset, timeout):
    """Convert one design; return (ok, method, seconds, message). ok is None when Fusion
    has nothing to convert, so there's no .sc to make."""
    s = common.stem(archive)
    sc = os.path.join(design_dir, s + ".sc")
    report_path = os.path.join(design_dir, s + ".dutchman.json")
    log = os.path.join(design_dir, s + ".dutchman.txt")
    t0 = time.time()
    attempts = []

    method, ok, report = "archive", False, {}
    for p in [preset] + LIGHTER[preset]:
        method = "archive" if p == preset else "archive-" + p
        ok, report, _ = dutchman(archive, sc, p, log, timeout)
        kind, msg = error_of(report)
        attempts.append({"method": method, "ok": ok, "error": msg or None})
        if ok or "needs about" not in msg:
            break  # only a memory refusal is worth retrying with a lighter preset

    if not ok:
        kind, msg = error_of(report)
        archive_unreadable = kind in ("decode", "no_geometry")
        meshes = mesh_files(design_dir) if archive_unreadable else []
        step = os.path.join(design_dir, s + ".step")
        if meshes:
            method = "mesh"
            ok, err = from_meshes(meshes, sc, timeout)
            attempts.append({"method": method, "ok": ok, "error": err or None})
            report = {"ok": ok, "mode": "mesh", "history": {"mapped": 0, "total": 0},
                      "mesh_files": [os.path.relpath(f, design_dir) for f in meshes]}
        elif archive_unreadable and step_has_solids(step):
            method = "step"
            ok, report, _ = dutchman(step, sc, "full", log, timeout)
            attempts.append({"method": method, "ok": ok, "error": error_of(report)[1] or None})

    report["f2s"] = {"method": method, "attempts": attempts}
    if not ok and empty_in_fusion(design_dir):
        report["f2s"]["empty"] = True
        ok = None
    if needs_params(report):
        report["f2s"]["params"] = restore_params(design_dir, sc, timeout)
    common.write_json(report_path, report)
    return ok, method, time.time() - t0, attempts[-1].get("error") or ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_workspace_arg(ap)
    ap.add_argument("--preset", default="full", choices=["fast", "standard", "full"])
    ap.add_argument("--force", action="store_true", help="re-convert even when the .sc is up to date")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds per design")
    ap.add_argument("--strict-versions", action="store_true", help="refuse to run off the pinned tool versions")
    args = ap.parse_args()

    ws = common.find_workspace(args.workspace)
    common.check_pins(ws, args.strict_versions)
    jobs = common.selected(ws, args)
    if not jobs:
        print("no Fusion archives under %s (run 'Export Hub to SolidCraft' in Fusion first, or check --only/--design)" % ws.designs)
        return 1
    done = skipped = failed = empty = 0
    for n, (d, archive) in enumerate(jobs, 1):
        rel = os.path.relpath(archive, ws.designs)
        s = common.stem(archive)
        report_path = os.path.join(d, s + ".dutchman.json")
        if not args.force and up_to_date(archive, os.path.join(d, s + ".sc"), report_path):
            report = common.load_json(report_path)
            if needs_params(report):  # converted before parameter restoring existed
                report["f2s"]["params"] = restore_params(d, os.path.join(d, s + ".sc"), args.timeout)
                common.write_json(report_path, report)
                print("[%d/%d] %-22s %s" % (n, len(jobs), "params", rel), flush=True)
            skipped += 1
            continue
        ok, method, secs, msg = convert(d, archive, args.preset, args.timeout)
        if ok is None:
            empty += 1
            print("[%d/%d] %-22s %6.1fs  %s" % (n, len(jobs), "empty", secs, rel), flush=True)
            continue
        status = ("ok " if ok else "FAIL ") + method
        print("[%d/%d] %-22s %6.1fs  %s" % (n, len(jobs), status, secs, rel), flush=True)
        if ok:
            done += 1
        else:
            failed += 1
            if msg:
                print("         " + msg, file=sys.stderr)
    print("converted %d, up to date %d, empty in Fusion %d, failed %d" % (done, skipped, empty, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
