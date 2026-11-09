# Operating principles (fusion-to-solidcraft ⇄ designs workspace)

This repo is the **public tools** half of a two-repo setup. The other half is a private
designs workspace (for me, `../3d-solid-designs`), identified by its `fusion-to-solidcraft.json`.

| Concern | Lives in | Rule |
|---|---|---|
| Export, convert and verify code; tool defaults | this repo | Keep it generic: no personal hub names, paths, design names or design data. It's public. |
| Designs, manifests, `.sc` files, reports | the workspace | The tools write only there, never into this repo. |
| Hub, folders, export options, pinned tool versions | the workspace's `fusion-to-solidcraft.json` | It's the single contract between the two repos. Add new settings there and document them in the README table. |
| Per-machine state (workspace path, last hub, one-off `limit`) | `fusion/lib/export_config.local.json` | Untracked. Never commit it. |

Working across the two:
- A bug found while exporting or converting gets fixed **here** first: commit, run `make check`, push. Then re-run from the workspace.
  The export resumes, and `convert` skips `.sc` files that are up to date (use `--force` after a tool change that affects output).
- A workspace commit that adds or refreshes converted designs comes after the tools commit it depends on, and its message
  names that tools revision (the scorecard header records it as `fusion-to-solidcraft <rev>`).
- Upgrading Dutchman or SolidCraft is a workspace change: `make reconvert verify` there, then update `versions`
  in its marker file in the same commit as the new `.sc` files.
- The Fusion scripts (`Export Hub to SolidCraft`, `Export to SolidCraft Test`) are thin wrappers over `fusion/lib/f2s_export.py`,
  loaded through symlinks (`make install-fusion`). They reload the module on every run, so edits take effect without restarting
  Fusion. Iterate on one design with the Test script: it exports the open design, converts and verifies it, and shows the result.
- A design fails only when its archive can't be exported. Manifest problems are recorded in the manifest's `errors` list.
  Any new Fusion API read must go through `attempt()`, and gets a stub test in `tests/` when it has failed in the field.
- Checks before pushing here: `make check` (syntax and the stub-API tests), plus a convert and verify run on a workspace when
  convert or verify change.
