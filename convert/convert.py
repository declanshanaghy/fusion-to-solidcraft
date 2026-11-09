#!/usr/bin/env python3
"""Convert every exported Fusion design into a SolidCraft .sc beside it, via Dutchman.

For <design>.f3d|.f3z it writes <design>.sc, the machine report
<design>.dutchman.json and the full text report <design>.dutchman.txt.
Designs whose .sc is newer than the archive are skipped unless --force,
so a run after a tool upgrade should use --force.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import common  # noqa: E402


def up_to_date(archive, sc, report):
    return (os.path.exists(sc) and os.path.exists(report)
            and os.path.getmtime(sc) >= os.path.getmtime(archive))


def convert(archive, preset, timeout):
    d, s = os.path.dirname(archive), common.stem(archive)
    sc = os.path.join(d, s + ".sc")
    report = os.path.join(d, s + ".dutchman.json")
    cmd = ["dutchman", "convert", archive, sc, "--preset", preset, "--yes", "--quiet", "--json",
           "--log", os.path.join(d, s + ".dutchman.txt")]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", "timed out after %ss" % timeout, -1
    if out.strip().startswith("{"):
        with open(report + ".tmp", "w", encoding="utf-8") as f:
            f.write(out)
        os.replace(report + ".tmp", report)
    else:
        common.write_json(report, {"ok": False, "exit": rc, "stderr": err[-4000:]})
    return rc, time.time() - t0, err


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_workspace_arg(ap)
    ap.add_argument("--only", help="only designs whose path contains this text")
    ap.add_argument("--preset", default="full", choices=["fast", "standard", "full"])
    ap.add_argument("--force", action="store_true", help="re-convert even when the .sc is up to date")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds per design")
    ap.add_argument("--strict-versions", action="store_true", help="refuse to run off the pinned tool versions")
    args = ap.parse_args()

    ws = common.find_workspace(args.workspace)
    common.check_pins(ws, args.strict_versions)
    jobs = list(common.design_dirs(ws.designs, args.only))
    if not jobs:
        print("no Fusion archives under %s (run the Fusion ExportAll script first)" % ws.designs)
        return 1
    done = skipped = failed = 0
    for n, (d, archive) in enumerate(jobs, 1):
        rel = os.path.relpath(archive, ws.designs)
        s = common.stem(archive)
        if not args.force and up_to_date(archive, os.path.join(d, s + ".sc"), os.path.join(d, s + ".dutchman.json")):
            skipped += 1
            continue
        rc, secs, err = convert(archive, args.preset, args.timeout)
        status = "ok" if rc == 0 else "FAIL(%s)" % rc
        print("[%d/%d] %-8s %6.1fs  %s" % (n, len(jobs), status, secs, rel), flush=True)
        if rc == 0:
            done += 1
        else:
            failed += 1
            if err.strip():
                print("         " + err.strip().splitlines()[-1], file=sys.stderr)
    print("converted %d, up to date %d, failed %d" % (done, skipped, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
