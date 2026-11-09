#!/usr/bin/env bash
# Link the Fusion scripts in this folder into Fusion's Scripts folder so they show
# under Utilities > Scripts and Add-Ins. Re-running is harmless.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
case "$(uname)" in
  Darwin) dest="$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/Scripts" ;;
  *)      dest="${APPDATA:?}/Autodesk/Autodesk Fusion 360/API/Scripts" ;;
esac
mkdir -p "$dest"
# Remove links from earlier versions that point into this repo.
for old in "$dest"/*; do
  if [ -L "$old" ] && [[ "$(readlink "$old")" == "$here"/* ]] && [ ! -e "$old" ]; then
    rm "$old" && echo "Removed stale link $old"
  fi
done
for script in "$here"/*/; do
  name="$(basename "$script")"
  [ -f "$script/$name.py" ] || continue   # lib/ and other helpers aren't scripts
  ln -sfn "${script%/}" "$dest/$name"
  echo "Linked $dest/$name"
done
