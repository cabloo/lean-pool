#!/usr/bin/env bash
# read-only-check.sh [--diff FILE] CONTAINER - whether the Lean server running in CONTAINER would
# keep working with its root file system read-only (LEAN_SERVER_ROOT_READ_ONLY=true).
#
# Why: a check is a Lean file, and Lean runs programs while it reads one, so every check runs a
# program inside the server's container. With a read-only root, a check can change neither Lean,
# Mathlib nor the REPL for the checks after it, nor fill the host's disk; /tmp, where Kimina writes
# its workers' error output, is a small file system in memory (compose.yaml).
#
# Run it against a server that has served checks with a writable root: Docker keeps every file the
# container changed in its writable layer (`docker diff`). A read-only root would have refused each
# of them except those under /tmp, under the container's mounts, and Docker's own (its init, and the
# three name files it mounts). Two more are not counted:
#   - Python's byte-code cache: Python goes on quietly when it cannot write it;
#   - git's index of a Lake package (.lake/packages/NAME/.git/index): each worker starts with
#     `lake env`, which refreshes it and saves it only when it can; unsaved, git reads the package's
#     files again at the next start and answers the same.
# A directory listed only because something under it changed is not counted either: the change
# itself is judged.
#
# It prints every other path, one a line, sorted, and exits 0 when there is none (a read-only root
# keeps this server working), 1 when there is any, and 2 when it could not read the container (the
# last line on stderr says why). --diff FILE keeps what `docker diff` said. A real check against the
# server once it runs read-only is still the test that counts.
set -euo pipefail

usage() {
  echo "usage: read-only-check.sh [--diff FILE] CONTAINER" >&2
  exit 2
}
diff_file=""
case ${1:-} in
  --diff)
    [ $# -ge 2 ] && [ -n "$2" ] || usage
    diff_file=$2
    shift 2
    ;;
  -*) usage ;;
esac
[ $# -eq 1 ] && [ -n "$1" ] || usage
container=$1

said=$(mktemp)
trap 'rm -f "$said"' EXIT
if ! docker diff "$container" > "$said" 2>&1; then
  [ -z "$diff_file" ] || cp "$said" "$diff_file"
  echo "docker diff: $(tail -n 1 "$said")" >&2
  exit 2
fi
[ -z "$diff_file" ] || cp "$said" "$diff_file"
mounts=$(docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$container" 2> /dev/null) || {
  echo "docker inspect: could not read the mounts of $container" >&2
  exit 2
}

# Docker's init (with `init: true`) and the three name files are mounted by Docker itself, and
# `.Mounts` does not list them.
outside=$(awk -v allowed="/tmp /sbin/docker-init /usr/sbin/docker-init /etc/hosts /etc/hostname /etc/resolv.conf $mounts" '
  BEGIN { kept = split(allowed, keep, " ") }
  /^[ACD] \// {
    path = substr($0, 3); listed[path] = 1
    parent = path
    while (sub(/\/[^\/]*$/, "", parent) && parent != "") above[parent] = 1
  }
  END {
    for (path in listed) {
      if (path in above || path ~ /\/__pycache__(\/|$)/ || path ~ /\.pyc$/) continue
      if (path ~ /\/\.lake\/packages\/[^\/]+\/\.git\/index$/) continue
      fine = 0
      for (i = 1; i <= kept; i++) if (path == keep[i] || index(path, keep[i] "/") == 1) fine = 1
      if (!fine) print path
    }
  }' "$said" | LC_ALL=C sort)
[ -n "$outside" ] || exit 0
printf '%s\n' "$outside"
exit 1
