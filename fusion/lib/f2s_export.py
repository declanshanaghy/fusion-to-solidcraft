"""Shared export code for the Fusion scripts in fusion/.

- "Export Hub to SolidCraft" (run_hub): every design in a hub, resumable.
- "Export Current Design to SolidCraft" (run_active): the design open in Fusion, always fresh,
  then convert and verify just that design and show the result.

Both export into a designs workspace, which is your designs repo, marked by
fusion-to-solidcraft.json at its root. Designs land in
<workspace>/<designs_dir>/<hub>/<project>/<folders...>/<design>/. Each one gets the Fusion
archive (.f3d, or .f3z when the design references other designs), a STEP copy, and
manifest.json, the ground truth that verify/check.py compares the SolidCraft design
against.

Settings are layered, later ones winning: export_config.json (the tool's defaults), the
workspace marker's "hub" and "export" block, then export_config.local.json beside this
file. That last one is untracked and holds the workspace path, the last hub chosen, and
one-off overrides such as "limit".

A design only fails when its archive can't be exported. Problems reading details for
the manifest are recorded in its "errors" list instead, because the archive, which is
what gets converted, is still good.
"""

import json
import os
import re
import shutil
import subprocess
import time
import traceback
from collections import Counter

import adsk.core
import adsk.fusion

HERE = os.path.dirname(os.path.realpath(__file__))  # realpath: Fusion loads scripts via symlinks
TOOLS = os.path.abspath(os.path.join(HERE, "..", ".."))
MARKER = "fusion-to-solidcraft.json"
LOCAL = os.path.join(HERE, "export_config.local.json")
MANIFEST_SCHEMA = "fusion-to-solidcraft.manifest/1"
TITLE = "Export to SolidCraft"


# ---------------------------------------------------------------- settings

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def save_local(updates):
    data = read_json(LOCAL, {}) or {}
    data.update(updates)
    write_json(LOCAL, data)


def is_workspace(path):
    return bool(path) and os.path.isfile(os.path.join(path, MARKER))


def guess_workspace():
    """A sibling of this tools repo that holds the marker, if there is one."""
    parent = os.path.dirname(TOOLS)
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return ""
    for name in names:
        path = os.path.join(parent, name)
        if path != TOOLS and is_workspace(path):
            return path
    return ""


def choose_workspace(ui):
    """Return the designs workspace, asking (and remembering) when it isn't known yet."""
    saved = (read_json(LOCAL, {}) or {}).get("workspace")
    if is_workspace(saved):
        return saved
    base = "Path to your designs repo (the folder holding %s):" % MARKER
    prompt, default = base, saved or guess_workspace() or os.path.expanduser("~")
    while True:
        value, cancelled = ui.inputBox(prompt, TITLE, default)
        if cancelled:
            return None
        path = os.path.abspath(os.path.expanduser(value.strip()))
        if is_workspace(path):
            save_local({"workspace": path})
            return path
        prompt, default = "No %s in %s.\n\n%s" % (MARKER, path, base), path


def load_config(ws):
    marker = read_json(os.path.join(ws, MARKER), {}) or {}
    cfg = read_json(os.path.join(HERE, "export_config.json"), {}) or {}
    cfg.update(marker.get("export", {}))
    if marker.get("hub"):
        cfg["hub"] = marker["hub"]
    cfg.update({k: v for k, v in (read_json(LOCAL, {}) or {}).items() if k != "workspace"})
    cfg["workspace"] = ws
    cfg["designs_dir"] = os.path.join(ws, marker.get("designs_dir", "designs"))
    cfg["reports_dir"] = os.path.join(ws, marker.get("reports_dir", "reports"))
    return cfg


def safe(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().rstrip(".")
    return name or "_"


class Log:
    def __init__(self, path, title):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "a", encoding="utf-8")
        self.write("---- %s started" % title)

    def write(self, msg):
        self.f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- discovery

def choose_hub(app, ui, preferred):
    """List the account's hubs and ask which to export; None if cancelled.

    The prompt is prefilled with the configured hub (or the first hub), and the choice
    is saved to export_config.local.json so the next run defaults to it.
    """
    hubs = [app.data.dataHubs.item(i) for i in range(app.data.dataHubs.count)]
    if not hubs:
        ui.messageBox("No Fusion hubs found for this account.")
        return None
    names = [h.name for h in hubs]
    default = names.index(preferred) + 1 if preferred in names else 1
    listing = "\n".join("%d. %s" % (i + 1, n) for i, n in enumerate(names))
    prompt = "Hubs on this account:\n\n%s\n\nEnter the number (or exact name) of the hub to export:" % listing
    while True:
        value, cancelled = ui.inputBox(prompt, TITLE, str(default))
        if cancelled:
            return None
        value = value.strip()
        if value.isdigit() and 1 <= int(value) <= len(hubs):
            hub = hubs[int(value) - 1]
            break
        if value in names:
            hub = hubs[names.index(value)]
            break
        prompt = "'%s' is not one of the hubs below.\n\n%s\n\nEnter a number or exact name:" % (value, listing)
    save_local({"hub": hub.name})
    return hub


def collect(folder, rel, out):
    """Append (DataFile, relative path parts) for every design under folder."""
    files = folder.dataFiles
    seen = Counter()
    for i in range(files.count):
        df = files.item(i)
        if df.fileExtension != "f3d":
            continue  # drawings (.f2d), CAM-only files, uploads: not designs
        base = safe(df.name)
        seen[base] += 1
        leaf = base if seen[base] == 1 else "%s (%s)" % (base, df.id[-8:])
        out.append((df, rel + [leaf]))
        adsk.doEvents()
    subs = folder.dataFolders
    for i in range(subs.count):
        sub = subs.item(i)
        collect(sub, rel + [safe(sub.name)], out)


def rel_parts(df):
    """[project, folders..., design] for one DataFile, the same layout the hub export uses."""
    folders = []
    folder = df.parentFolder
    while folder and folder.parentFolder and not folder.isRoot:
        folders.insert(0, safe(folder.name))
        folder = folder.parentFolder
    return [safe(df.parentProject.name)] + folders + [safe(df.name)]


# ---------------------------------------------------------------- manifest

def size(coll):
    """Item count of a Fusion collection (.count) or vector (a Python list)."""
    if isinstance(coll, (list, tuple)):
        return len(coll)
    return coll.count if hasattr(coll, "count") else len(coll)


def attempt(errors, what, fn, default=None):
    """Run fn(); on an API error record it in the manifest instead of failing the design."""
    try:
        return fn()
    except Exception as e:
        errors.append("%s: %s" % (what, e))
        return default


def fmt_value(um, p):
    try:
        return float(um.formatInternalValue(p.value, p.unit, False)) if p.unit else p.value
    except Exception:
        return p.value


def bbox_mm(boxes):
    if not boxes:
        return None
    lo = [min(b.minPoint.x for b in boxes), min(b.minPoint.y for b in boxes), min(b.minPoint.z for b in boxes)]
    hi = [max(b.maxPoint.x for b in boxes), max(b.maxPoint.y for b in boxes), max(b.maxPoint.z for b in boxes)]
    return {"min": [v * 10 for v in lo], "max": [v * 10 for v in hi]}


def read_parameters(design):
    um = design.unitsManager
    out = []
    for i in range(design.userParameters.count):
        p = design.userParameters.item(i)
        out.append({"name": p.name, "expression": p.expression, "unit": p.unit,
                    "value": fmt_value(um, p), "comment": p.comment})
    return out


def read_bodies(root):
    """Bodies in world space: root bodies plus every occurrence's body proxies."""
    bodies, boxes = [], []
    sources = [(root.name, root.bRepBodies)]
    for i in range(root.allOccurrences.count):
        occ = root.allOccurrences.item(i)
        sources.append((occ.fullPathName, occ.bRepBodies))
    for owner, coll in sources:
        for j in range(coll.count):
            b = coll.item(j)
            entry = {"name": b.name, "owner": owner, "solid": b.isSolid, "visible": b.isVisible}
            try:
                entry["volume_mm3"] = b.volume * 1000.0
                entry["area_mm2"] = b.area * 100.0
            except Exception:
                pass
            bodies.append(entry)
            if b.isSolid:
                boxes.append(b.boundingBox)
    return bodies, bbox_mm(boxes)


def read_sketches(design):
    out = []
    for i in range(design.allComponents.count):
        comp = design.allComponents.item(i)
        for j in range(comp.sketches.count):
            sk = comp.sketches.item(j)
            out.append({"name": sk.name, "component": comp.name,
                        "curves": sk.sketchCurves.count,
                        "constraints": sk.geometricConstraints.count,
                        "dimensions": sk.sketchDimensions.count,
                        "fully_constrained": sk.isFullyConstrained})
    return out


def read_timeline(design):
    out = []
    tl = design.timeline
    for i in range(tl.count):
        item = tl.item(i)
        kind = "Group" if item.isGroup else "Unknown"
        try:
            if not item.isGroup and item.entity:
                kind = item.entity.objectType.split("::")[-1]
        except Exception:
            pass
        out.append({"index": i, "name": item.name, "type": kind,
                    "suppressed": item.isSuppressed, "health": int(item.healthState)})
    return out


def build_manifest(df, design, archive_name):
    errors = []
    root = design.rootComponent
    parametric = attempt(errors, "design type",
                         lambda: design.designType == adsk.fusion.DesignTypes.ParametricDesignType, False)
    # Direct-modelling designs have no user parameters or timeline; Fusion raises if asked.
    params = attempt(errors, "parameters", lambda: read_parameters(design), []) if parametric else []
    timeline = attempt(errors, "timeline", lambda: read_timeline(design), []) if parametric else []
    bodies, bbox = attempt(errors, "bodies", lambda: read_bodies(root), ([], None))
    sketches = attempt(errors, "sketches", lambda: read_sketches(design), [])
    solids = [b for b in bodies if b.get("solid") and "volume_mm3" in b]
    return {
        "schema": MANIFEST_SCHEMA,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fusion": {"id": df.id, "name": df.name, "version": df.versionNumber,
                   "archive": archive_name, "parametric": parametric},
        "parameters": params,
        "totals": {
            "bodies": len(bodies),
            "solid_bodies": len(solids),
            "volume_mm3_all_solids": sum(b["volume_mm3"] for b in solids),
            "volume_mm3_visible_solids": sum(b["volume_mm3"] for b in solids if b["visible"]),
            "bbox_mm": bbox,
            "components": attempt(errors, "components", lambda: size(design.allComponents)),
            "occurrences": attempt(errors, "occurrences", lambda: size(root.allOccurrences)),
            "joints": attempt(errors, "joints", lambda: size(root.allJoints) + size(root.allAsBuiltJoints)),
            "sketches": len(sketches),
            "timeline_items": len(timeline),
            "feature_kinds": dict(Counter(t["type"] for t in timeline)),
        },
        "bodies": bodies,
        "sketches": sketches,
        "timeline": timeline,
        "errors": errors,
    }


def write_manifest(out_dir, df, design, archive, reused):
    """Write manifest.json; never raises, since the archive is already safe on disk."""
    try:
        manifest = build_manifest(df, design, os.path.basename(archive))
        manifest["fusion"]["archive_reused"] = reused
    except Exception:
        manifest = {"schema": MANIFEST_SCHEMA,
                    "fusion": {"id": df.id, "name": df.name, "version": df.versionNumber,
                               "archive": os.path.basename(archive)},
                    "totals": {}, "parameters": [],
                    "errors": ["manifest: " + traceback.format_exc()]}
    write_json(os.path.join(out_dir, "manifest.json"), manifest)
    return manifest


# ---------------------------------------------------------------- export

def open_doc(app, df):
    """Return (doc, opened_by_us). Reuses a document the user already has open."""
    for i in range(app.documents.count):
        d = app.documents.item(i)
        try:
            if d.dataFile and d.dataFile.id == df.id and d.dataFile.versionNumber == df.versionNumber:
                return d, False
        except Exception:
            pass
    return app.documents.open(df, False), True


def reusable_archive(out_dir, stem, df):
    """An archive from an earlier run that is at least as new as the design in Fusion.

    Lets a run whose manifests failed be finished without exporting everything again:
    the design is only opened to write the missing manifest (and STEP).
    """
    for ext in ("f3d", "f3z"):
        path = os.path.join(out_dir, "%s.%s" % (stem, ext))
        if os.path.exists(path):
            try:
                if os.path.getmtime(path) < df.dateModified:
                    return None  # the design changed in Fusion since this archive
            except Exception:
                pass
            return path
    return None


def export_doc(doc, df, out_dir, stem, cfg, with_manifest=True, reuse=None):
    """Export an open design. Returns (how, manifest): how is "exported" or "manifest"."""
    design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
    if not design:
        raise RuntimeError("no Design product")
    em = design.exportManager
    if reuse:
        archive = reuse
    else:
        ext = "f3z" if doc.documentReferences.count > 0 else "f3d"
        archive = os.path.join(out_dir, "%s.%s" % (stem, ext))
        if not em.execute(em.createFusionArchiveExportOptions(archive)):
            raise RuntimeError("Fusion archive export failed")
    manifest = None
    if with_manifest:
        step = os.path.join(out_dir, stem + ".step")
        if cfg.get("export_step", True) and not (reuse and os.path.exists(step)):
            try:
                em.execute(em.createSTEPExportOptions(step))
            except Exception:
                pass  # noted by verify (no STEP); the archive is what converts
        manifest = write_manifest(out_dir, df, design, archive, bool(reuse))
    return ("manifest" if reuse else "exported"), manifest


def export_file(app, df, out_dir, stem, cfg, with_manifest=True, allow_reuse=True):
    reuse = reusable_archive(out_dir, stem, df) if (with_manifest and allow_reuse) else None
    doc, ours = open_doc(app, df)
    try:
        return export_doc(doc, df, out_dir, stem, cfg, with_manifest, reuse)
    finally:
        if ours:
            doc.close(False)


def export_versions(app, df, out_dir, stem, cfg):
    vdir = os.path.join(out_dir, "versions")
    os.makedirs(vdir, exist_ok=True)
    versions = df.versions
    for v in range(versions.count):
        vf = versions.item(v)
        if vf.versionNumber == df.versionNumber:
            continue
        vstem = "%s.v%d" % (stem, vf.versionNumber)
        if any(os.path.exists(os.path.join(vdir, vstem + e)) for e in (".f3d", ".f3z")):
            continue
        export_file(app, vf, vdir, vstem, cfg, with_manifest=False)
        adsk.doEvents()


def already_done(out_dir, df):
    m = (read_json(os.path.join(out_dir, "manifest.json"), {}) or {}).get("fusion", {})
    return m.get("id") == df.id and m.get("version") == df.versionNumber


# ---------------------------------------------------------------- scripts

def run_hub():
    """Export Hub to SolidCraft: every design in a chosen hub, resumable."""
    app = adsk.core.Application.get()
    ui = app.userInterface
    ws = choose_workspace(ui)
    if not ws:
        return
    cfg = load_config(ws)
    log = Log(os.path.join(cfg["reports_dir"], "export.log"), "Export Hub to SolidCraft")
    summary = {"exported": [], "manifest_only": [], "skipped": [], "manifest_notes": [], "failed": []}
    progress = None
    try:
        hub = choose_hub(app, ui, cfg.get("hub"))
        if not hub:
            log.write("no hub chosen; nothing exported")
            return
        app.data.activeHub = hub
        hub_dir = os.path.join(cfg["designs_dir"], safe(hub.name))
        log.write("hub %s -> %s" % (hub.name, hub_dir))

        wanted = set(cfg.get("projects") or [])
        jobs = []
        projects = hub.dataProjects
        for i in range(projects.count):
            proj = projects.item(i)
            if not wanted or proj.name in wanted:
                collect(proj.rootFolder, [safe(proj.name)], jobs)
        limit = int(cfg.get("limit") or 0)
        if limit:
            jobs = jobs[:limit]
        log.write("%d designs found" % len(jobs))

        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        progress.show(TITLE, "%v of %m designs", 0, max(len(jobs), 1))
        for n, (df, rel) in enumerate(jobs):
            if progress.wasCancelled:
                log.write("cancelled by user")
                break
            progress.progressValue = n
            progress.message = "%d of %d: %s" % (n + 1, len(jobs), "/".join(rel))
            adsk.doEvents()
            out_dir = os.path.join(hub_dir, *rel)
            label = "/".join(rel)
            if already_done(out_dir, df):
                summary["skipped"].append(label)
                continue
            os.makedirs(out_dir, exist_ok=True)
            try:
                if cfg.get("all_versions"):
                    export_versions(app, df, out_dir, rel[-1], cfg)
                how, manifest = export_file(app, df, out_dir, rel[-1], cfg)
                summary["exported" if how == "exported" else "manifest_only"].append(label)
                notes = (manifest or {}).get("errors") or []
                if notes:
                    summary["manifest_notes"].append({"design": label, "notes": notes})
                log.write("ok   %s v%d%s%s" % (label, df.versionNumber,
                                               " (archive reused)" if how == "manifest" else "",
                                               " [manifest notes: %s]" % "; ".join(notes) if notes else ""))
            except Exception:
                summary["failed"].append({"design": label, "error": traceback.format_exc()})
                log.write("FAIL %s\n%s" % (label, traceback.format_exc()))
            adsk.doEvents()

        write_json(os.path.join(cfg["reports_dir"], "export-summary.json"), summary)
        progress.hide()
        progress = None
        ui.messageBox(
            "Export finished.\n\nExported: %d\nArchive reused, manifest written: %d\nAlready up to date: %d\n"
            "Failed (no archive): %d\n\nManifests with notes: %d (details in export-summary.json)\nLog: %s"
            % (len(summary["exported"]), len(summary["manifest_only"]), len(summary["skipped"]),
               len(summary["failed"]), len(summary["manifest_notes"]),
               os.path.join(cfg["reports_dir"], "export.log")), TITLE)
    except Exception:
        log.write("FATAL\n" + traceback.format_exc())
        ui.messageBox("Export failed:\n" + traceback.format_exc(), TITLE)
    finally:
        if progress:
            progress.hide()
        log.close()


def tool_env():
    """Environment for running the tools' Python and CLIs from inside Fusion.

    Fusion sets PYTHONHOME/PYTHONPATH for its embedded interpreter. A system python3
    that inherits them can't find its own standard library ("No module named
    'encodings'"), so those are dropped. PATH gains Homebrew's folders, which
    Fusion's environment often lacks.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON") and k != "__PYVENV_LAUNCHER__"}
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    env["PATH"] = os.pathsep.join([p for p in env.get("PATH", "").split(os.pathsep) if p] + extra)
    return env


def run_tool(progress, label, args, env, log):
    """Run a tools-repo command, keeping Fusion responsive; returns (rc, output)."""
    progress.message = label
    adsk.doEvents()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    while proc.poll() is None:
        adsk.doEvents()
        time.sleep(0.2)
    out = proc.stdout.read()
    log.write("$ %s (exit %d)\n%s" % (" ".join(args), proc.returncode, out.rstrip()))
    return proc.returncode, out


def scorecard_row(cfg, design_rel):
    import csv
    path = os.path.join(cfg["reports_dir"], "scorecard.csv")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("design") == design_rel:
                    return row
    except OSError:
        pass
    return None


def run_active():
    """Export Current Design to SolidCraft: the open design, always re-exported, then convert and verify it."""
    app = adsk.core.Application.get()
    ui = app.userInterface
    doc = app.activeDocument
    df = doc.dataFile if doc else None
    if not df:
        ui.messageBox("Open a saved design first: this exports the active document.", TITLE)
        return
    ws = choose_workspace(ui)
    if not ws:
        return
    cfg = load_config(ws)
    log = Log(os.path.join(cfg["reports_dir"], "export-current.log"), "Export Current Design to SolidCraft")
    progress = ui.createProgressDialog()
    try:
        rel = [safe(df.parentProject.parentHub.name)] + rel_parts(df)
        out_dir = os.path.join(cfg["designs_dir"], *rel)
        design_rel = "/".join(rel)
        os.makedirs(out_dir, exist_ok=True)
        progress.show(TITLE, "Exporting %s" % design_rel, 0, 1)
        adsk.doEvents()
        _, manifest = export_doc(doc, df, out_dir, rel[-1], cfg)
        log.write("exported %s v%d" % (design_rel, df.versionNumber))
        lines = ["Exported %s (v%d)" % (design_rel, df.versionNumber)]
        notes = manifest.get("errors") or []
        if notes:
            lines.append("Manifest notes: " + "; ".join(notes))

        if cfg.get("convert_after", True):
            env = tool_env()
            py = shutil.which("python3", path=env["PATH"])
            if not py:
                raise RuntimeError("python3 not found on PATH; cannot run convert/verify")
            common = ["--workspace", ws, "--design", out_dir]
            py = [py, "-E"]  # -E: ignore any PYTHON* variables, belt and braces with tool_env()
            rc, out = run_tool(progress, "Converting with Dutchman...",
                               py + [os.path.join(TOOLS, "convert", "convert.py"), "--force"] + common, env, log)
            if rc != 0:
                lines.append("Convert failed:\n" + "\n".join(out.strip().splitlines()[-5:]))
            rc, out = run_tool(progress, "Verifying...",
                               py + [os.path.join(TOOLS, "verify", "check.py")] + common, env, log)
            row = scorecard_row(cfg, design_rel)
            if row:
                lines += ["", "Category: %s (%s of %s features mapped)" % (row["category"], row["mapped"], row["total"]),
                          "Volume match: %s (relative error %s)" % (row["volume_ok"], row["volume_rel_err"]),
                          "Bodies: Fusion %s, SolidCraft %s" % (row["bodies_fusion"], row["bodies_sc"])]
                if row.get("first_blocker"):
                    lines.append("First blocker: " + row["first_blocker"])
                if row.get("params_missing"):
                    lines.append("Parameters missing: " + row["params_missing"])
                if row.get("note"):
                    lines.append("Note: " + row["note"])
            else:
                lines.append("Verify produced no result:\n" + "\n".join(out.strip().splitlines()[-5:]))
        lines += ["", "Folder: " + out_dir]
        progress.hide()
        ui.messageBox("\n".join(lines), TITLE)
    except Exception:
        log.write("FAIL\n" + traceback.format_exc())
        progress.hide()
        ui.messageBox("Export failed:\n" + traceback.format_exc(), TITLE)
    finally:
        log.close()
