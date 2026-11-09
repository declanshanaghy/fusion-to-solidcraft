#!/usr/bin/env python3
"""Score every converted design against the Fusion ground truth.

For each design folder it compares <design>.sc (measured with `solidcraft-cli eval`)
with manifest.json (written by the Fusion export) and <design>.dutchman.json, then
writes scorecard.csv and scorecard.md into the workspace's reports folder.

Category per design:
  full      every Fusion feature mapped into editable SolidCraft history
  partial   history up to the first unmapped feature, final shape imported after it
  geometry  shape only (no editable history), including designs converted from STEP
  mesh      mesh bodies only (Fusion mesh bodies, brought over as STL)
  empty     nothing to convert: Fusion has no solids, surfaces or meshes in it
  failed    no .sc, the conversion failed, or SolidCraft cannot evaluate it
A design is also flagged when its volume, body count or parameters disagree.

Reference: manifest.json when the export wrote one (geometry, parameters and
timeline). Without it, Fusion's own STEP export of the design is measured instead,
which checks geometry only.
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import common  # noqa: E402

FIELDS = ["design", "category", "reference", "mapped", "total", "volume_ok", "volume_rel_err", "bodies_fusion",
          "bodies_sc", "bbox_max_diff_mm", "params_fusion", "params_missing", "params_changed",
          "timeline_errors", "first_blocker", "losses", "note"]


def sc_eval(sc, timeout, script=None):
    """Measure a design (or STEP). With script, it runs on the design first (--in)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "eval.json")
        cmd = ["solidcraft-cli", "eval", sc, "--json-out", out]
        if script is not None:
            path = os.path.join(tmp, "script.json")
            common.write_json(path, script)
            cmd = ["solidcraft-cli", "eval", path, "--in", sc, "--json-out", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "eval timed out"
        if p.returncode != 0 or not os.path.exists(out):
            return None, (p.stderr.strip().splitlines() or ["eval failed"])[-1]
        return common.load_json(out), None


def final_bodies(sc, ev, timeout):
    """The bodies that make up the design's final shape, and a note on how they were chosen.

    A partial conversion keeps its editable history and then imports the exact final
    shape as a last base feature. The history's own bodies stay in the design, hidden,
    and must not be counted twice. Rolling back to just before that import shows which
    bodies belong to the history; the final shape is every other body.
    """
    bodies = ev.get("bodies", [])
    timeline = ev.get("timeline", [])
    imports = [i for i, t in enumerate(timeline) if t.get("type") == "BaseFeature"]
    if not imports or imports[-1] == 0:
        return bodies, ""
    before, err = sc_eval(sc, timeout, {"commands": [
        {"command": "timeline.rollback", "params": {"position": imports[-1]}}]})
    if before is None:
        return bodies, "history bodies not separated (%s)" % err
    history = Counter((b["name"], round(vol(b), 3)) for b in before.get("bodies", []))
    final = []
    for b in bodies:
        key = (b["name"], round(vol(b), 3))
        if history[key]:
            history[key] -= 1
        else:
            final.append(b)
    return final, ""


def vol(body):
    """A body's volume in mm³; SolidCraft reports null for open bodies such as surfaces."""
    return body.get("volume_mm3") or 0.0


def norm_expr(e):
    return re.sub(r"\s+", "", str(e or "")).lower()


CATEGORIES = ("full", "partial", "geometry", "mesh", "empty", "failed")


def category(report):
    if not report:
        return "failed"
    method = (report.get("f2s") or {}).get("method", "archive")
    if not report.get("ok"):
        kind = (report.get("error") or {}).get("kind")
        return "empty" if kind == "no_geometry" else "failed"
    if method == "mesh":
        return "mesh"
    if method == "step":
        return "geometry"
    h = report.get("history") or {}
    mapped, total = h.get("mapped", 0), h.get("total", 0)
    if total and mapped >= total:
        return "full"
    if mapped:
        return "partial"
    return "geometry"


def nothing_in_fusion(manifest):
    """True when Fusion's manifest records no solids, surfaces or visible meshes."""
    t = manifest.get("totals") or {}
    bodies = manifest.get("bodies") or []
    meshes = [m for m in manifest.get("meshes") or [] if m.get("visible")]
    return not bodies and not meshes and not t.get("solid_bodies") and not t.get("bodies")


def method_note(report):
    """Why a design used a fallback, from convert's f2s block."""
    f2s = (report or {}).get("f2s") or {}
    method, attempts = f2s.get("method", "archive"), f2s.get("attempts") or []
    first = (attempts[0].get("error") if attempts else None) or ""
    if method == "step":
        return "converted from Fusion STEP (archive: %s)" % first
    if method == "mesh":
        return "mesh bodies only (archive: %s)" % first
    if method.startswith("archive-"):
        return "used preset %s (%s)" % (method.split("-", 1)[1], first)
    return ""


def step_totals(step, timeout):
    """Manifest-style totals measured from Fusion's STEP export of the design."""
    ev, err = sc_eval(step, timeout)
    if ev is None:
        return None, err
    bodies = ev.get("bodies", [])
    boxes = [b["bbox"] for b in bodies if b.get("bbox")]
    bbox = None
    if boxes:
        bbox = {"min": [min(b["min"][i] for b in boxes) for i in range(3)],
                "max": [max(b["max"][i] for b in boxes) for i in range(3)]}
    total = sum(vol(b) for b in bodies)
    return {"solid_bodies": ev.get("body_count", len(bodies)), "volume_mm3_all_solids": total,
            "volume_mm3_visible_solids": total, "bbox_mm": bbox}, None


def score(root, design_dir, archive, tol, timeout):
    s = common.stem(archive)
    row = {"design": os.path.relpath(design_dir, root)}
    manifest_path = os.path.join(design_dir, "manifest.json")
    manifest = common.load_json(manifest_path) if os.path.exists(manifest_path) else None
    report_path = os.path.join(design_dir, s + ".dutchman.json")
    report = common.load_json(report_path) if os.path.exists(report_path) else None
    sc = os.path.join(design_dir, s + ".sc")

    row["category"] = category(report)
    if row["category"] == "failed" and manifest and nothing_in_fusion(manifest):
        row["category"] = "empty"  # the converter's own error aside, Fusion has nothing to bring over
    h = (report or {}).get("history") or {}
    row["mapped"], row["total"] = h.get("mapped", ""), h.get("total", "")
    blockers = [f for f in h.get("features", []) if f.get("status") == "blocked"]
    row["first_blocker"] = ("%s: %s" % (blockers[0]["name"], blockers[0].get("reason"))) if blockers else ""
    row["losses"] = len((report or {}).get("losses") or [])

    if row["category"] == "empty":
        msg = ((report or {}).get("error") or {}).get("message", "")
        row["note"] = "nothing to convert: %d sketches in Fusion, no bodies%s" % (
            (manifest or {}).get("totals", {}).get("sketches", 0), " (%s)" % msg if msg else "")
        return row
    if not os.path.exists(sc):
        err = (report or {}).get("error") or {}
        row.update(category="failed", note=err.get("message") or "no .sc (run convert first)")
        return row
    ev, err = sc_eval(sc, timeout)
    if ev is None:
        row.update(category="failed", note=err)
        return row
    row["timeline_errors"] = sum(1 for t in ev.get("timeline", []) if t.get("error"))
    bodies, body_note = (final_bodies(sc, ev, timeout) if row["category"] == "partial"
                         else (ev.get("bodies", []), ""))
    row["bodies_sc"] = len(bodies)
    if row["category"] == "mesh":
        # Fusion reports no volume for mesh bodies and leaves them out of STEP, so there
        # is no geometry reference; count them against the manifest when there is one.
        row["volume_ok"] = "n/a"
        notes = [method_note(report)]
        if manifest:
            want = sum(1 for m in manifest.get("meshes", []) if m.get("visible"))
            row["bodies_fusion"] = want
            if want != row["bodies_sc"]:
                notes.append("mesh count differs")
        row["note"] = "; ".join(n for n in notes if n)
        return row

    step = os.path.join(design_dir, s + ".step")
    if manifest:
        row["reference"] = "manifest"
        t = manifest["totals"]
    elif os.path.exists(step):
        row["reference"] = "step"
        t, err = step_totals(step, timeout)
        if t is None:
            row["note"] = "Fusion STEP could not be measured: %s" % err
            return row
    else:
        row["reference"] = "none"
        row["note"] = "no manifest.json or STEP: geometry not checked"
        return row

    row["bodies_fusion"] = t["solid_bodies"]
    vol_sc = sum(vol(b) for b in bodies)
    refs = [v for v in (t.get("volume_mm3_visible_solids"), t.get("volume_mm3_all_solids")) if v]
    if refs:
        rel = min(abs(vol_sc - v) / v for v in refs)
        row["volume_rel_err"] = "%.5f" % rel
        row["volume_ok"] = rel <= tol
    else:
        row["volume_ok"] = vol_sc == 0
    fb = t.get("bbox_mm")
    boxes = [b["bbox"] for b in bodies if b.get("bbox")]
    if fb and boxes:
        lo = [min(b["min"][i] for b in boxes) for i in range(3)]
        hi = [max(b["max"][i] for b in boxes) for i in range(3)]
        row["bbox_max_diff_mm"] = "%.3f" % max(
            max(abs(lo[i] - fb["min"][i]), abs(hi[i] - fb["max"][i])) for i in range(3))

    sc_params = {p["name"]: p for p in ev.get("params", []) if not p.get("model")}
    fparams = manifest.get("parameters", []) if manifest else []
    row["params_fusion"] = len(fparams) if manifest else "n/a"
    missing = [p["name"] for p in fparams if p["name"] not in sc_params]
    changed = [p["name"] for p in fparams if p["name"] in sc_params
               and norm_expr(p["expression"]) != norm_expr(sc_params[p["name"]].get("expression"))]
    row["params_missing"] = " ".join(missing)
    row["params_changed"] = " ".join(changed)
    notes = [n for n in (method_note(report), body_note) if n]
    if row.get("volume_ok") is False:
        notes.append("volume differs")
    if row["bodies_sc"] != row["bodies_fusion"]:
        notes.append("body count differs")
    if missing:
        notes.append("%d params missing" % len(missing))
    if row["timeline_errors"]:
        notes.append("%d timeline errors" % row["timeline_errors"])
    row["note"] = "; ".join(notes)
    return row


def write_md(rows, path, versions):
    counts = {c: sum(1 for r in rows if r["category"] == c) for c in CATEGORIES}
    flagged = [r for r in rows if r.get("note")]
    lines = ["# Migration scorecard", "",
             "Tools: " + ", ".join("%s %s" % kv for kv in versions.items())
             + ", fusion-to-solidcraft " + common.tools_rev(), "",
             "| Category | Designs |", "|---|---|"]
    lines += ["| %s | %d |" % (c, n) for c, n in counts.items()]
    lines += ["| **total** | **%d** |" % len(rows), "", "## Needs attention", ""]
    attention = [r for r in rows if r["category"] != "full" or r.get("note")]
    if not attention:
        lines.append("Nothing: every design converted with full history and matching geometry.")
    else:
        lines += ["| Design | Category | Mapped | First blocker | Note |", "|---|---|---|---|---|"]
        for r in attention:
            cell = lambda v: str(v if v is not None else "").replace("|", "\\|")  # noqa: E731
            lines.append("| %s | %s | %s/%s | %s | %s |" % (
                cell(r["design"]), r["category"], cell(r.get("mapped")), cell(r.get("total")),
                cell(r.get("first_blocker")), cell(r.get("note"))))
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return counts, len(flagged)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_workspace_arg(ap)
    ap.add_argument("--tolerance", type=float, default=0.001, help="relative volume tolerance (default 0.1%%)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per solidcraft-cli eval")
    ap.add_argument("--out", help="report folder (default: the workspace's reports_dir)")
    args = ap.parse_args()

    ws = common.find_workspace(args.workspace)
    out = args.out or ws.reports
    versions = common.check_pins(ws, strict=False)
    rows = []
    for d, archive in common.selected(ws, args):
        try:
            r = score(ws.designs, d, archive, args.tolerance, args.timeout)
        except Exception as e:  # one design's problem must not stop the scorecard
            r = {"design": os.path.relpath(d, ws.designs), "category": "failed",
                 "note": "verify error: %s: %s" % (type(e).__name__, e)}
        print("%-9s %s%s" % (r["category"], r["design"], ("  [" + r["note"] + "]") if r.get("note") else ""), flush=True)
        rows.append(r)
    if not rows:
        print("no designs under %s" % ws.designs)
        return 1
    os.makedirs(out, exist_ok=True)
    csv_path = os.path.join(out, "scorecard.csv")
    if (args.only or args.design) and os.path.exists(csv_path):
        # A subset run updates its designs' rows and keeps everyone else's.
        fresh = {r["design"] for r in rows}
        with open(csv_path, newline="", encoding="utf-8") as f:
            kept = [r for r in csv.DictReader(f) if r.get("design") not in fresh
                    and os.path.isdir(os.path.join(ws.designs, r.get("design", "")))]
        rows = sorted(kept + rows, key=lambda r: r["design"])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    counts, flagged = write_md(rows, os.path.join(out, "scorecard.md"), versions)
    print("\n" + ", ".join("%s %d" % kv for kv in counts.items()) + "; flagged %d" % flagged)
    print("wrote %s" % os.path.join(out, "scorecard.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
