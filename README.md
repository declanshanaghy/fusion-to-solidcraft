# fusion-to-solidcraft

Move your Autodesk Fusion designs into [SolidCraft](https://github.com/tangency-labs/solidcraft)
with as much of their parametric history as SolidCraft can replay, and check that
nothing was lost.

This repo holds only the **tools**. Your designs live in a separate **workspace**: any
folder or repo with a `fusion-to-solidcraft.json` at its root. The tools read their
settings from that file and write only into the workspace. That keeps the tools generic
and public, and your designs private.

```mermaid
flowchart LR
  subgraph Fusion
    F[Hub → projects → folders]
  end
  subgraph tools["fusion-to-solidcraft (this repo)"]
    E[fusion/ExportAll]
    C[convert/convert.py]
    V[verify/check.py]
  end
  subgraph ws["your designs workspace"]
    M[fusion-to-solidcraft.json]
    D["designs/&lt;hub&gt;/…/&lt;design&gt;/<br/>.f3d · .step · manifest.json · .sc · reports"]
    R[reports/scorecard.md]
  end
  F --> E --> D
  M -.settings.-> E & C & V
  D --> C --> D
  D --> V --> R
```

## Requirements

- Autodesk Fusion (for the export only)
- [Dutchman](https://github.com/dutchman-cad/dutchman), which does the Fusion → SolidCraft conversion: `brew install dutchman-cad/tap/dutchman`
- `solidcraft-cli`, which Dutchman uses to check its output and `verify` uses to measure it
- Python 3.9+ (stdlib only)

## Set up a workspace

Create a `fusion-to-solidcraft.json` at the root of your designs repo:

```json
{
  "hub": "My Hub",
  "designs_dir": "designs",
  "reports_dir": "reports",
  "export": { "projects": [], "all_versions": false, "export_step": true },
  "versions": { "dutchman": "0.2.7", "solidcraft-cli": "0.30.1" }
}
```

| Key | Meaning |
|---|---|
| `hub` | The default Fusion hub. The export lists every hub on your account and pre-fills this one. |
| `designs_dir`, `reports_dir` | Where designs and reports go, relative to the workspace. |
| `export.projects` | Limits the export to these project names (empty means every project). |
| `export.all_versions` | Also exports every past Fusion version into each design's `versions/` folder. Much slower. |
| `versions` | The tool versions the workspace's `.sc` files were made with. `convert` and `verify` warn when the installed tools differ. |

If the workspace is a git repo, track the CAD binaries with Git LFS (`*.f3d *.f3z *.sc *.step`).

## Run it

1. **Export.** Run `make install-fusion` once. In Fusion, go to *Utilities → Scripts and
   Add-Ins → ExportAll → Run*. The first run asks for the workspace path and pre-fills it
   with a sibling folder that has the marker file. It then asks which hub to export. Both
   answers are remembered in `fusion/ExportAll/export_config.local.json` (untracked), which
   is also where one-off overrides go, such as `"limit": 5` for a trial run. Re-running is safe:
   a design whose Fusion version hasn't changed is skipped. Each design gets its archive, a
   STEP copy, and a `manifest.json` that records its user parameters, timeline, sketches,
   bodies, volumes and bounding box. The manifest is the ground truth for verification.
2. **Convert.** Run `convert/convert.py` from the workspace (or pass `--workspace`, or
   set `$F2S_WORKSPACE`). It writes `<design>.sc` beside each archive, plus Dutchman's
   reports: `.dutchman.json` for the machine and `.dutchman.txt` with every loss explained.
   A design keeps editable history up to the first feature SolidCraft can't replay yet.
   After that, the final shape is imported. `--force` re-converts after a tool upgrade.
3. **Verify.** `verify/check.py` measures each `.sc` with `solidcraft-cli eval` and compares
   it with the manifest: volume (within 0.1%), body count, bounding box, and parameters with
   their expressions. It writes `scorecard.csv` and `scorecard.md`, which group designs into
   full, partial, geometry-only and failed, and name the first blocking feature. The
   scorecard records the tool versions and this repo's commit.

## Limits

Fusion's version history becomes archives, not SolidCraft history. T-splines (Form), mesh,
CAM, simulation and drawings aren't supported by SolidCraft yet. Appearances come across
as colours only. Coverage improves with each Dutchman and SolidCraft release, so re-convert
now and then.

## License

MIT
