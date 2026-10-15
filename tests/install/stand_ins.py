"""Stand-ins that let the lean-pool installers run here, as "root", without root and without Docker.

The installers (install.sh, install-pool.sh, lean-pool-ctl, the join script) cannot be executed for real in a
test: they need root, Docker and systemd. Reading them is not enough either: a real install once failed on
something no reading had caught (an unprivileged container handed a root-only directory). So the tests
run the REAL scripts against a fake machine:

* **A fake root.** Every host path the scripts name (`/srv/`, `/usr/local/sbin/` ...) is rewritten to
  `${FAKE_ROOT}/srv/` ... in a copy of the repository: its installers (deploy/install), the project they build
  the pool's image from, and a site (tests/install/site, where a join bundle carries its site). Nothing else in
  the scripts is changed.
* **`docker`** (a Python stand-in) records every call and
  - runs the lean-pool tools FROM SOURCE for `docker run IMAGE leanpool-pki|leanpool-haproxy-config|
    leanpool-join ...`, with container paths mapped back through the `-v` mounts, so certificates, configs
    and the join service are the real ones;
  - enforces what that install missed: a tool that runs as its image's unprivileged user may read
    a file only if the modes and the group the installer gave it allow that, from the mount's root down;
  - plays `haproxy -c` (every `crt` and `ca-file` the config names must exist under a mount and be
    readable by HAProxy's group, and so must a file an answer is returned from, beside the config), says
    what its HAProxy was built with (`haproxy -vv`: with the Prometheus exporter, unless
    STUB_HAPROXY_NO_EXPORTER), `docker compose` (the compose file and env file must exist, every
    `${VAR:?}` must be set, a container is "recreated" only when its resolved config changed), and the
    admission test (a scenario);
  - remembers how each container was MADE, as Docker does: whether its service said `init: true` when it was
    last (re)created, which is what `inspect -f '{{.HostConfig.Init}}'` answers, and the control group it was
    started in (`cgroup_parent:`, `{{.HostConfig.CgroupParent}}`), and the memory ceiling it was given
    (`mem_limit:`, `{{.HostConfig.Memory}}`, in bytes; a ceiling under Docker's 6 MB floor is refused, and
    nothing is made). A compose file that changed on disk changes
    no running container, and `compose up` makes again only the services whose OWN settings changed, as the
    real one does. A Docker with no docker-init (STUB_NO_INIT) refuses `run --init`;
  - says how it keeps control groups (`info -f '{{.CgroupDriver}} {{.CgroupVersion}}'`: `systemd 2`, or what
    STUB_CGROUPS says), and refuses `run --cgroup-parent` where a test says so (STUB_NO_CGROUP_PARENT);
  - answers for the usage agent when `kimina-ctl status` asks inside its container (STUB_AGENT_SAYS);
  - plays the build and the check of the compiled search step (`run --entrypoint bash IMAGE -c
    '... lean-native-build.sh ... lean-native-check.py ...'`). It refuses a container that is not hardened as
    the installer says (no network, no capability, no new privileges), one whose source mount lacks a file, and
    one whose output mount holds no launcher it could start; then it writes what the real build writes and says
    what the real check says: passed, or what a test says (STUB_NATIVE: `no-c-files`, an image without the C
    files; `check-fails`, a library that is built and does not pass). A Lean server remembers the program its
    workers were started as when it was MADE (`LEAN_SERVER_REPL_PATH`), which is what
    `inspect -f '{{range .Config.Env}}...'` answers, with the API key in the same list as on a real box;
  - remembers whether a container was made with its root filesystem read-only (`read_only:`,
    `{{.HostConfig.ReadonlyRootfs}}`) and where its volumes are mounted inside it (`{{range .Mounts}}...`), and
    plays `docker diff` of a Lean server that has served checks (unnamed files in /tmp, Python's byte-code cache,
    git's index of a Lake package, the mount points it was made with; STUB_DIFF_EXTRA adds lines, STUB_DIFF_FAILS fails it). `kimina-ctl check` fails against the
    container STUB_READ_ONLY_CHECK_FAILS names while that container is read-only;
  - builds with the context's `Dockerfile` where no `-f` is given, as Docker does; runs an image's own command
    detached (`run -d --name NAME ... IMAGE`), as a container that is made and runs until it is removed; and
    says which image a container was made from (`inspect -f '{{.Config.Image}}'`);
  - keeps Docker's own book on restarts, as the daemon does: `docker kill` (ANY signal) and `docker stop`
    mark a container "stopped by hand", `docker exec` does not; `Machine.restart_daemon()` then starts only
    what Docker would start (`restart: unless-stopped` and not stopped by hand). A container told to crash
    (STUB_CRASH_LOOP) reports `.State.Running` true, as a restarting one does, while its restart count rises.
* **`install`, `chown`, `mv`** record the owner and group the installer asked for (nobody here can chown to
  root); the real mode bits are on disk.
* `free` gives the box's memory: 64 GiB, or what a test says (STUB_BOX_MEMORY, in bytes), or nothing at all
  (STUB_FREE_FAILS: a box whose memory cannot be read).
* `id` says root; `systemctl`, `ip`, `hostname`, `df`, `sleep` answer or do nothing; `nproc` says 8.
* `iptables` records each call and finds no rule it is asked to check (`-C`), so every rule is added.
* `ss` shows a listener only on the port a test names (STUB_BUSY_PORT). `uname`, `date` and `python3` are the
  real ones, unless a test makes the box another kind (STUB_MACHINE), its clock jump (STUB_CLOCK_STEP) or its
  resolver answer (STUB_HOST_LOOKUP).
* The box's accounts are only those a test names (STUB_USERS, `name:uid ...`), each with its home under the fake
  root: `getent passwd` and `id -gn` answer for them and for nobody else, so the door's key line is written and
  removed under the fake root, never in a real home. `visudo -cf` checks that a sudoers file holds only
  `Defaults` and user rules. `systemctl start
  NAME.slice` makes that control group under the fake root with the CPU weight its unit names, as systemd
  does (not where STUB_SLICE_WITHOUT_WEIGHT is set: a box whose groups do not carry a weight).
* `ps` is the real one, unless a test gives the host a process table (STUB_ZOMBIES: that many zombies in the PID
  namespace of the container, beside its live processes and beside zombies that are not the container's).
* `apt-get`, `dnf`, `yum` record their call and install nothing real; `curl` is the real one except for the
  compose plugin's release binary, which it refuses to fetch (no test reaches the network or a real package
  manager).

A site that adds to the installers adds stand-ins of its own in `$STUB/site-bin`, which is on PATH in front of
these.

What this cannot show: that real Docker, real HAProxy, real iptables and a real Lean server behave as their
stand-ins do. That is what an install on a real machine shows.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[2]
)  # the lean-pool repository: the project the installers build
INSTALLERS = ROOT / "deploy" / "install"
SITE = (
    Path(__file__).resolve().parent / "site"
)  # the tests' site: prefix `acme`, instance 1 and `demo`
# Host paths the scripts name. `/etc/leanpool` and `/usr/local/etc` are paths INSIDE containers: not here.
HOST_PREFIXES = (
    "/srv/",
    "/usr/local/sbin/",
    "/usr/local/bin/",
    "/usr/local/lib/",
    "/etc/systemd/",
    "/etc/sudoers.d/",
    "/etc/lean-pool/",
    "/run/systemd/",
    "/sys/fs/cgroup/",
)
# The scripts without a `.sh` that name host paths.
PROGRAMS = ("kimina-ctl", "lean-pool-ctl", "kimina-firewall")
HAPROXY_GID = "99"
POOL_GID = "10001"

DOCKER = r'''#!__PYTHON__
"""A stand-in for docker: see tests/install/stand_ins.py."""
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

STUB = Path(os.environ["STUB"])
STATE = STUB / "state"
PROJECT = os.environ["STUB_LEAN_POOL"]
HAPROXY_GID = os.environ.get("STUB_HAPROXY_GID", "99")
POOL_GID = os.environ.get("STUB_POOL_GID", "10001")
argv = sys.argv[1:]
with open(STUB / "calls", "a") as log:
    log.write("docker " + " ".join(argv) + "\n")


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def note(name, text):
    with open(STUB / name, "a") as handle:
        handle.write(text + "\n")


def owners():
    try:
        return json.loads((STUB / "owners.json").read_text())
    except FileNotFoundError:
        return {}


def may(path, gid, want):
    """Whether a process that is not root, in group `gid`, has `want` (4 read, 2 write, 1 enter) on `path`."""
    group = str(owners().get(str(path), ["root", "root"])[1])
    mode = path.stat().st_mode & 0o777
    bits = (mode >> 3) & 7 if group == str(gid) else mode & 7
    return bits & want == want


def demand(root, target, gid, want=4):
    """An unprivileged container user reaches `target` through the mount at `root`, or the call fails."""
    if gid == "0":
        return
    root, target = Path(root), Path(target)
    if not target.exists():
        fail(f"{target}: No such file or directory")
    inside = target.relative_to(root).parts
    directories = [root] + [root.joinpath(*inside[:depth]) for depth in range(1, len(inside))]
    for directory in directories:
        if directory.is_dir() and not may(directory, gid, 1):
            fail(f"{directory}: Permission denied (group {gid} may not enter it)")
    if not may(target, gid, want | (1 if target.is_dir() else 0)):
        fail(f"{target}: Permission denied (group {gid}, wanted {want})")


def is_running(name):
    pid_file = STATE / f"pid.{name}"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 0)
            return True
        except OSError:
            return False
    return (STATE / "running" / name).exists()


def substitute(text, env):
    def value(match):
        name, default = match.group(1), match.group(3)
        if env.get(name):
            return env[name]
        if match.group(2) == ":?":
            fail(f"required variable {name} is missing a value: {default}")
        return default or ""
    return re.sub(r"\$\{([A-Z_]+)(?:(:-|:\?)([^}]*))?\}", value, text)


def compose(args):
    # A machine whose Docker has no compose plugin (STUB_COMPOSE_MISSING) has none until one is installed: by
    # a package (the package managers' stand-ins leave a mark) or as the release binary under the fake root.
    if os.environ.get("STUB_COMPOSE_MISSING") and not (STATE / "compose-plugin").exists() \
            and not Path(os.environ["FAKE_ROOT"], "usr/local/lib/docker/cli-plugins/docker-compose").exists():
        fail("docker: 'compose' is not a docker command.")
    if args == ["version"]:
        return 0
    project = env_file = compose_file = None
    while args and args[0].startswith("-"):
        if args[0] == "--project-name":
            project = args[1]
        elif args[0] == "--env-file":
            env_file = Path(args[1])
        elif args[0] == "-f":
            compose_file = Path(args[1])
        else:
            fail(f"docker compose: unknown option {args[0]}")
        args = args[2:]
    for needed in (env_file, compose_file):
        if needed is None or not needed.is_file():
            fail(f"docker compose: {needed}: no such file")
    env = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))
    env.update(os.environ)                                       # the shell's environment comes first, as for compose
    resolved = substitute("\n".join(line for line in compose_file.read_text().splitlines() if not line.lstrip().startswith("#")), env)
    services = restart_policies(resolved)                        # container name -> its `restart:` policy
    with_init = containers_with_init(resolved)                   # the containers whose service says `init: true`
    made_from = {service["name"]: service for service in service_settings(resolved)}
    verb = args[0] if args else ""
    if verb == "up":
        # Docker makes no container with a memory ceiling under 6 MB, and says so before it makes any.
        for service in made_from.values():
            if 0 < memory_bytes(service.get("mem_limit", "")) < 6 * 1024 ** 2:
                fail("Error response from daemon: Minimum memory limit allowed is 6MB")
        (STATE / f"project.{project}").write_text("up")          # the project (and its network) exists
        (STATE / f"resolved.{project}.{compose_file.name}").write_text(resolved)    # what compose made it from
        for name, policy in services.items():
            # A service is made again when ITS OWN settings changed (or its image was built again), not
            # when another service of the same file did: the real compose compares each service's config.
            digest = hashlib.sha256(made_from[name]["text"].encode()).hexdigest()
            for image in re.findall(r"image: (\S+)", made_from[name]["text"]):
                mark = STATE / ("built." + image.replace("/", "_").replace(":", "_"))
                digest = hashlib.sha256((digest + (mark.read_text() if mark.exists() else "")).encode()).hexdigest()
            known = STATE / f"service.{project}.{name}"
            changed = "--force-recreate" in args or not known.exists() or known.read_text() != digest
            known.write_text(digest)
            holder = STATE / f"label.{name}"
            was_running = is_running(name)
            if changed or not was_running or (holder.exists() and holder.read_text() != project):
                (STATE / f"id.{name}").write_text(uuid.uuid4().hex)
                (STATE / f"by-hand.{name}").unlink(missing_ok=True)      # a new container: nobody stopped it
                (STATE / f"restarts.{name}").unlink(missing_ok=True)
                if name in with_init:                                    # how it was made: asked of `inspect` later
                    (STATE / f"init.{name}").write_text("")
                else:
                    (STATE / f"init.{name}").unlink(missing_ok=True)
                (STATE / f"cgroup-parent.{name}").write_text(made_from[name].get("cgroup_parent", ""))
                (STATE / f"memory.{name}").write_text(str(memory_bytes(made_from[name].get("mem_limit", ""))))
                program = re.search(r"LEAN_SERVER_REPL_PATH: *(\S+)", made_from[name]["text"])
                if program:                                              # the program a worker is: as it was MADE
                    (STATE / f"repl-path.{name}").write_text(program.group(1))
                else:
                    (STATE / f"repl-path.{name}").unlink(missing_ok=True)
                # Whether its root filesystem is read-only, and where its volumes are mounted inside it.
                read_only = re.search(r"\n    read_only: *(\S+)", "\n" + made_from[name]["text"])
                (STATE / f"read-only.{name}").write_text(read_only.group(1) if read_only else "false")
                (STATE / f"mounts.{name}").write_text(" ".join(re.findall(r"\n      - [^:\n]+:(/[^:\s]*)", made_from[name]["text"])))
                note("recreated", name)
            holder.write_text(project)
            (STATE / f"policy.{name}").write_text(policy)
            (STATE / "running" / name).write_text("")
        return 0
    if verb == "down":
        for name in services:
            (STATE / "running" / name).unlink(missing_ok=True)
            (STATE / f"label.{name}").unlink(missing_ok=True)
        (STATE / f"project.{project}").unlink(missing_ok=True)
        for known in [*STATE.glob(f"service.{project}.*"), *STATE.glob(f"resolved.{project}.*")]:
            known.unlink()
        return 0
    if verb == "ps":
        for name in services:
            print(f"{name}   {'Up' if is_running(name) else 'Exited'}")
        return 0
    return 0


def memory_bytes(size):
    """A compose size (`28g`, `512m`) in bytes; 0 for none."""
    match = re.fullmatch(r"([0-9]+)([gGmM])", size)
    return int(match.group(1)) * (1024 ** 3 if match.group(2) in "gG" else 1024 ** 2) if match else 0


def restart_policies(resolved):
    """Each service's container name and `restart:` policy, from a resolved compose file."""
    return {service["name"]: service["policy"] for service in service_settings(resolved)}


def containers_with_init(resolved):
    """The container names of the services that ask for an init process (`init: true`)."""
    return {service["name"] for service in service_settings(resolved) if service.get("init")}


def service_settings(resolved):
    """What the stand-in reads of each service that names its container: the name, `restart:`, `init:`, the
    control group it is started in (`cgroup_parent:`), its memory ceiling (`mem_limit:`), and the service's
    own lines (`text`)."""
    found, current, inside = [], None, False
    for line in resolved.splitlines():
        if line.startswith("services:"):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            inside = False
        if not inside:
            continue
        if re.fullmatch(r"  [A-Za-z0-9_-]+:\s*", line):
            current = {"policy": "no", "text": ""}
            found.append(current)
        if current is not None:
            current["text"] += line + "\n"
        if current is not None and re.fullmatch(r"    cgroup_parent: *(\S*)\s*", line):
            current["cgroup_parent"] = re.fullmatch(r"    cgroup_parent: *(\S*)\s*", line).group(1)
        elif current is not None and re.match(r"    container_name: (\S+)", line):
            current["name"] = re.match(r"    container_name: (\S+)", line).group(1)
        elif current is not None and re.match(r"    restart: (\S+)", line):
            current["policy"] = re.match(r"    restart: (\S+)", line).group(1)
        elif current is not None and re.fullmatch(r"    init: true\s*", line):
            current["init"] = True
        elif current is not None and re.fullmatch(r"    mem_limit: *(\S*)\s*", line):
            current["mem_limit"] = re.fullmatch(r"    mem_limit: *(\S*)\s*", line).group(1)
    return [service for service in found if "name" in service]


def crash_loops(name):
    return name in os.environ.get("STUB_CRASH_LOOP", "").split(",")


def run(args):
    mounts, bare, user, entrypoint, name, ports, cgroup_parent, given = [], set(), None, None, None, [], None, {}
    with_value = {"--network", "--user", "--entrypoint", "-v", "--name", "--cap-drop", "--security-opt", "--tmpfs",
                  "--memory", "--memory-swap", "--cpus", "--pids-limit", "-p", "-e", "--dns", "--restart",
                  "--cgroup-parent"}
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option in ("--rm", "-i", "-d", "--read-only", "--init"):
            bare.add(option)
            index += 1
            continue
        if option not in with_value:
            fail(f"docker run: unknown option {option}")
        value = args[index + 1]
        given.setdefault(option, []).append(value)
        if option == "-v":
            host_path, container_path = value.split(":")[:2]
            if not Path(host_path).exists():
                fail(f"docker run: bind source {host_path} does not exist")
            mounts.append((host_path, container_path))
        elif option == "--user":
            user = value
        elif option == "--entrypoint":
            entrypoint = value
        elif option == "--name":
            name = value
        elif option == "-p":
            ports.append(value)
        elif option == "--cgroup-parent":
            cgroup_parent = value
        index += 2
    image, command = args[index], args[index + 1:]
    image_gid = HAPROXY_GID if image.startswith("haproxy") else POOL_GID
    gid = "0" if user == "0:0" else image_gid

    def mount_of(path):
        covering = [(host_path, container_path) for host_path, container_path in mounts
                    if path == container_path or path.startswith(container_path.rstrip("/") + "/")]
        return max(covering, key=lambda pair: len(pair[1])) if covering else None

    def host(path):
        found = mount_of(path)
        return path if found is None else found[0] + path[len(found[1]):]

    def demand_argument(path, want=4):
        found = mount_of(path)
        if found is None:
            fail(f"{path}: not mounted in this container")
        demand(found[0], host(path), gid, want)

    if entrypoint == "id":
        print(image_gid)
        return 0
    if entrypoint == "true":                                 # a container that does nothing: can this Docker start it?
        if "--init" in bare and os.environ.get("STUB_NO_INIT"):       # a Docker whose package ships no docker-init
            fail('docker: Error response from daemon: exec: "docker-init": executable file not found in $PATH.', 127)
        if cgroup_parent and os.environ.get("STUB_NO_CGROUP_PARENT"):  # a Docker that cannot start it in that group
            fail(f'docker: Error response from daemon: cgroup-parent for systemd cgroup should be a valid slice '
                 f'named as "xxx.slice", and this daemon will not make {cgroup_parent}.', 125)
        return 0
    if not command:
        # The image's own command, detached and named: a container that is made, and runs until it is removed.
        if "-d" not in bare or not name:
            fail("docker run: the stand-in runs an image's own command only detached and named")
        if (STATE / f"id.{name}").exists():
            fail(f'docker: Error response from daemon: Conflict. The container name "/{name}" is already in use.', 125)
        (STATE / f"id.{name}").write_text(uuid.uuid4().hex)
        (STATE / f"image.{name}").write_text(image)
        (STATE / f"policy.{name}").write_text(given.get("--restart", ["no"])[-1])
        if "--init" in bare:
            (STATE / f"init.{name}").write_text("")
        (STATE / "running" / name).write_text("")
        print((STATE / f"id.{name}").read_text())
        return 0
    if entrypoint == "bash" and command[:1] == ["-c"] and "lean-native-build.sh" in command[1]:
        return compiled_search(image, command[1], given, mount_of)
    tool, mapped = command[0], [host(argument) for argument in command[1:]]
    source = {**os.environ, "PYTHONPATH": PROJECT}
    if tool in ("leanpool-pki", "leanpool-haproxy-config"):
        for argument in command[1:]:
            if mount_of(argument) and Path(host(argument)).is_file():
                demand_argument(argument)
        module = "leanpool.pki" if tool == "leanpool-pki" else "leanpool.haproxy"
        return subprocess.run([sys.executable, "-m", module, *mapped], env=source).returncode
    if tool == "haproxy" and command[1:] == ["-vv"]:
        # What the image's HAProxy was built with, in the real one's words. The official image has the
        # Prometheus exporter; one built without it (STUB_HAPROXY_NO_EXPORTER) lists no service.
        exporter = not os.environ.get("STUB_HAPROXY_NO_EXPORTER")
        print("HAProxy version 3.0.29-de0ed89 2026/09/28 - https://haproxy.org/\n"
              "Build options :\n  TARGET  = linux-glibc\n"
              f"  OPTIONS = USE_OPENSSL=1{' USE_PROMEX=1' if exporter else ''}\n\n"
              + ("Built with the Prometheus exporter as a service\n" if exporter else "")
              + f"Available services : {'prometheus-exporter' if exporter else 'none'}\n"
              "Available filters :\n\t[BWLIM] bwlim-in\n\t[TRACE] trace")
        return 0
    if tool == "haproxy":
        config = command[command.index("-f") + 1]
        demand_argument(config)
        if os.environ.get("STUB_HAPROXY_REJECTS"):
            fail("[ALERT] (1) : config : the stand-in was told to reject this config")
        text = Path(host(config)).read_text()
        for path in re.findall(r"\b(?:crt|ca-file) (\S+)", text):
            demand_argument(path)
        # A file an answer is made of (`http-request return ... file NAME`) is read when the config is: the
        # real HAProxy refuses a config whose file is missing or that its user cannot read. A name with no
        # directory is looked for beside the config only where the config says `default-path config`.
        beside_config = re.search(r"^\s*default-path config\s*$", text, re.MULTILINE)
        for path in re.findall(r"^\s*http-request return\b.*?\sfile (\S+)", text, re.MULTILINE):
            if not path.startswith("/"):
                if not beside_config:
                    fail(f"[ALERT] (1) : config : error opening file '{path}' : it is named without a directory, "
                         "and the config does not say `default-path config`")
                path = config.rsplit("/", 1)[0] + "/" + path
            demand_argument(path)
        if "use-service prometheus-exporter" in text and os.environ.get("STUB_HAPROXY_NO_EXPORTER"):
            fail("[ALERT] (1) : config : 'http-request use-service' : unknown service name 'prometheus-exporter'")
        note("validated", host(config) + "\n" + text + "\n=====")
        return 0
    if tool == "leanpool-admit":
        options = dict(zip(command[1::2], command[2::2]))
        for file_option in ("--api-key-file", "--tls-ca-file", "--tls-client-pem"):
            demand_argument(options[file_option])
        demand_argument(options["--cases"], 4)
        server, server_name = options["--server"], options["--tls-server-name"]
        note("admissions", " ".join(command[1:]))
        outcome = os.environ.get("STUB_ADMIT", "pass")
        if outcome == "pass":
            print(json.dumps({"server": server, "admitted": True, "counts": {"cases": 12, "misbehaved": 0}, "cases": []}, indent=2))
            return 0
        if outcome == "tls":
            print(json.dumps({"server": server, "admitted": False,
                              "error": f"TLS with {server}: the server's certificate is for another name than '{server_name}'"}, indent=2))
            return 3
        print(json.dumps({"server": server, "admitted": False, "counts": {"cases": 12, "misbehaved": 1},
                          "cases": [{"case": "verify/sum_comm.lean", "expected": "accepted", "observed": "undecided",
                                     "behaved": False, "detail": "Lean timeout"}]}, indent=2))
        return 1
    if tool == "leanpool-join":
        options = dict(zip(mapped[0::2], mapped[1::2]))
        for file_option in ("--script", "--token-file", "--tls-pem", "--api-key-file"):
            demand_argument(command[1:][command[1:].index(file_option) + 1])
        spool_argument = command[1:][command[1:].index("--spool") + 1]
        demand(mount_of(spool_argument)[0], Path(host(spool_argument)) / "requests", gid, 2)
        if "--image-directory" in command:                   # the manifest and the image: readable by the service's group
            image_argument = command[1:][command[1:].index("--image-directory") + 1]
            for exported in sorted(Path(host(image_argument)).iterdir()):
                demand(mount_of(image_argument)[0], exported, gid, 4)
        host_port = ports[0].split(":")[1]
        options["--host"], options["--port"] = "127.0.0.1", host_port
        service = [sys.executable, "-m", "leanpool.join"] + [part for pair in options.items() for part in pair]
        with open(STUB / "join.log", "ab") as output:
            process = subprocess.Popen(service, stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                       start_new_session=True, env=source)
        (STATE / f"pid.{name}").write_text(str(process.pid))
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", int(host_port))) == 0 or process.poll() is not None:
                    break
            time.sleep(0.1)
        print(uuid.uuid4().hex)
        return 0
    if tool == "test":                                       # `test -f PATH`: does a mount arrive in the container?
        if os.environ.get("STUB_MOUNTS_EMPTY"):
            return 1
        return 0 if Path(mapped[-1]).is_file() else 1
    if tool == "python":
        if os.environ.get("STUB_MOUNTS_EMPTY"):
            fail(f"python: can't open file {command[1]!r}: [Errno 2] No such file or directory", 2)
        if command[1] != "-":
            return subprocess.run([sys.executable, *mapped]).returncode
        program = sys.stdin.read()
        if command[2:3] == ["lookup"]:
            canned = os.environ.get("STUB_PROXY_LOOKUP")
            if canned is not None:
                print(canned)
                return 0
            return subprocess.run([sys.executable, "-", *command[2:]], input=program, text=True).returncode
        if len(command) > 2:                                 # a program on standard input, with arguments
            for argument in command[2:]:
                if mount_of(argument):
                    demand_argument(argument)
            return subprocess.run([sys.executable, "-", *mapped[1:]], input=program, text=True).returncode
        certificates = mount_of("/certs")
        if certificates is not None:
            demand(certificates[0], certificates[0], gid, 4)
            return subprocess.run([sys.executable, "-"], input=program.replace("/certs", certificates[0]), text=True).returncode
        return inside_pool(program)
    fail(f"docker run: the stand-in does not know the command {tool}")


def compiled_search(image, script, given, mount_of):
    """The build and the check of the compiled search step, as a container of the Lean server's image runs them
    What the real ones do is tests/install/test_install_lean_native.py's, against
    a real Lean; here it is what the INSTALLER must give them, and what it does with each outcome."""
    for option, wanted in (("--network", "none"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges:true")):
        if given.get(option) != [wanted]:
            fail(f"docker run: the stand-in builds the compiled search step only in a hardened container ({option} {wanted})")
    source, out = mount_of("/lean-native-src"), mount_of("/opt/lean-native")
    if source is None or out is None:
        fail("docker run: /lean-native-src and /opt/lean-native must both be mounted")
    for needed in ("lean-native-build.sh", "lean-native-check.py", "lean-native-modules", "lean-native-repl"):
        if not Path(source[0], needed).is_file():
            fail(f"bash: /lean-native-src/{needed}: No such file or directory", 127)
    if "/opt/lean-native/repl-native" not in script or not os.access(Path(out[0], "repl-native"), os.X_OK):
        fail("lean-native-check.py: the launcher /opt/lean-native/repl-native is not there, or cannot be started", 126)
    note("native-builds", image)
    outcome = os.environ.get("STUB_NATIVE", "ok")
    if outcome == "no-c-files":
        print("lean-native-build.sh: this image holds no C file for Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm of mathlib "
              "(/lean-project/.lake/packages/mathlib/.lake/build/ir/Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm.c): "
              "nothing was built", file=sys.stderr)
        return 3
    Path(out[0], "linarith-search.so").write_bytes(b"\x7fELF made for " + image.encode())
    Path(out[0], "sources").write_text("/lean-project/.lake/packages/mathlib/.lake/build/ir/Mathlib/Data/Ineq.c\n"
                                       "/lean-project/lean-toolchain\n")
    Path(out[0], "built-from").write_text(hashlib.sha256(image.encode()).hexdigest() + "\n")
    print("COMPILED: 7 modules into /opt/lean-native/linarith-search.so (865976 bytes); 4 imported modules are not compiled "
          "and got an initializer that does nothing")
    if outcome == "check-fails":
        print("COMPILED SEARCH STEP: FAILED - the check 'starts a program' got a different answer with the library than without")
        return 1
    print("COMPILED SEARCH STEP: ok - linarith's search took 11.4 s as the image runs it and 1.4 s compiled (8.4 times less); "
          "the same answers; a program a tactic starts does not inherit the library")
    return 0


def image_state(tag):
    """Where the stand-in keeps "this image exists", and what it was made from."""
    return STATE / ("built." + tag.replace("/", "_").replace(":", "_"))


def image_layer_list(digest):
    return ["sha256:" + hashlib.sha256((digest + f"/layer{number}").encode()).hexdigest() for number in (1, 2)]


def inside_pool(program=""):
    code = int(os.environ.get("STUB_CHECK", "0"))
    slow_start = int(os.environ.get("STUB_CHECK_FAILS_FIRST", "0"))
    if slow_start and "/api/check" in program:               # a Lean server that needs a while before it answers
        counter = STATE / "checks"
        seen = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(seen + 1))
        code = 1 if seen < slow_start else code
    print(json.dumps({"check": "ok" if code == 0 else "FAILED", "through": "a stand-in"}))
    return code


def main():
    verb = argv[0] if argv else ""
    if verb == "compose":
        return compose(argv[1:])
    if verb == "run":
        return run(argv[1:])
    if verb == "info":
        if "-f" in argv and "CgroupDriver" in argv[argv.index("-f") + 1]:
            # How this Docker keeps its containers' control groups: the driver and the version.
            print(os.environ.get("STUB_CGROUPS", "systemd 2"))
        elif "-f" in argv:
            print(os.environ.get("STUB_DOCKER_ROOT") or str(STUB / "docker-root"))
        return int(os.environ.get("STUB_DOCKER_INFO", "0"))
    if verb == "build":
        legacy = bool(os.environ.get("STUB_LEGACY_BUILDER"))  # a Docker without the buildx component (Ubuntu's docker.io)
        if "--help" in argv:
            print("Usage:  docker build [OPTIONS] PATH | URL | -")
            if not legacy:
                print("      --progress string   Set type of progress output")
            return 0
        if legacy and any(argument.startswith("--progress") for argument in argv):
            fail("unknown flag: --progress\n\nUsage:  docker build [OPTIONS] PATH | URL | -", 125)
        tag = argv[argv.index("-t") + 1]
        context = Path(argv[-1])
        # A Lean image whose build fails (STUB_BUILD_FAILS), the way a build does when a download inside a
        # step fails. Nothing is tagged.
        if os.environ.get("STUB_BUILD_FAILS") and "/kimina:" in tag:
            print("Step 18/21 : RUN git fetch --depth 1 origin 0123abcd")
            fail("fatal: unable to access 'https://github.com/': Could not resolve host: github.com\n"
                 "The command '/bin/sh -c git fetch' returned a non-zero code: 128")
        dockerfile = Path(argv[argv.index("-f") + 1]) if "-f" in argv else context / "Dockerfile"   # Docker's default
        if not dockerfile.is_file():
            fail(f"unable to prepare context: unable to evaluate symlinks in Dockerfile path: lstat {dockerfile}: "
                 "no such file or directory")
        digest = hashlib.sha256(dockerfile.read_bytes())
        if (context / "pyproject.toml").exists():            # the lean-pool project: its source is the image
            for path in sorted((context / "leanpool").rglob("*.py")):
                digest.update(path.read_bytes())
        else:                                                # an image built from its context's other build files too
            for path in sorted(context.glob("setup-*.sh")):
                digest.update(path.read_bytes())
        (STATE / ("built." + tag.replace("/", "_").replace(":", "_"))).write_text(digest.hexdigest())
        print("sha256:" + digest.hexdigest())
        return 0
    if verb == "image":                                      # `image inspect -f TEMPLATE TAG`, `image rm TAG`
        holder = image_state(argv[-1])
        if argv[1:2] == ["inspect"]:
            if not holder.exists():
                fail(f"Error response from daemon: No such image: {argv[-1]}")
            digest, template = holder.read_text(), argv[argv.index("-f") + 1]
            if template == "{{.Id}}":
                # The image's ID is the digest of its config under one image store and of its manifest under
                # the other (STUB_IMAGE_STORE=containerd): two hosts, two IDs, one image.
                other_store = os.environ.get("STUB_IMAGE_STORE") == "containerd"
                print("sha256:" + (hashlib.sha256((digest + "/manifest").encode()).hexdigest() if other_store else digest))
            elif "RootFS.Layers" in template:                # the same list under both stores
                print(json.dumps(image_layer_list(digest), separators=(",", ":")))
            elif "Config.Env" in template:
                print("LEAN_SERVER_HOST=0.0.0.0\nLEAN_SERVER_PORT=8000")
                if os.environ.get("STUB_IMAGE_CARRIES_KEY"):
                    print("LEAN_SERVER_API_KEY=baked-into-the-image")
            elif template == "{{.Size}}":
                print(os.environ.get("STUB_IMAGE_SIZE", str(12 * 1024**3)))
            else:
                print("")
        elif argv[1:2] == ["rm"]:
            note("image-removals", argv[-1])
            holder.unlink(missing_ok=True)
        return 0
    if verb == "version":
        print(os.environ.get("STUB_DOCKER_VERSION", "28.0.4"))
        return 0
    if verb == "save":                                       # save -o FILE TAG: one line that says what it holds, then bulk
        holder = image_state(argv[-1])
        if not holder.exists():
            fail(f"Error response from daemon: reference does not exist: {argv[-1]}")
        tags = [argv[-1], *os.environ.get("STUB_SAVE_EXTRA_TAGS", "").split()]
        described = json.dumps({"tags": tags, "digest": holder.read_text()}).encode() + b"\n"
        bulk = hashlib.sha256(described).digest() * (int(os.environ.get("STUB_SAVE_BYTES", "600000")) // 32)
        Path(argv[argv.index("-o") + 1]).write_bytes(described + bulk)
        return 0
    if verb == "load":                                       # load -i FILE: what `save` wrote, under the tags it names
        if os.environ.get("STUB_LOAD_FAILS"):
            fail("open /var/lib/docker/tmp/docker-import-1/blobs/sha256: unsupported image format: this Docker cannot read it")
        with open(argv[argv.index("-i") + 1], "rb") as handle:
            described = json.loads(handle.readline())
        for tag in described["tags"]:
            digest = described["digest"] if not os.environ.get("STUB_LOAD_OTHER_LAYERS") else "0" * 64
            image_state(tag).write_text(digest)
            print(f"Loaded image: {tag}")
        return 0
    if verb == "exec":
        if argv[2:] == ["sh", "-c", "kill -HUP 1"]:          # a reload from inside: Docker's book is not touched
            if not is_running(argv[1]):
                fail(f"Error response from daemon: container {argv[1]} is not running")
            note("reloads", argv[1])
            return 0
        program = sys.stdin.read()
        if "18200" in program and "create_connection" in program:   # `kimina-ctl status` asks the usage agent
            said = os.environ.get("STUB_AGENT_SAYS", "ready up 97% maxconn:8")
            if not said:
                fail("ConnectionRefusedError: [Errno 111] Connection refused")
            print(said)
            return 0
        if "kimina_ctl_valid" in program and os.environ.get("STUB_READ_ONLY_CHECK_FAILS") == argv[-3] \
                and (STATE / f"read-only.{argv[-3]}").exists() and (STATE / f"read-only.{argv[-3]}").read_text() == "true":
            # `kimina-ctl check` against a Lean server that does not work with its root read-only.
            print('{"check": "FAILED", "reason": "OSError(30, \'Read-only file system\')"}')
            return 1
        return inside_pool(program)
    if verb == "ps":
        names = [path.name for path in sorted((STATE / "running").iterdir()) if is_running(path.name)]
        names += [path.name[4:] for path in STATE.glob("pid.*") if is_running(path.name[4:])]
        sys.stdout.write("".join(name + "\n" for name in names))          # one write, like the real listing
        return 0
    if verb == "inspect":
        template, name = argv[2], argv[3]
        if "Labels" in template:
            holder = STATE / f"label.{name}"
            if not holder.exists():
                return 1
            print(holder.read_text())
        elif template == "{{.Id}}":
            holder = STATE / f"id.{name}"
            if not holder.exists():
                return 1
            print(holder.read_text())
        elif "RestartCount" in template or "State.Running" in template:
            if not (STATE / f"id.{name}").exists() and not (STATE / f"pid.{name}").exists():
                fail(f"Error: No such object: {name}")
            counter = STATE / f"restarts.{name}"
            restarts = int(counter.read_text()) if counter.exists() else 0
            if crash_loops(name) and is_running(name):       # it dies and Docker starts it again, each time we look
                restarts += 1
                counter.write_text(str(restarts))
            print(template.replace("{{.State.Running}}", "true" if is_running(name) else "false")
                  .replace("{{.State.Restarting}}", "true" if crash_loops(name) and restarts % 2 else "false")
                  .replace("{{.RestartCount}}", str(restarts)))
        elif template == "{{.HostConfig.CgroupParent}}":     # as the container was MADE (see `compose up`)
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            holder = STATE / f"cgroup-parent.{name}"
            print(holder.read_text() if holder.exists() else "")
        elif template == "{{.HostConfig.Memory}}":           # as the container was MADE (see `compose up`)
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            holder = STATE / f"memory.{name}"
            print(holder.read_text() if holder.exists() else "0")
        elif template in ("{{.State.Pid}} {{.Id}}", "{{.HostConfig.Init}}"):
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            if "Pid" in template:
                # Where its first process is in the host's process table (PID 0 once it has stopped), and the
                # container's ID, after which Docker names the container's control group. A test that wants
                # the script's own look at /proc to agree gives a real process and the group it really is in
                # (STUB_FIRST_PID, STUB_CONTAINER_ID); otherwise that PID is nobody's, or somebody else's.
                pid = os.environ.get("STUB_FIRST_PID", "4242") if is_running(name) else "0"
                print(pid, os.environ.get("STUB_CONTAINER_ID", (STATE / f"id.{name}").read_text()))
            else:                                            # as the container was MADE (see `compose up`)
                print("true" if (STATE / f"init.{name}").exists() else "<nil>")
        elif "Config.Env" in template:                        # as the container was MADE (see `compose up`)
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            holder = STATE / f"repl-path.{name}"
            print("LEAN_SERVER_API_KEY=the-key-a-status-line-must-never-show")
            if holder.exists():
                print("LEAN_SERVER_REPL_PATH=" + holder.read_text())
            print("LEAN_SERVER_MAX_REPLS=8")
        elif template == "{{.HostConfig.ReadonlyRootfs}}":    # as the container was MADE (see `compose up`)
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            holder = STATE / f"read-only.{name}"
            print(holder.read_text() if holder.exists() else "false")
        elif template == "{{range .Mounts}}{{.Destination}} {{end}}":
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            holder = STATE / f"mounts.{name}"
            print(holder.read_text() + " " if holder.exists() and holder.read_text() else "")
        elif template == "{{.Config.Image}}":                 # the image it was made from
            if not (STATE / f"id.{name}").exists():
                fail(f"Error: No such object: {name}")
            holder = STATE / f"image.{name}"
            print(holder.read_text() if holder.exists() else "")
        elif "Health" in template:
            print("healthy")
        elif "State.Status" in template:
            print("running" if is_running(name) else "exited")
        else:
            print("")
        return 0
    if verb == "rm":
        name = argv[-1]
        pid_file = STATE / f"pid.{name}"
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGTERM)
            except OSError:
                pass
            pid_file.unlink()
        for holder in (STATE / "running" / name, STATE / f"label.{name}", STATE / f"id.{name}", STATE / f"by-hand.{name}",
                       STATE / f"policy.{name}", STATE / f"restarts.{name}", STATE / f"init.{name}",
                       STATE / f"cgroup-parent.{name}", STATE / f"memory.{name}", STATE / f"repl-path.{name}",
                       STATE / f"read-only.{name}", STATE / f"mounts.{name}", STATE / f"image.{name}"):
            holder.unlink(missing_ok=True)
        return 0
    if verb == "diff":
        # What a container changed in its writable layer: a Lean server that has served checks wrote unnamed
        # files in /tmp and Python's byte-code cache, and Docker made each of ITS mount points (a server made
        # before the compiled search step has no /opt/lean-native); every directory above a change is listed as
        # changed. STUB_DIFF_EXTRA adds lines; STUB_DIFF_FAILS fails.
        name = argv[-1]
        if not (STATE / f"id.{name}").exists():
            fail(f"Error response from daemon: No such container: {name}")
        if os.environ.get("STUB_DIFF_FAILS"):
            fail("Error response from daemon: " + os.environ["STUB_DIFF_FAILS"])
        note("diffs", name)
        print("C /root\nC /root/kimina-lean-server\nC /root/kimina-lean-server/server\n"
              "A /root/kimina-lean-server/server/__pycache__\nA /root/kimina-lean-server/server/__pycache__/repl.cpython-313.pyc\n"
              "C /tmp\nA /tmp/tmpq8x1v2c4\n"
              # `lake env` at each worker start: git saves the index it refreshed in each package.
              "C /lean-project\nC /lean-project/.lake\nC /lean-project/.lake/packages\n"
              "C /lean-project/.lake/packages/mathlib\nC /lean-project/.lake/packages/mathlib/.git\n"
              "C /lean-project/.lake/packages/mathlib/.git/index\n"
              # Docker's own init (`init: true`), which `{{.Mounts}}` does not list.
              "C /usr\nC /usr/sbin\nA /usr/sbin/docker-init")
        holder = STATE / f"mounts.{name}"
        for mount in (holder.read_text().split() if holder.exists() else []):
            parts = mount.strip("/").split("/")
            for depth in range(1, len(parts)):
                print("C /" + "/".join(parts[:depth]))
            print("A " + mount)
        if os.environ.get("STUB_DIFF_EXTRA"):
            print(os.environ["STUB_DIFF_EXTRA"])
        return 0
    if verb in ("kill", "stop"):
        # What the daemon does (moby, daemon/kill.go): a container Docker itself signals is marked as stopped
        # by hand, WHATEVER the signal, and such a container is not started again by its restart policy.
        name = argv[-1]
        if not is_running(name):
            fail(f"Error response from daemon: cannot {verb} container {name}: it is not running")
        (STATE / f"by-hand.{name}").write_text(" ".join(argv))
        if verb == "stop" or "HUP" not in argv:
            (STATE / "running" / name).unlink(missing_ok=True)
        else:
            note("reloads", name)
        return 0
    if verb == "start":
        name = argv[-1]
        (STATE / f"by-hand.{name}").unlink(missing_ok=True)
        (STATE / "running" / name).write_text("")
        return 0
    if verb == "logs":
        if (STUB / "join.log").exists() and "join" in argv[-1]:
            sys.stdout.write((STUB / "join.log").read_text())
        elif crash_loops(argv[-1]):
            print("[ALERT]    (1) : config : parsing [/usr/local/etc/haproxy/haproxy.cfg:34] : 'bind :18001' in section "
                  "'listen' : unable to stat SSL certificate from file '/etc/leanpool/tls/box.pem' : Permission denied.")
        return 0
    if verb == "network" and argv[1] == "inspect":
        if argv[2] == "bridge":
            print(os.environ.get("STUB_BRIDGE", "172.17.0.1"))
            return 0
        # A network exists once the compose project that makes it has been brought up (they share a name here).
        if not (STATE / f"project.{argv[2]}").exists():
            fail(f"Error response from daemon: network {argv[2]} not found")
        return 0
    if verb == "stats":                                      # stats --no-stream --format TEMPLATE NAME: one line
        if not (STATE / f"id.{argv[-1]}").exists():
            fail(f"Error response from daemon: No such container: {argv[-1]}")
        print(argv[argv.index("--format") + 1].replace("{{.CPUPerc}}", "412.50%").replace("{{.MemUsage}}", "5.120GiB / 24GiB")
              .replace("{{.PIDs}}", os.environ.get("STUB_PIDS", "83")))
        return 0
    if verb == "pull":                                       # pull REFERENCE: the image is here, under that name
        if os.environ.get("STUB_PULL_FAILS"):
            fail(f"Error response from daemon: pull access denied for {argv[-1]}, repository does not exist")
        image_state(argv[-1]).write_text(hashlib.sha256(("pulled " + argv[-1]).encode()).hexdigest())
        print(f"Status: Downloaded newer image for {argv[-1]}")
        return 0
    if verb == "tag":                                        # tag SOURCE TARGET: one image, one more name
        if not image_state(argv[1]).exists():
            fail(f"Error response from daemon: No such image: {argv[1]}")
        image_state(argv[2]).write_text(image_state(argv[1]).read_text())
        return 0
    return 0                     # network rm, volume rm


sys.exit(main())
'''

OWNERS = r'''#!__PYTHON__
"""Stand-ins for install, chown and mv: do the real thing without the owner, and remember the owner asked for."""
import json
import os
import subprocess
import sys
from pathlib import Path

STUB = Path(os.environ["STUB"])
tool, arguments = Path(sys.argv[0]).name, sys.argv[1:]
BOOK = STUB / "owners.json"
book = json.loads(BOOK.read_text()) if BOOK.exists() else {}


def remember(path, owner, group):
    known = book.get(str(path), ["root", "root"])
    book[str(path)] = [owner if owner is not None else known[0], group if group is not None else known[1]]


if tool == "install":
    owner = group = None
    kept, directories, index = [], False, 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("-o", "-g"):
            if argument == "-o":
                owner = arguments[index + 1]
            else:
                group = arguments[index + 1]
            index += 2
            continue
        directories = directories or argument == "-d"
        kept.append(argument)
        index += 1
    code = subprocess.run(["__REAL_install__", *kept]).returncode
    paths = [argument for position, argument in enumerate(kept)
             if not argument.startswith("-") and (position == 0 or kept[position - 1] != "-m")]
    if directories:
        targets = [Path(path) for path in paths]
    else:
        destination = Path(paths[-1])
        targets = [destination / Path(source).name for source in paths[:-1]] if destination.is_dir() else [destination]
    for target in targets:
        remember(target, owner or "root", group or "root")
elif tool == "chown":
    recursive = arguments[0] == "-R"
    spec, paths = (arguments[1], arguments[2:]) if recursive else (arguments[0], arguments[1:])
    owner, _, group = spec.partition(":")
    for path in paths:
        every = [Path(path), *Path(path).rglob("*")] if recursive else [Path(path)]
        for target in every:
            remember(target, owner or None, group or None)
    code = 0
else:                                    # mv: the owner travels with the file
    code = subprocess.run(["__REAL_mv__", *arguments]).returncode
    paths = [argument for argument in arguments if not argument.startswith("-")]
    if len(paths) == 2 and paths[0] in book:
        book[paths[1]] = book.pop(paths[0])
BOOK.write_text(json.dumps(book))
sys.exit(code)
'''

SHELL_STAND_INS = {
    "id": (
        '#!/usr/bin/env bash\nif [ "${1:-}" = -u ]; then echo "${STUB_UID:-0}"\n'
        # An account's group, for the accounts a test names (STUB_USERS): each has a group of its own name.
        'elif [ "${1:-}" = -gn ] && [[ " ${STUB_USERS:-} " == *" ${2:-}:"* ]]; then echo "$2"\n'
        'else exec __REAL_id__ "$@"; fi\n'
    ),
    # The box's accounts: only those a test names (STUB_USERS, `name:uid ...`), with homes under the fake root.
    # Never the real ones: removing the door edits the authorized_keys of every account it lists.
    "getent": (
        '#!/usr/bin/env bash\n[ "${1:-}" = passwd ] || exit 2\nfound=2\n'
        "for entry in ${STUB_USERS:-}; do\n  name=${entry%%:*}; uid=${entry#*:}\n"
        '  if [ -z "${2:-}" ] || [ "$2" = "$name" ]; then\n'
        '    echo "$name:x:$uid:$uid::$FAKE_ROOT/home/$name:/bin/bash"; found=0\n  fi\ndone\n'
        '[ -n "${2:-}" ] || exit 0\nexit "$found"\n'
    ),
    # `visudo -cf FILE`: the file holds only `Defaults` lines and user rules; nothing is installed.
    "visudo": (
        '#!/usr/bin/env bash\n[ "${1:-}" = -cf ] && [ -s "${2:-}" ] || exit 1\n'
        "! grep -v -E '^(Defaults[ !]|[a-z_][a-z0-9_-]* ALL=)' \"$2\" | grep -q .\n"
    ),
    "hostname": '#!/usr/bin/env bash\necho "${STUB_HOSTNAME:-Workstation}"\n',
    "nproc": "#!/usr/bin/env bash\necho 8\n",
    # The box's memory, as `kimina-ctl status` asks for it (`free -b`, the total of the `Mem:` line).
    "free": (
        "#!/usr/bin/env bash\n"
        'if [ -n "${STUB_FREE_FAILS:-}" ]; then echo "free: cannot read the box\'s memory" >&2; exit 1; fi\n'
        "printf '               total        used        free\\nMem:  %s 1 1\\nSwap: 0 0 0\\n' "
        '"${STUB_BOX_MEMORY:-68719476736}"\n'
    ),
    # The machine type (`uname -m`): the real one, unless a test makes this box another kind (STUB_MACHINE),
    # which is how a test gets a box that cannot use the image its pool ships and so builds its own.
    "uname": '#!/usr/bin/env bash\nif [ "$*" = -m ] && [ -n "${STUB_MACHINE:-}" ]; then echo "$STUB_MACHINE"; '
    'else exec __REAL_uname__ "$@"; fi\n',
    "sleep": "#!/usr/bin/env bash\nexec __REAL_sleep__ 0.02\n",
    # The host's process table, as `kimina-ctl status` asks for it: the PID namespace of one process, then the
    # namespace and state of every process. Where a test gives the number of the container's zombies
    # (STUB_ZOMBIES) the table holds that many in the container's namespace, in the three ways ps writes one,
    # beside its live processes and beside the host's own processes, two of them zombies. Else the real ps.
    "ps": (
        "#!/usr/bin/env bash\n"
        'if [ -n "${STUB_PS_FAILS:-}" ]; then echo "ps: cannot read the process table" >&2; exit 1; fi\n'
        'if [ -z "${STUB_ZOMBIES+set}" ]; then exec __REAL_ps__ "$@"; fi\n'
        'case "$*" in\n'
        '  "-o pidns= -p "*) echo "4026530001" ;;\n'
        '  "-eo pidns=,stat=")\n'
        "    printf '4026531836 Ss\\n4026531836 Z\\n4026531836 Zs\\n  17402340 S\\n4026530001 Ss\\n4026530001 Sl\\n4026530001 Rl\\n'\n"
        "    for ((i = 0; i < STUB_ZOMBIES; i++)); do\n"
        '      case $((i % 3)) in 0) echo "4026530001 Z" ;; 1) echo "4026530001 Zs" ;; *) echo "4026530001 Z+" ;; esac\n'
        "    done ;;\n"
        '  *) exec __REAL_ps__ "$@" ;;\n'
        "esac\n"
    ),
    # `start NAME.slice`: systemd makes that control group and gives it the CPU weight its unit names (100
    # with no unit). STUB_SLICE_WITHOUT_WEIGHT is a box whose groups do not carry one.
    "systemctl": (
        '#!/usr/bin/env bash\nprintf \'systemctl %s\\n\' "$*" >> "$STUB/calls"\n'
        'if [ "${1:-}" = start ] && [[ "${2:-}" == *.slice ]] && [ -z "${STUB_SLICE_WITHOUT_WEIGHT:-}" ]; then\n'
        '  weight=$(sed -n "s/^CPUWeight=//p" "$FAKE_ROOT/etc/systemd/system/$2" 2>/dev/null | head -n 1)\n'
        '  mkdir -p "$FAKE_ROOT/sys/fs/cgroup/$2"\n'
        '  echo "${weight:-100}" > "$FAKE_ROOT/sys/fs/cgroup/$2/cpu.weight"\n'
        "fi\n"
    ),
    "df": "#!/usr/bin/env bash\nprintf 'Avail\\n%sG\\n' \"${STUB_DISK_FREE:-500}\"\n",
    # A check (-C) answers "no such rule", so the firewall script's delete loops end; the rest succeeds.
    "iptables": '#!/usr/bin/env bash\nprintf \'iptables %s\\n\' "$*" >> "$STUB/calls"\n[ "$1" != -C ]\n',
    # The machine's own resolver, when a test says what it knows (STUB_HOST_LOOKUP); else the real python3.
    "python3": (
        '#!/usr/bin/env bash\nif [ "${2:-}" = lookup ] && [ -n "${STUB_HOST_LOOKUP+set}" ]; then\n'
        '  printf \'%s\\n\' "$STUB_HOST_LOOKUP"\nelse\n  exec __PYTHON__ "$@"\nfi\n'
    ),
    # A clock that jumps STUB_CLOCK_STEP seconds each time it is read, so a window's time limit is reached at once.
    "date": (
        '#!/usr/bin/env bash\nif [ -n "${STUB_CLOCK_STEP:-}" ] && [ "$*" = "+%s" ]; then\n'
        '  read=$(cat "$STUB/clock" 2>/dev/null || echo 0)\n  echo $((read + STUB_CLOCK_STEP)) > "$STUB/clock"\n'
        '  echo $((1700000000 + read))\nelse\n  exec __REAL_date__ "$@"\nfi\n'
    ),
    "ss": '#!/usr/bin/env bash\ncase "$*" in *":${STUB_BUSY_PORT:-none}") echo "LISTEN 0 128 0.0.0.0:$STUB_BUSY_PORT 0.0.0.0:*" ;; esac\n',
    "ip": (
        '#!/usr/bin/env bash\ncase "$*" in\n'
        '  "-4 route get 1.1.1.1") echo "1.1.1.1 via 192.168.50.1 dev eth0 src 192.168.50.20 uid 0" ;;\n'
        '  "-4 route show dev eth0 scope link") echo "192.168.50.0/24 proto kernel src 192.168.50.20" ;;\n'
        '  "-4 route show default") echo "default via 192.168.50.1 dev eth0 proto dhcp" ;;\n'
        "  \"-4 route show\") printf 'default via 192.168.50.1 dev eth0\\n192.168.50.0/24 dev eth0\\n'\n"
        '     [ -z "${STUB_EXTRA_ROUTE:-}" ] || echo "$STUB_EXTRA_ROUTE dev br-other proto kernel" ;;\n'
        "esac\n"
    ),
    # The package managers, never the real ones: a package is "there" only where a test says
    # so (STUB_APT_HAS), and installing it leaves the mark the docker stand-in reads.
    "apt-get": (
        '#!/usr/bin/env bash\nprintf \'apt-get %s\\n\' "$*" >> "$STUB/calls"\n'
        'case " $* " in *" update "*) exit 0 ;; esac\npackage=${*: -1}\n'
        'case " ${STUB_APT_HAS:-} " in *" $package "*) : > "$STUB/state/compose-plugin"; exit 0 ;; esac\n'
        'echo "E: Unable to locate package $package" >&2\nexit 100\n'
    ),
    "dnf": (
        '#!/usr/bin/env bash\nprintf \'dnf %s\\n\' "$*" >> "$STUB/calls"\n'
        'echo "Error: Unable to find a match: ${*: -1}" >&2\nexit 1\n'
    ),
    "yum": '#!/usr/bin/env bash\nprintf \'yum %s\\n\' "$*" >> "$STUB/calls"\nexit 1\n',
}
# curl is the REAL one (a join talks to the real join service), except for the one request that would leave
# this machine: the compose plugin's release binary, which no test may fetch.
CURL = (
    '#!/usr/bin/env bash\ncase "$*" in\n  *github.com/docker/compose*)\n'
    '    printf \'curl %s\\n\' "$*" >> "$STUB/calls"\n'
    '    echo "curl: (6) Could not resolve host: github.com" >&2\n    exit 6 ;;\nesac\n'
    # The first download of the pool's image is cut short (STUB_CUT_FIRST_DOWNLOAD): slowed, then stopped.
    'if [ -n "${STUB_CUT_FIRST_DOWNLOAD:-}" ] && [[ " $* " == *" -C - "* ]] && [ ! -e "$STUB/download-was-cut" ]; then\n'
    '  : > "$STUB/download-was-cut"\n  exec __REAL_curl__ "$@" --limit-rate 150k --max-time 1\nfi\n'
    'exec __REAL_curl__ "$@"\n'
)


# A pool's admission cases, as a test installs a pool with them: the repository's own example.
ADMIT_CASES = str(ROOT / "examples" / "admission-cases")
# What the installers build the pool's image from, and all a join bundle carries of the project.
PROJECT_FILES = ("pyproject.toml", "README.md", "LICENSE", "leanpool", "deploy/Dockerfile")


def tools_run_from_source() -> bool:
    """Whether the lean-pool project's tools start with this Python (cryptography for the certificates)."""
    result = subprocess.run(
        [sys.executable, "-m", "leanpool.pki", "--help"],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    return result.returncode == 0


def rewritten(name: str, content: bytes, programs: tuple[str, ...] = PROGRAMS) -> bytes:
    """A script's host paths moved under ${FAKE_ROOT}; anything that is not a script is left as it is."""
    if name.endswith(".sh") or name in programs:
        for prefix in HOST_PREFIXES:
            content = content.replace(prefix.encode(), b"${FAKE_ROOT}" + prefix.encode())
    return content


def copy_rewritten(source: Path, directory: Path, programs: tuple[str, ...] = PROGRAMS) -> None:
    """A directory copied with its modes, its scripts rewritten (`rewritten`); Python's caches are left out."""
    directory.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_dir() and path.name != "__pycache__":
            copy_rewritten(path, directory / path.name, programs)
        elif path.is_file():
            (directory / path.name).write_bytes(rewritten(path.name, path.read_bytes(), programs))
            (directory / path.name).chmod(path.stat().st_mode & 0o777)


def copy_project(directory: Path) -> Path:
    """The lean-pool project as the installers read it (PROJECT_FILES), at DIRECTORY."""
    for name in PROJECT_FILES:
        source, target = ROOT / name, directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return directory


def rewritten_installers(directory: Path, site: Path = SITE) -> Path:
    """A copy of the repository whose installers' host paths start at ${FAKE_ROOT}: the project at DIRECTORY, the
    installers at DIRECTORY/deploy/install, and SITE at DIRECTORY/deploy/install/site, where the installers find
    a site when they are given none (as in a join bundle). Returns the installers' directory."""
    copy_project(directory)
    installers = directory / "deploy" / "install"
    copy_rewritten(INSTALLERS, installers)
    shutil.copytree(site, installers / "site")
    return installers


class Machine:
    """One fake machine: its root, its stand-ins on PATH, and what happened on it.

    DEPLOY is where the scripts a test runs by name are looked for first, then `installers`; `project` is the
    lean-pool project the docker stand-in runs the pool's tools from. Both follow DEPLOY, which a test may change
    (to run another copy of the installers on the same machine); a site that keeps its own scripts apart from
    the installers says where they are by overriding the two."""

    def __init__(self, directory: Path, deploy: Path, create: bool = True) -> None:
        self.directory = directory
        self.root = directory / "root"
        self.stub = directory / "stub"
        self.deploy = deploy
        if not create:  # a copy of a machine that exists (see `copy_to`)
            return
        for path in (
            "srv",
            "usr/local/sbin",
            "usr/local/bin",
            "usr/local/lib",
            "etc/systemd/system",
            "etc/sudoers.d",
            "run/systemd/system",
            "sys/fs/cgroup",
        ):
            (self.root / path).mkdir(parents=True)
        (self.stub / "bin").mkdir(parents=True)
        (self.stub / "state" / "running").mkdir(parents=True)
        (self.stub / "docker-root").mkdir()
        (self.stub / "calls").write_text("")
        python = sys.executable

        def real(text: str) -> str:  # the real tools, by where they really are
            for name in ("install", "mv", "id", "sleep", "date", "uname", "ps"):
                text = text.replace(f"__REAL_{name}__", shutil.which(name) or name)
            return text.replace("__PYTHON__", python)

        (self.stub / "bin" / "docker").write_text(real(DOCKER))
        for name in ("install", "chown", "mv"):
            (self.stub / "bin" / name).write_text(real(OWNERS))
        for name, content in SHELL_STAND_INS.items():
            (self.stub / "bin" / name).write_text(real(content))
        curl = shutil.which("curl")
        if curl:  # without a real curl there is nothing to stand in front of
            (self.stub / "bin" / "curl").write_text(CURL.replace("__REAL_curl__", curl))
        # The certificate tool by name, for the program `status` runs inside the pool image.
        (self.stub / "bin" / "leanpool-pki").write_text(
            f'#!/usr/bin/env bash\nPYTHONPATH={ROOT} exec {python} -m leanpool.pki "$@"\n'
        )
        for program in (self.stub / "bin").iterdir():
            program.chmod(0o755)

    @property
    def installers(self) -> Path:
        """The installers' directory: DEPLOY itself."""
        return self.deploy

    @property
    def project(self) -> Path:
        """The lean-pool project: the repository copy the installers are in."""
        return project_of(self.installers)

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.stub}/site-bin:{self.stub}/bin:{os.environ['PATH']}",
            "STUB": str(self.stub),
            "FAKE_ROOT": str(self.root),
            "STUB_LEAN_POOL": str(self.project),
            "SUDO_USER": "",
            **extra,
        }

    def script(self, name: str) -> Path:
        """One of the (rewritten) scripts by name: in DEPLOY, else in the installers' directory."""
        own = self.deploy / name
        return own if own.exists() else self.installers / name

    def bash(
        self, script: str | Path, *arguments: str, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        """Run one of the (rewritten) scripts as root on this machine, for at most three minutes."""
        path = script if isinstance(script, Path) else self.script(script)
        return subprocess.run(
            ["bash", str(path), *arguments],
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            env=self.environment(**extra),
        )

    def start(self, script: str, *arguments: str, **extra: str) -> subprocess.Popen[str]:
        """Start one of the scripts and leave it running (a join window waits in the foreground)."""
        return subprocess.Popen(
            ["bash", str(self.script(script)), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=subprocess.DEVNULL,
            env=self.environment(**extra),
        )

    def copy_to(self, directory: Path) -> Machine:
        """This machine as it stands, copied: a test changes its own copy."""
        shutil.copytree(self.directory, directory, symlinks=True)
        copy = object.__new__(type(self))
        copy.__dict__.update(self.__dict__)
        copy.directory, copy.root, copy.stub = directory, directory / "root", directory / "stub"
        book = copy.stub / "owners.json"
        if book.exists():
            book.write_text(
                book.read_text().replace(str(self.directory) + "/", str(directory) + "/")
            )
        return copy

    def mark(self) -> int:
        """Where the record of calls stands now; `calls_since(mark)` is what happened after."""
        return len(self.calls)

    @property
    def calls(self) -> list[str]:
        return (self.stub / "calls").read_text().splitlines()

    def calls_since(self, mark: int) -> list[str]:
        return self.calls[mark:]

    def owner(self, path: str | Path) -> tuple[str, str, int]:
        """(owner, group, mode) of a file under the fake root, as the installer asked for them."""
        full = self.root / str(path).lstrip("/")
        book = (
            json.loads((self.stub / "owners.json").read_text())
            if (self.stub / "owners.json").exists()
            else {}
        )
        owner, group = book.get(str(full), ["root", "root"])
        return owner, group, full.stat().st_mode & 0o777

    def path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def container_id(self, name: str) -> str:
        holder = self.stub / "state" / f"id.{name}"
        return holder.read_text() if holder.exists() else ""

    def recreated(self) -> list[str]:
        holder = self.stub / "recreated"
        return holder.read_text().splitlines() if holder.exists() else []

    def control_group(self, name: str) -> str:
        """The control group a container was started in when it was last made (empty: Docker's own place)."""
        holder = self.stub / "state" / f"cgroup-parent.{name}"
        return holder.read_text() if holder.exists() else ""

    def memory_ceiling(self, name: str) -> int:
        """The memory ceiling, in bytes, a container was given when it was last made (0: none)."""
        holder = self.stub / "state" / f"memory.{name}"
        return int(holder.read_text()) if holder.exists() else 0

    def worker_program(self, name: str) -> str:
        """The program a Lean server's workers are started as, as its container was last made."""
        holder = self.stub / "state" / f"repl-path.{name}"
        return holder.read_text() if holder.exists() else ""

    def read_only(self, name: str) -> bool:
        """Whether a container's root filesystem was read-only when it was last made."""
        holder = self.stub / "state" / f"read-only.{name}"
        return holder.exists() and holder.read_text() == "true"

    def diffs(self) -> list[str]:
        """The containers `docker diff` was asked about, in order."""
        holder = self.stub / "diffs"
        return holder.read_text().splitlines() if holder.exists() else []

    def native_builds(self) -> list[str]:
        """The images the compiled search step was built and checked in, in order."""
        holder = self.stub / "native-builds"
        return holder.read_text().splitlines() if holder.exists() else []

    def reloads(self) -> list[str]:
        """The containers that were told to reload, in order."""
        holder = self.stub / "reloads"
        return holder.read_text().splitlines() if holder.exists() else []

    def running(self, name: str) -> bool:
        return (self.stub / "state" / "running" / name).exists()

    def stopped_by_hand(self, name: str) -> bool:
        """Whether Docker's own book says this container was stopped (or signalled) by hand."""
        return (self.stub / "state" / f"by-hand.{name}").exists()

    def restart_daemon(self) -> None:
        """A reboot, or `systemctl restart docker`: every container stops, and the daemon starts again those
        its book says to start: `restart: always`, or `unless-stopped` and not stopped by hand."""
        state = self.stub / "state"
        for holder in sorted(state.glob("id.*")):
            name = holder.name[3:]
            policy = (
                (state / f"policy.{name}").read_text()
                if (state / f"policy.{name}").exists()
                else "no"
            )
            comes_back = policy == "always" or (
                policy == "unless-stopped" and not self.stopped_by_hand(name)
            )
            (state / "running" / name).unlink(missing_ok=True)
            if comes_back:
                (state / "running" / name).write_text("")


def installed_instances(directory: Path, deploy: Path, plain_pool: bool = False) -> Machine:
    """A machine with the tests' site installed: instance 1 and the `demo` instance with its own published
    port and usage agent; with `plain_pool`, also `demo`'s pool, without TLS."""
    machine = Machine(directory, deploy)
    steps: list[tuple[str, ...]] = [("--no-door",), ("--instance", "demo", "--no-door")]
    if plain_pool:
        steps.append(("--proxy", "demo", "--admit-cases", ADMIT_CASES))
    for arguments in steps:
        result = machine.bash("install.sh", *arguments)
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return machine


def snapshot(directory: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under a directory: its mode and its content."""
    return {
        str(path.relative_to(directory)): (
            path.stat().st_mode & 0o777,
            path.read_bytes() if path.is_file() else b"",
        )
        for path in sorted(directory.rglob("*"))
    }


def copy_deploy(deploy: Path, directory: Path) -> Path:
    """A copy of the repository copy that DEPLOY (its installers) is in, which a test may change. Returns the
    copy's installers."""
    shutil.copytree(deploy.parents[1], directory, symlinks=True)
    return directory / deploy.relative_to(deploy.parents[1])


def project_of(deploy: Path) -> Path:
    """The lean-pool project a copy's installers build from: the repository copy they are in."""
    return deploy.parents[1]
