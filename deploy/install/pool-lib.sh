#!/usr/bin/env bash
# pool-lib.sh - shared by install.sh (the site's values, the server's control group, the compiled search
# step, the read-only root, the worker sandbox, the compose plugin, and the usage agent and TLS front beside a
# Lean server),
# install-pool.sh (the pool's front door) and a join script (a new box). Sourced, never run.
#
# Expects the caller to have defined: SRC (this directory), say, warn, die.

# The pinned HAProxy image: the pool's proxy and every box's TLS front run the same one.
LEAN_POOL_HAPROXY_IMAGE=haproxy:3.0
# The only caller a box's TLS front lets in: the pool proxy's client certificate, by this name.
LEAN_POOL_PROXY_CLIENT_NAME=lean-pool-proxy
# Where HAProxy sees its certificates, in the pool's proxy and in a box's front alike.
LEAN_POOL_TLS_MOUNT=/etc/leanpool/tls
# A DNS name in lower case: a name clients use for the front door, and the pool host's own server name.
LEAN_POOL_DNS_NAME='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
# The name of a server that JOINS: one DNS label in lower case, at most 40 (the door's pattern for add-backend).
LEAN_POOL_SERVER_NAME='^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$'
# What a join bundle holds beside the lean-pool project and the site (pack_join_bundle): the instance installer
# and all it needs. No key.
LEAN_POOL_JOIN_FILES=(install.sh pool-lib.sh kimina-firewall kimina-ctl join_helper.py)

# ------------------------------------------------------------------------------------------------ the site
# A site is a directory of values: site.env, and one file
# per instance in instances/ (default.env: the instance with no name), with the image recipes those name. The
# installers READ them, never run them, and check the shape of every value. The programs installed on the host
# read the site's names from LEAN_POOL_SITE_ENV, which the installers write.
LEAN_POOL_SITE_ENV=/etc/lean-pool/site.env

# load_site DIR : SITE (an absolute path) and the values of DIR/site.env. SITE_PREFIX names the site's images
# (<SITE_PREFIX>/kimina:TAG, <SITE_PREFIX>/lean-pool:VERSION) and the directory of its helpers,
# /usr/local/lib/<SITE_PREFIX>. Containers, networks and volumes have Compose's own names (the compose files).
load_site() {
  [ -f "$1/site.env" ] || die "no site in $1: site.env is missing (--site DIR names the directory of the site's values)"
  SITE=$(cd "$1" && pwd)
  SITE_PREFIX=$(setting_in "$SITE/site.env" SITE_PREFIX)
  SITE_LABEL=$(setting_in "$SITE/site.env" SITE_LABEL)
  SITE_DOOR_KEY_MARKER=$(setting_in "$SITE/site.env" SITE_DOOR_KEY_MARKER)
  SITE_LAN_DOMAIN=$(setting_in "$SITE/site.env" SITE_LAN_DOMAIN)
  [[ "$SITE_PREFIX" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]] \
    || die "$SITE/site.env: SITE_PREFIX must be lower-case letters, digits, '_' and '-' (it begins every image name)"
  SITE_HELPER_DIR=/usr/local/lib/$SITE_PREFIX
  [[ "$SITE_LABEL" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || die "$SITE/site.env: SITE_LABEL must be letters, digits, '.', '_' and '-'"
  [[ "$SITE_DOOR_KEY_MARKER" =~ ^[A-Za-z0-9_@-]{1,64}$ ]] \
    || die "$SITE/site.env: SITE_DOOR_KEY_MARKER must be letters, digits, '_', '@' and '-' (the operator key's comment)"
  [[ "$SITE_LAN_DOMAIN" =~ $LEAN_POOL_DNS_NAME ]] || die "$SITE/site.env: SITE_LAN_DOMAIN must be a DNS name in lower case"
}

# install_site_env : the site's names where the installed programs read them (no secret: readable by all).
install_site_env() {
  install -d -o root -g root -m 0755 "${LEAN_POOL_SITE_ENV%/*}"
  printf '# The site this host was installed for, written by the lean-pool installer: what its programs name things by.\nSITE_PREFIX=%s\nSITE_LAN_DOMAIN=%s\n' \
    "$SITE_PREFIX" "$SITE_LAN_DOMAIN" > "$LEAN_POOL_SITE_ENV.new"
  chown root:root "$LEAN_POOL_SITE_ENV.new"; chmod 0644 "$LEAN_POOL_SITE_ENV.new"
  mv -f "$LEAN_POOL_SITE_ENV.new" "$LEAN_POOL_SITE_ENV"
}
remove_site_env() {
  rm -f "$LEAN_POOL_SITE_ENV"
  rmdir "${LEAN_POOL_SITE_ENV%/*}" 2>/dev/null || true
}

# The lean-pool project: beside this directory in a join bundle, else the repository this directory is in.
LEAN_POOL_SOURCE_MISSING="cannot find the lean-pool project: run this from a checkout of the lean-pool repository (or a join bundle)"
lean_pool_source() {
  local candidate
  for candidate in "$SRC/lean_pool" "$SRC/../.."; do
    if [ -f "$candidate/pyproject.toml" ] && [ -f "$candidate/deploy/Dockerfile" ]; then
      (cd "$candidate" && pwd); return 0
    fi
  done
  return 1
}

# ------------------------------------------------------------------------------------- the compose files
# A Lean server instance and a pool each run as ONE compose project, from the lean-pool project's own compose
# files, copied unchanged into the instance's or the pool's directory:
#   deploy/lean-server/  compose.yaml (the server) and its overlays: compose.agent.yaml (the usage agent),
#                        compose.tls.yaml (the TLS front; the server and the agent then publish nothing),
#                        compose.compiled-search.yaml (linarith's search step, compiled) and
#                        compose.worker-sandbox.yaml (every worker in a sandbox of its own)
#   deploy/pool/         compose.yaml (the proxy and the cache), compose.tls.yaml and compose.dns.yaml
# The env file beside them (kimina.env, pool.env) names the files the box runs, in order (COMPOSE_FILE), and the
# project (COMPOSE_PROJECT_NAME), and every program passes both to `docker compose` itself. Containers, networks
# and volumes have Compose's own names: lean-server-NAME-kimina-1, lean-server-NAME_default,
# lean-pool-NAME_cache-data.
LEAN_SERVER_COMPOSE_DIR=deploy/lean-server     # in the lean-pool project
LEAN_POOL_COMPOSE_DIR=deploy/pool
LEAN_SERVER_COMPOSE_FILES=(compose.yaml compose.agent.yaml compose.tls.yaml compose.compiled-search.yaml
  compose.worker-sandbox.yaml)
LEAN_POOL_COMPOSE_FILES=(compose.yaml compose.tls.yaml compose.dns.yaml)

# lean_server_project INSTANCE : the compose project of a Lean server instance (lean-server for instance 1).
lean_server_project() { echo "lean-server${1:+-$1}"; }

# install_compose_files DIR server|pool : the project's compose files of a Lean server or of a pool, copied
# into DIR unchanged (root-owned, 0644). All of them: COMPOSE_FILE says which run.
install_compose_files() {
  local dir=$1 from file
  from=$(compose_source "$2") || exit 1
  for file in $(compose_names "$2"); do
    install -o root -g root -m 0644 "$from/$file" "$dir/$file"
  done
}
compose_names() { if [ "$1" = pool ]; then echo "${LEAN_POOL_COMPOSE_FILES[*]}"; else echo "${LEAN_SERVER_COMPOSE_FILES[*]}"; fi; }
# compose_source server|pool : the lean-pool project's directory of those compose files, once each one is
# there (a pre-flight asks this before anything is written).
compose_source() {
  local project from file
  project=$(lean_pool_source) || die "$LEAN_POOL_SOURCE_MISSING"
  from=$project/$LEAN_SERVER_COMPOSE_DIR
  [ "$1" != pool ] || from=$project/$LEAN_POOL_COMPOSE_DIR
  for file in $(compose_names "$1"); do
    [ -f "$from/$file" ] || die "missing $from/$file: $LEAN_POOL_SOURCE_MISSING"
  done
  echo "$from"
}

# compose_in ENV_FILE ARGS... : `docker compose` for the project ENV_FILE names (COMPOSE_PROJECT_NAME), with each
# file it names (COMPOSE_FILE, beside it) passed by itself, and with ENV_FILE's settings.
compose_in() {
  local env=$1 dir file project
  local -a names files=()
  shift
  dir=$(dirname "$env")
  project=$(setting_in "$env" COMPOSE_PROJECT_NAME)
  IFS=: read -r -a names <<<"$(setting_in "$env" COMPOSE_FILE)"
  [ -n "$project" ] && [ "${#names[@]}" -gt 0 ] \
    || die "$env names no compose project or no compose files (COMPOSE_PROJECT_NAME, COMPOSE_FILE)"
  for file in "${names[@]}"; do files+=(-f "$dir/$file"); done
  docker compose --project-name "$project" --env-file "$env" "${files[@]}" "$@"
}

# set_overlay ENV_FILE FILE on|off : COMPOSE_FILE with the Lean server's overlay FILE in it, or without it. The
# files keep one order (LEAN_SERVER_COMPOSE_FILES): the server's own first, the worker sandbox last.
set_overlay() {
  local env=$1 overlay=$2 state=$3 now file list=compose.yaml
  now=":$(setting_in "$env" COMPOSE_FILE):"
  for file in "${LEAN_SERVER_COMPOSE_FILES[@]:1}"; do
    if [ "$file" = "$overlay" ]; then
      [ "$state" = off ] || list=$list:$file
    elif [[ "$now" == *":$file:"* ]]; then
      list=$list:$file
    fi
  done
  set_env_in "$env" COMPOSE_FILE "$list"
}

# has_overlay ENV_FILE FILE : whether the instance runs the overlay FILE.
has_overlay() { [[ ":$(setting_in "$1" COMPOSE_FILE):" == *":$2:"* ]]; }

# lean_container ENV_FILE : the Lean server's container of the instance whose settings ENV_FILE holds.
lean_container() { echo "$(setting_in "$1" COMPOSE_PROJECT_NAME)-kimina-1"; }

# Build the one image that runs the cache, the usage agent, the join service and the pool's tools. Prints
# nothing but the tag.
LEAN_POOL_IMAGE=""
# Files and directories for ONE container's group. `install -g N` looks N up in the host's group database,
# and the `install` of uutils coreutils (Ubuntu 25.10 and later) refuses a number that has no entry there
# ("install: invalid group: '99'": the first TLS install on such a host stopped on it). A container
# image's group has no entry on the host. `chown` takes any number, so each is made for root alone and
# handed to the group afterwards; an existing directory is never closed while a container may be reading it.
install_for_gid() {       # MODE GID SOURCE DESTINATION
  install -o root -g root -m 0600 "$3" "$4"
  chown root:"$2" "$4"
  chmod "$1" "$4"
}
install_dir_for_gid() {   # MODE GID DIRECTORY...
  local mode=$1 gid=$2 directory; shift 2
  for directory in "$@"; do
    [ -d "$directory" ] || install -d -o root -g root -m 0700 "$directory"
    chown root:"$gid" "$directory"
    chmod "$mode" "$directory"
  done
}

# build_with_heartbeat LOGFILE docker-build-args... : a long `docker build` with its whole output in LOGFILE and
# one line of it here every minute; dies with the log's tail if the build fails.
build_with_heartbeat() {
  local logfile=$1 progress=(); shift
  echo "full log: $logfile (a one-line heartbeat prints here every minute)"
  # --progress is BuildKit's: a Docker without the buildx component (Ubuntu's docker.io) builds
  # with the legacy builder, which refuses the flag and writes plain lines anyway.
  if docker build --help 2>/dev/null | grep -q -- '--progress'; then progress=(--progress=plain); fi
  docker build ${progress[@]+"${progress[@]}"} "$@" > "$logfile" 2>&1 &
  local pid=$! started
  started=$(date +%s)
  while kill -0 "$pid" 2>/dev/null; do
    sleep 60
    kill -0 "$pid" 2>/dev/null || break
    printf '  [%3d min] %s\n' "$(( ($(date +%s) - started) / 60 ))" "$(tail -n 1 "$logfile" | cut -c1-140)"
  done
  if ! wait "$pid"; then
    tail -n 40 "$logfile" >&2
    die "the image build failed; the full log is $logfile"
  fi
}

build_lean_pool_image() {
  local source version
  source=$(lean_pool_source) || die "$LEAN_POOL_SOURCE_MISSING"
  version=$(sed -n 's/^version *= *"\([0-9A-Za-z.+-]*\)".*/\1/p' "$source/pyproject.toml" | head -n 1)
  [ -n "$version" ] || die "cannot read the version from $source/pyproject.toml"
  LEAN_POOL_IMAGE="$SITE_PREFIX/lean-pool:$version"
  say "Building $LEAN_POOL_IMAGE (the cache, the usage agent and the pool's tools)"
  docker build -q -f "$source/deploy/Dockerfile" -t "$LEAN_POOL_IMAGE" "$source" >/dev/null \
    || die "building $LEAN_POOL_IMAGE failed (docker build -f $source/deploy/Dockerfile $source)"
}

# The group an image's processes run as, READ FROM THE IMAGE. A file a container must read is given to
# exactly that group; nothing here assumes which number an image uses. (A root-only directory handed to
# HAProxy's unprivileged user stops an install.)
image_group_id() {
  local gid
  gid=$(docker run --rm --network none --entrypoint id "$1" -g 2>/dev/null) || return 1
  [[ "$gid" =~ ^[0-9]{1,10}$ ]] || return 1
  echo "$gid"
}

# haproxy_has_exporter IMAGE : whether the HAProxy in IMAGE was built with its Prometheus exporter (USE_PROMEX),
# which is what the pool's proxy answers `GET /metrics` with. An HAProxy without it refuses the
# pool's config, so the image is asked BEFORE anything of the pool is built or written: `haproxy -vv` lists
# what it was built with, the exporter under `Available services`. The answer is taken whole before it is
# searched: a `grep -q` at the end of a pipe can leave first and fail the pipe under pipefail.
haproxy_has_exporter() {
  local built
  built=$(docker run --rm --network none "$1" haproxy -vv 2>/dev/null </dev/null) || return 1
  grep -q '^Available services :.*prometheus-exporter' <<<"$built"
}
# What a host is told whose HAProxy image has none (the caller has changed nothing of the pool yet).
exporter_missing() {
  echo "the HAProxy in $1 was built without its Prometheus exporter (\`haproxy -vv\` does not list prometheus-exporter under its services; the official haproxy image has it), and the pool's proxy answers GET /metrics with it: that HAProxy would refuse the pool's config. Nothing was changed: no image was built, no file was written, and no container or control script was touched."
}

# init_starts IMAGE : whether this host's Docker starts a container of IMAGE that has an init process, under the
# Lean server's own hardening (compose.yaml: `init: true`, no capabilities, no new privileges).
# Docker puts its own docker-init binary into such a container, and a Docker whose package ships none
# refuses to start it. Asked with a container that does nothing, so that the answer is known BEFORE a running
# server is replaced by one that would not start. On a refusal Docker's own last line is in INIT_REFUSAL.
INIT_REFUSAL=""
init_starts() {
  local said
  said=$(docker run --rm --init --cap-drop ALL --security-opt no-new-privileges:true --network none \
           --entrypoint true "$1" 2>&1 </dev/null) && return 0
  INIT_REFUSAL=$({ tail -n 1 <<<"$said" || true; } | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-200)
  return 1
}
# What a host is told whose Docker cannot do that (the caller has changed nothing yet).
init_unsupported() {
  echo "this host's Docker cannot start $1 with an init process, which the Lean server's compose file asks for (without one every worker the server closes stays as a zombie until the container is full): ${INIT_REFUSAL:-docker run --init failed}. Docker's own packages bring docker-init (a distribution's Docker may want its tini package beside it); then run this again. Nothing was changed: a running server is as it was."
}

# ------------------------------------------------ the Lean server's control group
# The Lean server's container runs in a control group of its own, under ONE top-level group for every Lean
# server of the box whose CPU weight is the lowest there is. Whatever else runs on the box (a batch job, a
# person's program, in a container or not) then gets a contended CPU first and the Lean workers get what is
# left; with the CPUs free the server uses them up to its ceiling, as before. systemd keeps the groups
# ("slices") and Docker is told to start the container in one:
#   leanpool.slice                     CPUWeight=1: the unit file written here
#     leanpool-<instance>.slice        one Lean server ("main" for instance 1)
#   leanpoolnormal-<instance>.slice    `--cpu-priority normal`: under leanpoolnormal.slice, which has no unit
#                                      file and so the box's ordinary weight
# The usage agent reads that group's cpu.stat for the Lean server's OWN CPU use, which is what lets it say
# how many checks fit beside the box's other work: its compose files mount the box's control
# groups read-only at LEAN_POOL_AGENT_CGROUPS.
LEAN_POOL_SLICE_UNIT=/etc/systemd/system/leanpool.slice
LEAN_POOL_SLICE_WEIGHT=/sys/fs/cgroup/leanpool.slice/cpu.weight
LEAN_POOL_AGENT_CGROUPS=/host-cgroup
CPU_PRIORITY_REFUSAL=""
CPU_PRIORITY_INSTALLED=""        # set once install_cpu_priority has run in this process

# cpu_priority_parent INSTANCE PRIORITY : the control group Docker is given for that server's container.
cpu_priority_parent() {
  local name=${1:-main}
  if [ "$2" = normal ]; then echo "leanpoolnormal-$name.slice"; else echo "leanpool-$name.slice"; fi
}

# cpu_priority_possible : whether this box keeps containers in control groups that systemd makes, version 2.
# Asked of Docker and of the box, with nothing changed. On a refusal the reason is in CPU_PRIORITY_REFUSAL.
cpu_priority_possible() {
  local kinds
  CPU_PRIORITY_REFUSAL=""
  # The same question require_firewall_tools asks: Docker inside WSL often has no systemd.
  if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
    CPU_PRIORITY_REFUSAL="this box does not run systemd, which keeps the control group (Docker inside WSL?)"; return 1
  fi
  kinds=$(docker info -f '{{.CgroupDriver}} {{.CgroupVersion}}' 2>/dev/null || true)
  if [ "$kinds" != "systemd 2" ]; then
    CPU_PRIORITY_REFUSAL="Docker here keeps its containers' control groups as '${kinds:-not reported}', and this needs 'systemd 2' (systemd's own groups, version 2)"; return 1
  fi
}

# cpu_priority_starts IMAGE PARENT : whether this box's Docker starts a container of IMAGE in control group
# PARENT, under the Lean server's own hardening. Asked with a container that does nothing, like init_starts,
# so that the answer is known BEFORE a running server is replaced by one that would not start.
cpu_priority_starts() {
  local said
  said=$(docker run --rm --cgroup-parent "$2" --init --cap-drop ALL --security-opt no-new-privileges:true \
           --network none --entrypoint true "$1" 2>&1 </dev/null) && return 0
  CPU_PRIORITY_REFUSAL="Docker did not start a container in the control group $2: $({ tail -n 1 <<<"$said" || true; } | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-200)"
  return 1
}

# install_cpu_priority INSTANCE_DIR INSTANCE IMAGE [PRIORITY] : decide the control group of one Lean server
# instance and write it into the instance's settings, BEFORE its container is (re)made. PRIORITY is `lowest`
# or `normal`; empty keeps what the settings say (`lowest` on a first install). Nothing here stops an
# install: on a box that cannot do it (no systemd, a Docker that keeps its own groups or version 1, a group
# that does not carry the weight) the settings are left empty, the container starts where it always did,
# the agent states no slots, and the reason is said once here and by `kimina-ctl status` afterwards.
install_cpu_priority() {
  local dir=$1 instance=$2 image=$3 asked=${4:-} env=$1/kimina.env priority parent top weight
  CPU_PRIORITY_INSTALLED=1
  # The box's limit on checks beside other work: 1 on a box that has none
  # yet, and never touched once it is there. An EMPTY value is "no limit", set on purpose (set-beside none).
  grep -q '^LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK=' "$env" || set_env_in "$env" LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK 1
  priority=${asked:-$(setting_in "$env" LEAN_SERVER_CPU_PRIORITY)}
  priority=${priority:-lowest}
  set_env_in "$env" LEAN_SERVER_CPU_PRIORITY "$priority"
  parent=$(cpu_priority_parent "$instance" "$priority")
  top=${parent%%-*}.slice
  if cpu_priority_possible; then
    if [ "$priority" = lowest ]; then
      if [ "$(cat "$LEAN_POOL_SLICE_UNIT" 2>/dev/null || true)" != "$(cpu_priority_unit)" ]; then
        cpu_priority_unit > "$LEAN_POOL_SLICE_UNIT.new"
        chown root:root "$LEAN_POOL_SLICE_UNIT.new"; chmod 0644 "$LEAN_POOL_SLICE_UNIT.new"
        mv -f "$LEAN_POOL_SLICE_UNIT.new" "$LEAN_POOL_SLICE_UNIT"
        systemctl daemon-reload || true
      fi
      systemctl start leanpool.slice || true
    fi
    if cpu_priority_starts "$image" "$parent" && [ "$priority" = lowest ]; then
      weight=""
      [ ! -r "$LEAN_POOL_SLICE_WEIGHT" ] || weight=$(tr -d '[:space:]' < "$LEAN_POOL_SLICE_WEIGHT")
      [ "$weight" = 1 ] \
        || CPU_PRIORITY_REFUSAL="the control group leanpool.slice does not carry the lowest CPU weight ($LEAN_POOL_SLICE_WEIGHT reads '${weight:-nothing}', not 1)"
    fi
  fi
  if [ -n "$CPU_PRIORITY_REFUSAL" ]; then
    set_env_in "$env" LEAN_SERVER_CGROUP_PARENT ""
    set_env_in "$env" LEANPOOL_AGENT_LEAN_CPU_STAT_PATH ""
    warn "CPU priority '$priority' is NOT available on this box: $CPU_PRIORITY_REFUSAL. Everything else is installed. The Lean server runs at the box's ordinary CPU priority, where it always ran, and its usage agent states no open slots, so the pool may send this box as many checks as it has workers whatever else runs here (sudo kimina-ctl ${instance:+@$instance }status says so)."
    return 0
  fi
  set_env_in "$env" LEAN_SERVER_CGROUP_PARENT "$parent"
  set_env_in "$env" LEANPOOL_AGENT_LEAN_CPU_STAT_PATH "$LEAN_POOL_AGENT_CGROUPS/$top/$parent/cpu.stat"
  if [ "$priority" = lowest ]; then
    echo "CPU priority: lowest. The Lean server runs in the control group $parent (under leanpool.slice, CPU weight 1): other work on this box gets a contended CPU first."
  else
    echo "CPU priority: normal. The Lean server runs in the control group $parent at the box's ordinary CPU weight; its usage agent still reads that group to state open slots."
  fi
}

# The unit of the top-level group, as text.
cpu_priority_unit() {
  cat <<EOF
# Every Lean server of this box runs in a control group under this one (leanpool-<instance>.slice): written
# by the lean-pool installer (pool-lib.sh). The lowest CPU weight there is:
# whatever else runs on the box gets a contended CPU first.
[Unit]
Description=Lean servers of a lean-pool, at the lowest CPU priority ($SITE_LABEL)

[Slice]
CPUWeight=1
EOF
}

# remove_cpu_priority : the top-level group's unit goes with the box's last Lean server (uninstall).
remove_cpu_priority() {
  [ -f "$LEAN_POOL_SLICE_UNIT" ] || return 0
  rm -f "$LEAN_POOL_SLICE_UNIT"
  systemctl daemon-reload 2>/dev/null || true
  echo "removed $LEAN_POOL_SLICE_UNIT"
}

# ------------------------------------------- linarith's search step as compiled code
# Most of a pool's Lean time is one step of `linarith` and `nlinarith`: the search for the numbers that close
# a goal, which runs in Lean's interpreter because Mathlib is not shipped as native code. A box compiles that
# step from the C files its own copy of the image already holds, checks it, and only then lets the Lean
# server start its workers through a launcher that loads it. The image is not changed and nothing travels
# between boxes. What Lean accepts does not change: the kernel checks every proof as before. The lean-pool
# project's compiled-search/make.sh does all of it (docs/compiled-search.md), for the instance's directory:
#   <instance dir>/native/            mounted read-only at /opt/lean-native by compose.compiled-search.yaml
#     repl-native, repl-path          the launcher (the program a worker is started as), and the image's REPL
#     linarith-search.so              the library; `sources` and `built-from` tie it to the image's C files
#     state                           one line: `on ...` with what the box's own check measured, or `off REASON`
#   <instance dir>/native.built       what the library was made from: make.sh's files and the image's ID
#   <instance dir>/native.log         the last build and check, in full
#   <instance dir>/native.work.*      a build in progress; removed when it ends, and by the next run
LEAN_NATIVE_MAKE=deploy/lean-server/compiled-search/make.sh     # in the lean-pool project
LEAN_NATIVE_OVERLAY=compose.compiled-search.yaml
COMPILED_SEARCH_INSTALLED=""     # set once install_compiled_search has run in this process
COMPILED_SEARCH_SAID=""          # its one line, for the installer's last lines

# install_compiled_search INSTANCE_DIR INSTANCE IMAGE : make the library from IMAGE on this box, check it,
# and write into the instance's settings which program its workers are (whether COMPOSE_FILE holds
# compose.compiled-search.yaml), BEFORE its container is (re)made. Nothing here stops an install: a build or a
# check that fails leaves the server on the plain REPL, which is what it ran before, and says so. Instance 1
# has no compiled step.
install_compiled_search() {
  local dir=$1 instance=$2 image=$3 env=$1/kimina.env native=$1/native project said state reason
  COMPILED_SEARCH_INSTALLED=1
  [ -n "$instance" ] || return 0
  project=$(lean_pool_source) && [ -f "$project/$LEAN_NATIVE_MAKE" ] \
    || die "missing $LEAN_NATIVE_MAKE: $LEAN_POOL_SOURCE_MISSING"
  if [ "$(setting_in "$env" LEAN_SERVER_COMPILED_SEARCH)" = off ]; then
    set_overlay "$env" "$LEAN_NATIVE_OVERLAY" off
    COMPILED_SEARCH_SAID="COMPILED SEARCH STEP: off by this box's own setting (LEAN_SERVER_COMPILED_SEARCH=off in $env); 'sudo kimina-ctl @$instance compiled-search on' turns it on"
    echo "$COMPILED_SEARCH_SAID"
    return 0
  fi
  # make.sh says what it does as it goes, and its last line is the answer: on, or off and why. A make.sh that
  # stopped before it could say (it cannot run at all) leaves the server on the plain REPL too.
  said=$(mktemp)
  if bash "$project/$LEAN_NATIVE_MAKE" --image "$image" --box "$dir" --name "lean-server-$instance-native" \
       </dev/null | tee "$said"; then
    COMPILED_SEARCH_SAID=$(tail -n 1 "$said")
    set_overlay "$env" "$LEAN_NATIVE_OVERLAY" on
  else
    state=$(head -n 1 "$native/state" 2>/dev/null || true)
    case $state in
      "off "*) reason=${state#off } ;;
      *) reason="make.sh stopped: $(last_line_of "$said")" ;;
    esac
    set_overlay "$env" "$LEAN_NATIVE_OVERLAY" off
    COMPILED_SEARCH_SAID="COMPILED SEARCH STEP: OFF - $reason. The Lean server starts its workers as it did before (the plain REPL): every answer is the same, linarith's search is slower. Full log: $dir/native.log"
    warn "$COMPILED_SEARCH_SAID"
  fi
  rm -f "$said"
}

# ------------------------------------------- the Lean server's root filesystem read-only
# A check is a Lean file, and Lean runs programs while it reads one, so every check runs a program inside the
# Lean server's container. With its root filesystem read-only, a check can change neither Lean, Mathlib nor the
# REPL for the checks after it, nor fill the host's disk; /tmp, where Kimina writes its workers' error output,
# is a small filesystem in memory (the compose file). The switch is made only where the RUNNING server has been
# seen to write nothing else, and kept only where a real check then passes. A server that cannot have it stays
# writable, and the box says so: what is lost is the protection, never an answer.
#   LEAN_SERVER_READ_ONLY=off              in kimina.env: turned off by hand (kimina-ctl read-only off); every
#                                          run keeps it
#   LEAN_SERVER_ROOT_READ_ONLY=true|false  in kimina.env: what the compose file makes the container with
#   <instance dir>/read-only.state    one line: `on`, or `off REASON` when the installer turned it off
#   <instance dir>/read-only.diff     what `docker diff` said of the running server at the last reading
#   <instance dir>/read-only.outside  of that, every path a read-only root would have refused (when any was)
# The reading is the lean-pool project's read-only-check.sh, which says what it counts and why.
LEAN_READ_ONLY_CHECK=deploy/lean-server/read-only-check.sh     # in the lean-pool project
READ_ONLY_INSTALLED=""     # set once install_read_only_root has run in this process
READ_ONLY_SAID=""          # its one line, for the installer's last lines

# read_only_off INSTANCE_DIR REASON : the server is made writable, as it was before the read-only root, and the box says why.
read_only_off() {
  printf 'off %s\n' "$2" > "$1/read-only.state"
  chmod 0644 "$1/read-only.state"
  set_env_in "$1/kimina.env" LEAN_SERVER_ROOT_READ_ONLY false
}

# install_read_only_root INSTANCE_DIR INSTANCE : decide, BEFORE the Lean server's container is (re)made, whether
# its root filesystem is read-only, and write that into the instance's settings. Nothing here stops an install.
install_read_only_root() {
  local dir=$1 instance=$2 env=$1/kimina.env container made writes reason="" hint project checked=0 said
  READ_ONLY_INSTALLED=1
  container=$(lean_container "$env")
  hint="sudo kimina-ctl ${instance:+@$instance }read-only"
  if [ "$(setting_in "$env" LEAN_SERVER_READ_ONLY)" = off ]; then
    set_env_in "$env" LEAN_SERVER_ROOT_READ_ONLY false
    READ_ONLY_SAID="READ-ONLY ROOT: off by this box's own setting (LEAN_SERVER_READ_ONLY=off in $env); '$hint on' turns it on"
    echo "$READ_ONLY_SAID"
    return 0
  fi
  made=$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$container" 2>/dev/null || true)
  if [ "$made" = true ]; then
    set_env_in "$env" LEAN_SERVER_ROOT_READ_ONLY true
    printf 'on\n' > "$dir/read-only.state"
    chmod 0644 "$dir/read-only.state"
    READ_ONLY_SAID="READ-ONLY ROOT: on - the server already runs with it; not read again"
    echo "$READ_ONLY_SAID"
    return 0
  fi
  if [ -n "$made" ]; then
    # A server that has served checks with a writable root: every file it changed is in its writable layer.
    say "Reading what the running Lean server has written since it was made (docker diff)"
    rm -f "$dir/read-only.outside"
    project=$(lean_pool_source) && [ -f "$project/$LEAN_READ_ONLY_CHECK" ] \
      || die "missing $LEAN_READ_ONLY_CHECK: $LEAN_POOL_SOURCE_MISSING"
    said=$(mktemp)
    writes=$(bash "$project/$LEAN_READ_ONLY_CHECK" --diff "$dir/read-only.diff" "$container" 2>"$said") || checked=$?
    [ ! -f "$dir/read-only.diff" ] || chmod 0644 "$dir/read-only.diff"
    if [ "$checked" -le 1 ]; then
      if [ -n "$writes" ]; then
        # Every one of them, for `kimina-ctl status` (the reason below holds the first few).
        printf '%s\n' "$writes" > "$dir/read-only.outside"
        chmod 0644 "$dir/read-only.outside"
        # shellcheck disable=SC2086  # the paths, one space apart
        reason="the running server wrote outside /tmp: $(echo $writes | cut -c 1-300)"
      fi
      echo "docker diff: $(grep -c . "$dir/read-only.diff") lines, $(printf '%s' "$writes" | grep -c .) outside /tmp, the mount points (Docker's own too), Python's byte-code cache and git's index of each Lake package (in $dir/read-only.diff)"
    else
      reason="could not read what the running server writes ($(last_line_of "$said"))"
    fi
    rm -f "$said"
  fi
  if [ -n "$reason" ]; then
    reason=$(printf '%s' "$reason" | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-400)
    read_only_off "$dir" "$reason"
    READ_ONLY_SAID="READ-ONLY ROOT: OFF - $reason. The Lean server stays writable, as it was: every answer is the same, and a check can still change this server for the checks after it. The installer's next run reads it again."
    warn "$READ_ONLY_SAID"
    return 0
  fi
  set_env_in "$env" LEAN_SERVER_ROOT_READ_ONLY true
  printf 'on\n' > "$dir/read-only.state"
  chmod 0644 "$dir/read-only.state"
  if [ -n "$made" ]; then
    READ_ONLY_SAID="READ-ONLY ROOT: on - the running server wrote nothing outside /tmp; a real check is sent to it once it is made again"
  else
    READ_ONLY_SAID="READ-ONLY ROOT: on - a new server, with nothing to read first; a real check is sent to it once it is made"
  fi
  echo "$READ_ONLY_SAID"
}

# ------------------------------------------- each worker in a sandbox of its own
# A check runs code inside the worker that answers it, and Kimina runs every worker in one container as one
# user. Started through the lean-pool project's worker-sandbox/ (docs/worker-sandbox.md), each worker puts
# itself in a Landlock domain of its own first: the code a check runs can then reach no other worker, nor
# Kimina, nor the server's key. A worker that cannot be sandboxed does not start, so the overlay is added only
# where a container of the box's own image made the sandbox (make.sh), and kept only where a real check then
# passes. Elsewhere the workers start as before, and the box says why.
#   LEAN_SERVER_WORKER_SANDBOX=off    in kimina.env: turned off by hand (kimina-ctl sandbox off); every run keeps it
#   <instance dir>/sandbox/           mounted read-only at /opt/lean-sandbox by compose.worker-sandbox.yaml
#     lean-worker, sandbox.py         the launcher (the program a worker is started as) and the sandbox
#     repl-path                       the image's REPL, which lean-worker starts where no compiled step is on
#     state                           one line: `on ...` with what the probe found, or `off REASON`
#   <instance dir>/sandbox.log        the last probe, in full
LEAN_SANDBOX_MAKE=deploy/lean-server/worker-sandbox/make.sh     # in the lean-pool project
LEAN_SANDBOX_OVERLAY=compose.worker-sandbox.yaml
WORKER_SANDBOX_INSTALLED=""     # set once install_worker_sandbox has run in this process
WORKER_SANDBOX_SAID=""          # its one line, for the installer's last lines

# worker_sandbox_off INSTANCE_DIR REASON : the workers start as they did before the sandbox, and the box says why.
worker_sandbox_off() {
  printf 'off %s\n' "$2" > "$1/sandbox/state"
  chmod 0644 "$1/sandbox/state"
  set_overlay "$1/kimina.env" "$LEAN_SANDBOX_OVERLAY" off
}

# install_worker_sandbox INSTANCE_DIR INSTANCE IMAGE : ask a container of IMAGE whether this box can sandbox
# each worker, put the sandbox in the instance's directory, and write into its settings whether its workers
# start in it (whether COMPOSE_FILE holds compose.worker-sandbox.yaml), BEFORE its container is (re)made.
# Only a missing make.sh stops the install; a probe that fails leaves the workers without the sandbox.
install_worker_sandbox() {
  local dir=$1 instance=$2 image=$3 env=$1/kimina.env project said state reason hint
  WORKER_SANDBOX_INSTALLED=1
  hint="sudo kimina-ctl ${instance:+@$instance }sandbox"
  project=$(lean_pool_source) && [ -f "$project/$LEAN_SANDBOX_MAKE" ] \
    || die "missing $LEAN_SANDBOX_MAKE: $LEAN_POOL_SOURCE_MISSING"
  if [ "$(setting_in "$env" LEAN_SERVER_WORKER_SANDBOX)" = off ]; then
    set_overlay "$env" "$LEAN_SANDBOX_OVERLAY" off
    WORKER_SANDBOX_SAID="WORKER SANDBOX: off by this box's own setting (LEAN_SERVER_WORKER_SANDBOX=off in $env); '$hint on' turns it on"
    echo "$WORKER_SANDBOX_SAID"
    return 0
  fi
  # make.sh's last line is the answer: on, or off and why. One that stopped before it could say leaves the
  # workers as they were before the sandbox too.
  said=$(mktemp)
  if bash "$project/$LEAN_SANDBOX_MAKE" --image "$image" --box "$dir" --name "$(lean_server_project "$instance")-sandbox" \
       </dev/null | tee "$said"; then
    WORKER_SANDBOX_SAID="$(tail -n 1 "$said"); a real check is sent to the server once it is made"
    set_overlay "$env" "$LEAN_SANDBOX_OVERLAY" on
  else
    state=$(head -n 1 "$dir/sandbox/state" 2>/dev/null || true)
    case $state in
      "off "*) reason=${state#off } ;;
      *) reason="make.sh stopped: $(last_line_of "$said")" ;;
    esac
    mkdir -p "$dir/sandbox"
    worker_sandbox_off "$dir" "$reason"
    WORKER_SANDBOX_SAID="WORKER SANDBOX: OFF - $reason. The Lean server starts its workers as it did before, without it: every answer is the same, and a check can still reach the other checks of its server. The installer's next run tries again. Full log: $dir/sandbox.log"
    warn "$WORKER_SANDBOX_SAID"
  fi
  rm -f "$said"
}

# remake_and_check INSTANCE_DIR INSTANCE : make the Lean server again with its settings as they now are, wait
# until it is healthy, and send it a real check by itself.
remake_and_check() {
  local env=$1/kimina.env instance=$2 container state
  # shellcheck disable=SC2086  # no prefix for instance 1
  /usr/local/bin/kimina-ctl ${instance:+@$instance} restart
  container=$(lean_container "$env")
  for _ in $(seq 1 60); do
    state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null || true)
    # shellcheck disable=SC2086
    [ "$state" != healthy ] || { /usr/local/bin/kimina-ctl ${instance:+@$instance} check; return; }
    sleep 5
  done
  return 1
}

# confirm_lean_server INSTANCE_DIR INSTANCE : after the Lean server was (re)made, a real check against it ALONE
# (not through a pool, which may answer from another server). A server that fails it loses one protection at
# a time, each time made again and checked once more: first the worker sandbox; if the check fails without it
# too, the sandbox was not the cause and comes back while the read-only root is tried. Where neither alone was
# the cause, the server is made as it was before both and checked once more. The caller's verdict is the last
# check's status.
confirm_lean_server() {
  local dir=$1 instance=$2 env=$1/kimina.env probe="" read_only_tried=""
  # shellcheck disable=SC2086  # no prefix for instance 1
  /usr/local/bin/kimina-ctl ${instance:+@$instance} check && return 0
  if has_overlay "$env" "$LEAN_SANDBOX_OVERLAY"; then
    probe=$(head -n 1 "$dir/sandbox/state" 2>/dev/null || true)     # what the probe found: to put back
    worker_sandbox_off "$dir" "a real check failed against the server with each worker in its sandbox"
    WORKER_SANDBOX_SAID="WORKER SANDBOX: OFF - a real check failed against the server with each worker in its sandbox, so it was made again without it (as before: every answer is the same, and a check can still reach the other checks of its server). The installer's next run tries again."
    warn "$WORKER_SANDBOX_SAID"
    remake_and_check "$dir" "$instance" && return 0
  fi
  if [ "$(setting_in "$env" LEAN_SERVER_ROOT_READ_ONLY)" = true ]; then
    read_only_tried=1
    if [ -n "$probe" ]; then
      # It failed without the sandbox as well, so the sandbox was not the cause: it comes back.
      printf '%s\n' "$probe" > "$dir/sandbox/state"
      set_overlay "$env" "$LEAN_SANDBOX_OVERLAY" on
      WORKER_SANDBOX_SAID="WORKER SANDBOX: on - ${probe#on }; a real check failed with it AND without it, so it was not the cause"
      echo "$WORKER_SANDBOX_SAID"
    fi
    read_only_off "$dir" "a real check failed against the server with its root read-only"
    READ_ONLY_SAID="READ-ONLY ROOT: OFF - a real check failed against the server with its root read-only, so it was made writable again (as it was: every answer is the same, and a check can still change this server for the checks after it). The installer's next run tries again."
    warn "$READ_ONLY_SAID"
    remake_and_check "$dir" "$instance" && return 0
    # Writable, it fails as well: the read-only root was not the cause. It stays writable until the next run.
    read_only_off "$dir" "a real check failed against the server read-only and again writable, so the cause is not the read-only root"
    READ_ONLY_SAID="READ-ONLY ROOT: OFF - a real check failed against the server with its root read-only AND again writable, so the cause is not the read-only root: see the check's failure above. The server stays writable until the installer's next run, which tries again."
    warn "$READ_ONLY_SAID"
  fi
  [ -n "$probe" ] || return 1
  # Neither protection alone was the cause. The workers start without the sandbox until the next run.
  worker_sandbox_off "$dir" "a real check failed against the server with each worker in its sandbox and again without it, so the cause is not the sandbox"
  WORKER_SANDBOX_SAID="WORKER SANDBOX: OFF - a real check failed against the server with each worker in its sandbox AND again without it, so the cause is not the sandbox: see the check's failure above. The workers start without it until the installer's next run, which tries again."
  warn "$WORKER_SANDBOX_SAID"
  [ -n "$read_only_tried" ] || return 1
  # The last check ran with the sandbox back: once more with neither protection, as the server was before both.
  remake_and_check "$dir" "$instance" || return 1
  worker_sandbox_off "$dir" "a real check passed only with the sandbox and the read-only root both off"
  read_only_off "$dir" "a real check passed only with the sandbox and the read-only root both off"
  WORKER_SANDBOX_SAID="WORKER SANDBOX: OFF - a real check passed only with the sandbox and the read-only root both off. The installer's next run tries again."
  READ_ONLY_SAID="READ-ONLY ROOT: OFF - a real check passed only with the sandbox and the read-only root both off. The installer's next run tries again."
  warn "$WORKER_SANDBOX_SAID"
  warn "$READ_ONLY_SAID"
  return 0
}

# Docker from the snap package cannot do what these installers need: a snap-confined daemon sees its own
# /tmp and no /srv, so nothing here can be mounted into a container. Said plainly, before anything is done.
docker_is_snap() {
  case "$(command -v docker 2>/dev/null)" in /snap/*|/var/lib/snapd/*) return 0 ;; esac
  case "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)" in /var/snap/*|/snap/*) return 0 ;; esac
  return 1
}
SNAP_DOCKER_UNSUPPORTED="Docker on this host is the snap package, which is not supported: it cannot mount /srv or /tmp into a container, and the Lean server runs from /srv. Remove it (sudo snap remove docker), install Docker's own packages (https://docs.docker.com/engine/install/), then run this again. Nothing was installed."

# ----------------------------------------------------------------------- the docker compose plugin
# A box with a working Docker but no `docker compose` (Ubuntu's own docker.io ships none) gets
# the plugin installed here instead of being sent away: from its package manager, else the release binary
# pinned below. The ONE implementation: the join script, install.sh and install-pool.sh all call it.
#
# The pinned release, for a box whose package manager has no such package. Each checksum was computed from
# the downloaded file and equals the release's own checksums.txt; a download with any other is refused.
LEAN_POOL_COMPOSE_VERSION=v5.6.0
LEAN_POOL_COMPOSE_SHA256_X86_64=40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a
LEAN_POOL_COMPOSE_SHA256_AARCH64=733ec76717ceb59052a9609b9dadfb523b2df8eab57a54212872d10a58078ea2
# Where Docker looks for a plugin installed for every user of the machine.
LEAN_POOL_COMPOSE_PLUGIN=/usr/local/lib/docker/cli-plugins/docker-compose
# What ensure_compose_plugin leaves for its caller's messages:
COMPOSE_PLUGIN_INSTALLED=""      # what THIS run installed, and by which route; empty if it installed nothing
COMPOSE_PLUGIN_REMOVAL=""        # the command that removes it
COMPOSE_PLUGIN_FAILURE=""        # why each route that was tried did not give a working plugin

compose_plugin_works() { docker compose version </dev/null >/dev/null 2>&1; }

# The last line a tool wrote, as one line of printable text: a package manager's own reason, kept short.
last_line_of() { { tail -n 1 "$1" 2>/dev/null || true; } | tr -d '\n' | tr -c '[:print:]' ' ' | cut -c 1-200; }

# compose_release_binary LOG : the pinned release binary, checked against its pinned SHA-256, put where
# Docker finds it. 0 once `docker compose version` works with it; else 1 with the reason in
# COMPOSE_BINARY_REASON and nothing left behind (no partial download, no binary that does not work, no
# directory this made).
COMPOSE_BINARY_REASON=""
compose_release_binary() {
  local log=$1 machine arch sum url staged got plugins lib made_plugins=0 made_lib=0
  machine=$(uname -m)
  case "$machine" in
    x86_64|amd64)  arch=x86_64;  sum=$LEAN_POOL_COMPOSE_SHA256_X86_64 ;;
    aarch64|arm64) arch=aarch64; sum=$LEAN_POOL_COMPOSE_SHA256_AARCH64 ;;
    *) COMPOSE_BINARY_REASON="there is no pinned release binary for this machine type ($machine)"; return 1 ;;
  esac
  if [ -e "$LEAN_POOL_COMPOSE_PLUGIN" ]; then
    COMPOSE_BINARY_REASON="a file is already at $LEAN_POOL_COMPOSE_PLUGIN and 'docker compose version' fails with it: it was not replaced"
    return 1
  fi
  command -v curl >/dev/null 2>&1 && command -v sha256sum >/dev/null 2>&1 \
    || { COMPOSE_BINARY_REASON="curl and sha256sum are needed to fetch and check the release binary"; return 1; }
  url=https://github.com/docker/compose/releases/download/$LEAN_POOL_COMPOSE_VERSION/docker-compose-linux-$arch
  staged=$(mktemp) || { COMPOSE_BINARY_REASON="no temporary file for the download"; return 1; }
  if ! curl -fsSL --proto '=https' --proto-redir '=https' --retry 2 --max-time 600 -o "$staged" "$url" </dev/null >"$log" 2>&1; then
    rm -f "$staged"
    COMPOSE_BINARY_REASON="the download of $url failed: $(last_line_of "$log")"
    return 1
  fi
  got=$(sha256sum "$staged" | cut -d ' ' -f 1)
  if [ "$got" != "$sum" ]; then
    rm -f "$staged"
    COMPOSE_BINARY_REASON="the download of $url does not have the pinned SHA-256 (it has ${got:-none}, pinned is $sum): it was refused and deleted"
    return 1
  fi
  plugins=${LEAN_POOL_COMPOSE_PLUGIN%/*}
  lib=${plugins%/*}
  [ -d "$lib" ] || made_lib=1
  [ -d "$plugins" ] || made_plugins=1
  if install -d -o root -g root -m 0755 "$lib" "$plugins" >"$log" 2>&1 \
     && install -o root -g root -m 0755 "$staged" "$LEAN_POOL_COMPOSE_PLUGIN" >"$log" 2>&1; then
    rm -f "$staged"
    compose_plugin_works && return 0
    COMPOSE_BINARY_REASON="the release binary $LEAN_POOL_COMPOSE_VERSION was put at $LEAN_POOL_COMPOSE_PLUGIN, but 'docker compose version' still fails: it was removed again"
  else
    COMPOSE_BINARY_REASON="the release binary could not be put at $LEAN_POOL_COMPOSE_PLUGIN: $(last_line_of "$log")"
  fi
  rm -f "$staged" "$LEAN_POOL_COMPOSE_PLUGIN"
  [ "$made_plugins" -eq 0 ] || rmdir "$plugins" 2>/dev/null || true
  [ "$made_lib" -eq 0 ] || rmdir "$lib" 2>/dev/null || true
  return 1
}

# ensure_compose_plugin : 0 if `docker compose version` works, installing the plugin first where it does not
# (COMPOSE_PLUGIN_INSTALLED and COMPOSE_PLUGIN_REMOVAL then say what and how to undo it); 1 with every
# route's reason in COMPOSE_PLUGIN_FAILURE. Runs as root. Every route has standard input closed: a join
# script arrives on standard input, and a package manager that read it would eat the script.
ensure_compose_plugin() {
  COMPOSE_PLUGIN_INSTALLED=""; COMPOSE_PLUGIN_REMOVAL=""; COMPOSE_PLUGIN_FAILURE=""
  compose_plugin_works && return 0
  local log package attempt manager known=0 reasons=() apt_reasons=()
  log=$(mktemp) || { COMPOSE_PLUGIN_FAILURE="no temporary file"; return 1; }
  echo "the docker compose plugin is not installed here: adding it"

  # apt. docker-compose-plugin is the package in Docker's own repository, docker-compose-v2 the one beside
  # Ubuntu's docker.io. The lists are refreshed only if neither installs, and both are then tried once more.
  if command -v apt-get >/dev/null 2>&1; then
    known=1
    for attempt in 1 2; do
      apt_reasons=()
      for package in docker-compose-plugin docker-compose-v2; do
        if DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "$package" </dev/null >"$log" 2>&1; then
          if compose_plugin_works; then
            COMPOSE_PLUGIN_INSTALLED="the apt package $package"; COMPOSE_PLUGIN_REMOVAL="sudo apt-get remove $package"
            break 2
          fi
          apt_reasons+=("apt installed $package, but 'docker compose version' still fails (the package stays installed)")
        else
          apt_reasons+=("apt-get install $package: $(last_line_of "$log")")
        fi
      done
      [ "$attempt" -eq 1 ] || break
      if ! apt-get -o DPkg::Lock::Timeout=120 update </dev/null >"$log" 2>&1; then
        apt_reasons+=("apt-get update: $(last_line_of "$log")")
        break
      fi
    done
    [ -n "$COMPOSE_PLUGIN_INSTALLED" ] || reasons+=("${apt_reasons[@]}")
  fi

  # dnf, else yum: one package manager under two names. The package is in Docker's own repository.
  if [ -z "$COMPOSE_PLUGIN_INSTALLED" ]; then
    for manager in dnf yum; do
      command -v "$manager" >/dev/null 2>&1 || continue
      known=1
      if "$manager" install -y docker-compose-plugin </dev/null >"$log" 2>&1; then
        if compose_plugin_works; then
          COMPOSE_PLUGIN_INSTALLED="the $manager package docker-compose-plugin"; COMPOSE_PLUGIN_REMOVAL="sudo $manager remove docker-compose-plugin"
        else
          reasons+=("$manager installed docker-compose-plugin, but 'docker compose version' still fails (the package stays installed)")
        fi
      else
        reasons+=("$manager install docker-compose-plugin: $(last_line_of "$log")")
      fi
      break
    done
  fi
  [ "$known" -eq 1 ] || reasons+=("this box has no package manager this installer knows (apt-get, dnf, yum)")

  # No package gave a working plugin: the pinned release binary.
  if [ -z "$COMPOSE_PLUGIN_INSTALLED" ]; then
    if compose_release_binary "$log"; then
      COMPOSE_PLUGIN_INSTALLED="the release binary $LEAN_POOL_COMPOSE_VERSION from github.com/docker/compose, SHA-256 checked, at $LEAN_POOL_COMPOSE_PLUGIN"
      COMPOSE_PLUGIN_REMOVAL="sudo rm $LEAN_POOL_COMPOSE_PLUGIN"
    else
      reasons+=("$COMPOSE_BINARY_REASON")
    fi
  fi
  rm -f "$log"

  if [ -z "$COMPOSE_PLUGIN_INSTALLED" ]; then
    COMPOSE_PLUGIN_FAILURE=$(printf '%s; ' "${reasons[@]}")
    COMPOSE_PLUGIN_FAILURE=${COMPOSE_PLUGIN_FAILURE%; }
    return 1
  fi
  echo "installed the docker compose plugin: $COMPOSE_PLUGIN_INSTALLED. To remove it: $COMPOSE_PLUGIN_REMOVAL"
}

# The firewall rules are iptables rules, kept across reboots by systemd units. A host without either (Docker
# inside WSL without systemd, Docker Desktop) is told in the pre-flight, not after an hour's build.
require_firewall_tools() {
  command -v iptables >/dev/null \
    || die "the firewall rules need iptables, which is not installed here; install it, or pass --no-firewall (the ports are then reachable from every network this host is on)"
  command -v systemctl >/dev/null && [ -d /run/systemd/system ] \
    || die "the firewall rules are kept by systemd units, and this host does not run systemd (Docker inside WSL?); pass --no-firewall (the ports are then reachable from every network this host is on)"
}

# A LAN-only inbound rule for one published container port, re-applied at boot. Separate from
# kimina-firewall on purpose: it never touches a Lean server's own rules.
#   install_lan_firewall NAME PORT LAN_SUBNET
install_lan_firewall() {
  local name=$1 port=$2 subnet=$3
  [[ "$name" =~ ^[a-z0-9-]{1,24}$ ]] || die "bad firewall rule name '$name'"
  install -d -o root -g root -m 0755 /srv/lean-pool-firewall
  printf 'LEANPOOL_FW_PORT=%s\nLEANPOOL_FW_SUBNET=%s\n' "$port" "$subnet" > "/srv/lean-pool-firewall/$name.conf"
  cat > /usr/local/sbin/lean-pool-firewall <<'EOF'
#!/usr/bin/env bash
# Installed by the lean-pool installers. One inbound rule per NAME: the port only from the LAN subnet and the
# docker bridges. Docker-published ports are forwarded, so ufw's INPUT rules never see them; DOCKER-USER
# does, and it matches the ORIGINAL destination port.
set -euo pipefail
name=${2:-}
[[ "$name" =~ ^[a-z0-9-]{1,24}$ ]] || { echo "usage: lean-pool-firewall apply|remove NAME" >&2; exit 2; }
chain=LEANPOOL-$(echo "$name" | tr '[:lower:]' '[:upper:]')
. "/srv/lean-pool-firewall/$name.conf"
match=(-p tcp -m conntrack --ctorigdstport "$LEANPOOL_FW_PORT" --ctdir ORIGINAL)
remove() {
  while iptables -C DOCKER-USER "${match[@]}" -j "$chain" 2>/dev/null; do
    iptables -D DOCKER-USER "${match[@]}" -j "$chain"
  done
  iptables -F "$chain" 2>/dev/null || true
  iptables -X "$chain" 2>/dev/null || true
}
case "${1:-}" in
  apply)
    iptables -nL DOCKER-USER >/dev/null 2>&1 || { echo "lean-pool-firewall: no DOCKER-USER chain (is docker running?)" >&2; exit 1; }
    remove
    iptables -N "$chain"
    iptables -A "$chain" -s "$LEANPOOL_FW_SUBNET" -j RETURN
    iptables -A "$chain" -s 172.16.0.0/12 -j RETURN      # docker bridge networks
    iptables -A "$chain" -s 127.0.0.0/8 -j RETURN
    iptables -A "$chain" -j DROP
    iptables -I DOCKER-USER 1 "${match[@]}" -j "$chain"
    ;;
  remove) remove ;;
  *) echo "usage: lean-pool-firewall apply|remove NAME" >&2; exit 2 ;;
esac
EOF
  chmod 0755 /usr/local/sbin/lean-pool-firewall
  cat > /etc/systemd/system/lean-pool-firewall@.service <<'EOF'
[Unit]
Description=Limit a lean-pool port to the LAN (%i)
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/lean-pool-firewall apply %i
ExecStop=/usr/local/sbin/lean-pool-firewall remove %i

[Install]
WantedBy=multi-user.target docker.service
EOF
  systemctl daemon-reload
  if systemctl enable "lean-pool-firewall@$name.service" >/dev/null 2>&1 && systemctl restart "lean-pool-firewall@$name.service"; then
    echo "port $port: reachable from $subnet and the docker bridges only (lean-pool-firewall@$name)"
  else
    warn "could not apply the LAN rule for port $port (systemctl status lean-pool-firewall@$name)"
  fi
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw allow from "$subnet" to any port "$port" proto tcp comment "$SITE_LABEL $name" >/dev/null
  fi
}

remove_lan_firewall() {
  local name=$1
  systemctl disable --now "lean-pool-firewall@$name.service" 2>/dev/null || true
  rm -f "/srv/lean-pool-firewall/$name.conf"
}

# /usr/local/sbin/kimina-firewall from the file beside this one (its header says what it does).
# One script serves every instance on the host; an instance without a TLS front gets the rules it always had.
install_kimina_firewall() {
  [ -f "$SRC/kimina-firewall" ] || die "missing $SRC/kimina-firewall - run this from the repo checkout"
  bash -n "$SRC/kimina-firewall" || die "$SRC/kimina-firewall does not parse; the installed firewall script was not replaced"
  install -o root -g root -m 0755 "$SRC/kimina-firewall" /usr/local/sbin/kimina-firewall
}

# set_env_in FILE KEY VALUE : replace or append one KEY=VALUE line.
set_env_in() {
  if grep -q "^$2=" "$1"; then sed -i "s|^$2=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi
}

# setting_in FILE KEY : the value of one KEY=VALUE line (empty when absent).
setting_in() {
  sed -n "s/^$2=//p" "$1" | head -n 1
}
# setting_moved_in FILE KEY OLD_KEY : KEY's value, else OLD_KEY's: what is read BEFORE a box's settings have
# moved to Compose's names (move_to_compose_names), for a box that has not moved yet.
setting_moved_in() {
  local value
  value=$(setting_in "$1" "$2")
  [ -n "$value" ] || value=$(setting_in "$1" "$3")
  echo "$value"
}

# The usage agent beside a Lean server instance: HAProxy asks it for the box's CPU and RAM headroom.
#   install_usage_agent INSTANCE_DIR INSTANCE_NAME AGENT_PORT LAN_SUBNET   (after build_lean_pool_image)
# LAN_SUBNET empty = no firewall rule (the caller ran with --no-firewall). This is the PLAIN agent, with its
# own published port; an instance behind a TLS front gets its agent from install_tls_front instead. It is a
# service of the instance's own project (compose.agent.yaml): the caller's one `compose up` starts it.
install_usage_agent() {
  local dir=$1 instance=$2 port=$3 subnet=$4
  [ -n "$LEAN_POOL_IMAGE" ] || die "build_lean_pool_image must run before install_usage_agent"
  set_env_in "$dir/kimina.env" LEAN_SERVER_AGENT_IMAGE "$LEAN_POOL_IMAGE"
  set_env_in "$dir/kimina.env" LEAN_SERVER_AGENT_PORT "$port"
  record_installer_version "$dir/kimina.env" "$instance"
  set_overlay "$dir/kimina.env" compose.agent.yaml on
  if [ -n "$subnet" ]; then
    install_lan_firewall "agent${instance:+-$instance}" "$port" "$subnet"
  else
    warn "no firewall rule for the usage agent's port $port (it reports only CPU and RAM headroom)"
  fi
  echo "usage agent $(lean_server_project "$instance")-agent-1 on port $port"
}

# The LAN subnet of the interface that carries the default route (what install.sh has always detected).
detect_lan_subnet() {
  local device
  device=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
  [ -n "$device" ] && ip -4 route show dev "$device" scope link | awk '$1 ~ /\// {print $1; exit}'
}

# --------------------------------------------------------------------------------- a box's TLS front
# With TLS a box publishes neither its Lean server nor its usage agent: it publishes
# a small HAProxy that requires the pool proxy's client certificate and forwards to both over the
# instance's own Docker network (compose.tls.yaml, the lean-pool project's).

# front_address_for SUBNET : the fixed address of the TLS front on a /24 (its .250).
front_address_for() {
  [[ "$1" =~ ^([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\.0/24$ ]] || return 1
  echo "${BASH_REMATCH[1]}.250"
}

# reload_if_kept CONTAINER ID_BEFORE : tell an HAProxy container that `compose up -d` left running as it
# was to load the config and certificates just installed, without dropping a connection. A container that
# was just created has already read them.
# The signal is sent FROM INSIDE the container (PID 1 there is HAProxy's master, run by the user `docker
# exec` gets). NEVER `docker kill -s HUP`: Docker marks a container it signalled as stopped by hand,
# whatever the signal, and with `restart: unless-stopped` such a container is not started at the next boot
# or daemon start, nor restarted after a crash.
reload_if_kept() {
  local now
  now=$(docker inspect -f '{{.Id}}' "$1" 2>/dev/null || true)
  if [ -n "$2" ] && [ "$now" = "$2" ]; then
    docker exec "$1" sh -c 'kill -HUP 1' || warn "$1 could not be told to reload; it runs on what it had loaded (docker restart $1)"
  fi
}

# container_is_steady CONTAINER : it runs, it is not being restarted, and it was not restarted while we
# watched for a few seconds. `.State.Running` alone is true for a container that crashes and is started
# again in a loop, so the restart count is read twice.
container_is_steady() {
  local before after
  before=$(docker inspect -f '{{.RestartCount}}' "$1" 2>/dev/null) || return 1
  sleep 6
  after=$(docker inspect -f '{{.State.Running}} {{.State.Restarting}} {{.RestartCount}}' "$1" 2>/dev/null) || return 1
  [ "$after" = "true false $before" ]
}

# The TLS front of one named Lean server instance, with the usage agent behind it. ONE function for the
# pool host's own server (install-pool.sh) and for a box that joins (install.sh --tls-dir).
#   install_tls_front INSTANCE_DIR INSTANCE SERVER_NAME TLS_SOURCE LEAN_TIMEOUT SERVER_WAIT LAN_SUBNET
# TLS_SOURCE holds box.pem (the box's certificate, then its private key), box.crt and ca.crt; it may be
# INSTANCE_DIR/tls itself (a re-run keeps what is installed). LEAN_TIMEOUT and SERVER_WAIT are the POOL's,
# so the front never cuts a check the pool would still wait for. LAN_SUBNET empty = no firewall rule.
# After build_lean_pool_image. Until HAProxy has accepted the new config, no
# container is touched; then the instance's ONE project is brought up, with the agent and the front
# (compose.agent.yaml, compose.tls.yaml: the server and the agent then publish nothing themselves).
install_tls_front() {
  local dir=$1 instance=$2 name=$3 source=$4 lean_timeout=$5 server_wait=$6 subnet=$7
  local env=$dir/kimina.env file gid port agent_port project network container_net address
  local work holder before front
  [ -n "$instance" ] || die "a TLS front stands beside a named instance only (instance 1 stays plain HTTP)"
  [ -n "$LEAN_POOL_IMAGE" ] || die "build_lean_pool_image must run before install_tls_front"
  [[ "$name" =~ $LEAN_POOL_DNS_NAME ]] && [ "${#name}" -le 40 ] \
    || die "the server name '$name' must be a DNS name in lower case (it is the name on the certificate)"
  for file in box.pem box.crt ca.crt; do
    [ -s "$source/$file" ] || die "the TLS front needs $source/$file"
  done
  install_compose_files "$dir" server     # the overlays this needs, as the lean-pool project has them
  port=$(setting_in "$env" LEAN_SERVER_PUBLISHED_PORT)
  project=$(setting_in "$env" COMPOSE_PROJECT_NAME)
  container_net=$(setting_in "$env" LEAN_SERVER_SUBNET)
  [ -n "$port" ] && [ -n "$project" ] && [ -n "$container_net" ] \
    || die "$env is incomplete - re-run install.sh --instance $instance"
  front=$project-front-1
  network=${project}_default
  agent_port=$((port + 200))

  say "TLS front for '$name': ports $port (Lean) and $agent_port (usage) accept only the pool proxy's certificate"
  docker pull -q "$LEAN_POOL_HAPROXY_IMAGE" >/dev/null || die "could not pull $LEAN_POOL_HAPROXY_IMAGE"
  gid=$(image_group_id "$LEAN_POOL_HAPROXY_IMAGE") || die "could not read the group $LEAN_POOL_HAPROXY_IMAGE runs as"

  # The certificate files, each with an explicit owner and mode. box.pem holds the private key: root and
  # the front's group may read it, nobody else; the directory is closed to everyone else as well.
  install_dir_for_gid 0750 "$gid" "$dir/tls"
  if [ "$(readlink -f "$source")" != "$(readlink -f "$dir/tls")" ]; then
    install_for_gid 0640 "$gid" "$source/box.pem" "$dir/tls/box.pem.new"
    mv -f "$dir/tls/box.pem.new" "$dir/tls/box.pem"
    install -o root -g root -m 0644 "$source/box.crt" "$dir/tls/box.crt"
    install -o root -g root -m 0644 "$source/ca.crt" "$dir/tls/ca.crt"
  fi
  chown root:"$gid" "$dir/tls" "$dir/tls/box.pem"; chmod 0750 "$dir/tls"; chmod 0640 "$dir/tls/box.pem"
  chown root:root "$dir/tls/box.crt" "$dir/tls/ca.crt"; chmod 0644 "$dir/tls/box.crt" "$dir/tls/ca.crt"

  # The front's fixed address on the instance's network (see kimina-firewall, KIMINA-FRONT).
  address=$(setting_in "$env" LEAN_SERVER_FRONT_ADDRESS)
  if [ -z "$address" ]; then
    address=$(front_address_for "$container_net") \
      || die "cannot place the TLS front on $container_net (not a /24): set LEAN_SERVER_FRONT_ADDRESS in $env to a free address of that network"
  fi
  [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "LEAN_SERVER_FRONT_ADDRESS in $env must be an IPv4 address"
  holder=$({ docker network inspect "$network" -f '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}' 2>/dev/null || true; } \
    | awk -v address="$address" '{split($2, parts, "/"); if (parts[1] == address) print $1}')
  [ -z "$holder" ] || [ "$holder" = "$front" ] \
    || die "$address on network $network is in use by $holder: set LEAN_SERVER_FRONT_ADDRESS in $env to a free address"

  render_tls_front "$dir" "$LEAN_POOL_IMAGE" "$LEAN_POOL_HAPROXY_IMAGE" "$lean_timeout" "$server_wait" \
    "$dir/front/haproxy.cfg.new"
  mv -f "$dir/front/haproxy.cfg.new" "$dir/front/haproxy.cfg"

  set_env_in "$env" LEAN_SERVER_NAME "$name"
  set_env_in "$env" LEAN_SERVER_FRONT_IMAGE "$LEAN_POOL_HAPROXY_IMAGE"
  set_env_in "$env" LEAN_SERVER_FRONT_ADDRESS "$address"
  set_env_in "$env" LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS "$lean_timeout"
  set_env_in "$env" LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS "$server_wait"
  set_env_in "$env" LEAN_SERVER_AGENT_IMAGE "$LEAN_POOL_IMAGE"
  set_env_in "$env" LEAN_SERVER_AGENT_PORT "$agent_port"
  record_installer_version "$env" "$instance"
  set_overlay "$env" compose.agent.yaml on
  set_overlay "$env" compose.tls.yaml on

  # The Lean server's control group, where the caller has not already decided it: the pool's own
  # host comes here from install-pool.sh, which keeps whatever priority the instance's settings name.
  [ -n "$CPU_PRIORITY_INSTALLED" ] \
    || install_cpu_priority "$dir" "$instance" "$(setting_in "$env" LEAN_SERVER_IMAGE)"
  # Which program a worker is, where the caller has not already decided it: the pool's own host,
  # again. The overlay it adds or removes mounts the directory it makes.
  [ -n "$COMPILED_SEARCH_INSTALLED" ] \
    || install_compiled_search "$dir" "$instance" "$(setting_in "$env" LEAN_SERVER_IMAGE)"
  # Whether its root filesystem is read-only, decided here for the pool's own host likewise.
  [ -n "$READ_ONLY_INSTALLED" ] || install_read_only_root "$dir" "$instance"
  # And whether each worker starts in a sandbox of its own.
  [ -n "$WORKER_SANDBOX_INSTALLED" ] \
    || install_worker_sandbox "$dir" "$instance" "$(setting_in "$env" LEAN_SERVER_IMAGE)"
  # The egress rule names the whole network; the front's fixed address is its one exception. In place
  # BEFORE the front starts, so its first connections to the server are let through.
  if [ -f "$dir/firewall.conf" ]; then
    set_env_in "$dir/firewall.conf" KIMINA_FW_FRONT_ADDRESS "$address"
    install_kimina_firewall
    systemctl restart "kimina-firewall-$instance.service" \
      || warn "could not re-apply kimina-firewall-$instance.service; the TLS front may not reach the Lean server (systemctl status kimina-firewall-$instance)"
  fi
  # From here on containers change, and only those whose settings did: the server stops publishing its
  # port if it still did, the agent likewise, and the front takes both ports.
  before=$(docker inspect -f '{{.Id}}' "$front" 2>/dev/null || true)
  compose_in "$env" up -d || die "could not start the Lean server, its usage agent and its TLS front (docker compose logs above)"
  reload_if_kept "$front" "$before"
  if ! container_is_steady "$front"; then
    docker logs --tail 20 "$front" >&2 || true
    die "the TLS front $front of '$name' does not stay up: it is stopped or keeps restarting (its last lines are above). Its Lean server is unreachable until it runs: fix what the lines say, then run this again"
  fi
  if [ -n "$subnet" ]; then
    install_lan_firewall "agent-$instance" "$agent_port" "$subnet"
  else
    warn "no firewall rule for the usage port $agent_port (the TLS front still requires the pool proxy's certificate)"
  fi
  echo "TLS front $front at $address: the Lean server and the usage agent are no longer published themselves"
}

# render_tls_front INSTANCE_DIR POOL_IMAGE HAPROXY_IMAGE LEAN_TIMEOUT SERVER_WAIT OUT : the front's config,
# rendered by the pool's tools (POOL_IMAGE), then judged by the real HAProxy (HAPROXY_IMAGE) AS ITS OWN USER,
# with the certificate files mounted where the config says they are, and installed at OUT (0644: it holds no
# secret). mktemp -d is root-only, so the candidate gets the modes of the installed config. The front reaches
# the server and the agent by their service names, on the ports they listen on inside the project. Either
# failing stops the installer with no container changed.
render_tls_front() {
  local dir=$1 pool_image=$2 haproxy_image=$3 lean_timeout=$4 server_wait=$5 out=$6 work
  work=$(mktemp -d)
  docker run --rm --network none "$pool_image" leanpool-haproxy-config render-box \
      --tls-server-pem "$LEAN_POOL_TLS_MOUNT/box.pem" --tls-ca-file "$LEAN_POOL_TLS_MOUNT/ca.crt" \
      --proxy-client-name "$LEAN_POOL_PROXY_CLIENT_NAME" \
      --lean-upstream kimina:8000 --agent-upstream agent:18200 \
      --lean-port 8000 --agent-port 18200 \
      --lean-timeout "$lean_timeout" --server-wait "$server_wait" > "$work/haproxy.cfg" \
    || { rm -rf "$work"; die "could not render the TLS front's config; no container was changed"; }
  chmod 0755 "$work"; chmod 0644 "$work/haproxy.cfg"
  docker run --rm --network none -v "$work":/cfg:ro -v "$dir/tls":"$LEAN_POOL_TLS_MOUNT":ro \
      "$haproxy_image" haproxy -c -f /cfg/haproxy.cfg >/dev/null \
    || { rm -rf "$work"; die "HAProxy rejected the TLS front's config, or cannot read its certificate; no container was changed"; }
  install -d -o root -g root -m 0755 "$dir/front"
  install -o root -g root -m 0644 "$work/haproxy.cfg" "$out"
  rm -rf "$work"
}

# ------------------------------------------------------------------------------------ the join bundle
# pack_join_bundle FILE INSTANCE : what a new box unpacks, as base64 of a gzipped tar with one
# directory `join/`: the instance installer and everything it needs; `join/site/`, the site's values for INSTANCE
# alone (site.env, instances/INSTANCE.env and that instance's image recipe), which the installer reads when it is
# given no --site; and `join/lean_pool/`, the lean-pool project's source (the box builds the pool's image from
# it, runs the Lean server from its compose files, and makes the compiled search step and reads its server
# with it). It holds NO key: the pool's key reaches a box inside its join, and a joining box has no door.
pack_join_bundle() {
  local out=$1 instance=$2 stage source file recipe
  source=$(lean_pool_source) || die "$LEAN_POOL_SOURCE_MISSING"
  [ -f "$SITE/instances/$instance.env" ] || die "the site has no instance '$instance' ($SITE/instances/$instance.env)"
  read -r -a recipe <<<"$(setting_in "$SITE/instances/$instance.env" INSTANCE_BUILD_FILES)"
  stage=$(mktemp -d)
  mkdir "$stage/join" "$stage/join/lean_pool" "$stage/join/site" "$stage/join/site/instances"
  for file in "${LEAN_POOL_JOIN_FILES[@]}"; do
    [ -f "$SRC/$file" ] || { rm -rf "$stage"; die "missing $SRC/$file - run this from the repo checkout"; }
    cp "$SRC/$file" "$stage/join/"
  done
  for file in site.env "instances/$instance.env" ${recipe[@]+"${recipe[@]}"}; do
    [ -f "$SITE/$file" ] || { rm -rf "$stage"; die "missing $SITE/$file - the site's values for instance '$instance'"; }
    cp "$SITE/$file" "$stage/join/site/$file"
  done
  local -a bundled
  mapfile -t bundled < <(bundled_project_paths)
  tar -cf - -C "$source" --exclude=__pycache__ "${bundled[@]}" \
    | tar -xf - -C "$stage/join/lean_pool" || { rm -rf "$stage"; die "could not copy the lean-pool project"; }
  tar -czf - -C "$stage" --owner=0 --group=0 join | base64 > "$out" \
    || { rm -rf "$stage"; die "could not pack the join bundle"; }
  rm -rf "$stage"
}

# bundled_project_paths : the lean-pool project's paths a join bundle carries, one a line: only what the image
# build reads (see the project's .dockerignore), the Lean server's compose files, the compiled search step,
# the read-only check and the worker sandbox. No tests, no environment, no caches.
bundled_project_paths() {
  local file
  printf '%s\n' pyproject.toml README.md LICENSE leanpool deploy/Dockerfile
  for file in "${LEAN_SERVER_COMPOSE_FILES[@]}"; do printf '%s\n' "$LEAN_SERVER_COMPOSE_DIR/$file"; done
  printf '%s\n' deploy/lean-server/compiled-search "$LEAN_READ_ONLY_CHECK" deploy/lean-server/worker-sandbox
}

# installer_version INSTANCE : the version of this installer, one md5 of everything a join bundle of INSTANCE
# carries (pack_join_bundle): the installer's files, the site's values for INSTANCE and the lean-pool project's
# source as the bundle packs it. It is the md5 of the list `md5  path` (installer_files), one line a file, with
# the file's path in the bundle, sorted by path. The pool's host computes it from its checkout and a joined
# box from the bundle it unpacked, so one release gives one version on every box. Prints 32 hex digits;
# fails, printing nothing, when a file is missing.
installer_version() {
  local files
  files=$(installer_files "$1") || return 1
  printf '%s\n' "$files" | md5sum | cut -c 1-32
}
installer_files() {       # INSTANCE : the list installer_version hashes
  local instance=${1:-default} source file recipe lines="" bundled
  source=$(lean_pool_source) || return 1
  mapfile -t bundled < <(bundled_project_paths)
  [ -f "$SITE/instances/$instance.env" ] || return 1
  read -r -a recipe <<<"$(setting_in "$SITE/instances/$instance.env" INSTANCE_BUILD_FILES)"
  for file in "${LEAN_POOL_JOIN_FILES[@]}"; do
    [ -f "$SRC/$file" ] || return 1
    lines+="$(md5sum < "$SRC/$file" | cut -c 1-32)  $file"$'\n'
  done
  for file in site.env "instances/$instance.env" ${recipe[@]+"${recipe[@]}"}; do
    [ -f "$SITE/$file" ] || return 1
    lines+="$(md5sum < "$SITE/$file" | cut -c 1-32)  site/$file"$'\n'
  done
  # The same files as the bundle's tar of the project: no __pycache__ anywhere.
  while IFS= read -r file; do
    lines+="$(md5sum < "$source/$file" | cut -c 1-32)  lean_pool/$file"$'\n'
  done < <(cd "$source" && find "${bundled[@]}" -name __pycache__ -prune -o -type f -print)
  printf '%s' "$lines" | LC_ALL=C sort -k 2
}

# record_installer_version ENV_FILE INSTANCE : LEANPOOL_AGENT_VERSION in a Lean server's settings, which its
# usage agent ends every answer with. Left empty when it cannot be computed: the agent then answers without one.
record_installer_version() {
  local version
  version=$(installer_version "$2") \
    || { warn "could not compute this installer's version: the usage agent's answers carry none"; version=""; }
  set_env_in "$1" LEANPOOL_AGENT_VERSION "$version"
}

# ---------------------------------------------------------------- the Lean image a joining box loads
# A joining box gets the Lean server's image from the pool's host and builds only as a
# fallback. No registry: the pool's host exports the image to a file once, and a join window serves it.

# The API key is a setting of the RUNNING container (compose.yaml). An image whose config carries the
# variable is never exported: nothing secret travels in an image.
LEAN_POOL_KEY_VARIABLE=LEAN_SERVER_API_KEY

# image_layers IMAGE : the image's LAYER IDENTITY, one word that is the same for the same image on every
# host. Docker's own image ID is the digest of the image's config under one image store and of its manifest
# under the other (containerd), so two hosts may show two IDs for one image; the layer list is the same
# under both. Fails for an image that is not there.
image_layers() {
  local layers
  layers=$(docker image inspect -f '{{json .RootFS.Layers}}' "$1" 2>/dev/null) || return 1
  [[ "$layers" == '["sha256:'* ]] || return 1
  printf '%s' "$layers" | sha256sum | cut -d ' ' -f 1
}

# export_join_image DIR GID IMAGE : on the pool's host, as root. Writes DIR/join/image/image-<12 hex>.tar
# (`docker save`, a plain tar) and manifest.json, readable by root and group GID (the join service's) only.
# ONCE: an export of the image the host holds now is kept. A host without the image, or one whose image
# carries the key variable, is left with no export: a joining box then builds its own. Never fatal: the
# pool works without it.
export_join_image() {
  local dir=$1 gid=$2 image=$3 out=$1/join/image
  local id layers size need free started saved sum bytes file shown machine version
  id=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)
  if [ -z "$id" ]; then
    rm -rf "$out"
    echo "this host holds no image $image: nothing to ship, a joining box builds its own"
    return 0
  fi
  if [ -s "$out/manifest.json" ] \
     && shown=$(python3 "$SRC/join_helper.py" image-export --manifest "$out/manifest.json" --directory "$out" --image-id "$id" 2>/dev/null); then
    echo "keeping the exported image ($(sed -n 5p <<<"$shown"), $(sed -n 2p <<<"$shown")): this host's image has not changed"
    return 0
  fi
  rm -rf "$out"                                          # an older export goes first: its space is needed
  if docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$image" 2>/dev/null | grep -q "^$LEAN_POOL_KEY_VARIABLE="; then
    warn "the image $image carries $LEAN_POOL_KEY_VARIABLE in its config: it is NOT exported (a key must never travel in an image). A joining box builds its own."
    return 0
  fi
  layers=$(image_layers "$image") || { warn "could not read the layers of $image: it is not exported, a joining box builds its own"; return 0; }
  size=$(docker image inspect -f '{{.Size}}' "$image" 2>/dev/null || true)
  [[ "$size" =~ ^[0-9]{1,15}$ ]] || { warn "could not read the size of $image: it is not exported, a joining box builds its own"; return 0; }
  need=$(( size / 1024 / 1024 / 1024 + 2 ))
  free=$({ df -BG --output=avail "$dir" 2>/dev/null || true; } | tail -n 1 | tr -dc '0-9')
  if [ -z "$free" ] || [ "$free" -lt "$need" ]; then
    warn "exporting $image needs $need GB free under $dir and ${free:-an unknown number of} GB are: it is not exported, a joining box builds its own"
    return 0
  fi
  install_dir_for_gid 0750 "$gid" "$out"
  echo "exporting $image for joining boxes (docker save, a plain tar; done once per image)"
  started=$(date +%s)
  if ! docker save -o "$out/image.tar.part" "$image"; then
    rm -rf "$out"
    warn "docker save of $image failed: it is not exported, a joining box builds its own"
    return 0
  fi
  saved=$(date +%s)
  sum=$(sha256sum "$out/image.tar.part" | cut -d ' ' -f 1)
  bytes=$(stat -c %s "$out/image.tar.part")
  file=image-${sum:0:12}.tar
  mv -f "$out/image.tar.part" "$out/$file"
  machine=$(uname -m)
  version=$(docker version -f '{{.Server.Version}}' 2>/dev/null || true)
  if ! python3 "$SRC/join_helper.py" image-manifest --tag "$image" --image-id "$id" --layers "$layers" --file "$file" \
        --sha256 "$sum" --bytes "$bytes" --machine "$machine" --docker "${version:-unknown}" --out "$out/manifest.json"; then
    rm -rf "$out"
    warn "could not write the manifest of the exported image: it is not shipped, a joining box builds its own"
    return 0
  fi
  chown root:"$gid" "$out/$file" "$out/manifest.json"
  chmod 0640 "$out/$file" "$out/manifest.json"
  echo "exported $image: $(awk -v bytes="$bytes" 'BEGIN {printf "%.1f GB", bytes / 1e9}') in $(( $(date +%s) - started )) s (docker save $(( saved - started )) s, SHA-256 $(( $(date +%s) - saved )) s) to $out/$file"
  echo "a joining $machine box loads it instead of building; it is exported again only when this host's image changes"
}

# ------------------------------------------------------------------------ moving a box to Compose's names
# Installers before this one ran an instance as up to three compose projects of their own files (the server,
# its usage agent and its TLS front) and a pool as one, named after the site's prefix, with settings under names
# of their own. Now an instance is ONE project of the lean-pool project's files and a pool likewise (the compose
# files, above), and every setting has the name of what reads it. kimina-ctl is one program for every instance
# of a box, so the box moves whole: at the end of an installer's pre-flight, before the installer changes
# anything else, each instance and then each pool comes down under its older names and starts under the new
# ones. One that does not come up healthy and pass its check is taken down again and comes back as it was, and
# the installer stops; those that moved before it stay moved. A box that has moved has nothing to move.
# What a move replaced is kept beside it, with the ending .before-compose-names:
#   <instance dir>/kimina.env.*   the settings as they were (0600)     <pool dir>/pool.env.*   likewise
#   <instance dir>/docker-compose*.yml.*  front/haproxy.cfg.*           <pool dir>/docker-compose.pool.yml.*
# Those files put back, with the new projects down, are what an older installer runs again. A move changes no
# host port, subnet, front address, firewall rule, control group, server list, certificate or key.
MOVED=before-compose-names
# The settings an older installer wrote, OLD=NEW. An empty NEW is a setting nothing reads any more. Every other
# line stays as it is, in its place.
MOVED_INSTANCE_SETTINGS=(
  KIMINA_API_KEY=LEAN_SERVER_API_KEY KIMINA_MAX_REPLS=LEAN_SERVER_MAX_REPLS
  KIMINA_MAX_REPL_USES=LEAN_SERVER_MAX_REPL_USES KIMINA_MAX_REPL_MEM=LEAN_SERVER_MAX_REPL_MEM
  KIMINA_MAX_WAIT=LEAN_SERVER_MAX_WAIT KIMINA_LOG_LEVEL=LEAN_SERVER_LOG_LEVEL KIMINA_IMAGE_TAG=LEAN_SERVER_IMAGE
  KIMINA_PORT=LEAN_SERVER_PUBLISHED_PORT KIMINA_AGENT_PORT=LEAN_SERVER_AGENT_PORT KIMINA_SUBNET=LEAN_SERVER_SUBNET
  KIMINA_CPUS=LEAN_SERVER_CPUS KIMINA_MEM_LIMIT=LEAN_SERVER_MEM_LIMIT KIMINA_CGROUP_PARENT=LEAN_SERVER_CGROUP_PARENT
  KIMINA_CPU_PRIORITY=LEAN_SERVER_CPU_PRIORITY KIMINA_ROOT_READ_ONLY=LEAN_SERVER_ROOT_READ_ONLY
  KIMINA_READ_ONLY=LEAN_SERVER_READ_ONLY KIMINA_COMPILED_SEARCH=LEAN_SERVER_COMPILED_SEARCH
  KIMINA_SERVER_NAME=LEAN_SERVER_NAME KIMINA_AGENT_IMAGE=LEAN_SERVER_AGENT_IMAGE
  KIMINA_FRONT_IMAGE=LEAN_SERVER_FRONT_IMAGE KIMINA_FRONT_ADDRESS=LEAN_SERVER_FRONT_ADDRESS
  KIMINA_AGENT_BESIDE_OTHER_WORK=LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK
  KIMINA_AGENT_LEAN_CPU_STAT=LEANPOOL_AGENT_LEAN_CPU_STAT_PATH KIMINA_AGENT_CPU_RESERVE=LEANPOOL_AGENT_CPU_RESERVE
  KIMINA_FRONT_LEAN_TIMEOUT=LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS
  KIMINA_FRONT_SERVER_WAIT=LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS KIMINA_INSTALLER_VERSION=LEANPOOL_AGENT_VERSION
  KIMINA_REPL_PATH= KIMINA_CONTAINER= KIMINA_PROJECT= KIMINA_NETWORK= KIMINA_AGENT_CONTAINER= KIMINA_FRONT_CONTAINER=
)
# What the older compose file used where a setting was not written: written out, so that no default of the
# compose files of deploy/ changes a box.
MOVED_INSTANCE_DEFAULTS=(
  LEAN_SERVER_MAX_REPLS=12 LEAN_SERVER_MAX_REPL_USES=200 LEAN_SERVER_MAX_REPL_MEM=8G LEAN_SERVER_MAX_WAIT=120
  LEAN_SERVER_LOG_LEVEL=WARNING LEAN_SERVER_PUBLISHED_PORT=18000 LEAN_SERVER_MEM_LIMIT=36g LEAN_SERVER_CPUS=16
  LEAN_SERVER_SUBNET=172.29.53.0/24 LEAN_SERVER_ROOT_READ_ONLY=false
)
MOVED_POOL_SETTINGS=(
  POOL_PIN=LEANPOOL_CACHE_PIN POOL_CACHE_MAX_BYTES=LEANPOOL_CACHE_MAX_BYTES
  POOL_CACHE_UPSTREAM_TIMEOUT=LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS
  POOL_MAX_REQUEST_BYTES=LEANPOOL_CACHE_MAX_REQUEST_BYTES POOL_PUBLIC_PORT=LEANPOOL_HAPROXY_PUBLIC_PORT
  POOL_LEAN_TIMEOUT=LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS POOL_SERVER_WAIT=LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS
  POOL_JOIN_PORT=LEANPOOL_JOIN_PUBLIC_PORT POOL_JOIN_HOST=LEANPOOL_JOIN_PUBLIC_HOST POOL_IMAGE=LEANPOOL_IMAGE
  POOL_HAPROXY_IMAGE=LEANPOOL_HAPROXY_IMAGE POOL_IMAGE_GID=LEANPOOL_IMAGE_GID POOL_SUBNET=LEANPOOL_SUBNET
  POOL_DNS=LEANPOOL_DNS POOL_FRONT_NAMES=LEANPOOL_FRONT_NAMES
  POOL_INSTALLER_VERSION=LEANPOOL_HAPROXY_INSTALLER_VERSION POOL_NAME=
)
MOVED_POOL_DEFAULTS=(
  LEANPOOL_HAPROXY_PUBLIC_PORT=18100 LEANPOOL_CACHE_MAX_BYTES=4294967296 LEANPOOL_CACHE_MAX_REQUEST_BYTES=262144
  LEANPOOL_SUBNET=172.29.55.0/24
)
LEAN_NATIVE_LAUNCHER=/opt/lean-native/repl-native     # what an older installer's settings named as the worker
# Set by old_instance: the instance's older names, and what of it runs.
OLD_INSTANCE="" OLD_SERVER="" OLD_PROJECT="" OLD_NETWORK="" OLD_AGENT="" OLD_FRONT=""
OLD_RAN=0 OLD_WITH_AGENT=0 OLD_WITH_FRONT=0

# moved_settings FILE PAIRS... : FILE's lines, with each OLD key of PAIRS (OLD=NEW) renamed, or dropped where NEW
# is empty. KIMINA_IMAGE_TAG becomes the whole image reference, under the site's prefix. No value is ever put
# on a command line (the API key is one of them).
moved_settings() {
  local file=$1
  shift
  awk -v pairs="$*" -v prefix="$SITE_PREFIX" '
    BEGIN {
      n = split(pairs, list, " ")
      for (i = 1; i <= n; i++) { at = index(list[i], "="); renamed[substr(list[i], 1, at - 1)] = substr(list[i], at + 1) }
    }
    /^[A-Za-z_][A-Za-z0-9_]*=/ {
      key = substr($0, 1, index($0, "=") - 1)
      if (key in renamed) {
        if (renamed[key] == "") next
        value = substr($0, length(key) + 2)
        if (key == "KIMINA_IMAGE_TAG") value = prefix "/kimina:" value
        print renamed[key] "=" value
        next
      }
    }
    { print }' "$file"
}

# with_defaults FILE PAIRS... : each KEY=VALUE of PAIRS whose KEY FILE does not set yet, added to FILE.
with_defaults() {
  local file=$1 pair
  shift
  for pair in "$@"; do
    grep -q "^${pair%%=*}=" "$file" || echo "$pair" >> "$file"
  done
}

container_exists() { docker inspect -f '{{.Id}}' "$1" >/dev/null 2>&1; }
image_here() { docker image inspect -f '{{.Id}}' "$1" >/dev/null 2>&1; }

# became_healthy CONTAINER : its health check passed within five minutes.
became_healthy() {
  local state
  for _ in $(seq 1 60); do
    state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$1" 2>/dev/null || true)
    [ "$state" = healthy ] && return 0
    sleep 5
  done
  return 1
}

# old_instance DIR : an instance an older installer set up, into OLD_*: its names (its settings', else the ones
# that installer gave), and what of it runs (OLD_RAN: its server; OLD_WITH_AGENT, OLD_WITH_FRONT). Where its
# server is not there (taken down by hand), its agent and front are taken as its settings name them.
old_instance() {
  local env=$1/kimina.env
  OLD_INSTANCE=${1##*/kimina}; OLD_INSTANCE=${OLD_INSTANCE#-}
  OLD_SERVER=$(setting_in "$env" KIMINA_CONTAINER)
  OLD_SERVER=${OLD_SERVER:-$SITE_PREFIX-kimina${OLD_INSTANCE:+-$OLD_INSTANCE}}
  OLD_PROJECT=$(setting_in "$env" KIMINA_PROJECT); OLD_PROJECT=${OLD_PROJECT:-$OLD_SERVER}
  OLD_NETWORK=$(setting_in "$env" KIMINA_NETWORK); OLD_NETWORK=${OLD_NETWORK:-$OLD_PROJECT}
  OLD_AGENT=$(setting_in "$env" KIMINA_AGENT_CONTAINER)
  OLD_FRONT=$(setting_in "$env" KIMINA_FRONT_CONTAINER)
  OLD_RAN=0; OLD_WITH_AGENT=0; OLD_WITH_FRONT=0
  if container_exists "$OLD_SERVER"; then OLD_RAN=1; fi
  if [ -n "$OLD_FRONT" ] && { container_exists "$OLD_FRONT" || [ "$OLD_RAN" -eq 0 ]; }; then
    OLD_WITH_FRONT=1; OLD_WITH_AGENT=1                 # a front always had its agent behind it
  elif [ -n "$OLD_AGENT" ] && { container_exists "$OLD_AGENT" || [ "$OLD_RAN" -eq 0 ]; }; then
    OLD_WITH_AGENT=1
  fi
}

# old_compose ENV FILE PROJECT ARGS... : `docker compose` as an older installer ran one of its files, which read
# the site's prefix and the server's names from the environment.
old_compose() {
  local env=$1 file=$2 project=$3
  shift 3
  SITE_PREFIX=$SITE_PREFIX KIMINA_CONTAINER=$OLD_SERVER KIMINA_NETWORK=$OLD_NETWORK \
    docker compose --project-name "$project" --env-file "$env" -f "$file" "$@"
}

# old_instance_down DIR : an instance's older projects down: the front (and the agent behind it), the agent,
# then the server, whose network goes with it.
old_instance_down() {
  local dir=$1 env=$1/kimina.env
  if [ -n "$OLD_FRONT" ] && [ -f "$dir/docker-compose.front.yml" ]; then
    old_compose "$env" "$dir/docker-compose.front.yml" "$OLD_FRONT" down || return 1
  fi
  if [ -n "$OLD_AGENT" ] && [ -f "$dir/docker-compose.agent.yml" ]; then
    old_compose "$env" "$dir/docker-compose.agent.yml" "$OLD_AGENT" down || return 1
  fi
  old_compose "$env" "$dir/docker-compose.yml" "$OLD_PROJECT" down
}

# old_instance_up DIR : what of the instance ran, back under its older projects: the server, then its front
# (which brings the agent behind it) or its agent.
old_instance_up() {
  local dir=$1 env=$1/kimina.env
  old_compose "$env" "$dir/docker-compose.yml" "$OLD_PROJECT" up -d || return 1
  if [ "$OLD_WITH_FRONT" -eq 1 ]; then
    old_compose "$env" "$dir/docker-compose.front.yml" "$OLD_FRONT" up -d
  elif [ "$OLD_WITH_AGENT" -eq 1 ]; then
    old_compose "$env" "$dir/docker-compose.agent.yml" "$OLD_AGENT" up -d
  fi
}

# can_move_instance DIR : what moving the instance needs is there: its settings, its images, its older files.
can_move_instance() {
  local dir=$1 env=$1/kimina.env key image
  old_instance "$dir"
  for key in KIMINA_API_KEY KIMINA_IMAGE_TAG; do
    [ -n "$(setting_in "$env" "$key")" ] || die "$env has no $key, so it cannot be moved to Compose's names. Nothing was changed."
  done
  [ -f "$dir/docker-compose.yml" ] || die "$dir/docker-compose.yml is missing, so the server cannot be taken down under its older name. Nothing was changed."
  image=$SITE_PREFIX/kimina:$(setting_in "$env" KIMINA_IMAGE_TAG)
  image_here "$image" || die "$image (the server's image) is not on this box. Nothing was changed."
  [ "$OLD_WITH_AGENT" -eq 0 ] && return 0
  for key in KIMINA_AGENT_IMAGE KIMINA_AGENT_PORT; do
    [ -n "$(setting_in "$env" "$key")" ] || die "$env has no $key, so its usage agent cannot be moved. Nothing was changed."
  done
  image_here "$(setting_in "$env" KIMINA_AGENT_IMAGE)" \
    || die "$(setting_in "$env" KIMINA_AGENT_IMAGE) (the usage agent's image) is not on this box. Nothing was changed."
  [ "$OLD_WITH_FRONT" -eq 0 ] && return 0
  for key in KIMINA_FRONT_IMAGE KIMINA_SERVER_NAME KIMINA_FRONT_ADDRESS; do
    [ -n "$(setting_in "$env" "$key")" ] || die "$env has no $key, so its TLS front cannot be moved. Nothing was changed."
  done
  [ -f "$dir/docker-compose.front.yml" ] && [ -s "$dir/tls/box.pem" ] \
    || die "$dir has no docker-compose.front.yml or no tls/box.pem, so its TLS front cannot be moved. Nothing was changed."
  image_here "$(setting_in "$env" KIMINA_FRONT_IMAGE)" \
    || die "$(setting_in "$env" KIMINA_FRONT_IMAGE) (the TLS front's image) is not on this box. Nothing was changed."
}

# move_instance DIR : one instance, from an older installer's names and files to today's; as it was if it
# does not come up (move_instance_back).
move_instance() {
  local dir=$1 env=$1/kimina.env next=$1/kimina.env.next instance project files=compose.yaml file
  old_instance "$dir"
  instance=$OLD_INSTANCE
  project=$(lean_server_project "$instance")
  [ "$OLD_WITH_AGENT" -eq 0 ] || files+=:compose.agent.yaml
  [ "$OLD_WITH_FRONT" -eq 0 ] || files+=:compose.tls.yaml
  [ "$(setting_in "$env" KIMINA_REPL_PATH)" != "$LEAN_NATIVE_LAUNCHER" ] || files+=:$LEAN_NATIVE_OVERLAY
  say "Moving ${instance:-instance 1} to Compose's names: the compose project $project ($files)"
  ( umask 077; moved_settings "$env" "${MOVED_INSTANCE_SETTINGS[@]}" > "$next" )
  with_defaults "$next" "${MOVED_INSTANCE_DEFAULTS[@]}"
  printf 'COMPOSE_PROJECT_NAME=%s\nCOMPOSE_FILE=%s\n' "$project" "$files" >> "$next"
  chown root:root "$next"; chmod 0600 "$next"
  # The front's config for the services' names, judged by HAProxy before any container changes.
  if [ "$OLD_WITH_FRONT" -eq 1 ]; then
    render_tls_front "$dir" "$(setting_in "$env" KIMINA_AGENT_IMAGE)" "$(setting_in "$env" KIMINA_FRONT_IMAGE)" \
      "$(setting_in "$env" KIMINA_FRONT_LEAN_TIMEOUT)" "$(setting_in "$env" KIMINA_FRONT_SERVER_WAIT)" \
      "$dir/front/haproxy.cfg.next"
  fi
  cp -p "$env" "$env.$MOVED"
  [ ! -f "$dir/front/haproxy.cfg" ] || cp -p "$dir/front/haproxy.cfg" "$dir/front/haproxy.cfg.$MOVED"
  install_compose_files "$dir" server
  old_instance_down "$dir" || move_instance_back "$dir" "its older projects did not all come down"
  mv -f "$next" "$env"
  [ "$OLD_WITH_FRONT" -eq 0 ] || mv -f "$dir/front/haproxy.cfg.next" "$dir/front/haproxy.cfg"
  for file in docker-compose.yml docker-compose.agent.yml docker-compose.front.yml; do
    [ ! -f "$dir/$file" ] || mv -f "$dir/$file" "$dir/$file.$MOVED"
  done
  if [ "$OLD_RAN" -eq 0 ]; then
    echo "moved ${instance:-instance 1}: its server was not running, and nothing was started"
    return 0
  fi
  compose_in "$env" up -d || move_instance_back "$dir" "its project did not start"
  became_healthy "$project-kimina-1" || move_instance_back "$dir" "its Lean server did not become healthy in 5 minutes"
  if [ "$OLD_WITH_FRONT" -eq 1 ] && ! container_is_steady "$project-front-1"; then
    move_instance_back "$dir" "its TLS front does not stay up"
  fi
  # shellcheck disable=SC2086  # no prefix for instance 1
  /usr/local/bin/kimina-ctl ${instance:+@$instance} check || move_instance_back "$dir" "its check failed"
  echo "moved ${instance:-instance 1}: $project runs, and its check passed"
}

# move_instance_back DIR REASON : the instance as it was before its move began: the new project down, the older
# files back, the older projects up. Then the installer stops with REASON.
move_instance_back() {
  local dir=$1 env=$1/kimina.env file
  if [ -n "$(setting_in "$env" COMPOSE_PROJECT_NAME)" ]; then compose_in "$env" down || true; fi
  [ ! -f "$env.$MOVED" ] || mv -f "$env.$MOVED" "$env"
  for file in docker-compose.yml docker-compose.agent.yml docker-compose.front.yml front/haproxy.cfg; do
    [ ! -f "$dir/$file.$MOVED" ] || mv -f "$dir/$file.$MOVED" "$dir/$file"
  done
  rm -f "$env.next" "$dir/front/haproxy.cfg.next"
  for file in "${LEAN_SERVER_COMPOSE_FILES[@]}"; do rm -f "$dir/$file"; done
  if [ "$OLD_RAN" -eq 1 ]; then
    old_instance_up "$dir" || warn "its older projects did not all start again: docker compose's words are above"
  fi
  die "could not move ${OLD_INSTANCE:-instance 1} to Compose's names: $2. It is back under its older names and files, as it was; fix what the lines above say, then run this again"
}

# can_move_pool DIR : what moving the pool needs is there: its settings, its images, its older file.
can_move_pool() {
  local dir=$1 env=$1/pool.env key
  for key in POOL_IMAGE POOL_HAPROXY_IMAGE POOL_PIN POOL_DNS POOL_CACHE_UPSTREAM_TIMEOUT; do
    [ -n "$(setting_in "$env" "$key")" ] || die "$env has no $key, so it cannot be moved to Compose's names. Nothing was changed."
  done
  [ -f "$dir/docker-compose.pool.yml" ] \
    || die "$dir/docker-compose.pool.yml is missing, so the pool cannot be taken down under its older name. Nothing was changed."
  # deploy/pool always runs TLS: a pool set up before TLS has no certificates to run it with.
  [ -s "$dir/tls/front.pem" ] && [ -s "$dir/tls/proxy-client.pem" ] && [ -s "$dir/tls/ca.crt" ] \
    || die "$dir has no certificates (the pool was set up before TLS), and only a pool with TLS can be moved. Nothing was changed."
  for key in POOL_IMAGE POOL_HAPROXY_IMAGE; do
    image_here "$(setting_in "$env" "$key")" || die "$(setting_in "$env" "$key") is not on this box. Nothing was changed."
  done
}

# move_pool DIR : one pool, from an older installer's names and files to today's, its cache's data
# copied into the new project's volume while nothing runs; as it was if it does not come up (move_pool_back).
move_pool() {
  local dir=$1 env=$1/pool.env next=$1/pool.env.next pool project old_project old_volume new_volume image ran=0
  local checked=0
  pool=${dir##*/lean-pool-}
  project=lean-pool-$pool
  old_project=$SITE_PREFIX-pool-$pool
  old_volume=$SITE_PREFIX-cache-$pool
  new_volume=${project}_cache-data
  image=$(setting_in "$env" POOL_IMAGE)
  if container_exists "$SITE_PREFIX-proxy-$pool"; then ran=1; fi
  say "Moving pool $pool to Compose's names: the compose project $project, its cache's data into $new_volume"
  ( umask 077; moved_settings "$env" "${MOVED_POOL_SETTINGS[@]}" > "$next" )
  with_defaults "$next" "${MOVED_POOL_DEFAULTS[@]}"
  printf 'COMPOSE_PROJECT_NAME=%s\nCOMPOSE_FILE=%s\n' "$project" "$(IFS=:; echo "${LEAN_POOL_COMPOSE_FILES[*]}")" >> "$next"
  chown root:root "$next"; chmod 0600 "$next"
  cp -p "$env" "$env.$MOVED"
  install_compose_files "$dir" pool
  old_compose "$env" "$dir/docker-compose.pool.yml" "$old_project" down || move_pool_back "$dir" "its older project did not come down"
  mv -f "$next" "$env"
  mv -f "$dir/docker-compose.pool.yml" "$dir/docker-compose.pool.yml.$MOVED"
  # Made, not started: the new volume is there, and empty, while nothing runs.
  compose_in "$env" up --no-start || move_pool_back "$dir" "its project could not be made"
  if docker volume inspect "$old_volume" >/dev/null 2>&1; then
    docker run --rm --network none --user 0:0 -v "$old_volume":/from:ro -v "$new_volume":/to \
        --entrypoint cp "$image" -a /from/. /to/ \
      || move_pool_back "$dir" "the cache's data could not be copied from $old_volume into $new_volume"
  fi
  if [ "$ran" -eq 0 ]; then
    compose_in "$env" down || true
    echo "moved pool $pool: it was not running, so nothing was started or checked; its cache's data is in $new_volume, and $old_volume is kept as it was: remove it once the pool has run (docker volume rm $old_volume)"
    return 0
  fi
  compose_in "$env" up -d || move_pool_back "$dir" "its project did not start"
  container_is_steady "$project-haproxy-1" || move_pool_back "$dir" "its proxy does not stay up"
  # Five minutes, as the pool's installer allows: a Lean server behind it may just have started again.
  for _ in $(seq 1 30); do
    if /usr/local/sbin/lean-pool-ctl "$pool" check; then checked=1; break; fi
    sleep 10
  done
  [ "$checked" -eq 1 ] || move_pool_back "$dir" "its check through the pool failed"
  docker volume rm "$old_volume" >/dev/null 2>&1 \
    || warn "could not remove $old_volume, which nothing reads any more: docker volume rm $old_volume"
  echo "moved pool $pool: $project runs with its cache's data, and its check passed"
}

# move_pool_back DIR REASON : the pool as it was before its move began: the new project down and its volume
# removed, the older files back, the older project up (its volume was never changed). Then the installer stops.
move_pool_back() {
  local dir=$1 env=$1/pool.env pool file
  pool=${dir##*/lean-pool-}
  if [ -n "$(setting_in "$env" COMPOSE_PROJECT_NAME)" ]; then
    compose_in "$env" down || true
    docker volume rm "lean-pool-${pool}_cache-data" >/dev/null 2>&1 || true
  fi
  [ ! -f "$env.$MOVED" ] || mv -f "$env.$MOVED" "$env"
  [ ! -f "$dir/docker-compose.pool.yml.$MOVED" ] || mv -f "$dir/docker-compose.pool.yml.$MOVED" "$dir/docker-compose.pool.yml"
  rm -f "$env.next"
  for file in "${LEAN_POOL_COMPOSE_FILES[@]}"; do rm -f "$dir/$file"; done
  old_compose "$env" "$dir/docker-compose.pool.yml" "$SITE_PREFIX-pool-$pool" up -d \
    || warn "its older project did not start again: docker compose's words are above"
  die "could not move pool $pool to Compose's names: $2. It is back under its older names and files, with its cache, as it was; fix what the lines above say, then run this again"
}

# move_to_compose_names : every instance and every pool of this box that an older installer set up, moved (see
# above). Asked once at the end of each installer's pre-flight; a box that has moved passes straight through.
move_to_compose_names() {
  local dir instances=() pools=()
  for dir in /srv/kimina /srv/kimina-*; do
    if [ -f "$dir/kimina.env" ] && [ -z "$(setting_in "$dir/kimina.env" COMPOSE_PROJECT_NAME)" ]; then instances+=("$dir"); fi
  done
  for dir in /srv/lean-pool-*; do
    if [ -f "$dir/pool.env" ] && [ -z "$(setting_in "$dir/pool.env" COMPOSE_PROJECT_NAME)" ]; then pools+=("$dir"); fi
  done
  [ "${#instances[@]}" -gt 0 ] || [ "${#pools[@]}" -gt 0 ] || return 0
  say "This box runs under an older installer's names: moving all of it to the lean-pool project's compose files and names first"
  # Nothing changes unless every one of them can move.
  compose_source server >/dev/null
  [ "${#pools[@]}" -eq 0 ] || compose_source pool >/dev/null
  for dir in ${instances[@]+"${instances[@]}"}; do can_move_instance "$dir"; done
  for dir in ${pools[@]+"${pools[@]}"}; do can_move_pool "$dir"; done
  [ -f "$SRC/kimina-ctl" ] || die "missing $SRC/kimina-ctl - run this from the repo checkout"
  [ "${#pools[@]}" -eq 0 ] || [ -f "$SRC/lean-pool-ctl" ] || die "missing $SRC/lean-pool-ctl - run this from the repo checkout"
  # The control programs that read the new names: they refuse an instance or a pool that has not moved. And the
  # site's names, which lean-pool-ctl reads in a pool's check: a box can be older than the file.
  install -o root -g root -m 0755 "$SRC/kimina-ctl" /usr/local/bin/kimina-ctl
  [ "${#pools[@]}" -eq 0 ] || install -o root -g root -m 0755 "$SRC/lean-pool-ctl" /usr/local/sbin/lean-pool-ctl
  install_site_env
  for dir in ${instances[@]+"${instances[@]}"}; do move_instance "$dir"; done
  for dir in ${pools[@]+"${pools[@]}"}; do move_pool "$dir"; done
  echo "MOVED TO COMPOSE'S NAMES: what each move replaced is kept beside it, ending in .$MOVED"
}
