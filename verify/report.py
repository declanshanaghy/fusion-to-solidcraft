#!/usr/bin/env python3
"""Write reports/conversion-report.md: the whole migration in one readable page.

It reads what convert and verify already wrote (scorecard.csv, each design's
<design>.dutchman.json and manifest.json), so run it after verify:
  - how many designs kept full, partial or no editable history
  - which Fusion features stop the history, most common first
  - what happened to the user parameters (kept, restored by convert, still missing)
  - every shape difference, grouped by its cause, with what to do about it
  - one row per design
"""

import argparse
import csv
import datetime
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import common  # noqa: E402
from check import CATEGORIES  # noqa: E402

ICON = {"full": "✅", "partial": "🟡", "geometry": "🟠", "mesh": "🔷", "empty": "⚪", "failed": "❌"}
MEANING = {
    "full": "The SolidCraft history rebuilds the Fusion shape on its own: edit any feature",
    "partial": "Editable history up to a feature SolidCraft can't replay, then the exact final shape",
    "geometry": "The exact shape, not editable as history (sketches may be kept beside it)",
    "mesh": "Mesh bodies, brought over from STL",
    "empty": "Nothing to convert: no bodies or meshes in Fusion",
    "failed": "No usable .sc",
}

# Shape differences, matched against verify's notes, most specific first.
CAUSES = [
    ("hidden", re.compile(r"hidden in Fusion came in visible"),
     "Hidden bodies came in visible",
     "Fusion bodies that were hidden (old versions, construction copies, alternates) are in the "
     ".sc as ordinary visible bodies, so the total volume is higher than Fusion's visible shape.",
     "Hide or delete the extra bodies in SolidCraft. Nothing is lost."),
    ("refs", re.compile(r"referenced designs .* missing"),
     "Referenced designs left out",
     "The design inserts other Fusion designs. Fusion's archive doesn't carry their geometry, so "
     "those parts are missing from the .sc.",
     "Insert the referenced design's own .sc in SolidCraft, or convert this design from its STEP "
     "(shape only)."),
    ("open", re.compile(r"open bodies"),
     "Not watertight",
     "At least one body came in as an open shell, so SolidCraft can't give it a volume. The "
     "imported shape has a gap.",
     "Re-check after the next Dutchman release. The Fusion STEP has the closed body if it's needed now."),
    ("merged", re.compile(r"bodies merged or split"),
     "Bodies merged (volume matches)",
     "Several Fusion bodies became one SolidCraft body, or the reverse. The material is all there.",
     "Split them again in SolidCraft only if you print or move them separately."),
    ("volume", re.compile(r"volume differs|body count differs"),
     "Shape differs",
     "The volume or body count doesn't match Fusion and none of the causes above explains it. "
     "Treat it as a real conversion error until checked by eye.",
     "Open both and compare. Keep the Fusion archive and STEP as the reference."),
]


def classify(note):
    for key, pattern, *_ in CAUSES:
        if pattern.search(note or ""):
            return key
    return None


def simplify(reason):
    """A blocker's reason with names and numbers taken out, so like reasons group together."""
    r = re.sub(r'"[^"]*"|`[^`]*`', "…", reason or "")
    return re.sub(r"\d+", "N", r).strip()


def short(design):
    return design.rsplit("/", 1)[-1]


def esc(v):
    return str(v if v is not None else "").replace("|", "\\|")


def load(ws, rows):
    """Each row plus its Dutchman report and manifest (when there is one)."""
    out = []
    for r in rows:
        d = os.path.join(ws.designs, r["design"])
        reports = [f for f in os.listdir(d) if f.endswith(".dutchman.json")] if os.path.isdir(d) else []
        report = common.load_json(os.path.join(d, reports[0])) if reports else {}
        mpath = os.path.join(d, "manifest.json")
        manifest = common.load_json(mpath) if os.path.exists(mpath) else {}
        out.append((r, report, manifest))
    return out


def build(ws, rows, versions):
    data = load(ws, rows)
    counts = Counter(r["category"] for r in rows)
    n = len(rows)
    L = ["# Conversion report", "",
         "Every Fusion design in the workspace, converted to SolidCraft and measured against "
         "Fusion's own numbers.", "",
         "- **Generated:** %s" % datetime.date.today().isoformat(),
         "- **Tools:** " + ", ".join("%s %s" % kv for kv in versions.items())
         + ", fusion-to-solidcraft " + common.tools_rev(),
         "- **Detail:** `scorecard.md` / `scorecard.csv` beside this file, and each design's "
         "`<design>.dutchman.txt` for every loss", ""]

    # At a glance
    L += ["## At a glance", "", "```mermaid", "pie showData title %d designs" % n]
    L += ['  "%s" : %d' % (c, counts[c]) for c in CATEGORIES if counts[c]]
    L += ["```", "", "| | Category | Designs | Meaning |", "|---|---|---|---|"]
    L += ["| %s | %s | %d | %s |" % (ICON[c], c, counts[c], MEANING[c]) for c in CATEGORIES]
    L.append("")

    # Headline numbers
    hist = [(r, rep) for r, rep, _ in data if r["category"] in ("full", "partial")]
    mapped = sum(int(r["mapped"] or 0) for r, _ in hist)
    total = sum(int(r["total"] or 0) for r, _ in hist)
    all_total = sum(int(r["total"] or 0) for r, _, _ in data if r["total"])
    checked = [r for r in rows if r["volume_ok"] in ("True", "False")]
    shape_ok = sum(1 for r in checked if r["volume_ok"] == "True")
    fparams = sum(len(m.get("parameters") or []) for _, _, m in data)
    restored = sum(len(((rep.get("f2s") or {}).get("params") or {}).get("added") or []) for _, rep, _ in data)
    unrestored = Counter()
    for _, rep, _ in data:
        for name, why in (((rep.get("f2s") or {}).get("params") or {}).get("skipped") or {}).items():
            unrestored[why] += 1
    missing = sum(len(r["params_missing"].split()) for r in rows)
    changed = sum(len(r["params_changed"].split()) for r in rows)
    L += ["## What came across", "",
          "| | Result |", "|---|---|",
          "| Editable timeline | %d of %d Fusion timeline features (%d%%) are editable SolidCraft history, in "
          "the %d full and partial designs. Across all designs: %d of %d |"
          % (mapped, total, round(100.0 * mapped / total) if total else 0, len(hist), mapped, all_total),
          "| Shape | %d of %d measured designs match Fusion's volume within 0.1%% "
          "(mesh and empty designs have no volume to compare) |" % (shape_ok, len(checked)),
          "| User parameters | %d in Fusion: %d kept by Dutchman, %d restored from the Fusion manifest, "
          "%d still missing, %d with a different expression |"
          % (fparams, fparams - restored - missing, restored, missing, changed), ""]
    if restored:
        L += ["- **Restored parameters** are user parameters only: they're in the parameter table with "
              "Fusion's expressions, but they don't drive the imported part of a design. Features "
              "rebuilt as history use them where Dutchman wired them up.", ""]
    if unrestored:
        L += ["- **Not restorable:** " + "; ".join("%s (%d)" % (w, c) for w, c in unrestored.most_common()), ""]

    # Blockers
    first = Counter()
    examples = defaultdict(list)
    for r, rep, _ in data:
        feats = (rep.get("history") or {}).get("features") or []
        blocked = [f for f in feats if f.get("status") == "blocked"
                   and not str(f.get("reason", "")).startswith("after the first blocking")]
        if blocked and r["category"] in ("partial", "geometry"):
            key = (blocked[0].get("kind") or "?", simplify(blocked[0].get("reason")))
            first[key] += 1
            examples[key].append(short(r["design"]))
    if first:
        L += ["## What stops the editable history", "",
              "The first feature in each design that SolidCraft can't replay yet. Everything after it "
              "comes in as the imported final shape, so each fix upstream unlocks whole timelines.", "",
              "| Designs | Feature | Why | For example |", "|---|---|---|---|"]
        for (kind, why), c in first.most_common():
            L.append("| %d | %s | %s | %s |" % (c, esc(kind), esc(why), esc(", ".join(examples[(kind, why)][:3]))))
        L.append("")

    # Shape differences by cause
    by_cause = defaultdict(list)
    for r in rows:
        key = classify(r["note"])
        if key:
            by_cause[key].append(r)
    if by_cause:
        L += ["## Shape differences, by cause", "",
              "```mermaid", "flowchart LR",
              '  A["%d designs measured"] --> OK["%d match Fusion"]' % (len(checked), shape_ok)]
        for key, _, title, *_ in CAUSES:
            if by_cause.get(key):
                L.append('  A --> %s["%s: %d"]' % (key, title, len(by_cause[key])))
        L += ["```", ""]
        for key, _, title, what, todo in CAUSES:
            if not by_cause.get(key):
                continue
            L += ["### %s (%d)" % (title, len(by_cause[key])), "",
                  "- **What:** " + what, "- **What to do:** " + todo, "",
                  "| Design | Category | Volume error | Bodies (Fusion → .sc) |", "|---|---|---|---|"]
            for r in by_cause[key]:
                err = "%.1f%%" % (100 * float(r["volume_rel_err"])) if r["volume_rel_err"] else "n/a"
                L.append("| %s | %s | %s | %s → %s |" % (esc(short(r["design"])), r["category"], err,
                                                        r["bodies_fusion"], r["bodies_sc"]))
            L.append("")

    # Fallbacks
    fall = [(r, rep) for r, rep, _ in data if (rep.get("f2s") or {}).get("method", "archive") != "archive"]
    if fall:
        L += ["## Converted another way", "",
              "| Design | Method | Why |", "|---|---|---|"]
        for r, rep in fall:
            f2s = rep["f2s"]
            why = next((a.get("error") for a in f2s.get("attempts") or [] if a.get("error")), "") or ""
            L.append("| %s | %s | %s |" % (esc(short(r["design"])), f2s["method"], esc(why.split(";")[0][:110])))
        L.append("")

    # Next steps
    L += ["## What to do next", "",
          "- **Keep the Fusion archives and STEP files.** They're the reference for every design that "
          "isn't full, and the input for re-converting.",
          "- **Re-convert after each Dutchman or SolidCraft release:** upgrade, then `make reconvert verify "
          "report`. Each blocker fixed upstream turns partial and geometry designs into full ones.",
          "- **Report the top blockers upstream** (the table above), with a small design that shows each one.",
          "- **Check the designs under *Shape differs* by eye** before relying on their .sc.", ""]

    # Every design
    L += ["## Every design", "",
          "| Design | Category | History | Shape | Parameters | Notes |", "|---|---|---|---|---|---|"]
    for r, rep, m in sorted(data, key=lambda x: x[0]["design"].lower()):
        hist_cell = "%s/%s" % (r["mapped"], r["total"]) if r["total"] else ""
        shape = {"True": "✓", "False": "✗", "n/a": "n/a"}.get(r["volume_ok"], "")
        np_ = len(m.get("parameters") or [])
        params = ""
        if np_:
            miss = len(r["params_missing"].split())
            params = "%d/%d" % (np_ - miss, np_)
        note = "; ".join(x for x in (r["note"] or "").split("; ") if x and not x.startswith(("converted from", "mesh bodies only")))
        L.append("| %s | %s %s | %s | %s | %s | %s |" % (
            esc(r["design"]), ICON.get(r["category"], ""), r["category"], hist_cell, shape, params, esc(note[:140])))
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", help="designs repo (default: $%s, else cwd or a parent)" % common.ENV)
    args = ap.parse_args()
    ws = common.find_workspace(args.workspace)
    csv_path = os.path.join(ws.reports, "scorecard.csv")
    if not os.path.exists(csv_path):
        print("no %s: run verify first" % csv_path)
        return 1
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    versions = common.check_pins(ws, strict=False)
    out = os.path.join(ws.reports, "conversion-report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build(ws, rows, versions))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
