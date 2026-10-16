# join-box.sh - the BODY of a lean-pool join script: what runs, as root, on a box that joins a pool.
#
# Not run from here. `lean-pool-ctl NAME join-open` (on the pool's host) puts five settings in front of
# this text (JOIN_URL, which holds the window's token; JOIN_PIN; JOIN_POOL; JOIN_LEAN_TIMEOUT;
# JOIN_SERVER_WAIT), the installer bundle behind it as the function `bundle`, and the call of `main` on the
# LAST line, so a download that was cut short runs nothing. The new box then runs the result with
#
#   curl -fsSk --pinnedpubkey '<pin>' https://<pool host>:18110/j/<token> | sudo bash
#   ... | sudo bash -s -- --name box2 --workers 6          (options go after `bash -s --`)
#
# It holds no key. This box makes its own private key, which never leaves it; the pool signs a certificate
# for this box's NAME and nothing else; the pool's API key arrives inside the pinned connection.
#
# What it does, in order. A failure at any step says which step, and leaves the pool as it was:
#   1. checks Docker (the snap package of Docker is refused here: it cannot mount what the installer needs)
#   2. unpacks the installer into a root-only temporary directory (deleted on EVERY way out, with the key copy)
#   3. builds the pool's image here: its Python reads and writes every JSON below, and makes the key
#   4. asks the pool whether it ships the Lean server's image (a request that binds nothing), then
#      runs the instance installer's own pre-flight (free disk, free ports, the LAN subnet, the firewall's
#      tools), so that a box that cannot take the installer stops HERE, with the installer's own message,
#      before the pool has been asked for anything: the join window stays open for another try. Then, if
#      this box has Docker without its compose plugin, installs the plugin (from the package
#      manager, else a pinned release binary whose SHA-256 is checked)
#   5. gets the Lean server's image. From the POOL where the pool ships one for this machine type:
#      downloaded through the pinned request, its SHA-256 checked before it is loaded, the loaded
#      image checked against the manifest; the file is deleted on every way out. Otherwise, or if that
#      cannot be done, it is BUILT here (the long part). Either way no secret is needed, so this comes
#      BEFORE the pool is asked to sign anything: a box that fails here has not used its join
#      window, and runs the same command again once the cause is put right. A copy of the installer is
#      kept from here on, so that removing what this box holds is one command
#   6. makes this box's private key and a signing request for its name
#   7. sends the request to the pool and waits for the certificate, the authority's certificate and the key
#      (from here on the window is this box's: a new attempt needs a new window)
#   8. runs the instance installer: the Lean server from the image of step 5 (not built now) with the
#      pool's key, the usage agent, the TLS front (only the pool proxy's certificate is let in), the
#      LAN-only firewall
#   9. tells the pool it is ready; the pool tests this server ALONE and only then adds it
#  10. prints the pool's verdict
#
# With `--refresh` it is for a box that is ALREADY in the pool, and does none of steps 3 to
# 10: after steps 1 and 2 it runs the instance installer of this bundle the way the box's join ran it, told
# to keep the image, and replaces the installer copy. The box keeps its key, its certificate, its settings
# and its place in the pool; the pool is asked for nothing, so the window is not used up.
set -euo pipefail

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'lean-pool join: %s\n' "$*" >&2; exit 1; }

WORK=""
IMAGE_FILE=""             # the pool's image while it is being downloaded and loaded: gone on EVERY way out
cleanup() {
  [ -z "$IMAGE_FILE" ] || rm -f "$IMAGE_FILE"
  [ -z "$WORK" ] || rm -rf "$WORK"
  # The lowest CPU weight again, if the admission test below was running at the box's ordinary one.
  if declare -F restore_cpu_priority >/dev/null; then restore_cpu_priority; fi
}

# What the pool's host serves is not what its manifest says: stop, with nothing loaded or
# built in its place. Never a fallback: this is looked at before anything joins.
alarm() {
  [ -z "$IMAGE_FILE" ] || rm -f "$IMAGE_FILE"
  die "step 5 ALARM: $1. What the pool's host sent is not what its manifest names, so it was deleted, what the load brought in was removed, and NOTHING was built in its place. Do not join this pool until that is understood: on the pool's host, 'sudo lean-pool-ctl $JOIN_POOL status', and 'sudo bash install.sh --proxy $JOIN_POOL' exports the image again. The pool has signed nothing. (Once it is understood, the same command with '--build' after 'sudo bash -s --' builds the image here and does not take the pool's.)"
}

join_usage() {
  cat <<'EOF'
Join this box to a lean-pool. Run exactly what `lean-pool-ctl NAME join-open` printed on the pool's host:
  curl -fsSk --pinnedpubkey '<pin>' https://<pool host>:18110/j/<token> | sudo bash -s -- [options]

Options:
  --name NAME      this server's name in the pool: one DNS label, no dot (default: this host's short name,
                   in lower case)
  --workers N      Lean workers, 1-32 (default: the smaller of cores - 2 and available RAM / 4 GiB, at least 1)
  --cpus N         CPU ceiling of the Lean server (default: the worker count)
  --no-firewall    do not add the LAN-only firewall rules (a box whose firewall is not systemd-managed
                   iptables, such as Docker inside WSL)
  --build          build the Lean server's image on this box even if the pool ships one (the pool's image is
                   then neither asked for nor loaded)
  --refresh        for a box that is ALREADY in this pool: install this window's installer over the one the
                   box has (a changed compose file, a newer control script) and nothing else. Its Lean image,
                   its key, its certificate, its settings and its place in the pool stay; the pool is asked
                   for nothing, so the window is not used up and the same command refreshes the next box.
                   It goes alone: no other option
EOF
}

# A copy of the installer in the instance's directory, so that removing what this box holds is one command
# there, whatever happens next. The copy holds no key: the bundle never did.
keep_installer_copy() {   # INSTANCE_DIR
  rm -rf "$1/installer"
  install -d -o root -g root -m 0755 "$1/installer"
  cp -R "$SRC/." "$1/installer/"
  chown -R root:root "$1/installer"
  chmod -R go-w "$1/installer"
}

# A box that is ALREADY in the pool takes the installer this window carries, and nothing else
# of it changes. The instance installer of the bundle is run the way the box's join ran it (no door, the
# firewall as the box has it) and told to keep the image whatever its own build files and
# job count say. What that installer finds on a box it keeps: the pool's key, the settings, the certificate,
# the TLS front. The pool is asked for nothing: no signing request, so nothing is signed, its list does not
# change, and the window is as good as before for the next box.
refresh_box() {           # INSTANCE_DIR IMAGE_TAG
  local dir=$1 tag=$2 name options
  [ ! -e "/srv/lean-pool-$JOIN_POOL/pool.env" ] \
    || die "--refresh is for a box that JOINED pool $JOIN_POOL, and this is the pool's own host: its Lean server is refreshed with the pool, by 'sudo bash install.sh --proxy $JOIN_POOL' from the checkout. Nothing was changed."
  [ -f "$dir/kimina.env" ] && [ -s "$dir/tls/box.pem" ] \
    || die "--refresh: this box holds no server of pool $JOIN_POOL ($dir has no settings file or no certificate), so there is nothing to refresh. To JOIN the pool, run the same command without --refresh. Nothing was changed."
  # Read before the installer moves the box's settings to Compose's names, on a box that has not moved yet.
  name=$(setting_moved_in "$dir/kimina.env" LEAN_SERVER_NAME KIMINA_SERVER_NAME)
  options=(--instance "$JOIN_POOL" --no-door --keep-image)
  [ -f "$dir/firewall.conf" ] || options+=(--no-firewall)      # it joined without the firewall, and stays so
  say "3/3 Installing this window's installer over the one '$name' has (its image, key, certificate, settings and place in the pool stay)"
  bash "$SRC/install.sh" "${options[@]}" </dev/null \
    || die "step 3 failed: the installer did not finish (its output is above). The pool's list is unchanged and the pool was asked for nothing: put the cause right and run the same command again while the window is open. The installer copy in $dir/installer is still the one this box had."
  keep_installer_copy "$dir"
  cat <<EOF

REFRESHED: '$name' on this box now runs from this window's installer.
  kept          its Lean image ($tag), the pool's key, its certificate, its settings, its place in pool $JOIN_POOL
  the pool      was asked for nothing: this window is still open, and the same command refreshes another box
  status here   sudo kimina-ctl @$JOIN_POOL status
EOF
}

# EVERY request to the pool goes through here. It is pinned to the front door's public key (curl refuses
# any other server before it sends or receives anything: exit 90), and the URL, which holds the window's
# token, is handed to curl on standard input: it is never on a command line, where `ps` would show it.
#   join_curl PATH [curl options...] : prints the HTTP status; the body goes where -o says
join_curl() {
  local path=$1
  shift
  printf 'url = "%s%s"\n' "$JOIN_URL" "$path" \
    | curl -sSk --pinnedpubkey "$JOIN_PIN" --config - --max-time 60 -w '%{http_code}' "$@"
}

# The pool image's tools, with no network and the temporary directory as their only view of this box.
# (A new box may have no python3 and no jq; nothing below parses JSON with sed.)
helper() { docker run --rm --network none --user 0:0 -v "$WORK":/work "$LEAN_POOL_IMAGE" python /work/join/join_helper.py "$@" </dev/null; }
pki()    { docker run --rm --network none --user 0:0 -v "$WORK":/work "$LEAN_POOL_IMAGE" leanpool-pki "$@" </dev/null; }

main() {
  local name="" workers="" cpus="" firewall=1 build_here=0 refresh=0 of=10
  while [ $# -gt 0 ]; do
    case "$1" in
      --name|--workers|--cpus) [ $# -ge 2 ] || die "$1 needs a value" ;;&
      --name) name=$2; shift 2 ;;
      --workers) workers=$2; shift 2 ;;
      --cpus) cpus=$2; shift 2 ;;
      --no-firewall) firewall=0; shift ;;
      --build) build_here=1; shift ;;
      --refresh) refresh=1; shift ;;
      -h|--help) join_usage; exit 0 ;;
      *) join_usage >&2; die "unknown option $1" ;;
    esac
  done
  if [ "$refresh" -eq 1 ]; then      # a refresh: three steps, and nothing of the box is chosen anew
    [ -z "$name$workers$cpus" ] && [ "$firewall" -eq 1 ] && [ "$build_here" -eq 0 ] \
      || die "--refresh goes alone: a box that is refreshed keeps the name, the workers, the limits, the firewall and the image it has"
    of=3
  fi
  [ "$(id -u)" -eq 0 ] || die "this must run as root: end the command with '| sudo bash'"
  if [ "$refresh" -eq 0 ]; then
    [ -n "$name" ] || name=$(hostname -s | tr '[:upper:]' '[:lower:]')
    # One DNS label: the name is also the name on this box's certificate, and the pool signs no dotted name.
    [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] \
      || die "the server name '$name' cannot be this box's name in the pool; pass --name NAME (one label: letters, digits and '-', in lower case, no dot, at most 40)"
  fi
  [ -z "$workers" ] || { [[ "$workers" =~ ^[0-9]{1,2}$ ]] && [ "$workers" -ge 1 ] && [ "$workers" -le 32 ]; } || die "--workers must be 1-32"
  [ -z "$cpus" ] || { [[ "$cpus" =~ ^[0-9]{1,2}$ ]] && [ "$cpus" -ge 1 ]; } || die "--cpus must be a number"

  # ----------------------------------------------------------------------------------------- 1. Docker
  say "1/$of Checking Docker"
  local tool
  for tool in docker curl tar base64 mktemp; do
    command -v "$tool" >/dev/null || die "step 1 failed: '$tool' is not installed on this box. Nothing was installed."
  done
  # Docker from the snap package sees its own /tmp and no /srv: the installer could mount nothing into a
  # container, and would stop two steps on with a message about a missing file. Said here instead.
  local unsupported="step 1 failed: Docker on this box is the snap package, which is not supported: it cannot mount /tmp or /srv into a container. Remove it (sudo snap remove docker), install Docker's own packages (https://docs.docker.com/engine/install/), then run the join command again. Nothing was installed."
  case "$(command -v docker)" in /snap/*|/var/lib/snapd/*) die "$unsupported" ;; esac
  docker info >/dev/null 2>&1 || die "step 1 failed: the docker daemon is not reachable. Nothing was installed."
  case "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)" in /var/snap/*|/snap/*) die "$unsupported" ;; esac
  # The compose plugin is not looked at here: a box without it gets it in step 4, by the
  # one implementation in pool-lib.sh, which reaches this box only inside the bundle unpacked next.

  # ------------------------------------------------------------------------------------------ 2. unpack
  say "2/$of Unpacking the installer"
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  WORK=$(mktemp -d) || die "step 2 failed: no temporary directory"
  chmod 0700 "$WORK"
  bundle | base64 -d | tar -xzf - --no-same-owner -C "$WORK" || die "step 2 failed: the installer bundle did not unpack. Nothing was installed."
  SRC=$WORK/join
  [ -f "$SRC/install.sh" ] && [ -f "$SRC/pool-lib.sh" ] && [ -f "$SRC/join_helper.py" ] && [ -f "$SRC/site/site.env" ] \
    || die "step 2 failed: the installer bundle is incomplete. Nothing was installed."
  # shellcheck disable=SC1091
  . "$SRC/pool-lib.sh"
  load_site "$SRC/site"      # the pool's site, which the bundle carries: the installer reads it from there too
  # What the instance installer would put where, from the installer itself.
  local shown lean_port agent_port instance_dir image_tag
  shown=$(bash "$SRC/install.sh" --instance "$JOIN_POOL" --show-instance) || die "step 2 failed: the installer does not know instance '$JOIN_POOL'"
  lean_port=$(sed -n 's/^port=//p' <<<"$shown")
  agent_port=$(sed -n 's/^agent_port=//p' <<<"$shown")
  instance_dir=$(sed -n 's/^dir=//p' <<<"$shown")
  image_tag=$(sed -n 's/^image=//p' <<<"$shown")
  [ -n "$lean_port" ] && [ -n "$agent_port" ] && [ -n "$instance_dir" ] && [ -n "$image_tag" ] \
    || die "step 2 failed: the installer did not describe instance '$JOIN_POOL'"

  # A box that is already in the pool takes this installer, and that is all.
  if [ "$refresh" -eq 1 ]; then
    refresh_box "$instance_dir" "$image_tag"
    return 0
  fi

  # ----------------------------------------------------------------------------------- 3. pool's image
  say "3/10 The pool's image (certificate tools, usage agent)"
  build_lean_pool_image
  # Whatever confines this Docker, a container must see the directory just unpacked, or nothing below works.
  docker run --rm --network none --user 0:0 -v "$WORK":/work "$LEAN_POOL_IMAGE" test -f /work/join/join_helper.py </dev/null \
    || die "step 3 failed: a container on this box cannot see $WORK (a confined Docker, or one that runs on another machine than this shell: Docker Desktop without access to this directory). That is not supported. Nothing was installed."

  # Stated before the Lean build starts: how many workers, and why.
  local cores available sized memory
  cores=$(nproc)
  if [ -z "$workers" ]; then
    available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    sized=$(helper workers --cores "$cores" --available-kib "${available:-0}") || die "step 3 failed: could not size the worker count"
    workers=$(sed -n 1p <<<"$sized")
    [[ "$workers" =~ ^[0-9]{1,3}$ ]] || die "step 3 failed: could not size the worker count"
    [ "$workers" -le 32 ] || workers=32
    echo "$(sed -n 2p <<<"$sized") (override: --workers N)"
  fi
  if [ -z "$cpus" ]; then cpus=$workers; [ "$cpus" -le "$cores" ] || cpus=$cores; fi
  memory=$((workers * 4 + 4))g
  echo "this server: '$name', $workers workers, at most $cpus CPUs and $memory of memory (4 GiB a worker and 4 GiB beside them)"

  # ---------------------------------------------------------------------------------- 4. pre-flight
  # The installer's OWN checks, with the options it will be run with, before the pool is asked for
  # anything. A box that fails here has not used the join window: its token is still good.
  say "4/10 Checking that this box can take the installer (its own pre-flight)"
  local options built=0 installed=0 removal=""
  # The Lean server's image comes from the pool where the pool ships one this box can use,
  # and is built here otherwise. Decided HERE, because the disk a box needs depends on it. The question
  # binds nothing: whatever the answer, the window is as good as before.
  local image_path=build image_reason="" plan="" code="" disk=()
  local image_sum="" image_bytes="" image_wanted="" image_gib="" image_size="" pool_docker=""
  # --build: the pool's image is not asked for at all (the way round a box whose Docker cannot take it).
  if [ "$build_here" -eq 1 ]; then code=asked-to-build; else code=$(join_curl /image.json -o "$WORK/image.json") || code="no answer"; fi
  case "$code" in
    asked-to-build) image_reason="--build was given" ;;
    200)
      if plan=$(helper image-plan --reply /work/image.json --machine "$(uname -m)" --tag "$image_tag"); then
        image_sum=$(sed -n 2p <<<"$plan"); image_bytes=$(sed -n 3p <<<"$plan"); image_wanted=$(sed -n 4p <<<"$plan")
        image_gib=$(sed -n 5p <<<"$plan"); pool_docker=$(sed -n 6p <<<"$plan"); image_size=$(sed -n 7p <<<"$plan")
        [[ "$image_sum" =~ ^[0-9a-f]{64}$ ]] && [[ "$image_bytes" =~ ^[0-9]{1,15}$ ]] && [[ "$image_wanted" =~ ^[0-9a-f]{64}$ ]] \
          && [[ "$image_gib" =~ ^[0-9]{1,4}$ ]] || die "step 4 failed: the pool's image manifest could not be read. Nothing was installed and the pool has signed nothing."
        if [ "$(image_layers "$image_tag" || true)" = "$image_wanted" ]; then image_path=here; else image_path=load; fi
      else
        image_reason=${plan:-"the pool's image manifest could not be read"}
      fi ;;
    204) image_reason="the pool ships no image in this window" ;;
    404) image_reason="this pool's join service does not ship images" ;;
    *) image_reason="the pool did not say whether it ships an image ($code)" ;;
  esac
  rm -f "$WORK/image.json"
  case "$image_path" in
    load) echo "the Lean server's image comes from the pool: $image_tag, $image_size to download and load (it is not built here)"
          disk=(--min-free-gb $((2 * image_gib + 2))) ;;      # the file and the unpacked image may share a disk
    here) echo "this box already holds the pool's image $image_tag: nothing to download and nothing to build"
          disk=(--min-free-gb 2) ;;
    build) echo "the Lean server's image is built here: $image_reason" ;;
  esac
  options=(--instance "$JOIN_POOL" --no-door --repls "$workers" --cpus "$cpus" --mem-limit "$memory"
           --build-jobs "$cpus" --tls-dir "$WORK/tls" --server-name "$name"
           --pool-lean-timeout "$JOIN_LEAN_TIMEOUT" --pool-server-wait "$JOIN_SERVER_WAIT")
  [ "$firewall" -eq 1 ] || options+=(--no-firewall)
  bash "$SRC/install.sh" "${options[@]}" ${disk[@]+"${disk[@]}"} --preflight-only </dev/null \
    || die "step 4 failed: this box cannot take the installer (its own message is above). Nothing was installed and the pool has signed nothing: put it right and run the same join command again while its window is open."
  # The one thing that pre-flight leaves to the install: a missing compose plugin. Added
  # HERE, after every other check has passed (a box that fails one has nothing installed on it) and before
  # the pool is asked for anything (a box on which it cannot be added has not used its window).
  ensure_compose_plugin \
    || die "step 4 failed: this box has Docker without its compose plugin, and the plugin could not be added: $COMPOSE_PLUGIN_FAILURE. Nothing else was installed and the pool has signed nothing: install the plugin (https://docs.docker.com/compose/install/linux/), then run the same join command again while its window is open."
  # What a later failure may truthfully say about the compose plugin on this box.
  local plugin=""
  [ -z "$COMPOSE_PLUGIN_INSTALLED" ] || plugin=" The docker compose plugin stays too ($COMPOSE_PLUGIN_REMOVAL removes it)."

  # ------------------------------------------------------------------------------ 5. the Lean image
  # The image needs no secret, so it is got BEFORE the pool is asked to sign anything:
  # the long step, and the one a failed download can stop, then costs no join window.
  say "5/10 The Lean server's image: from the pool, else built here (the pool has signed nothing yet)"
  if [ "$image_path" = load ]; then
    local attempt have=0 started loaded="" brought="" reference fallback="" room
    install -d -o root -g root -m 0755 "$instance_dir"
    IMAGE_FILE=$instance_dir/pool-image.tar.part
    rm -f "$IMAGE_FILE"
    # The file goes beside the instance (not into /tmp, which may be memory): is there room for it there?
    room=$({ df -BG --output=avail "$instance_dir" 2>/dev/null || true; } | tail -n 1 | tr -dc '0-9')
    if [ -n "$room" ] && [ "$room" -le "$image_gib" ]; then
      fallback="$instance_dir has $room GB free and the pool's image file is $image_size"
    else
      echo "downloading $image_size from the pool (pinned; a download that stops goes on from where it stopped)"
    fi
    started=$(date +%s)
    for attempt in 1 2 3 4 5; do
      [ -z "$fallback" ] || break
      have=$(stat -c %s "$IMAGE_FILE" 2>/dev/null || echo 0)
      [ "$have" -lt "$image_bytes" ] || break
      if [ "$attempt" -gt 1 ]; then
        echo "the download stopped at $have of $image_bytes bytes: going on from there (attempt $attempt of 5)"
        sleep 5
      fi
      code=$(join_curl /image -f -C - -o "$IMAGE_FILE" --max-time 14400 --speed-limit 1024 --speed-time 120) || true
    done
    have=$(stat -c %s "$IMAGE_FILE" 2>/dev/null || echo 0)
    if [ -n "$fallback" ]; then
      :                         # there was no room for the file: nothing was asked for
    elif [ "$have" -lt "$image_bytes" ]; then
      fallback="the download from the pool did not complete in five attempts ($have of $image_bytes bytes; its last answer: ${code:-none})"
    else
      echo "downloaded in $(( $(date +%s) - started )) s; checking its SHA-256 before anything is loaded"
      [ "$(sha256sum "$IMAGE_FILE" | cut -d ' ' -f 1)" = "$image_sum" ] \
        || alarm "the file the pool sent does not have the SHA-256 its manifest names"
      started=$(date +%s)
      if loaded=$(docker load -i "$IMAGE_FILE" 2>&1 </dev/null); then
        rm -f "$IMAGE_FILE"
        # What the load says it brought in. (A Docker that keeps images in containerd may give a name in full,
        # `docker.io/NAME`: the same name.)
        brought=$(grep '^Loaded image' <<<"$loaded" | sed 's#^Loaded image: docker\.io/#Loaded image: #' || true)
        if [ "$brought" != "Loaded image: $image_tag" ]; then
          while read -r reference; do
            [ -z "$reference" ] || docker image rm "$reference" >/dev/null 2>&1 || true
          done < <(sed -n 's/^Loaded image\( ID\)\{0,1\}: //p' <<<"$loaded")
          alarm "the load brought in something other than the one image $image_tag ($(tr '\n' ';' <<<"$brought" | tr -c ' -~' ' ' | cut -c1-300))"
        fi
        if [ "$(image_layers "$image_tag" || true)" != "$image_wanted" ]; then
          docker image rm "$image_tag" >/dev/null 2>&1 || true
          alarm "the image now under $image_tag does not have the layers the pool's manifest names"
        fi
        image_path=here
        echo "loaded $image_tag from the pool in $(( $(date +%s) - started )) s; its layers are the manifest's. Nothing is built here."
      else
        fallback="this box's Docker could not load the pool's image: $(tail -n 1 <<<"$loaded" | tr -d '\n' | tr -c ' -~' ' ' | cut -c1-200) (the pool's Docker is $pool_docker, this box's is $(docker version -f '{{.Server.Version}}' 2>/dev/null || echo unknown))"
      fi
    fi
    rm -f "$IMAGE_FILE"; IMAGE_FILE=""
    if [ -n "$fallback" ]; then
      echo "building the image here instead: $fallback"
      image_path=build
    fi
  fi
  if [ "$image_path" = build ]; then
    bash "$SRC/install.sh" "${options[@]}" --build-only </dev/null && built=1
  else
    built=1                 # the pool's image is here: the install is told so, and does not build
    options+=(--image-from-pool "$image_wanted")
    install -d -o root -g root -m 0755 "$instance_dir"         # for the installer copy, kept below
  fi
  # A copy of the installer stays in the instance's directory from here on, so that removing what this box
  # holds is one command, whatever happens next. The copy holds no key: the bundle never did.
  if [ -d "$instance_dir" ]; then
    keep_installer_copy "$instance_dir"
    removal="sudo bash $instance_dir/installer/install.sh --instance $JOIN_POOL --uninstall --purge"
  fi
  [ "$built" -eq 1 ] \
    || die "step 5 failed: the Lean server's image did not build (the installer's output is above). The pool has signed nothing, so this join window is still good: put the cause right and run the same join command again while it is open; Docker keeps the layers that were built, so the next attempt goes on from there.${removal:+ On this box so far: $instance_dir (the build files, build.log, a copy of the installer) and those layers; to remove them: $removal}$plugin"
  # What a later failure may truthfully say is on this box.
  local kept="On this box: the Lean server's image and $instance_dir stay, no key and no certificate${removal:+ (to remove them: $removal)}.$plugin"

  # ------------------------------------------------------------------------------- 6. key and request
  say "6/10 This box's private key and its signing request (the key never leaves this box)"
  pki csr --out-dir /work/tls --name "$name" --file-stem box >/dev/null \
    || die "step 6 failed: could not make a key and a signing request for '$name'. The pool has signed nothing. $kept"
  helper csr-body --name "$name" --lean-port "$lean_port" --agent-port "$agent_port" --workers "$workers" \
      --csr /work/tls/box.csr --out /work/csr.json \
    || die "step 6 failed: could not write the request. The pool has signed nothing. $kept"

  # ---------------------------------------------------------------------------------- 7. certificate
  say "7/10 Asking pool $JOIN_POOL for a certificate"
  local code issued=0 _
  code=$(join_curl /csr -o "$WORK/reply.json" -H 'Content-Type: application/json' --data-binary "@$WORK/csr.json") \
    || die "step 7 failed: the pool did not answer, or its key is not the pinned one (curl's message is above). If its join window has closed, open a new one there and run the new command here: the image is here and is neither downloaded nor built again. $kept"
  [ "$code" = 202 ] || die "step 7 failed: the pool answered $code: $(helper reason --reply /work/reply.json). $kept"
  for _ in $(seq 1 90); do
    code=$(join_curl /certificate -o "$WORK/reply.json") \
      || die "step 7 failed: the pool stopped answering (did its join window close?). $kept"
    case "$code" in
      200) issued=1; break ;;
      204) sleep 2 ;;
      409) die "step 7 failed: the pool refused this box: $(helper reason --reply /work/reply.json). $kept" ;;
      *) die "step 7 failed: the pool answered $code. $kept" ;;
    esac
  done
  [ "$issued" -eq 1 ] || die "step 7 failed: the pool did not decide within three minutes. $kept"
  # The certificate, the authority's certificate and the pool's API key, each into a root-only file.
  helper certificate --reply /work/reply.json --out-dir /work/tls --api-key-out /work/api-key \
    || die "step 7 failed: the pool's answer is not a certificate, the authority's certificate and the pool's key. $kept"
  rm -f "$WORK/reply.json"
  ( umask 077; cat "$WORK/tls/box.crt" "$WORK/tls/box.key" > "$WORK/tls/box.pem" )
  echo "received a certificate for '$name', the pool authority's certificate and the pool's key"

  # ------------------------------------------------------------------------------------ 8. installer
  say "8/10 Installing the Lean server (from the image of step 5), its usage agent and its TLS front"
  bash "$SRC/install.sh" "${options[@]}" --api-key-file "$WORK/api-key" </dev/null && installed=1
  [ "$installed" -eq 1 ] \
    || die "step 8 failed: the installer did not finish (its output is above). The pool is unchanged, but it has signed this box's certificate, so this join window is used up: on the pool's host close it (Ctrl-C) and open a new one, then run the NEW command here; the image is here and is neither downloaded nor built again.${removal:+ To remove what was installed here: $removal}"

  # ------------------------------------------------------------------------------- 9. ready, admission
  say "9/10 Telling the pool this server is ready; the pool now tests it alone (admission)"
  # The pool's test, like the installer's own checks, at the box's ordinary CPU weight (pool-lib.sh).
  lift_cpu_priority
  code=$(join_curl /ready -o "$WORK/reply.json" -X POST --data-binary '') \
    || die "step 9 failed: the pool did not answer (did its join window close?). The pool is unchanged; open a new window there and run the new command here."
  [ "$code" = 202 ] || die "step 9 failed: the pool answered $code: $(helper reason --reply /work/reply.json). The pool is unchanged."
  local decided=0
  for _ in $(seq 1 720); do
    code=$(join_curl /verdict -o "$WORK/reply.json") \
      || die "step 9 failed: the pool stopped answering before its verdict. On the pool's host: sudo lean-pool-ctl $JOIN_POOL status"
    case "$code" in
      200) decided=1; break ;;
      204) sleep 5 ;;
      *) die "step 9 failed: the pool answered $code. On the pool's host: sudo lean-pool-ctl $JOIN_POOL status" ;;
    esac
  done
  [ "$decided" -eq 1 ] || die "step 9 failed: no verdict within an hour. On the pool's host: sudo lean-pool-ctl $JOIN_POOL status"
  restore_cpu_priority

  # ------------------------------------------------------------------------------------ 10. verdict
  say "10/10 The pool's verdict"
  local detail outcome=0
  detail=$(helper verdict-of --reply /work/reply.json) || outcome=$?
  case "$outcome" in
    0) cat <<EOF
JOINED: $detail
  this server   '$name' on this box: Lean port $lean_port and usage port $agent_port, both behind the TLS front
  status here   sudo kimina-ctl @$JOIN_POOL status
  take it out   on the pool's host: sudo lean-pool-ctl $JOIN_POOL remove-backend $name
  remove here   $removal
EOF
       ;;
    4) die "NOT ADMITTED: ${detail:-the pool gave no reason}. The Lean server stays installed on this box; to remove it: $removal" ;;
    *) die "step 10 failed: the pool's verdict could not be read. On the pool's host: sudo lean-pool-ctl $JOIN_POOL servers" ;;
  esac
}
