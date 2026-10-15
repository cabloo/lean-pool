#!/usr/bin/env bash
# make.sh [--image IMAGE] [--box DIRECTORY] [--name CONTAINER] [IMAGE] - compile linarith's search
# step for this box's Kimina image, check it, and put it in the box's native/ directory for
# compose.compiled-search.yaml (docs/compiled-search.md).
#
# IMAGE is the Kimina image this box runs: the option or the argument, else LEAN_SERVER_IMAGE in
# the box's .env, else the image compose.yaml runs by default. DIRECTORY is the box's directory,
# the one compose.yaml runs from: by default the directory above this one. CONTAINER names the
# build's container: by default a new name for each run. Run this on the box, as a user who may
# use Docker, and again whenever the image changes. It takes a few minutes. The image must already
# be on the box: nothing is downloaded, and the image is not changed:
#   1. One container of IMAGE, with no network, no capabilities and no way to gain privileges,
#      compiles the step from the C files the image already holds (build.sh) and checks it there
#      (check.py). Its whole output is kept in native.log.
#   2. The library is copied out into native/ only if the check passed. The launcher
#      (repl-native) and the image's REPL path (repl-path) are copied either way, so that a server
#      started with compose.compiled-search.yaml runs the plain REPL when there is no library.
#   3. native/state says `on` with the times the check measured, or `off` with the reason.
#   4. native.built says what the step was made from: one hash over the files in this directory,
#      and the image's ID. While both are unchanged and the step is on, a run makes nothing again
#      (remove native.built to make it anyway).
# It prints that as one line, and exits 0 when the step is on and 1 when it is off.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_IMAGE=projectnumina/kimina-lean-server:2.0.0   # compose.yaml's own default
MOUNT=/opt/lean-native                                 # where compose.compiled-search.yaml mounts native/
FILES=(build.sh check.py inside.sh make.sh modules repl-native)
SAID_OK="COMPILED SEARCH STEP: ok - "
SAID_FAILED="COMPILED SEARCH STEP: FAILED - "

usage() {
  echo "usage: make.sh [--image IMAGE] [--box DIRECTORY] [--name CONTAINER] [IMAGE]" >&2
  exit 2
}
image="" box=$(dirname "$HERE") name=""
while [ $# -gt 0 ]; do
  case $1 in
    --image | --box | --name)
      [ $# -ge 2 ] && [ -n "$2" ] || usage
      case $1 in
        --image) image=$2 ;;
        --box) box=$2 ;;
        --name) name=$2 ;;
      esac
      shift 2
      ;;
    -*) usage ;;
    *)
      [ -z "$image" ] || usage
      image=$1
      shift
      ;;
  esac
done
[ -d "$box" ] || { echo "make.sh: no directory $box" >&2; exit 2; }
BOX=$(cd "$box" && pwd)
NATIVE=$BOX/native
LOG=$BOX/native.log
BUILT=$BOX/native.built
if [ -z "$image" ] && [ -f "$BOX/.env" ]; then
  image=$(sed -n 's/^LEAN_SERVER_IMAGE=//p' "$BOX/.env" | tail -n 1 | tr -d "\"'")
fi
image=${image:-$DEFAULT_IMAGE}

mkdir -p "$NATIVE"
chmod 0755 "$NATIVE"
rm -rf "$BOX"/native.work.*
work=""

# off REASON : the box keeps no library, says why, and exits 1.
off() {
  local reason
  reason=$(printf '%s' "$1" | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-400)
  rm -f "$NATIVE/built-from" "$NATIVE/linarith-search.so" "$NATIVE/sources" "$BUILT"
  if [ -n "$work" ] && [ -s "$work/repl-path" ]; then
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
}

id=$(docker image inspect -f '{{.Id}}' "$image" 2> /dev/null || true)
[ -n "$id" ] || off "there is no image $image on this box"
inputs=$(cd "$HERE" && sha256sum "${FILES[@]}" | sha256sum | cut -d ' ' -f 1)
state=$(head -n 1 "$NATIVE/state" 2> /dev/null || true)
if [ "$(cat "$BUILT" 2> /dev/null || true)" = "$inputs $id" ] && [ "${state%% *}" = on ] \
   && [ -x "$NATIVE/repl-native" ] && [ -s "$NATIVE/repl-path" ] && [ -s "$NATIVE/linarith-search.so" ] \
   && [ -s "$NATIVE/built-from" ] && [ -s "$NATIVE/sources" ]; then
  echo "COMPILED SEARCH STEP: on - already made from this image and checked (${state#on }); not made again"
  exit 0
fi

work=$(mktemp -d "$BOX/native.work.XXXXXX")
name=${name:-lean-native-${work##*.}}
cleanup() {
  docker rm -f "$name" > /dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
docker rm -f "$name" > /dev/null 2>&1 || true   # one an interrupted run left under the same name

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
  rm -f "$NATIVE/built-from" "$BUILT"
  install -m 0755 "$HERE/repl-native" "$NATIVE/repl-native"
  install -m 0644 "$work/repl-path" "$NATIVE/repl-path"
  install -m 0644 "$work/linarith-search.so" "$NATIVE/linarith-search.so.new"
  mv -f "$NATIVE/linarith-search.so.new" "$NATIVE/linarith-search.so"
  install -m 0644 "$work/sources" "$NATIVE/sources"
  install -m 0644 "$work/built-from" "$NATIVE/built-from"
  printf 'on %s\n' "${said#"$SAID_OK"}" > "$NATIVE/state"
  chmod 0644 "$NATIVE/state"
  printf '%s %s\n' "$inputs" "$id" > "$BUILT"
  chmod 0644 "$BUILT"
  echo "COMPILED SEARCH STEP: on - ${said#"$SAID_OK"}"
  exit 0
fi

reason=$({ grep -a "^$SAID_FAILED" "$LOG" || true; } | tail -n 1)
reason=${reason#"$SAID_FAILED"}
[ -n "$reason" ] || reason="nothing was built: $({ grep -av '^\s*$' "$LOG" || true; } | tail -n 1)"
off "$reason"
