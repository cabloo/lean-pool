#!/usr/bin/env bash
# install-pool.sh - install the lean-pool front door (HAProxy + the shared result cache) for one pool on
# this host. Reached through `sudo bash install.sh --proxy NAME`; idempotent.
#
# The pool is named after the Lean server instance already installed here (install.sh --instance NAME):
# that server is the pool's first member and its API key is the pool's key.
#
# ALL of the pool's traffic is TLS, with the pool's own certificate authority: clients reach the
# front door over HTTPS, the proxy reaches every Lean server over mutual TLS, and a new server joins over
# HTTPS with a pinned key. Running this on a pool that was installed plain moves it to TLS.
#
# What it does, in order (each step is safe to repeat; a re-run keeps the authority and every certificate):
#   1. pre-flight: docker, the instance, the LAN subnet and resolver; pulls the pinned HAProxy image and
#      stops, with nothing of the pool changed, if it was built without its Prometheus exporter
#   2. builds the lean-pool image (cache, usage agent, join service, tools)
#   3. /srv/lean-pool-NAME (root-owned): compose file, pool.env, the pool key (readable by root and the pool
#      image's group), the server list (this box's server first), the admission cases, the join bundle, and
#      this box's Lean image exported once for joining boxes
#   4. certificates: the pool's authority (root only, made once), the front door's, the proxy's client
#      certificate, and one for this box's own Lean server under its name in the server list
#   5. renders haproxy.cfg in TLS mode and has HAProxy validate it with the certificates in place.
#      Up to here nothing that runs, and no installed control script, has been touched
#   6. the door: lean-pool-ctl and its helper (root-owned), and the kimina-ctl that knows `@proxy-NAME`
#   7. this box's own Lean server goes behind a TLS front (the server and its usage agent stop publishing
#      their ports), then the proxy and the cache start with the new config
#   8. firewall: the pool port and the join port (18110) from the LAN subnet and the docker bridges only
#   9. verifies: a valid and an invalid proof THROUGH the pool, over HTTPS
#
# It never touches instance 1 (/srv/kimina, the instance with no name): not its container, its settings, its
# compose file or its firewall rules.
set -euo pipefail

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'install-pool.sh: %s\n' "$*" >&2; exit 1; }
# shellcheck source=pool-lib.sh
. "$SRC/pool-lib.sh"

POOL=${1:-}
[ $# -gt 0 ] && shift
SITE=""
ADMIT_CASES=""
PORT=18100
JOIN_PORT=18110
SUBNET=""
DNS=""
POOL_SUBNET=172.29.55.0/24
LEAN_TIMEOUT=300
HAPROXY_IMAGE=$LEAN_POOL_HAPROXY_IMAGE
FIREWALL=1
UNINSTALL=0
PURGE=0
FRONT_NAMES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE=$2; shift 2 ;;
    --admit-cases) ADMIT_CASES=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --subnet) SUBNET=$2; shift 2 ;;
    --dns) DNS=$2; shift 2 ;;
    --lean-timeout) LEAN_TIMEOUT=$2; shift 2 ;;
    --front-name) FRONT_NAMES+=("$2"); shift 2 ;;
    --no-firewall) FIREWALL=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    *) die "unknown option $1" ;;
  esac
done
[[ "$POOL" =~ ^[a-z0-9]{1,16}$ ]] || die "usage: install.sh [--site DIR] --proxy NAME --admit-cases DIR [--port N] [--subnet CIDR] [--dns IP] [--lean-timeout S] [--front-name NAME]... [--no-firewall] [--uninstall [--purge]]"
# The site's values: --site DIR, else the site/ a join bundle carries beside this file.
[ -n "$SITE" ] || [ ! -f "$SRC/site/site.env" ] || SITE=$SRC/site
[ -n "$SITE" ] || die "--site DIR is needed: the directory of this site's values (site.env, instances/NAME.env)"
load_site "$SITE"
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || die "--port must be a number"
[[ "$LEAN_TIMEOUT" =~ ^[0-9]{2,4}$ ]] || die "--lean-timeout must be seconds"
for front_name in ${FRONT_NAMES[@]+"${FRONT_NAMES[@]}"}; do
  [[ "$front_name" =~ $LEAN_POOL_DNS_NAME ]] || die "--front-name must be a DNS name in lower case (a name clients use for the pool)"
done
[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo bash install.sh --proxy $POOL"

DIR=/srv/lean-pool-$POOL
INSTANCE_DIR=/srv/kimina-$POOL
compose() { docker compose --project-name "$SITE_PREFIX-pool-$POOL" --env-file "$DIR/pool.env" -f "$DIR/docker-compose.pool.yml" "$@"; }

if [ "$UNINSTALL" -eq 1 ]; then
  say "Uninstalling the $POOL pool's front door (the Lean servers stay)"
  docker rm -f -v "$SITE_PREFIX-join-$POOL" >/dev/null 2>&1 || true
  if [ -f "$DIR/pool.env" ]; then compose down || true; fi
  remove_lan_firewall "proxy-$POOL"
  remove_lan_firewall "join-$POOL"
  if [ "$PURGE" -eq 1 ]; then
    docker volume rm "$SITE_PREFIX-cache-$POOL" 2>/dev/null || true
    rm -rf "$DIR"
    echo "removed $DIR (the pool's certificate authority with it: every certificate it signed is void) and the cache volume"
  else
    echo "kept $DIR (the certificate authority included) and the cache volume; add --purge to remove them"
  fi
  exit 0
fi

# ------------------------------------------------------------------------------------------ pre-flight
say "Pre-flight (pool $POOL)"
command -v docker >/dev/null || die "docker is not installed"
! docker_is_snap || die "$SNAP_DOCKER_UNSUPPORTED"
command -v python3 >/dev/null || die "python3 is needed on the host (the admission cases, and the join window's checks)"
[ -f "$INSTANCE_DIR/kimina.env" ] || die "no Lean server instance '$POOL' here; first: sudo bash install.sh --instance $POOL"
for file in docker-compose.pool.yml docker-compose.agent.yml docker-compose.front.yml lean-pool-ctl kimina-ctl kimina-firewall \
            join_helper.py join-box.sh "${LEAN_POOL_JOIN_FILES[@]}"; do
  [ -f "$SRC/$file" ] || die "missing $SRC/$file - run this from the repo checkout"
done
[ -f "$SITE/instances/$POOL.env" ] || die "the site has no instance '$POOL' ($SITE/instances/$POOL.env): a joining box installs it from there"
# What a server must verify and reject before it may join: the site's own cases (leanpool-admit reads them).
[ -n "$ADMIT_CASES" ] || die "--admit-cases DIR is needed: the cases a server must verify and reject before it may join pool '$POOL'"
[ -d "$ADMIT_CASES" ] && [ -n "$(ls -A "$ADMIT_CASES" 2>/dev/null)" ] || die "--admit-cases: $ADMIT_CASES is not a directory of cases"
instance_setting() { setting_in "$INSTANCE_DIR/kimina.env" "$1"; }
INSTANCE_PORT=$(instance_setting KIMINA_PORT)
INSTANCE_WORKERS=$(instance_setting KIMINA_MAX_REPLS)
INSTANCE_WAIT=$(instance_setting KIMINA_MAX_WAIT)
PIN=$(instance_setting KIMINA_IMAGE_TAG)
[ -n "$INSTANCE_PORT" ] && [ -n "$INSTANCE_WORKERS" ] && [ -n "$PIN" ] || die "$INSTANCE_DIR/kimina.env is incomplete"
AGENT_PORT=$((INSTANCE_PORT + 200))
SERVER_WAIT=${INSTANCE_WAIT:-120}

[ "$FIREWALL" -eq 0 ] || require_firewall_tools
if [ "$FIREWALL" -eq 1 ] && [ -z "$SUBNET" ]; then
  SUBNET=$(detect_lan_subnet) || true
  [ -n "$SUBNET" ] || die "could not detect the LAN subnet; pass --subnet CIDR or --no-firewall"
  echo "LAN subnet: $SUBNET"
fi
if [ -z "$DNS" ]; then
  DNS=$(ip -4 route show default | awk '{print $3; exit}')
  [ -n "$DNS" ] || die "could not find the LAN resolver (the default gateway); pass --dns IP"
fi
[[ "$DNS" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--dns must be an IPv4 address"
echo "resolver for the proxy (other boxes are named on the LAN): $DNS"
# This box's own Lean server is recreated below if its compose file changed, and that file gives the container
# an init process: a Docker that cannot start one is found out here, before anything is written.
if [ -n "$(docker image inspect -f '{{.Id}}' "$SITE_PREFIX/kimina:$PIN" 2>/dev/null || true)" ] && ! init_starts "$SITE_PREFIX/kimina:$PIN"; then
  die "$(init_unsupported "$SITE_PREFIX/kimina:$PIN")"
fi
GATEWAY=$(echo "$POOL_SUBNET" | sed 's|\.[0-9]*/[0-9]*$|.1|')
# The names clients use for this host: its short name and that name on the LAN.
SHORT=$(hostname -s | tr '[:upper:]' '[:lower:]')
[[ "$SHORT" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || die "this host's short name '$SHORT' cannot be written on a certificate (letters, digits and '-' only)"
# The proxy answers a Prometheus scraper with HAProxy's own exporter, and an HAProxy built
# without it refuses the pool's config. The pinned image is pulled here, as every run pulls it, and asked
# what it was built with: before the pool's image is built and before anything is written or installed.
docker pull -q "$HAPROXY_IMAGE" >/dev/null || die "could not pull $HAPROXY_IMAGE"
haproxy_has_exporter "$HAPROXY_IMAGE" || die "$(exporter_missing "$HAPROXY_IMAGE")"
# A missing compose plugin is added, not reported: here, once every other check has passed.
ensure_compose_plugin \
  || die "the docker compose plugin is not installed and could not be added: $COMPOSE_PLUGIN_FAILURE. Nothing else was installed."

# --------------------------------------------------------------------------------------------- images
build_lean_pool_image
# The groups the two images run as, read from the images: every file a container must read is given to
# exactly its group below, and nothing is world-readable that holds a key.
HAPROXY_GID=$(image_group_id "$HAPROXY_IMAGE") || die "could not read the group $HAPROXY_IMAGE runs as"
POOL_GID=$(image_group_id "$LEAN_POOL_IMAGE") || die "could not read the group $LEAN_POOL_IMAGE runs as"

# ----------------------------------------------------------------------------------- files and settings
say "Installing files into $DIR"
install_site_env             # the site's names, for lean-pool-ctl (its check of the config below runs it)
install -d -o root -g root -m 0755 "$DIR" "$DIR/haproxy"
install -o root -g root -m 0644 "$SRC/docker-compose.pool.yml" "$DIR/"
# The pool's key is this instance's key. Readable by root and by the pool image's group (the cache, and
# the join service while a window is open), nobody else.
( umask 077; instance_setting KIMINA_API_KEY > "$DIR/api-key" )
[ -s "$DIR/api-key" ] || die "the instance has no API key"
chown root:"$POOL_GID" "$DIR/api-key"; chmod 0640 "$DIR/api-key"

( umask 077; : >> "$DIR/pool.env" )
chown root:root "$DIR/pool.env"; chmod 0600 "$DIR/pool.env"
pool_setting() { setting_in "$DIR/pool.env" "$1"; }
set_env_in "$DIR/pool.env" POOL_NAME "$POOL"
set_env_in "$DIR/pool.env" POOL_IMAGE "$LEAN_POOL_IMAGE"
set_env_in "$DIR/pool.env" POOL_IMAGE_GID "$POOL_GID"
set_env_in "$DIR/pool.env" POOL_HAPROXY_IMAGE "$HAPROXY_IMAGE"
set_env_in "$DIR/pool.env" POOL_PUBLIC_PORT "$PORT"
set_env_in "$DIR/pool.env" POOL_PIN "$PIN"
set_env_in "$DIR/pool.env" POOL_LEAN_TIMEOUT "$LEAN_TIMEOUT"
set_env_in "$DIR/pool.env" POOL_SERVER_WAIT "$SERVER_WAIT"
set_env_in "$DIR/pool.env" POOL_SUBNET "$POOL_SUBNET"
set_env_in "$DIR/pool.env" POOL_DNS "$DNS"
set_env_in "$DIR/pool.env" POOL_JOIN_PORT "$JOIN_PORT"
# This installer's version (pool-lib.sh, installer_version), which the pool's GET /health carries.
POOL_VERSION=$(installer_version "$POOL") \
  || { warn "could not compute this installer's version: the pool's GET /health carries none"; POOL_VERSION=""; }
set_env_in "$DIR/pool.env" POOL_INSTALLER_VERSION "$POOL_VERSION"
grep -q '^POOL_MAX_REQUEST_BYTES=' "$DIR/pool.env" || echo "POOL_MAX_REQUEST_BYTES=262144" >> "$DIR/pool.env"
grep -q '^POOL_CACHE_MAX_BYTES=' "$DIR/pool.env" || echo "POOL_CACHE_MAX_BYTES=4294967296" >> "$DIR/pool.env"
grep -q '^POOL_CACHE_UPSTREAM_TIMEOUT=' "$DIR/pool.env" || echo "POOL_CACHE_UPSTREAM_TIMEOUT=1200" >> "$DIR/pool.env"
# The host name a new box is told to fetch its join script from. Kept once set, so it can be edited here.
grep -q '^POOL_JOIN_HOST=' "$DIR/pool.env" || echo "POOL_JOIN_HOST=$SHORT.$SITE_LAN_DOMAIN" >> "$DIR/pool.env"
# Further names clients use for the front door (--front-name). Kept across re-runs: a re-run without the
# option asks for the same certificate as before.
if [ "${#FRONT_NAMES[@]}" -gt 0 ]; then
  set_env_in "$DIR/pool.env" POOL_FRONT_NAMES "$(IFS=,; echo "${FRONT_NAMES[*]}")"
fi
EXTRA_FRONT_NAMES=$(pool_setting POOL_FRONT_NAMES)
[[ "$EXTRA_FRONT_NAMES" =~ ^[a-z0-9.,-]*$ ]] || die "POOL_FRONT_NAMES in $DIR/pool.env must be DNS names separated by commas"

if [ ! -f "$DIR/servers" ]; then
  # This box's own Lean server, reached at the pool network's gateway (an IPv4 literal, see the compose file).
  printf '# The Lean servers of pool %s: NAME HOST:PORT WORKERS AGENT_PORT. Edit with lean-pool-ctl, not by hand.\n%s %s:%s %s %s\n' \
    "$POOL" "$SHORT" "$GATEWAY" "$INSTANCE_PORT" "$INSTANCE_WORKERS" "$AGENT_PORT" > "$DIR/servers"
  chmod 0644 "$DIR/servers"
  echo "server list started with this box's server ($GATEWAY:$INSTANCE_PORT, $INSTANCE_WORKERS workers)"
else
  echo "keeping the existing server list"
fi
# This box's own server, by the name the list gives it: that name is what its certificate must carry.
OWN_NAME=$(awk -v address="$GATEWAY:$INSTANCE_PORT" '$1 !~ /^#/ && $2 == address {print $1; exit}' "$DIR/servers")
OWN_NAME=${OWN_NAME:-$SHORT}
# The proxy checks a server's certificate against its listed name in LOWER CASE, whatever case the list has
# (an older install listed the host name as it was). A name with a '_' cannot be on a certificate at all.
OWN_NAME=$(echo "$OWN_NAME" | tr '[:upper:]' '[:lower:]')
[[ "$OWN_NAME" =~ $LEAN_POOL_DNS_NAME ]] && [ "${#OWN_NAME}" -le 40 ] \
  || die "this box's server is listed as '$OWN_NAME', which cannot be the name on a certificate (letters, digits, '-' and '.'): rename it in $DIR/servers, then re-run"

say "Admission cases (what a server must verify and reject before it may join), from $ADMIT_CASES"
rm -rf "$DIR/admit-cases.new"
cp -R "$ADMIT_CASES" "$DIR/admit-cases.new"
chown -R root:root "$DIR/admit-cases.new"
rm -rf "$DIR/admit-cases"
mv -f "$DIR/admit-cases.new" "$DIR/admit-cases"

say "Join bundle (what a new box downloads while a join window is open; it holds no key)"
install -d -o root -g root -m 0755 "$DIR/join"
pack_join_bundle "$DIR/join/bundle.b64.new" "$POOL"
chown root:root "$DIR/join/bundle.b64.new"; chmod 0644 "$DIR/join/bundle.b64.new"
mv -f "$DIR/join/bundle.b64.new" "$DIR/join/bundle.b64"
install -o root -g root -m 0644 "$SRC/join-box.sh" "$DIR/join/join-box.sh"

# This host's Lean image, exported once, so that a joining box loads it instead of building.
say "The Lean server's image for joining boxes (exported once per image; a box without it builds its own)"
export_join_image "$DIR" "$POOL_GID" "$SITE_PREFIX/kimina:$PIN"

# ----------------------------------------------------------------------------------------- certificates
# Layout, with who may read what:
#   pki/        root only (0700). pki/ca: the authority (ca.key never leaves this directory and no running
#               container mounts it). pki/proxy and pki/box: what leanpool-pki issued, keys included.
#   tls/        the PROXY's view: root:<HAProxy's group> 0750; front.pem and proxy-client.pem 0640 (they hold
#               private keys), ca.crt 0644. Mounted read-only in the proxy container.
#   certs/      public certificates only (0755/0644): what `status` reads for the days left, and `ca` prints.
say "Certificates: the pool's own authority, the front door, the proxy's client certificate, this box's server"
install -d -o root -g root -m 0700 "$DIR/pki"
pki() { docker run --rm --network none --user 0:0 -v "$DIR/pki":/pki "$LEAN_POOL_IMAGE" leanpool-pki "$@"; }
issued() { [ -s "$DIR/pki/$1.crt" ] && [ -s "$DIR/pki/$1.key" ] && [ -s "$DIR/pki/$1.pem" ]; }

if [ -s "$DIR/pki/ca/ca.crt" ] && [ -s "$DIR/pki/ca/ca.key" ]; then
  echo "keeping the pool's certificate authority (so every certificate it signed stays valid)"
elif [ -e "$DIR/pki/ca/ca.crt" ] || [ -e "$DIR/pki/ca/ca.key" ]; then
  die "$DIR/pki/ca holds half an authority (ca.crt or ca.key is missing or empty); it is never overwritten: look at it by hand"
else
  pki init --ca-dir /pki/ca --name "lean-pool $POOL CA" >/dev/null || die "could not make the pool's certificate authority"
  echo "made the pool's certificate authority (its key is root-only in $DIR/pki/ca and never leaves this host)"
fi

# The front door: every name and address a client uses for the pool. The names are DNS names; the two
# addresses are loopback (lean-pool-ctl checks the pool through it) and the docker0 bridge (where a
# container on this host reaches it). A changed set of names is the only thing that makes a new one.
BRIDGE=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
[ -n "$BRIDGE" ] || BRIDGE=$({ ip -4 -o addr show dev docker0 2>/dev/null || true; } | awk '{split($4, parts, "/"); print parts[1]; exit}')
[[ "$BRIDGE" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { warn "could not read the docker0 bridge address: the front door's certificate will not name it"; BRIDGE=""; }
front_arguments=(--name "$SHORT" --dns "$SHORT.$SITE_LAN_DOMAIN")
wanted_names="$SHORT, $SHORT.$SITE_LAN_DOMAIN"
if [ -n "$EXTRA_FRONT_NAMES" ]; then
  IFS=, read -r -a extra_names <<<"$EXTRA_FRONT_NAMES"
  for front_name in "${extra_names[@]}"; do
    [[ "$front_name" =~ $LEAN_POOL_DNS_NAME ]] || die "POOL_FRONT_NAMES in $DIR/pool.env: '$front_name' is not a DNS name in lower case"
    front_arguments+=(--dns "$front_name"); wanted_names="$wanted_names, $front_name"
  done
fi
front_arguments+=(--ip 127.0.0.1); wanted_names="$wanted_names, 127.0.0.1"
if [ -n "$BRIDGE" ]; then front_arguments+=(--ip "$BRIDGE"); wanted_names="$wanted_names, $BRIDGE"; fi
if issued proxy/front && [ "$(cat "$DIR/pki/proxy/front.names" 2>/dev/null || true)" = "$wanted_names" ]; then
  echo "keeping the front door's certificate (its public-key pin is unchanged)"
else
  if issued proxy/front; then echo "the front door's names changed: issuing a new certificate (clients keep working: they trust the authority)"; fi
  pki issue-server --ca-dir /pki/ca --out-dir /pki/proxy --file-stem front "${front_arguments[@]}" --replace >/dev/null \
    || die "could not issue the front door's certificate"
  printf '%s\n' "$wanted_names" > "$DIR/pki/proxy/front.names"
  echo "issued the front door's certificate for: $wanted_names"
fi
if issued proxy/proxy-client; then
  echo "keeping the proxy's client certificate"
else
  pki issue-client --ca-dir /pki/ca --out-dir /pki/proxy --file-stem proxy-client --name "$LEAN_POOL_PROXY_CLIENT_NAME" --replace >/dev/null \
    || die "could not issue the proxy's client certificate"
  echo "issued the proxy's client certificate ($LEAN_POOL_PROXY_CLIENT_NAME)"
fi
if issued box/box && [ "$(cat "$DIR/pki/box/box.name" 2>/dev/null || true)" = "$OWN_NAME" ]; then
  echo "keeping this box's server certificate ($OWN_NAME)"
else
  pki issue-server --ca-dir /pki/ca --out-dir /pki/box --file-stem box --name "$OWN_NAME" --replace >/dev/null \
    || die "could not issue this box's server certificate"
  printf '%s\n' "$OWN_NAME" > "$DIR/pki/box/box.name"
  echo "issued this box's server certificate for its name in the server list: $OWN_NAME"
fi
install -o root -g root -m 0644 "$DIR/pki/ca/ca.crt" "$DIR/pki/box/ca.crt"

# What the proxy container reads, by HAProxy's own group; and the public certificates.
install_dir_for_gid 0750 "$HAPROXY_GID" "$DIR/tls"
for pem in front proxy-client; do
  install_for_gid 0640 "$HAPROXY_GID" "$DIR/pki/proxy/$pem.pem" "$DIR/tls/$pem.pem.new"
  mv -f "$DIR/tls/$pem.pem.new" "$DIR/tls/$pem.pem"
done
install -o root -g root -m 0644 "$DIR/pki/ca/ca.crt" "$DIR/tls/ca.crt"
install -d -o root -g root -m 0755 "$DIR/certs" "$DIR/certs/servers"
install -o root -g root -m 0644 "$DIR/pki/ca/ca.crt" "$DIR/certs/ca.crt"
install -o root -g root -m 0644 "$DIR/pki/proxy/front.crt" "$DIR/certs/front.crt"
install -o root -g root -m 0644 "$DIR/pki/proxy/proxy-client.crt" "$DIR/certs/proxy-client.crt"
install -o root -g root -m 0644 "$DIR/pki/box/box.crt" "$DIR/certs/servers/$OWN_NAME.crt"

# ------------------------------------------------------------------- config: validated before any change
# With the control script from this checkout, before the installed one is replaced: if HAProxy refuses the
# config, the pool, its containers and its control scripts are all exactly what they were.
say "Rendering haproxy.cfg in TLS mode and having HAProxy validate it with the certificates in place"
bash "$SRC/lean-pool-ctl" "$POOL" render --check >/dev/null \
  || die "could not render a valid HAProxy config; nothing that runs was changed"

# ------------------------------------------------------------------------------------------- the door
install -o root -g root -m 0755 "$SRC/lean-pool-ctl" /usr/local/sbin/lean-pool-ctl
install -o root -g root -m 0755 "$SRC/kimina-ctl" /usr/local/bin/kimina-ctl
install -d -o root -g root -m 0755 "$SITE_HELPER_DIR"
install -o root -g root -m 0755 "$SRC/join_helper.py" "$SITE_HELPER_DIR/join_helper.py"

# ------------------------------------------------------------------ this box's own server: a TLS front
# The shared function a joining box uses too. It validates the front's config before it touches a
# container; then the Lean server is recreated without its published port, once.
if [ "$FIREWALL" -eq 1 ]; then install_tls_front "$INSTANCE_DIR" "$POOL" "$OWN_NAME" "$DIR/pki/box" "$LEAN_TIMEOUT" "$SERVER_WAIT" "$SUBNET"
else install_tls_front "$INSTANCE_DIR" "$POOL" "$OWN_NAME" "$DIR/pki/box" "$LEAN_TIMEOUT" "$SERVER_WAIT" ""; fi

# ---------------------------------------------------------------------------------- start, firewall
say "Installing the config and starting the proxy and the cache"
rendered=$(/usr/local/sbin/lean-pool-ctl "$POOL" render --no-reload) || die "could not install the HAProxy config"
upstream_timeout=$(echo "$rendered" | sed -n 's/^cache_upstream_timeout=//p')
[ -z "$upstream_timeout" ] || set_env_in "$DIR/pool.env" POOL_CACHE_UPSTREAM_TIMEOUT "$upstream_timeout"
proxy_before=$(docker inspect -f '{{.Id}}' "$SITE_PREFIX-proxy-$POOL" 2>/dev/null || true)
compose up -d
reload_if_kept "$SITE_PREFIX-proxy-$POOL" "$proxy_before"     # a proxy that kept running loads the config now

if [ "$FIREWALL" -eq 1 ]; then
  say "Firewall"
  install_lan_firewall "proxy-$POOL" "$PORT" "$SUBNET"
  # The join service runs only while a window is open; its port is LAN-only all the time.
  install_lan_firewall "join-$POOL" "$JOIN_PORT" "$SUBNET"
else
  warn "--no-firewall: ports $PORT and $JOIN_PORT are reachable from every network this host is on"
fi

# --------------------------------------------------------------------------------------------- verify
say "Verifying: one valid and one invalid proof through the pool, over HTTPS (the first check imports Mathlib)"
verdict="POOL INSTALLED AND VERIFIED"
checked=0
# Five minutes, as install.sh allows the same server: this box's Lean server may just have been recreated
# (to drop its published port), and it answers nothing until its workers have started.
for _ in $(seq 1 30); do
  if /usr/local/sbin/lean-pool-ctl "$POOL" check; then checked=1; break; fi
  sleep 10
done
[ "$checked" -eq 1 ] || verdict="POOL INSTALLED, BUT THE CHECK THROUGH IT FAILED - see: sudo lean-pool-ctl $POOL status ; sudo lean-pool-ctl $POOL logs 200"
# This box's own Lean server with its root read-only, checked by itself: through the pool,
# another server could have answered. One that fails is made writable again and checked once more. It runs
# after a failed check through the pool too (a pool whose only server the read-only root broke is put back),
# and the first failure stays the verdict.
if [ "$(setting_in "$INSTANCE_DIR/kimina.env" KIMINA_ROOT_READ_ONLY)" = true ]; then
  say "Verifying this box's own Lean server by itself, with its root read-only"
  if ! confirm_read_only_root "$INSTANCE_DIR" "$POOL" && [ "$checked" -eq 1 ]; then
    verdict="POOL INSTALLED, BUT THIS BOX'S OWN LEAN SERVER FAILED ITS CHECK - see: sudo kimina-ctl @$POOL logs 200"
  fi
fi
/usr/local/sbin/lean-pool-ctl "$POOL" status || true

cat <<EOF

$verdict
  address     https://$SHORT.$SITE_LAN_DOMAIN:$PORT   (the pool's API key: sudo kimina-ctl @$POOL show-key)
  trust       clients check the pool's authority: sudo lean-pool-ctl $POOL ca > lean-pool-$POOL-ca.crt
  settings    $DIR/pool.env, server list $DIR/servers
  operate     sudo lean-pool-ctl $POOL status | servers | check | ca | admit HOST:PORT WORKERS [SERVER] | logs [N] | restart
  add a box   sudo lean-pool-ctl $POOL join-open     (prints the one command to run on the new box)
EOF
# This host's own Lean server: whether its workers run linarith's search step compiled.
[ -z "${COMPILED_SEARCH_SAID:-}" ] || echo "  search      $COMPILED_SEARCH_SAID"
# And whether its root filesystem is read-only.
[ -z "${READ_ONLY_SAID:-}" ] || echo "  read-only   $READ_ONLY_SAID"
[ "$verdict" = "POOL INSTALLED AND VERIFIED" ]
