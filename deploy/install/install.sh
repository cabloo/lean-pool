#!/usr/bin/env bash
# install.sh - install a pinned Kimina Lean Server on this HOST, in Docker, with a site's values.
#
#   sudo bash install.sh --site DIR                    # the site's instance with no name (instances/default.env)
#   sudo bash install.sh --site DIR --instance NAME    # a named instance (instances/NAME.env)
#   sudo bash install.sh --site DIR [--instance NAME] --uninstall
#
# A site (--site DIR; else the site/ beside this file, which a join bundle carries) holds the values:
# site.env, instances/NAME.env, and the image recipes those name. Run it ON THE HOST.
#
# What it does, in order (each step is safe to repeat):
#   1. pre-flight: docker present, disk space, port free, CPU microcode reported; a missing docker compose
#      plugin is installed at its end (package manager, else a pinned release binary)
#   1b. a box an older installer set up is first moved, all of it, to the compose files of deploy/ under
#      Compose's names, each setting under its new name (pool-lib.sh's move_to_compose_names); the files
#      each instance and pool held are kept beside them as *.before-compose-names
#   2. copies the build + compose files into /srv/kimina (/srv/kimina-NAME for an instance; root-owned);
#      writes kimina.env (0600) with a generated API key - an existing key and existing settings are KEPT
#      (the port and the image tag are written on every run; the instance's workers, worker memory, CPUs
#      and subnet only fill in settings that kimina.env does not have yet)
#   3. builds the image from the instance's recipe, or pulls the image the instance names (INSTANCE_IMAGE);
#      a first build of Lean and its libraries can take an hour or more. `--build-only` stops here, having
#      written no key and started nothing: a join script builds the image this way BEFORE it asks its pool
#      for anything, and the install that follows does not build a named instance's image again
#   3b. a named instance: compiles linarith's search step from the image's own C files, checks it in a
#      container of that image, and only then has the server start its workers with it;
#      a box where that fails runs the plain REPL, and is told
#   4. starts the service (`restart: unless-stopped`, healthcheck), in a control group of its own at the
#      box's lowest CPU priority where the box can do that: whatever else
#      runs here gets a contended CPU first; a named instance's root filesystem is made read-only where the
#      server already running was seen to write nothing else. Beside a named instance: the lean-pool usage
#      agent (not with --no-agent), behind a TLS front with --tls-dir
#   5. firewall: a DOCKER-USER rule that limits the port to the LAN subnet and the docker bridges and an
#      egress block, re-applied at boot by a systemd unit per instance; plus the matching `ufw` rule
#   6. /usr/local/bin/kimina-ctl (root-owned, shared by all instances), and with --operator-key the door: a
#      sudoers rule naming the door's program (--door-program), and a forced-command line for that key in the
#      invoking user's authorized_keys
#   7. verifies: one valid and one invalid proof through the live server
#
# A named instance that belongs to a lean-pool stands behind a TLS front: the Lean server and its usage agent
# are then not published at all, and the front on their ports lets in only the pool proxy's certificate.
# `--tls-dir` turns that on (a join script passes it); once on, a re-run keeps it. Instance 1 is never
# fronted: it stays plain HTTP.
#
# Apart from step 1b's one-time move, it does NOT touch any other container.
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

SITE=""
INSTANCE=""
PORT=""
SUBNET=""
REPLS=""
MEM_LIMIT=""
BUILD_JOBS=16
NO_DOOR=0
OPERATOR_KEY=""
DOOR_PROGRAM=/usr/local/bin/kimina-ctl
FIREWALL=1
UNINSTALL=0
PURGE=0
SHOW_INSTANCE=0
PREFLIGHT_ONLY=0
BUILD_ONLY=0
KEEP_IMAGE=0
IMAGE_FROM_POOL=""
MIN_FREE_GB_GIVEN=""
API_KEY_FILE=""
CPUS=""
CPU_PRIORITY=""
AGENT=""
TLS_DIR=""
SERVER_NAME=""
POOL_LEAN_TIMEOUT=""
POOL_SERVER_WAIT=""
DOOR_USER=${SUDO_USER:-}

usage() {
  sed -n '2,/^# It does NOT touch any other container/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --site DIR        the site's values: DIR/site.env, DIR/instances/NAME.env and the image recipes they name
                    (default: the site/ beside this installer, which a join bundle carries)
  --instance NAME   install a named server, independent of the others, from the site's instances/NAME.env
                    (NAME: 1-16 lower-case letters and digits, and neither 'default' nor 'main')
  --proxy NAME      install the lean-pool front door (HAProxy + cache) for instance NAME's pool, all of it
                    TLS; only --site may come before it, and it takes its own options (--port, --subnet,
                    --dns, --lean-timeout, --front-name NAME, --admit-cases DIR, --no-firewall,
                    --uninstall [--purge]); every install of a pool needs --admit-cases
  --api-key-file F  install the server with the API key in file F (a pool's key) instead of generating one
  --cpus N          CPU ceiling to write into kimina.env (default: the instance's INSTANCE_CPUS; later:
                    kimina-ctl set-cpus N)
  --cpu-priority lowest|normal
                    `lowest` (the default): the server runs in a control group whose CPU weight is the
                    lowest there is, so other work on this box gets a contended CPU first. `normal`: the
                    box's ordinary weight, for a box whose first job is Lean. Kept in kimina.env: a later
                    run without the option keeps what was chosen
  --no-agent        do not run the lean-pool usage agent beside a named instance
  --tls-dir DIR     put a named instance behind a TLS front: DIR holds box.pem (this box's certificate, then
                    its private key), box.crt and ca.crt (the pool authority's). A join script passes this.
  --server-name N   with --tls-dir: this server's name in the pool, which is the name on its certificate
                    (one DNS label in lower case, no dot)
  --pool-lean-timeout S, --pool-server-wait S
                    with --tls-dir: the pool's Lean timeout and server wait (default 300 and 120), so that
                    the front never cuts a check the pool would still wait for
  --subnet CIDR     LAN subnet allowed to reach the port (default: detected from the route table)
  --port N          host port (default: the instance's INSTANCE_PORT)
  --repls N         worker count to write into kimina.env (default: the instance's INSTANCE_WORKERS; later:
                    kimina-ctl set-repls N)
  --mem-limit SIZE  container memory ceiling, e.g. 36g (default 36g)
  --build-jobs N    parallel Lean jobs while building (default 16)
  --operator-key F  install the ssh door for the public key in file F: a forced-command line in the door
                    user's authorized_keys, and a sudoers rule. The key's comment must hold the site's
                    SITE_DOOR_KEY_MARKER: a re-run and --uninstall find the key's line by it. Without
                    --operator-key there is no door
  --door-program P  what the door runs, as root (default /usr/local/bin/kimina-ctl); a site may put a
                    program of its own in front of kimina-ctl
  --door-user USER  account whose authorized_keys gets the forced-command line (default: $SUDO_USER)
  --no-door         no door, even with --operator-key (you then run `sudo kimina-ctl ...` by hand)
  --no-firewall     do not add the DOCKER-USER / ufw rules
  --uninstall       stop the service and remove its firewall rule and unit (the door goes with the last instance)
  --purge           with --uninstall: also remove the instance's directory and images
  --show-instance   print what this instance would install (names, port, subnet, firewall unit) and exit
  --preflight-only  run the pre-flight checks with the options given, install nothing, and exit 0 only if
                    this host can take the install (a join script runs this before it asks its pool for
                    anything)
  --build-only      run the pre-flight, copy the build files and build the image, then stop: no key, no
                    settings, no certificate, no container, no firewall rule, no door (a join script runs
                    this, too, before it asks its pool for anything: a build that fails then costs no join
                    window). The install that follows finds a named instance's image and does not build
                    it again; instance 1's calls `docker build`, which the builder's cache answers.
  --image-from-pool LAYERS
                    the image of this instance's tag is already here, loaded from its pool, and has this
                    layer identity (a join script passes it): do not build; refuse if it differs. Later
                    runs keep that image whatever the build files here say (delete image.built to build)
  --keep-image      the image under this instance's tag stays exactly as it is: nothing is built, whatever
                    the build files and the job count of this run, image.built is not touched, and the run
                    is refused before anything is written if no such image is on this host (a join script
                    passes it with --refresh, for a box that is already in its pool)
  --min-free-gb N   the free disk the pre-flight asks for where Docker keeps its images, in place of the
                    build's (a join script that loads the pool's image passes the load's need)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --proxy) pool=${2:-}; shift 2 || true; exec bash "$SRC/install-pool.sh" "$pool" ${SITE:+--site "$SITE"} "$@" ;;
    --site) SITE=$2; shift 2 ;;
    --instance) INSTANCE=$2; shift 2 ;;
    --api-key-file) API_KEY_FILE=$2; shift 2 ;;
    --cpus) CPUS=$2; shift 2 ;;
    --cpu-priority) CPU_PRIORITY=$2; shift 2 ;;
    --no-agent) AGENT=0; shift ;;
    --tls-dir) TLS_DIR=$2; shift 2 ;;
    --server-name) SERVER_NAME=$2; shift 2 ;;
    --pool-lean-timeout) POOL_LEAN_TIMEOUT=$2; shift 2 ;;
    --pool-server-wait) POOL_SERVER_WAIT=$2; shift 2 ;;
    --subnet) SUBNET=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --repls) REPLS=$2; shift 2 ;;
    --mem-limit) MEM_LIMIT=$2; shift 2 ;;
    --build-jobs) BUILD_JOBS=$2; shift 2 ;;
    --operator-key) OPERATOR_KEY=$2; shift 2 ;;
    --door-program) DOOR_PROGRAM=$2; shift 2 ;;
    --door-user) DOOR_USER=$2; shift 2 ;;
    --no-door) NO_DOOR=1; shift ;;
    --no-firewall) FIREWALL=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --show-instance) SHOW_INSTANCE=1; shift ;;
    --preflight-only) PREFLIGHT_ONLY=1; shift ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --keep-image) KEEP_IMAGE=1; shift ;;
    --image-from-pool) IMAGE_FROM_POOL=$2; shift 2 ;;
    --min-free-gb) MIN_FREE_GB_GIVEN=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
[ -f "$SRC/pool-lib.sh" ] || die "missing $SRC/pool-lib.sh - run this from the repo checkout"
# shellcheck source=pool-lib.sh
. "$SRC/pool-lib.sh"

# ----------------------------------------------------------------------------------------------- site
# The site's values (load_site, pool-lib.sh): --site DIR, else the site/ a join bundle carries beside this file.
[ -n "$SITE" ] || [ ! -f "$SRC/site/site.env" ] || SITE=$SRC/site
[ -n "$SITE" ] || die "--site DIR is needed: the directory of this site's values (site.env, instances/NAME.env)"
load_site "$SITE"

# ------------------------------------------------------------------------------------------- instance
# Everything that differs between instances is the site's instance file and nowhere else: instances/NAME.env,
# or instances/default.env for the instance with no name ("instance 1"). Re-running with the same file changes
# none of what it says. The names follow from the instance's own: /srv/kimina[-NAME], and the compose project
# lean-server[-NAME], whose containers Compose names (lean-server[-NAME]-kimina-1, -agent-1, -front-1).
# (What every instance's own installer brings it: the init process, and a control group of its own
# with three settings added to kimina.env.)
load_instance() {   # NAME (empty: the instance with no name) : its values, each checked for its shape
  local file=$SITE/instances/${1:-default}.env known="" name
  if [ ! -f "$file" ]; then
    for name in "$SITE"/instances/*.env; do
      [ -f "$name" ] || continue
      name=${name##*/}; name=${name%.env}
      [ "$name" = default ] || known="$known${known:+, }$name"
    done
    [ -z "$1" ] || die "unknown --instance '$1' (this site's: ${known:-none})"
    die "this site has no instance without a name ($file); pass --instance NAME (this site's: ${known:-none})"
  fi
  IMAGE_TAG=$(setting_in "$file" INSTANCE_IMAGE_TAG)
  PREBUILT=$(setting_in "$file" INSTANCE_IMAGE)
  DOCKERFILE=$(setting_in "$file" INSTANCE_DOCKERFILE)
  read -r -a BUILD_FILES <<<"$(setting_in "$file" INSTANCE_BUILD_FILES)"
  DEFAULT_PORT=$(setting_in "$file" INSTANCE_PORT)
  DEFAULT_NET=$(setting_in "$file" INSTANCE_SUBNET)
  DEFAULT_REPLS=$(setting_in "$file" INSTANCE_WORKERS)
  DEFAULT_REPL_MEM=$(setting_in "$file" INSTANCE_WORKER_MEMORY)
  DEFAULT_CPUS=$(setting_in "$file" INSTANCE_CPUS)
  MIN_FREE_GB=$(setting_in "$file" INSTANCE_MIN_FREE_GB)
  [[ "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "$file: INSTANCE_IMAGE_TAG must be an image tag"
  if [ -n "$PREBUILT" ]; then      # an image to pull, under the local tag: no recipe
    [[ "$PREBUILT" =~ ^[a-z0-9][A-Za-z0-9._/:@-]{0,254}$ ]] && [[ "$PREBUILT" != *..* ]] \
      || die "$file: INSTANCE_IMAGE must be an image reference, like projectnumina/kimina-lean-server:2.0.0"
    [ -z "$DOCKERFILE" ] && [ "${#BUILD_FILES[@]}" -eq 0 ] \
      || die "$file: INSTANCE_IMAGE (an image to pull) cannot go with INSTANCE_DOCKERFILE or INSTANCE_BUILD_FILES (a recipe to build)"
  else
    [ "${#BUILD_FILES[@]}" -gt 0 ] || die "$file: INSTANCE_BUILD_FILES must name the files of the image's recipe (or INSTANCE_IMAGE an image to pull)"
    for name in "${BUILD_FILES[@]}"; do
      [[ "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "$file: INSTANCE_BUILD_FILES must name files in $SITE, not '$name'"
    done
    [[ " ${BUILD_FILES[*]} " == *" $DOCKERFILE "* ]] || die "$file: INSTANCE_DOCKERFILE must be one of INSTANCE_BUILD_FILES"
  fi
  [[ "$DEFAULT_PORT" =~ ^[0-9]{2,5}$ ]] || die "$file: INSTANCE_PORT must be a port number"
  [[ "$DEFAULT_NET" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "$file: INSTANCE_SUBNET must be CIDR"
  [[ "$DEFAULT_REPLS" =~ ^[0-9]{1,2}$ ]] || die "$file: INSTANCE_WORKERS must be a number"
  [[ "$DEFAULT_REPL_MEM" =~ ^[0-9]+[gGmM]$ ]] || die "$file: INSTANCE_WORKER_MEMORY looks like 8G"
  [[ "$DEFAULT_CPUS" =~ ^[0-9]{1,2}$ ]] || die "$file: INSTANCE_CPUS must be a number"
  [[ "$MIN_FREE_GB" =~ ^[0-9]{1,4}$ ]] || die "$file: INSTANCE_MIN_FREE_GB must be a number of GB"
}
[ -z "$INSTANCE" ] || { [[ "$INSTANCE" =~ ^[a-z0-9]{1,16}$ ]] && [ "$INSTANCE" != default ] && [ "$INSTANCE" != main ]; } \
  || die "--instance takes 1-16 lower-case letters and digits (not 'default': that is the file of the instance with no name; not 'main': that is the name of its control group)"
load_instance "$INSTANCE"
DIR=/srv/kimina${INSTANCE:+-$INSTANCE}
PROJECT=$(lean_server_project "$INSTANCE")
CONTAINER=$PROJECT-kimina-1
IMAGE=$SITE_PREFIX/kimina:$IMAGE_TAG
FW_UNIT=kimina-firewall${INSTANCE:+-$INSTANCE}.service
PORT=${PORT:-$DEFAULT_PORT}
# The usage agent (lean-pool) runs beside a named instance by default; instance 1 is left as it was.
if [ -z "$AGENT" ]; then if [ -n "$INSTANCE" ]; then AGENT=1; else AGENT=0; fi; fi
AGENT_PORT=$((PORT + 200))
AGENT_CONTAINER=$PROJECT-agent-1
FRONT_CONTAINER=$PROJECT-front-1
CTL_PREFIX=${INSTANCE:+@$INSTANCE }
# The door: an operator key, unless --no-door.
DOOR=0
if [ -n "$OPERATOR_KEY" ] && [ "$NO_DOOR" -eq 0 ]; then DOOR=1; fi

if [ "$SHOW_INSTANCE" -eq 1 ]; then   # what this instance would install, without root and without changes
  printf 'dir=%s\ncontainer=%s\nproject=%s\nimage=%s\ndockerfile=%s\nport=%s\nsubnet=%s\nfirewall_unit=%s\nagent=%s\nagent_port=%s\n' \
    "$DIR" "$CONTAINER" "$PROJECT" "$IMAGE" "$DOCKERFILE" "$PORT" "$DEFAULT_NET" "$FW_UNIT" "$AGENT" "$AGENT_PORT"
  [ -z "$PREBUILT" ] || printf 'pull=%s\n' "$PREBUILT"
  exit 0
fi

# Arguments are checked before root is demanded, so a typo is reported without a sudo prompt.
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || die "--port must be a number"
[[ "$BUILD_JOBS" =~ ^[0-9]{1,2}$ ]] || die "--build-jobs must be a number"
[ -z "$REPLS" ] || [[ "$REPLS" =~ ^[0-9]{1,2}$ ]] || die "--repls must be a number"
[ -z "$MEM_LIMIT" ] || [[ "$MEM_LIMIT" =~ ^[0-9]+[gGmM]$ ]] || die "--mem-limit looks like 36g"
[ -z "$CPUS" ] || [[ "$CPUS" =~ ^[0-9]{1,2}$ ]] || die "--cpus must be a number"
[ -z "$CPU_PRIORITY" ] || [ "$CPU_PRIORITY" = lowest ] || [ "$CPU_PRIORITY" = normal ] || die "--cpu-priority is lowest or normal"
[ -z "$MIN_FREE_GB_GIVEN" ] || [[ "$MIN_FREE_GB_GIVEN" =~ ^[0-9]{1,4}$ ]] || die "--min-free-gb must be a number of GB"
[ "$DOOR" -eq 0 ] || [ -s "$OPERATOR_KEY" ] || die "--operator-key: no such file, or an empty one: $OPERATOR_KEY"
[[ "$DOOR_PROGRAM" =~ ^(/[A-Za-z0-9_.-]+)+$ ]] && [[ "$DOOR_PROGRAM" != *..* ]] || die "--door-program must be an absolute path"
if [ -n "$IMAGE_FROM_POOL" ]; then
  [ -n "$INSTANCE" ] || die "--image-from-pool needs --instance NAME: instance 1 builds its own image"
  [[ "$IMAGE_FROM_POOL" =~ ^[0-9a-f]{64}$ ]] || die "--image-from-pool takes an image's layer identity (64 hexadecimal digits)"
  declare -F image_layers >/dev/null || die "missing $SRC/pool-lib.sh - run this from the repo checkout"
fi
if [ "$KEEP_IMAGE" -eq 1 ]; then
  [ -n "$INSTANCE" ] || die "--keep-image needs --instance NAME: instance 1 builds its own image"
  [ "$BUILD_ONLY" -eq 0 ] && [ -z "$IMAGE_FROM_POOL" ] || die "--keep-image cannot go with --build-only or --image-from-pool"
fi
POOL_KEY=""
if [ -n "$API_KEY_FILE" ]; then
  [ -f "$API_KEY_FILE" ] || die "--api-key-file: no such file $API_KEY_FILE"
  POOL_KEY=$(tr -d '[:space:]' < "$API_KEY_FILE")
  [[ "$POOL_KEY" =~ ^[A-Za-z0-9]{16,128}$ ]] || die "--api-key-file must hold one key of 16-128 letters and digits"
fi
# A TLS front stands beside a NAMED instance only. Instance 1 is plain HTTP, and no option may change that.
if [ -n "$TLS_DIR$SERVER_NAME$POOL_LEAN_TIMEOUT$POOL_SERVER_WAIT" ]; then
  [ -n "$INSTANCE" ] || die "--tls-dir, --server-name and the --pool-* options need --instance NAME: instance 1 stays plain HTTP"
  [ -n "$TLS_DIR" ] || die "--server-name and the --pool-* options go with --tls-dir"
  [ -n "$SERVER_NAME" ] || die "--tls-dir needs --server-name NAME (the name on the certificate)"
  [ "$AGENT" -eq 1 ] || die "--tls-dir cannot go with --no-agent: the TLS front carries the usage agent's port"
  declare -F install_tls_front >/dev/null || die "missing $SRC/pool-lib.sh - run this from the repo checkout"
  [[ "$SERVER_NAME" =~ $LEAN_POOL_SERVER_NAME ]] \
    || die "--server-name must be one DNS label in lower case (no dot), at most 40 characters"
  [ -z "$POOL_LEAN_TIMEOUT" ] || [[ "$POOL_LEAN_TIMEOUT" =~ ^[0-9]{2,4}$ ]] || die "--pool-lean-timeout must be seconds"
  [ -z "$POOL_SERVER_WAIT" ] || [[ "$POOL_SERVER_WAIT" =~ ^[0-9]{1,4}$ ]] || die "--pool-server-wait must be seconds"
fi
[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo bash $0"

# The instance's one project, with the files kimina.env names (compose_in, pool-lib.sh).
compose() { compose_in "$DIR/kimina.env" "$@"; }

door_home() { getent passwd "$1" | cut -d: -f6; }

remove_door() {
  rm -f /etc/sudoers.d/kimina-ctl /usr/local/bin/kimina-ctl
  local user home keys
  for user in "$DOOR_USER" $(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 {print $1}'); do
    [ -n "$user" ] || continue
    home=$(door_home "$user"); keys="$home/.ssh/authorized_keys"
    if [ -f "$keys" ] && grep -q "$SITE_DOOR_KEY_MARKER" "$keys"; then
      sed -i "/$SITE_DOOR_KEY_MARKER/d" "$keys"
      echo "removed the door key from $keys"
    fi
  done
}

other_instances_remain() {   # any OTHER installed instance still has a kimina.env
  local env
  for env in /srv/kimina/kimina.env /srv/kimina-*/kimina.env; do
    [ -f "$env" ] && [ "$(dirname "$env")" != "$DIR" ] && return 0
  done
  return 1
}

# ------------------------------------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  say "Uninstalling ${INSTANCE:-instance 1}"
  # The instance's one project: the server, and beside a named instance its usage agent and TLS front. On a
  # box an older installer set up, its older projects.
  if [ -f "$DIR/kimina.env" ] && [ -n "$(setting_in "$DIR/kimina.env" COMPOSE_PROJECT_NAME)" ]; then
    compose down || true
  elif [ -f "$DIR/kimina.env" ]; then
    old_instance "$DIR"
    old_instance_down "$DIR" || true
  fi
  if declare -F remove_lan_firewall >/dev/null; then remove_lan_firewall "agent${INSTANCE:+-$INSTANCE}"; fi
  if [ -x /usr/local/sbin/kimina-firewall ]; then /usr/local/sbin/kimina-firewall remove $INSTANCE || true; fi
  systemctl disable --now "$FW_UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$FW_UNIT"
  systemctl daemon-reload || true
  if command -v ufw >/dev/null && [ -f "$DIR/firewall.conf" ]; then
    # shellcheck disable=SC1091
    . "$DIR/firewall.conf"
    ufw delete allow from "$KIMINA_FW_SUBNET" to any port "$KIMINA_FW_PORT" proto tcp >/dev/null 2>&1 || true
  fi
  if other_instances_remain; then
    echo "another instance is still installed: keeping the door and /usr/local/sbin/kimina-firewall"
  else
    remove_door
    rm -f /usr/local/sbin/kimina-firewall
    remove_site_env
    if declare -F remove_cpu_priority >/dev/null; then remove_cpu_priority; fi
  fi
  if [ "$PURGE" -eq 1 ]; then
    docker image rm "$IMAGE" 2>/dev/null || true
    [ -z "$PREBUILT" ] || docker image rm "$PREBUILT" 2>/dev/null || true
    rm -f "$DIR/kimina.env"
    rm -rf "$DIR"
    echo "removed $DIR and the image(s)"
  else
    echo "kept $DIR (settings, API key, build log) and the image(s); add --purge to remove them"
  fi
  exit 0
fi

# Behind a TLS front? Asked for now (--tls-dir), or already so: a re-run keeps the front, its certificate
# and its settings. TLS stays 0 for instance 1, whatever is on disk.
TLS=0
TLS_SOURCE=""
if [ -n "$TLS_DIR" ]; then
  if [ "$PREFLIGHT_ONLY" -eq 0 ] && [ "$BUILD_ONLY" -eq 0 ]; then   # a pre-flight and a build come before the certificate exists
    for file in box.pem box.crt ca.crt; do
      [ -s "$TLS_DIR/$file" ] || die "--tls-dir: $TLS_DIR/$file is missing or empty"
    done
  fi
  TLS=1; TLS_SOURCE=$TLS_DIR
elif [ -n "$INSTANCE" ] && [ -s "$DIR/tls/box.pem" ] && [ -f "$DIR/kimina.env" ] && declare -F install_tls_front >/dev/null; then
  TLS=1; TLS_SOURCE=$DIR/tls
  SERVER_NAME=$(setting_moved_in "$DIR/kimina.env" LEAN_SERVER_NAME KIMINA_SERVER_NAME)
  [ -n "$SERVER_NAME" ] || die "$DIR/tls holds a certificate but $DIR/kimina.env names no server (LEAN_SERVER_NAME)"
  [ "$AGENT" -eq 1 ] || die "this instance is behind a TLS front, which carries the usage agent's port: --no-agent cannot go with it"
  echo "this instance is behind a TLS front as '$SERVER_NAME': keeping it"
fi
if [ "$TLS" -eq 1 ]; then      # the pool's two durations: given now, else what the front was installed with
  if [ -f "$DIR/kimina.env" ]; then
    POOL_LEAN_TIMEOUT=${POOL_LEAN_TIMEOUT:-$(setting_moved_in "$DIR/kimina.env" LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS KIMINA_FRONT_LEAN_TIMEOUT)}
    POOL_SERVER_WAIT=${POOL_SERVER_WAIT:-$(setting_moved_in "$DIR/kimina.env" LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS KIMINA_FRONT_SERVER_WAIT)}
  fi
  POOL_LEAN_TIMEOUT=${POOL_LEAN_TIMEOUT:-300}
  POOL_SERVER_WAIT=${POOL_SERVER_WAIT:-120}
fi

# ---------------------------------------------------------------------------------------------- image
# The image needs no secret: the key and a pool member's certificate are read when the server runs. So it
# can be built on its own (`--build-only`), before a joining box asks its
# pool for anything, and a build that fails then costs no join window.
install_build_files() {    # the build's context: root-owned copies in $DIR
  local file
  install -d -o root -g root -m 0755 "$DIR"
  for file in ${BUILD_FILES[@]+"${BUILD_FILES[@]}"}; do      # none for an image that is pulled
    case "$file" in
      *.sh) install -o root -g root -m 0755 "$SITE/$file" "$DIR/" ;;
      *)    install -o root -g root -m 0644 "$SITE/$file" "$DIR/" ;;
    esac
  done
}

# What a build is made from, as one word: the tag, the job count and the build files (as they lie in $DIR, or
# in the directory given: the pre-flight asks about the files it is about to copy). For an image that is
# pulled: the tag and the reference pulled.
image_inputs() {
  local from=${1:-$DIR}
  if [ -n "$PREBUILT" ]; then
    printf 'tag=%s pull=%s\n' "$IMAGE_TAG" "$PREBUILT" | sha256sum | cut -d ' ' -f 1
    return
  fi
  { printf 'tag=%s jobs=%s\n' "$IMAGE_TAG" "$BUILD_JOBS"; (cd "$from" && sha256sum "${BUILD_FILES[@]}"); } | sha256sum | cut -d ' ' -f 1
}
image_id() { docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true; }
# A named instance's last build left $DIR/image.built: those inputs, and the image they made. The same inputs
# and that very image still under its tag: there is nothing to build. An image that came from the instance's
# POOL left `pool <layer identity>` there instead: it is kept as long as the image under
# the tag has those layers, WHATEVER the build files here say. The pool's image is the pool's pin.
# Instance 1 built from a recipe has no such file, and every run of its installer calls `docker build`.
# An image pulled by name (INSTANCE_IMAGE) has one for any instance: the same name is not pulled again.
image_from_pool() { [ -n "$INSTANCE" ] && [ "$(cut -d ' ' -f 1 "$DIR/image.built" 2>/dev/null || true)" = pool ]; }
image_is_built() {       # [DIRECTORY of the build files to compare: default $DIR]
  local id stamp
  { [ -n "$INSTANCE" ] || [ -n "$PREBUILT" ]; } && [ -f "$DIR/image.built" ] || return 1
  stamp=$(cat "$DIR/image.built")
  if image_from_pool; then
    declare -F image_layers >/dev/null || return 1
    [ "$(image_layers "$IMAGE" 2>/dev/null || true)" = "${stamp#pool }" ]
    return
  fi
  id=$(image_id)
  [ -n "$id" ] && [ "$stamp" = "$(image_inputs "${1:-$DIR}") $id" ]
}

build_image() {
  local id have
  if [ "$KEEP_IMAGE" -eq 1 ]; then        # a refresh: the image and its record are not this run's
    say "Keeping the image $IMAGE exactly as it is (--keep-image): not building"
    return 0
  fi
  if [ -n "$IMAGE_FROM_POOL" ]; then      # a join loaded the pool's image and says which: nothing is built here
    have=$(image_layers "$IMAGE" 2>/dev/null || true)
    [ -n "$have" ] || die "--image-from-pool: there is no image $IMAGE on this host; nothing was built"
    [ "$have" = "$IMAGE_FROM_POOL" ] \
      || die "--image-from-pool: the image $IMAGE on this host does not have the layers given ($have here); nothing was built"
    say "The image $IMAGE came from this box's pool: not building it"
    printf 'pool %s\n' "$IMAGE_FROM_POOL" > "$DIR/image.built"
    echo "recorded in $DIR/image.built; a later run keeps this image whatever the build files say (delete that file to build here)"
    return 0
  fi
  if image_is_built; then
    if image_from_pool; then
      say "The image $IMAGE is this box's pool's, loaded by a join: keeping it, not building"
      echo "($DIR/image.built; delete that file to build the image here instead)"
    else
      if [ -n "$PREBUILT" ]; then
        say "The image $IMAGE is already pulled from $PREBUILT: not pulling it again"
      else
        say "The image $IMAGE is already built from these build files: not building it again"
      fi
      echo "image $(image_id) ($DIR/image.built)"
    fi
    return 0
  fi
  rm -f "$DIR/image.built"
  if [ -n "$PREBUILT" ]; then             # the instance names an image to pull: under the local tag, as if built
    say "Pulling $PREBUILT as $IMAGE: nothing is built"
    docker pull "$PREBUILT" || die "could not pull $PREBUILT (INSTANCE_IMAGE)"
    docker tag "$PREBUILT" "$IMAGE" || die "could not tag $PREBUILT as $IMAGE"
    id=$(image_id)
    [ -n "$id" ] || die "the pull ended, but there is no image $IMAGE"
    printf '%s %s\n' "$(image_inputs)" "$id" > "$DIR/image.built"
    return 0
  fi
  say "Building $IMAGE from $DOCKERFILE (a first build of Lean and its libraries takes a while)"
  build_with_heartbeat "$DIR/build.log" --build-arg "BUILD_JOBS=$BUILD_JOBS" -f "$DIR/$DOCKERFILE" \
    -t "$IMAGE" "$DIR"
  [ -n "$INSTANCE" ] || return 0
  id=$(image_id)
  [ -n "$id" ] || die "the build ended, but there is no image $IMAGE; the full log is $DIR/build.log"
  printf '%s %s\n' "$(image_inputs)" "$id" > "$DIR/image.built"
}

# ------------------------------------------------------------------------------------------ pre-flight
say "Pre-flight (${INSTANCE:-instance 1})"
command -v docker >/dev/null || die "docker is not installed"
# Docker from the snap package cannot mount $DIR into a container: unsupported, and said before anything else.
if declare -F docker_is_snap >/dev/null && docker_is_snap; then die "$SNAP_DOCKER_UNSUPPORTED"; fi
docker info >/dev/null 2>&1 || die "the docker daemon is not reachable"
# A missing compose plugin is added, not reported: at the END of this pre-flight, once every
# other check has passed, so a host that fails one of them has nothing installed on it.
COMPOSE_MISSING=0
if ! docker compose version >/dev/null 2>&1; then
  declare -F ensure_compose_plugin >/dev/null || die "the docker compose plugin is not installed"
  COMPOSE_MISSING=1
  echo "the docker compose plugin is not installed: the install adds it once the checks below have passed"
fi
# --keep-image (a refresh of a box that is in a pool): the image this installer runs the server from must be
# the one that is here. A box whose pool has moved to another image leaves the pool and joins again.
if [ "$KEEP_IMAGE" -eq 1 ] && [ -z "$(image_id)" ]; then
  die "--keep-image: there is no image $IMAGE on this host, and this installer runs the server from that tag. Nothing was changed."
fi
for file in ${BUILD_FILES[@]+"${BUILD_FILES[@]}"}; do
  [ -f "$SITE/$file" ] || die "missing $SITE/$file, which the instance's recipe names (INSTANCE_BUILD_FILES)"
done
[ -f "$SRC/kimina-ctl" ] || die "missing $SRC/kimina-ctl - run this from the repo checkout"
# The compose files are the lean-pool project's own (deploy/lean-server/), for every instance.
compose_source server >/dev/null
if [ "$FIREWALL" -eq 1 ] || [ "$TLS" -eq 1 ]; then
  [ -f "$SRC/kimina-firewall" ] || die "missing $SRC/kimina-firewall - run this from the repo checkout"
fi
# A named instance compiles linarith's search step from its own image, with the lean-pool project's make.sh.
if [ -n "$INSTANCE" ] && declare -F install_compiled_search >/dev/null; then
  project=$(lean_pool_source) && [ -f "$project/$LEAN_NATIVE_MAKE" ] \
    || die "missing $LEAN_NATIVE_MAKE: $LEAN_POOL_SOURCE_MISSING"
fi
# Every instance asks its own image whether each worker can have a sandbox of its own, with its make.sh.
if declare -F install_worker_sandbox >/dev/null; then
  project=$(lean_pool_source) && [ -f "$project/$LEAN_SANDBOX_MAKE" ] \
    || die "missing $LEAN_SANDBOX_MAKE: $LEAN_POOL_SOURCE_MISSING"
fi
# The server's container has an init process. Where the image is already on
# this host (a re-run, a refresh), a Docker that cannot start such a container is found out HERE, before
# anything is written: otherwise the running server would be replaced by one that does not start.
if declare -F init_starts >/dev/null && [ -n "$(image_id)" ] && ! init_starts "$IMAGE"; then
  die "$(init_unsupported "$IMAGE")"
fi

# The free disk this run needs where Docker keeps its images: a build's, unless nothing will be built (the
# image is the pool's, or is already built from these very build files), or the caller says (a join that
# loads the pool's image passes the load's need).
NEED_GB=$MIN_FREE_GB; NEED_FOR="for a build of the image"
if [ -n "$MIN_FREE_GB_GIVEN" ]; then
  NEED_GB=$MIN_FREE_GB_GIVEN; NEED_FOR="(the caller's figure: a join that loads its pool's image needs room for the file and the image, not for a build)"
elif [ -n "$IMAGE_FROM_POOL" ] || [ "$KEEP_IMAGE" -eq 1 ] || image_is_built "$SITE"; then
  NEED_GB=2; NEED_FOR="(the image is already here: nothing is built)"
fi
# Free space where Docker keeps its images. Where that directory is not in this system's view (Docker
# Desktop, also under WSL, keeps it inside its own VM) it cannot be measured from here: said, not died on.
docker_root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)
free_gb=""
if [ -n "$docker_root" ] && [ -d "$docker_root" ]; then
  free_gb=$({ df -BG --output=avail "$docker_root" 2>/dev/null || true; } | tail -n 1 | tr -dc '0-9')
fi
if [ -n "$free_gb" ]; then
  echo "docker root $docker_root: ${free_gb} GB free"
  [ "$free_gb" -ge "$NEED_GB" ] || die "need at least $NEED_GB GB free under $docker_root $NEED_FOR"
else
  warn "COULD NOT CHECK free disk space: Docker's data directory (${docker_root:-not reported}) is not a directory this system can see (Docker Desktop, also under WSL, keeps it in its own VM). This install needs $NEED_GB GB there ($NEED_FOR): check it in Docker's own settings. Going on without that check."
fi

echo "memory: $(free -g | awk '/^Mem:/ {print $2" GB total, "$7" GB available"}')"
echo "cpu microcode: $(grep -m1 microcode /proc/cpuinfo | awk '{print $3}')"

# The port may be held by this instance's own server, or by its TLS front; by anything else it is taken.
# On a box an older installer set up, they run under their older names until the move below.
# (Asked of Docker for one container: a listing piped into `grep -q` can fail under pipefail.)
runs() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" = true ]; }
if [ -f "$DIR/kimina.env" ] && [ -z "$(setting_in "$DIR/kimina.env" COMPOSE_PROJECT_NAME)" ]; then old_instance "$DIR"; fi
if ! runs "$CONTAINER" && ! runs "$FRONT_CONTAINER" && ! runs "${OLD_SERVER:-$CONTAINER}" \
   && ! runs "${OLD_FRONT:-$FRONT_CONTAINER}"; then
  if ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
    die "port $PORT is already in use; pass --port N"
  fi
fi
# The usage port likewise: held by this instance's own agent or front, or free.
if [ "$AGENT" -eq 1 ] && ! runs "$AGENT_CONTAINER" && ! runs "$FRONT_CONTAINER" \
   && ! runs "${OLD_AGENT:-$AGENT_CONTAINER}" && ! runs "${OLD_FRONT:-$FRONT_CONTAINER}"; then
  if ss -ltnH "sport = :$AGENT_PORT" 2>/dev/null | grep -q .; then
    die "port $AGENT_PORT (the usage agent's: the Lean port + 200) is already in use; pass --port N to move both"
  fi
fi
# The container's own network must not collide with one the host already routes (other than ours). Checked
# here, before anything is written, as well as where the setting is read below.
wanted_net=$DEFAULT_NET
if [ -f "$DIR/kimina.env" ]; then
  wanted_net=$(setting_moved_in "$DIR/kimina.env" LEAN_SERVER_SUBNET KIMINA_SUBNET)
  wanted_net=${wanted_net:-$DEFAULT_NET}
fi
if { ip -4 route show 2>/dev/null || true; } | awk '{print $1}' | grep -qx "$wanted_net" \
   && ! docker network inspect "${PROJECT}_default" >/dev/null 2>&1 \
   && ! docker network inspect "${OLD_NETWORK:-${PROJECT}_default}" >/dev/null 2>&1; then
  die "the host already routes $wanted_net, which this server's own network would use; put LEAN_SERVER_SUBNET=<a free /24> in $DIR/kimina.env (make the file if it is not there), then run this again"
fi

if [ "$FIREWALL" -eq 1 ] && [ -z "$SUBNET" ]; then
  lan_dev=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
  [ -n "$lan_dev" ] && SUBNET=$(ip -4 route show dev "$lan_dev" scope link | awk '$1 ~ /\// {print $1; exit}')
  [ -n "$SUBNET" ] || die "could not detect the LAN subnet; pass --subnet CIDR (e.g. 192.168.50.0/24) or --no-firewall"
  echo "LAN subnet: $SUBNET (interface $lan_dev)"
fi
[ -z "$SUBNET" ] || [[ "$SUBNET" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "--subnet must be CIDR, e.g. 192.168.50.0/24"
# The firewall rules need iptables and systemd: a host without them is told now, not after the build.
[ "$FIREWALL" -eq 0 ] || require_firewall_tools

if [ "$DOOR" -eq 1 ]; then
  [ -n "$DOOR_USER" ] || die "cannot tell which account gets the door key; pass --door-user USER or --no-door"
  [ -n "$(door_home "$DOOR_USER")" ] || die "no such user: $DOOR_USER"
fi

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  if [ "$COMPOSE_MISSING" -eq 1 ]; then
    # Honest about the one thing a pre-flight cannot know without installing it.
    echo "pre-flight passed, with one thing left to the install: the docker compose plugin is missing here, and the install adds it first (this host's package manager, else the pinned release binary). Whether that works is known only then. Nothing was installed."
  else
    echo "pre-flight passed: this host can take the install. Nothing was installed."
  fi
  exit 0
fi

if [ "$BUILD_ONLY" -eq 1 ]; then
  install_build_files
  build_image
  echo
  echo "BUILT $IMAGE, and nothing else: no key, no settings, no certificate, no container, no firewall rule, no door."
  [ "$COMPOSE_MISSING" -eq 0 ] || echo "  still missing   the docker compose plugin: the install adds it"
  if [ -n "$INSTANCE" ]; then
    echo "  in $DIR   the build files, build.log and image.built"
    echo "  next       the install (the same command without --build-only) finds this image and does not build it again"
  else                           # instance 1 keeps no stamp: its install calls `docker build`, and the builder's cache answers
    echo "  in $DIR   the build files and build.log"
    echo "  next       the install (the same command without --build-only); the builder's cache answers its build"
  fi
  exit 0
fi

if [ "$COMPOSE_MISSING" -eq 1 ]; then
  ensure_compose_plugin \
    || die "the docker compose plugin is not installed and could not be added: $COMPOSE_PLUGIN_FAILURE. Nothing else was installed."
fi
# A box an older installer set up moves to Compose's names now, all of it (pool-lib.sh), before this run
# changes anything else. A box that has moved passes straight through.
move_to_compose_names

# ------------------------------------------------------------------------------- files and settings
say "Installing files into $DIR"
install_site_env             # the site's names, for the programs installed on this host (kimina-ctl, lean-pool-ctl)
install_build_files
install_compose_files "$DIR" server     # unchanged; kimina.env says which of them the instance runs

set_env() {   # set_env KEY VALUE : replace or append one line of kimina.env
  if grep -q "^$1=" "$DIR/kimina.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/kimina.env"; else echo "$1=$2" >> "$DIR/kimina.env"; fi
}
default_env() {   # default_env KEY VALUE : only when the key is absent (keeps the operator's tuning)
  grep -q "^$1=" "$DIR/kimina.env" || echo "$1=$2" >> "$DIR/kimina.env"
}

if [ ! -f "$DIR/kimina.env" ]; then
  ( umask 077; : > "$DIR/kimina.env" )
  echo "LEAN_SERVER_API_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$DIR/kimina.env"
  echo "generated a new API key"
else
  echo "keeping the existing API key and settings"
fi
chown root:root "$DIR/kimina.env"; chmod 0600 "$DIR/kimina.env"
# Which project, and which of its files: compose.yaml alone until the agent, the TLS front or the compiled
# search step adds its overlay. Never reset here: a run that stops halfway must not drop a running box's.
set_env COMPOSE_PROJECT_NAME "$PROJECT"
default_env COMPOSE_FILE compose.yaml
set_env LEAN_SERVER_IMAGE "$IMAGE"
set_env LEAN_SERVER_PUBLISHED_PORT "$PORT"
default_env LEAN_SERVER_MAX_REPLS "$DEFAULT_REPLS"
default_env LEAN_SERVER_MAX_REPL_USES 200
default_env LEAN_SERVER_MAX_REPL_MEM "$DEFAULT_REPL_MEM"
default_env LEAN_SERVER_MAX_WAIT 120
default_env LEAN_SERVER_MEM_LIMIT 36g
default_env LEAN_SERVER_LOG_LEVEL WARNING
default_env LEAN_SERVER_CPUS "$DEFAULT_CPUS"
default_env LEAN_SERVER_SUBNET "$DEFAULT_NET"
CONTAINER_NET=$(setting_in "$DIR/kimina.env" LEAN_SERVER_SUBNET)
[[ "$CONTAINER_NET" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "LEAN_SERVER_SUBNET in $DIR/kimina.env must be CIDR"
# The container's own network must not collide with one the host already routes (other than ours).
if ip -4 route show | awk '{print $1}' | grep -qx "$CONTAINER_NET" \
   && ! docker network inspect "${PROJECT}_default" >/dev/null 2>&1; then
  die "the host already routes $CONTAINER_NET; set LEAN_SERVER_SUBNET in $DIR/kimina.env to a free /24"
fi
[ -z "$REPLS" ] || set_env LEAN_SERVER_MAX_REPLS "$REPLS"
[ -z "$MEM_LIMIT" ] || set_env LEAN_SERVER_MEM_LIMIT "$MEM_LIMIT"
[ -z "$CPUS" ] || set_env LEAN_SERVER_CPUS "$CPUS"
if [ -n "$POOL_KEY" ]; then
  # Not through set_env: sed would be handed the key as an argument, and a command line is visible to every
  # user of the box. grep is given no secret, and printf is the shell's own.
  other_settings=$(grep -v '^LEAN_SERVER_API_KEY=' "$DIR/kimina.env" || true)
  ( umask 077
    printf 'LEAN_SERVER_API_KEY=%s\n' "$POOL_KEY" > "$DIR/kimina.env.new"       # the key's place: the first line
    [ -z "$other_settings" ] || printf '%s\n' "$other_settings" >> "$DIR/kimina.env.new" )
  chown root:root "$DIR/kimina.env.new"; chmod 0600 "$DIR/kimina.env.new"
  mv -f "$DIR/kimina.env.new" "$DIR/kimina.env"
  echo "installed the API key from $API_KEY_FILE (this server answers to its pool's key)"
fi

# ---------------------------------------------------------------------------------------------- build
build_image

# --------------------------------------------------------------------------------------------- start
# The server's control group, decided BEFORE its container is made. A box
# that cannot keep one is told so, and its server starts where Docker puts every container.
if declare -F install_cpu_priority >/dev/null; then
  install_cpu_priority "$DIR" "$INSTANCE" "$IMAGE" "$CPU_PRIORITY"
fi
# Which program a worker is, decided BEFORE the container is made too: on a named instance,
# linarith's search step is compiled from this image's own C files and checked here, and only a library that
# passed is given to the server. A box where that fails runs the plain REPL, and is told.
if declare -F install_compiled_search >/dev/null; then
  install_compiled_search "$DIR" "$INSTANCE" "$IMAGE"
fi
# Whether its root filesystem is read-only, decided BEFORE the container is made again too: from what
# the running server has written, so a box whose server writes elsewhere keeps it writable, and is told.
if declare -F install_read_only_root >/dev/null; then
  install_read_only_root "$DIR" "$INSTANCE"
fi
# And whether each worker starts in a sandbox of its own: only where a container of this image made one.
if declare -F install_worker_sandbox >/dev/null; then
  install_worker_sandbox "$DIR" "$INSTANCE" "$IMAGE"
fi
# The usage agent and the TLS front are services of the instance's own project: their settings and overlays
# come first, and the project starts once.
agent_subnet=""
if [ "$AGENT" -eq 1 ]; then
  build_lean_pool_image
  [ "$FIREWALL" -eq 0 ] || agent_subnet=$SUBNET
elif [ -n "$INSTANCE" ]; then
  set_overlay "$DIR/kimina.env" compose.agent.yaml off
fi
if [ "$TLS" -eq 1 ]; then
  # The front publishes the Lean port and the usage port; the server and the agent run behind it, unpublished.
  install_tls_front "$DIR" "$INSTANCE" "$SERVER_NAME" "$TLS_SOURCE" "$POOL_LEAN_TIMEOUT" "$POOL_SERVER_WAIT" "$agent_subnet"
else
  if [ "$AGENT" -eq 1 ]; then
    say "Usage agent: reports this box's CPU and RAM headroom to the pool's proxy"
    install_usage_agent "$DIR" "$INSTANCE" "$AGENT_PORT" "$agent_subnet"
  fi
  say "Starting the service"
  compose up -d
fi

# ------------------------------------------------------------------------------------------ firewall
if [ "$FIREWALL" -eq 1 ]; then
  say "Firewall: port $PORT reachable from $SUBNET and the docker bridges only; the container may not open connections"
  printf 'KIMINA_FW_PORT=%s\nKIMINA_FW_SUBNET=%s\nKIMINA_FW_CONTAINER_NET=%s\n' "$PORT" "$SUBNET" "$CONTAINER_NET" > "$DIR/firewall.conf"
  # Behind a TLS front: the front's fixed address is the one exception to the egress block.
  [ "$TLS" -eq 0 ] || echo "KIMINA_FW_FRONT_ADDRESS=$(setting_in "$DIR/kimina.env" LEAN_SERVER_FRONT_ADDRESS)" >> "$DIR/firewall.conf"
  install_kimina_firewall      # /usr/local/sbin/kimina-firewall, from the file beside this one
  cat > "/etc/systemd/system/$FW_UNIT" <<EOF
[Unit]
Description=Limit the Kimina Lean Server ${INSTANCE:-instance 1} port to the LAN ($SITE_LABEL)
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/kimina-firewall apply $INSTANCE
ExecStop=/usr/local/sbin/kimina-firewall remove $INSTANCE

[Install]
WantedBy=multi-user.target docker.service
EOF
  systemctl daemon-reload
  if systemctl enable "$FW_UNIT" >/dev/null 2>&1 && systemctl restart "$FW_UNIT"; then
    echo "DOCKER-USER rule applied and enabled at boot ($FW_UNIT)"
  else
    warn "could not apply the DOCKER-USER rule (see: systemctl status $FW_UNIT). The API key still protects the port."
  fi
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw allow from "$SUBNET" to any port "$PORT" proto tcp comment "$SITE_LABEL kimina${INSTANCE:+ $INSTANCE}" >/dev/null
    echo "ufw rule added: allow from $SUBNET to port $PORT/tcp"
  else
    echo "ufw is not active; no ufw rule added"
  fi
else
  warn "--no-firewall: port $PORT is reachable from every network this host is on; only the API key protects it"
fi

# ---------------------------------------------------------------------------------------------- door
install -o root -g root -m 0755 "$SRC/kimina-ctl" /usr/local/bin/kimina-ctl
if [ "$DOOR" -eq 1 ]; then
  say "Door: $DOOR_USER may run $DOOR_PROGRAM as root, and nothing else, via one ssh key"
  sudoers_tmp=$(mktemp)
  printf 'Defaults!%s env_keep += "SSH_ORIGINAL_COMMAND SSH_CLIENT"\n%s ALL=(root) NOPASSWD: %s\n' "$DOOR_PROGRAM" "$DOOR_USER" "$DOOR_PROGRAM" > "$sudoers_tmp"
  visudo -cf "$sudoers_tmp" >/dev/null || { rm -f "$sudoers_tmp"; die "generated sudoers rule failed validation"; }
  install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/kimina-ctl
  rm -f "$sudoers_tmp"

  home=$(door_home "$DOOR_USER")
  keys="$home/.ssh/authorized_keys"
  install -d -o "$DOOR_USER" -g "$(id -gn "$DOOR_USER")" -m 0700 "$home/.ssh"
  touch "$keys"; chown "$DOOR_USER:$(id -gn "$DOOR_USER")" "$keys"; chmod 0600 "$keys"
  sed -i "/$SITE_DOOR_KEY_MARKER/d" "$keys"
  printf 'command="sudo -n %s",restrict %s\n' "$DOOR_PROGRAM" "$(tr -d '\n' < "$OPERATOR_KEY")" >> "$keys"
  echo "forced-command key added to $keys"
else
  rm -f /etc/sudoers.d/kimina-ctl
  echo "no door (no --operator-key, or --no-door): use 'sudo kimina-ctl <verb>' on this host"
fi

# -------------------------------------------------------------------------------------------- verify
say "Waiting for the server to become healthy"
became_healthy "$CONTAINER" || { compose logs --tail 40 >&2 || true; die "the server did not become healthy in 5 minutes"; }

say "Verifying: one valid and one invalid proof through the live server (the first check imports Mathlib)"
verdict="INSTALLED AND VERIFIED"
# A server that fails this check loses one protection at a time, the worker sandbox first and then the
# read-only root, each time made again and checked once more.
if declare -F confirm_lean_server >/dev/null; then
  confirm_lean_server "$DIR" "$INSTANCE" \
    || verdict="INSTALLED, BUT THE CHECK FAILED - see the output above and: sudo kimina-ctl ${CTL_PREFIX}logs 200"
else
  # shellcheck disable=SC2086
  /usr/local/bin/kimina-ctl $CTL_PREFIX check || verdict="INSTALLED, BUT THE CHECK FAILED - see the output above and: sudo kimina-ctl ${CTL_PREFIX}logs 200"
fi

shown_url="http://$(hostname):$PORT"
[ "$TLS" -eq 0 ] || shown_url="https://$(hostname):$PORT, mutual TLS as '$SERVER_NAME': only the pool's proxy is let in"
cat <<EOF

$verdict
  service     $CONTAINER, image $IMAGE
  url         $shown_url   (API key: sudo kimina-ctl ${CTL_PREFIX}show-key)
  settings    $DIR/kimina.env
  operate     sudo kimina-ctl ${CTL_PREFIX}status | check | logs [N|build] | up | down | restart | set-repls N | set-cpus N | set-mem SIZE${INSTANCE:+ | compiled-search on|off} | read-only on|off | sandbox on|off
EOF
[ -z "${COMPILED_SEARCH_SAID:-}" ] || echo "  search      $COMPILED_SEARCH_SAID"
[ -z "${READ_ONLY_SAID:-}" ] || echo "  read-only   $READ_ONLY_SAID"
[ -z "${WORKER_SANDBOX_SAID:-}" ] || echo "  sandbox     $WORKER_SANDBOX_SAID"
# The site's operating guide, where the site has one (a box that joined a pool has only the installer).
[ ! -f "$SITE/README.md" ] || echo "  guide       $SITE/README.md"
[ "$verdict" = "INSTALLED AND VERIFIED" ]
