#!/usr/bin/env bash
# make.sh [--image IMAGE] [--box DIRECTORY] [--name CONTAINER] [IMAGE] - ask a container of this box's
# Kimina image whether this kernel and this Docker can put each worker in a sandbox of its own, and
# put the sandbox in the box's sandbox/ directory for compose.worker-sandbox.yaml
# (docs/worker-sandbox.md).
#
# IMAGE is the Kimina image this box runs: the option or the argument, else LEAN_SERVER_IMAGE in
# the box's .env, else the image compose.yaml runs by default. DIRECTORY is the box's directory,
# the one compose.yaml runs from: by default the directory above this one. CONTAINER names the
# probe's container: by default a new name for each run. Run this on the box, as a user who may use
# Docker, and again whenever the image, the kernel or Docker changes. It takes a few seconds. The
# image must already be on the box: nothing is downloaded, and the image is not changed:
#   1. One container of IMAGE, hardened like the server (no network, no capabilities, no way to gain
#      privileges, a read-only root, a /tmp in memory), says the path of its REPL and runs
#      `sandbox.py --probe`, which makes the sandbox there and says which Landlock version it got.
#      Its whole output is kept in sandbox.log.
#   2. If the probe passed, the launcher (lean-worker), sandbox.py and the REPL's path (repl-path)
#      are put in sandbox/, each replaced whole.
#   3. sandbox/state says `on` with what the probe found, or `off` with the reason.
# It prints that as one line, and exits 0 when the sandbox is on and 1 when it is off.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_IMAGE=projectnumina/kimina-lean-server:2.0.0   # compose.yaml's own default
MOUNT=/opt/lean-sandbox                                # where compose.worker-sandbox.yaml mounts sandbox/
SAID_OK="WORKER SANDBOX PROBE: ok - "
SAID_FAILED="WORKER SANDBOX PROBE: FAILED - "
SAID_REPL="LEAN_SERVER_REPL_PATH="

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
SANDBOX=$BOX/sandbox
LOG=$BOX/sandbox.log
if [ -z "$image" ] && [ -f "$BOX/.env" ]; then
  image=$(sed -n 's/^LEAN_SERVER_IMAGE=//p' "$BOX/.env" | tail -n 1 | tr -d "\"'")
fi
image=${image:-$DEFAULT_IMAGE}

mkdir -p "$SANDBOX"
chmod 0755 "$SANDBOX"

# put FILE MODE CONTENT_FILE : replace sandbox/FILE whole, so a worker starting meanwhile reads
# either the old file or the new one.
put() {
  install -m "$2" "$3" "$SANDBOX/$1.new"
  mv -f "$SANDBOX/$1.new" "$SANDBOX/$1"
}

# off REASON : the sandbox is off, says why, and exits 1.
off() {
  local reason
  reason=$(printf '%s' "$1" | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-400)
  printf 'off %s\n' "$reason" > "$SANDBOX/state.new"
  chmod 0644 "$SANDBOX/state.new"
  mv -f "$SANDBOX/state.new" "$SANDBOX/state"
  echo "WORKER SANDBOX: OFF - $reason. Do not start the server with compose.worker-sandbox.yaml: its workers would not start. Full log: $LOG"
  exit 1
}

if ! inspected=$(docker image inspect "$image" 2>&1 > /dev/null); then
  case $inspected in
    *"No such image"*) off "there is no image $image on this box" ;;
    *) off "Docker could not be asked about the image $image: $inspected" ;;
  esac
fi

name=${name:-lean-sandbox-probe-$$-$RANDOM}
cleanup() { docker rm -f "$name" > /dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup   # one an interrupted run left under the same name

status=0
# shellcheck disable=SC2016  # expanded inside the container, from the image's own environment
timeout 300 docker run --rm --pull never --name "$name" --network none --cap-drop ALL \
  --security-opt no-new-privileges:true --read-only \
  --tmpfs /tmp:size=16m,mode=1777,noexec,nosuid,nodev -v "$HERE:$MOUNT:ro" \
  --entrypoint sh "$image" -c \
  'printf "LEAN_SERVER_REPL_PATH=%s\n" "$LEAN_SERVER_REPL_PATH"; exec python3 -I -S -B /opt/lean-sandbox/sandbox.py --probe' \
  > "$LOG" 2>&1 < /dev/null || status=$?

said=$({ grep -a "^$SAID_OK" "$LOG" || true; } | tail -n 1)
repl=$({ grep -a "^$SAID_REPL" "$LOG" || true; } | tail -n 1)
repl=${repl#"$SAID_REPL"}
if [ "$status" -eq 0 ] && [ -n "$said" ]; then
  case "$repl" in
    /*) ;;
    *) off "this image does not name its REPL by an absolute path in LEAN_SERVER_REPL_PATH ('$repl')" ;;
  esac
  printf '%s\n' "$repl" > "$SANDBOX/repl-path.from-image"
  put repl-path 0644 "$SANDBOX/repl-path.from-image"
  rm -f "$SANDBOX/repl-path.from-image"
  put sandbox.py 0644 "$HERE/sandbox.py"
  put lean-worker 0755 "$HERE/lean-worker"
  printf 'on %s\n' "${said#"$SAID_OK"}" > "$SANDBOX/state.new"
  chmod 0644 "$SANDBOX/state.new"
  mv -f "$SANDBOX/state.new" "$SANDBOX/state"
  echo "WORKER SANDBOX: on - ${said#"$SAID_OK"}"
  exit 0
fi

reason=$({ grep -a "^$SAID_FAILED" "$LOG" || true; } | tail -n 1)
reason=${reason#"$SAID_FAILED"}
# Else its last line, but the REPL's path: Docker copies the container's output and its errors separately,
# so the error that stopped the probe may come before that path in the log.
if [ -z "$reason" ]; then
  last=$({ grep -av -e '^\s*$' -e "^$SAID_REPL" "$LOG" || true; } | tail -n 1)
  reason="the probe did not run: ${last:-it said nothing (exit $status)}"
fi
off "$reason"
