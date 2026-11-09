#!/usr/bin/env bash
# Link the ExportAll script into Fusion's Scripts folder so it shows under
# Utilities > Scripts and Add-Ins. Re-running is harmless.
set -euo pipefail
src="$(cd "$(dirname "$0")/ExportAll" && pwd)"
case "$(uname)" in
  Darwin) dest="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/Scripts" ;;
  *)      dest="${APPDATA:?}/Autodesk/Autodesk Fusion 360/API/Scripts" ;;
esac
mkdir -p "$dest"
ln -sfn "$src" "$dest/ExportAll"
echo "Linked $dest/ExportAll -> $src"
