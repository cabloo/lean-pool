#!/usr/bin/env bash
# make.sh [IMAGE] - compile linarith's search step for this box's Kimina image, check it, and put
# it in ../native for compose.compiled-search.yaml (docs/compiled-search.md).
#
# IMAGE is the Kimina image this box runs: the argument, else KIMINA_IMAGE in ../.env, else the
# image compose.yaml runs by default. Run this on the box, as a user who may use Docker, and again
# whenever the image changes. It takes a few minutes. The image must already be on the box: nothing
# is downloaded, and the image is not changed:
#   1. One container of IMAGE, with no network, no capabilities and no way to gain privileges,
#      compiles the step from the C files the image already holds (build.sh) and checks it there
#      (check.py). Its whole output is kept in ../native.log.
#   2. The library is copied out into ../native only if the check passed. The launcher
#      (repl-native) and the image's REPL path (repl-path) are copied either way, so that a server
#      started with compose.compiled-search.yaml runs the plain REPL when there is no library.
#   3. ../native/state says `on` with the times the check measured, or `off` with the reason.
# It prints that as one line, and exits 0 when the step is on and 1 when it is off.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BOX=$(dirname "$HERE")
NATIVE=$BOX/native
LOG=$BOX/native.log
DEFAULT_IMAGE=projectnumina/kimina-lean-server:2.0.0   # compose.yaml's own default
MOUNT=/opt/lean-native                                 # where compose.compiled-search.yaml mounts ../native
SAID_OK="COMPILED SEARCH STEP: ok - "
SAID_FAILED="COMPILED SEARCH STEP: FAILED - "

image=${1:-}
if [ -z "$image" ] && [ -f "$BOX/.env" ]; then
  image=$(sed -n 's/^KIMINA_IMAGE=//p' "$BOX/.env" | tail -n 1 | tr -d "\"'")
fi
image=${image:-$DEFAULT_IMAGE}

mkdir -p "$NATIVE"
chmod 0755 "$NATIVE"
rm -rf "$BOX"/native.work.*
work=$(mktemp -d "$BOX/native.work.XXXXXX")
name=lean-native-${work##*.}
cleanup() {
  docker rm -f "$name" > /dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "Compiling linarith's search step from the C files of $image, and checking it (a few minutes)"
status=0
timeout 1800 docker run --pull never --name "$name" --network none --cap-drop ALL \
  --security-opt no-new-privileges:true -v "$HERE:/lean-native-src:ro" \
  --entrypoint bash "$image" /lean-native-src/inside.sh "$MOUNT" \
  > "$LOG" 2>&1 < /dev/null || status=$?
# Copied out by Docker as the user who runs this, never written into the box by the container.
docker cp "$name:$MOUNT/." "$work/" > /dev/null 2>&1 || true

said=$({ grep -a "^$SAID_OK" "$LOG" || true; } | tail -n 1)
if [ "$status" -eq 0 ] && [ -n "$said" ] && [ -s "$work/linarith-search.so" ] \
   && [ -s "$work/built-from" ] && [ -s "$work/sources" ] && [ -s "$work/repl-path" ]; then
  # `built-from` goes first and comes back last: a worker that starts in between runs the plain REPL.
  rm -f "$NATIVE/built-from"
  install -m 0755 "$HERE/repl-native" "$NATIVE/repl-native"
  install -m 0644 "$work/repl-path" "$NATIVE/repl-path"
  install -m 0644 "$work/linarith-search.so" "$NATIVE/linarith-search.so.new"
  mv -f "$NATIVE/linarith-search.so.new" "$NATIVE/linarith-search.so"
  install -m 0644 "$work/sources" "$NATIVE/sources"
  install -m 0644 "$work/built-from" "$NATIVE/built-from"
  printf 'on %s\n' "${said#"$SAID_OK"}" > "$NATIVE/state"
  chmod 0644 "$NATIVE/state"
  echo "COMPILED SEARCH STEP: on - ${said#"$SAID_OK"}"
  exit 0
fi

reason=$({ grep -a "^$SAID_FAILED" "$LOG" || true; } | tail -n 1)
reason=${reason#"$SAID_FAILED"}
[ -n "$reason" ] || reason="nothing was built: $({ grep -av '^\s*$' "$LOG" || true; } | tail -n 1)"
reason=$(printf '%s' "$reason" | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-400)
rm -f "$NATIVE/built-from" "$NATIVE/linarith-search.so" "$NATIVE/sources"
if [ -s "$work/repl-path" ]; then
  install -m 0755 "$HERE/repl-native" "$NATIVE/repl-native"
  install -m 0644 "$work/repl-path" "$NATIVE/repl-path"
fi
printf 'off %s\n' "$reason" > "$NATIVE/state"
chmod 0644 "$NATIVE/state"
if [ -x "$NATIVE/repl-native" ] && [ -s "$NATIVE/repl-path" ]; then
  echo "COMPILED SEARCH STEP: OFF - $reason. With compose.compiled-search.yaml the server runs the plain REPL, as before. Full log: $LOG"
else
  echo "COMPILED SEARCH STEP: OFF - $reason. Do not start the server with compose.compiled-search.yaml: there is no launcher in $NATIVE. Full log: $LOG"
fi
exit 1
