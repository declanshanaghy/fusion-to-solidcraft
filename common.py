"""Shared helpers for the convert and verify tools (stdlib only).

The tools operate on a *workspace*: a separate repo (or folder) holding your designs,
marked by a fusion-to-solidcraft.json at its root. The marker names the Fusion hub,
where designs and reports live, and the tool versions the designs were converted
with. The tools never write into this repo.
"""

import json
import os
import re
import subprocess
import sys

MARKER = "fusion-to-solidcraft.json"
ENV = "F2S_WORKSPACE"
ARCHIVE_EXTS = (".f3d", ".f3z")


class Workspace:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.marker = os.path.join(self.root, MARKER)
        self.config = load_json(self.marker)
        self.designs = os.path.join(self.root, self.config.get("designs_dir", "designs"))
        self.reports = os.path.join(self.root, self.config.get("reports_dir", "reports"))
        self.versions = self.config.get("versions", {})


def find_workspace(explicit=None):
    """Resolve the workspace: --workspace, then $F2S_WORKSPACE, then cwd and its parents."""
    for candidate in (explicit, os.environ.get(ENV)):
        if candidate:
            if not os.path.exists(os.path.join(candidate, MARKER)):
                sys.exit("error: %s has no %s" % (candidate, MARKER))
            return Workspace(candidate)
    d = os.getcwd()
    while True:
        if os.path.exists(os.path.join(d, MARKER)):
            return Workspace(d)
        parent = os.path.dirname(d)
        if parent == d:
            sys.exit("error: no %s in this folder or above it; run from your designs repo, "
                     "pass --workspace, or set %s" % (MARKER, ENV))
        d = parent


def add_workspace_arg(ap):
    ap.add_argument("--workspace", help="designs repo holding %s (default: $%s, else cwd or a parent)"
                    % (MARKER, ENV))
    ap.add_argument("--only", help="only designs whose path contains this text")
    ap.add_argument("--design", help="only this one design folder (exact path)")


def selected(ws, args):
    """The (design_dir, archive) pairs a command's --only/--design select."""
    if args.design:
        d = os.path.abspath(args.design)
        return [(dd, a) for dd, a in design_dirs(d) if dd == d]
    return list(design_dirs(ws.designs, args.only))


def design_dirs(root, only=None):
    """Yield (design_dir, archive_path) for each exported Fusion design under root.

    A design directory is one holding a Fusion archive at its top level; archives
    under a versions/ folder are history copies, not designs of their own.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "versions" and not d.startswith("."))
        archives = sorted(f for f in filenames if f.lower().endswith(ARCHIVE_EXTS))
        if not archives:
            continue
        manifest = os.path.join(dirpath, "manifest.json")
        if os.path.exists(manifest):
            name = load_json(manifest).get("fusion", {}).get("archive")
            if name in archives:
                archives = [name]
        rel = os.path.relpath(dirpath, root)
        if only and only.lower() not in rel.lower():
            continue
        yield dirpath, os.path.join(dirpath, archives[0])


def nothing_in_fusion(manifest):
    """True when Fusion's manifest records no solids, surfaces or visible meshes."""
    t = manifest.get("totals") or {}
    bodies = manifest.get("bodies") or []
    meshes = [m for m in manifest.get("meshes") or [] if m.get("visible")]
    return not bodies and not meshes and not t.get("solid_bodies") and not t.get("bodies")


def stem(archive):
    """Design name of an archive: X.f3d -> X (and X.f3z.f3d, from older exports, -> X)."""
    name = os.path.splitext(os.path.basename(archive))[0]
    return name[:-4] if name.endswith(".f3z") else name


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def tool_version(cmd):
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+\S*", out)
    return m.group(0) if m else out.strip()


def tools_rev():
    """This tools repo's commit (with -dirty when modified), for reproducibility notes."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return subprocess.run(["git", "-C", here, "describe", "--always", "--dirty"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def check_pins(ws, strict):
    """Compare installed tools with the workspace's pinned versions; return what's installed."""
    found, bad = {}, []
    for cmd in ("dutchman", "solidcraft-cli"):
        have, want = tool_version(cmd), ws.versions.get(cmd)
        found[cmd] = have
        if have is None:
            bad.append("%s is not installed" % cmd)
        elif want and have != want:
            bad.append("%s is %s but the workspace pins %s (update \"versions\" in %s after a full "
                       "re-run)" % (cmd, have, want, MARKER))
    for msg in bad:
        print("warning: " + msg, file=sys.stderr)
    if bad and (strict or any("not installed" in m for m in bad)):
        sys.exit(2)
    return found
