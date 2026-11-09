#!/usr/bin/env python3
"""Score every converted design against the Fusion ground truth.

For each design folder it compares <design>.sc (measured with `solidcraft-cli eval`)
with manifest.json (written by the Fusion export) and <design>.dutchman.json, then
writes scorecard.csv and scorecard.md into the workspace's reports folder.

Category per design:
  full      every Fusion feature mapped into editable SolidCraft history
  partial   history up to the first unmapped feature, final shape imported after it
  geometry  shape only (no editable history)
  failed    no .sc, the conversion failed, or SolidCraft cannot evaluate it
A design is also flagged when its volume, body count or parameters disagree.
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import common  # noqa: E402

FIELDS = ["design", "category", "mapped", "total", "volume_ok", "volume_rel_err", "bodies_fusion",
          "bodies_sc", "bbox_max_diff_mm", "params_fusion", "params_missing", "params_changed",
          "timeline_errors", "first_blocker", "losses", "note"]


def sc_eval(sc, timeout):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "eval.json")
        try:
            p = subprocess.run(["solidcraft-cli", "eval", sc, "--json-out", out],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "eval timed out"
        if p.returncode != 0 or not os.path.exists(out):
            return None, (p.stderr.strip().splitlines() or ["eval failed"])[-1]
        return common.load_json(out), None


def norm_expr(e):
    return re.sub(r"\s+", "", str(e or "")).lower()


def category(report):
    if not report or not report.get("ok"):
        return "failed"
    h = report.get("history") or {}
    mapped, total = h.get("mapped", 0), h.get("total", 0)
    if total and mapped >= total:
        return "full"
    if mapped:
        return "partial"
    return "geometry"


def score(root, design_dir, archive, tol, timeout):
    s = common.stem(archive)
    row = {"design": os.path.relpath(design_dir, root)}
    manifest_path = os.path.join(design_dir, "manifest.json")
    manifest = common.load_json(manifest_path) if os.path.exists(manifest_path) else None
    report_path = os.path.join(design_dir, s + ".dutchman.json")
    report = common.load_json(report_path) if os.path.exists(report_path) else None
    sc = os.path.join(design_dir, s + ".sc")

    row["category"] = category(report)
    h = (report or {}).get("history") or {}
    row["mapped"], row["total"] = h.get("mapped", ""), h.get("total", "")
    blockers = [f for f in h.get("features", []) if f.get("status") == "blocked"]
    row["first_blocker"] = ("%s: %s" % (blockers[0]["name"], blockers[0].get("reason"))) if blockers else ""
    row["losses"] = len((report or {}).get("losses") or [])

    if not os.path.exists(sc):
        row.update(category="failed", note="no .sc (run tools/convert/convert.py)")
        return row
    ev, err = sc_eval(sc, timeout)
    if ev is None:
        row.update(category="failed", note=err)
        return row
    row["timeline_errors"] = sum(1 for t in ev.get("timeline", []) if t.get("error"))
    row["bodies_sc"] = ev.get("body_count", len(ev.get("bodies", [])))

    if not manifest:
        row["note"] = "no manifest.json: geometry not checked"
        return row

    t = manifest["totals"]
    row["bodies_fusion"] = t["solid_bodies"]
    vol_sc = sum(b.get("volume_mm3", 0.0) for b in ev.get("bodies", []))
    refs = [v for v in (t.get("volume_mm3_visible_solids"), t.get("volume_mm3_all_solids")) if v]
    if refs:
        rel = min(abs(vol_sc - v) / v for v in refs)
        row["volume_rel_err"] = "%.5f" % rel
        row["volume_ok"] = rel <= tol
    else:
        row["volume_ok"] = vol_sc == 0
    fb = t.get("bbox_mm")
    boxes = [b["bbox"] for b in ev.get("bodies", []) if b.get("bbox")]
    if fb and boxes:
        lo = [min(b["min"][i] for b in boxes) for i in range(3)]
        hi = [max(b["max"][i] for b in boxes) for i in range(3)]
        row["bbox_max_diff_mm"] = "%.3f" % max(
            max(abs(lo[i] - fb["min"][i]), abs(hi[i] - fb["max"][i])) for i in range(3))

    sc_params = {p["name"]: p for p in ev.get("params", []) if not p.get("model")}
    fparams = manifest.get("parameters", [])
    row["params_fusion"] = len(fparams)
    missing = [p["name"] for p in fparams if p["name"] not in sc_params]
    changed = [p["name"] for p in fparams if p["name"] in sc_params
               and norm_expr(p["expression"]) != norm_expr(sc_params[p["name"]].get("expression"))]
    row["params_missing"] = " ".join(missing)
    row["params_changed"] = " ".join(changed)
    notes = []
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
    counts = {c: sum(1 for r in rows if r["category"] == c) for c in ("full", "partial", "geometry", "failed")}
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
    ap.add_argument("--only", help="only designs whose path contains this text")
    ap.add_argument("--tolerance", type=float, default=0.001, help="relative volume tolerance (default 0.1%%)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per solidcraft-cli eval")
    ap.add_argument("--out", help="report folder (default: the workspace's reports_dir)")
    args = ap.parse_args()

    ws = common.find_workspace(args.workspace)
    out = args.out or ws.reports
    versions = common.check_pins(ws, strict=False)
    rows = []
    for d, archive in common.design_dirs(ws.designs, args.only):
        r = score(ws.designs, d, archive, args.tolerance, args.timeout)
        print("%-9s %s%s" % (r["category"], r["design"], ("  [" + r["note"] + "]") if r.get("note") else ""), flush=True)
        rows.append(r)
    if not rows:
        print("no designs under %s" % ws.designs)
        return 1
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "scorecard.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    counts, flagged = write_md(rows, os.path.join(out, "scorecard.md"), versions)
    print("\n" + ", ".join("%s %d" % kv for kv in counts.items()) + "; flagged %d" % flagged)
    print("wrote %s" % os.path.join(out, "scorecard.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
