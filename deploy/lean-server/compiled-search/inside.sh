#!/usr/bin/env bash
# inside.sh OUT_DIRECTORY - what make.sh runs inside a container of the Kimina image
# (docs/compiled-search.md): the launcher and the image's REPL path into OUT_DIRECTORY, then the
# library (build.sh), then the check (check.py). It exits as the first of them that fails.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=${1:?usage: inside.sh OUT_DIRECTORY}
repl=${LEAN_SERVER_REPL_PATH:-}
case "$repl" in
  /*) ;;
  *) echo "inside.sh: this image does not name its REPL by an absolute path in LEAN_SERVER_REPL_PATH ('$repl')" >&2; exit 2 ;;
esac
mkdir -p "$OUT"
printf '%s\n' "$repl" > "$OUT/repl-path"
install -m 0755 "$HERE/repl-native" "$OUT/repl-native"
bash "$HERE/build.sh" "$OUT"
python3 "$HERE/check.py" --launcher "$OUT/repl-native" --native-dir "$OUT"
