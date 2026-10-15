"""The script a new box runs to join.

The join script runs as root on a box nobody has prepared, so it cannot be run for real here (no root, no
Docker). Instead the REAL script (join-box.sh, assembled by join_helper.py exactly as `join-open` does, and
fed to bash on standard input exactly as `curl ... | sudo bash` does) is run against stand-ins on PATH:

  docker    records its arguments; runs the real join_helper.py for `docker run ... python /work/join/...`
            (with /work mapped back to the host directory), writes a fake key and request for
            `leanpool-pki csr`, and answers `test -f` by whether the mount arrives (STUB_MOUNTS_EMPTY)
  curl      reads its URL from standard input as the real one does with `--config -`, records its
            arguments, and plays the pool's join service from a scenario
  id, hostname, nproc, sleep, install, chown
            say "root", a host name, 8 cores, do not wait, and do not need root
  apt-get, dnf, yum
            record their call and install nothing: a package exists only where a test says so
  install.sh (inside the bundle)   records how the instance installer was called (its pre-flight, the image
            build, then the install) and what it was handed

So the control flow, the argument passing, every file's content at the moment the installer runs, what is
printed, and what is left behind on every way out are observed for real. What the stand-ins replace (the
real Docker, the real pool, the real installer) is covered by a join of a real box.
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent / "site"
INSTALLERS = Path(__file__).resolve().parents[2] / "deploy" / "install"

spec = importlib.util.spec_from_file_location("join_helper", INSTALLERS / "join_helper.py")
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
sys.modules["join_helper"] = helper
spec.loader.exec_module(helper)

TOKEN = "Zm9vYmFyYmF6cXV4MTIzNA"
PIN = "sha256//aR+oLs7t3Drg8iCuBU78cmTEW0H6s6AFU4x873jIMAc="
API_KEY = "0123456789abcdef0123456789abcdef"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nQk9YIENFUlRJRklDQVRF\n-----END CERTIFICATE-----\n"
AUTHORITY = "-----BEGIN CERTIFICATE-----\nUE9PTCBBVVRIT1JJVFk=\n-----END CERTIFICATE-----\n"
FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nVEhFIEJPWCdTIFBSSVZBVEUgS0VZ\n-----END PRIVATE KEY-----\n"
FAKE_CSR = "-----BEGIN CERTIFICATE REQUEST-----\nVEhFIEJPWCdTIFJFUVVFU1Q=\n-----END CERTIFICATE REQUEST-----\n"
IMAGE_TAG = "acme/kimina:lean-demo-1"
# The image a pool ships in these tests: its file, and the layer list Docker reports for it once it is loaded.
POOL_IMAGE = b"the pool's Lean image, as docker save wrote it\n" * 400
POOL_LAYERS = '["sha256:' + "1a" * 32 + '","sha256:' + "2b" * 32 + '"]'

DOCKER = r"""#!/usr/bin/env bash
# A stand-in for docker: see the module docstring.
printf 'docker %s\n' "$*" >> "$STUB/calls"
case "${1:-}" in
  info) [ "${2:-}" != -f ] || echo "${STUB_DOCKER_ROOT:-/var/lib/docker}"; exit "${STUB_DOCKER_INFO:-0}" ;;
  # A box without the compose plugin (STUB_COMPOSE_MISSING) has none until a package's stand-in leaves its mark.
  compose) [ -z "${STUB_COMPOSE_MISSING:-}" ] || [ -e "$STUB/compose-plugin" ]; exit ;;
  build) exit 0 ;;
  version) echo 27.5.1; exit 0 ;;
  # The Lean image: what is under its tag ($STUB/image-layers holds its layer list once it is there).
  image)
    [ "${2:-}" = inspect ] || exit 0                 # `image rm`: recorded above
    case "$4" in
      *RootFS.Layers*) [ -e "$STUB/image-layers" ] || exit 1; cat "$STUB/image-layers" ;;
    esac
    exit 0 ;;
  load)                                              # what the pool sent, loaded: says what it brought in
    [ -z "${STUB_LOAD_FAILS:-}" ] || { echo "open blobs/sha256: unsupported image format: this Docker cannot read it" >&2; exit 1; }
    printf '%s\n' "${STUB_LOAD_SAYS:-Loaded image: $STUB_IMAGE_TAG}"
    if [ -n "${STUB_LOAD_OTHER_LAYERS:-}" ]; then echo '["sha256:0000"]' > "$STUB/image-layers"
    else cp "$STUB/pool-layers" "$STUB/image-layers"; fi
    stat -c %s "${3:-}" > "$STUB/loaded-bytes"
    exit 0 ;;
  run) shift ;;
  *) exit 0 ;;
esac
work=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rm|-i) shift ;;
    --network|--user) shift 2 ;;
    -v) work=${2%:/work}; shift 2 ;;
    *) break ;;
  esac
done
printf '%s\n' "$work" > "$STUB/work"
shift                                     # the image
case "$1" in
  test) [ -z "${STUB_MOUNTS_EMPTY:-}" ] && [ -f "${3//\/work/$work}" ]; exit ;;
  python)
    [ -z "${STUB_MOUNTS_EMPTY:-}" ] || { echo "python: can't open file '$2': [Errno 2] No such file or directory" >&2; exit 2; }
    shift
    mapped=()
    for argument in "$@"; do mapped+=("${argument//\/work/$work}"); done
    exec "$STUB_PYTHON" "${mapped[@]}" ;;
  leanpool-pki)                           # csr --out-dir /work/tls --name NAME --file-stem box
    [ "$2" = csr ] || exit 2
    out=${4//\/work/$work}
    printf '%s\n' "$6" > "$STUB/csr-name"
    mkdir -m 0700 -p "$out"
    ( umask 077; printf '%s' "$STUB_KEY" > "$out/box.key" )
    printf '%s' "$STUB_CSR" > "$out/box.csr"
    echo "$out/box.csr" ;;
  *) exit 2 ;;
esac
"""

CURL = r"""#!/usr/bin/env bash
# A stand-in for curl that plays the pool's join service: see the module docstring.
case "$*" in                                         # the compose plugin's release binary: never the network
  *github.com/docker/compose*)
    printf 'curl %s\n' "$*" >> "$STUB/calls"
    echo "curl: (6) Could not resolve host: github.com" >&2
    exit 6 ;;
esac
url=$(sed -n 's/^url = "\(.*\)"$/\1/p')              # from standard input, as `--config -` reads it
printf 'curl %s\n' "$*" >> "$STUB/calls"
printf '%s\n' "$url" >> "$STUB/urls"
what=${url##*/}
out=""; data=""
arguments=("$@")
for ((i = 0; i < ${#arguments[@]}; i++)); do
  case "${arguments[i]}" in
    -o) out=${arguments[i + 1]} ;;
    --data-binary) data=${arguments[i + 1]} ;;
  esac
done
seen=$(cat "$STUB/seen.$what" 2>/dev/null || echo 0)
echo $((seen + 1)) > "$STUB/seen.$what"
if [ "$what" = "${STUB_CURL_FAILS_AT:-}" ]; then
  echo "curl: (90) SSL: public key does not match pinned public key" >&2
  printf '000'
  exit 90
fi
code=200; body=""
case "$what" in
  csr) cp "${data#@}" "$STUB/posted-csr.json"; code=${STUB_CSR_CODE:-202}; body=${STUB_CSR_BODY:-'{"status": "received"}'} ;;
  certificate)
    if [ "$seen" -lt 1 ]; then code=204
    else code=${STUB_CERTIFICATE_CODE:-200}; body=$(cat "$STUB/certificate-reply.json"); fi ;;
  ready) code=${STUB_READY_CODE:-202}; body='{"status": "received"}' ;;
  verdict)
    if [ "$seen" -lt 1 ]; then code=204
    else body=$(cat "$STUB/verdict-reply.json"); fi ;;
  # The image a pool ships: its manifest if the test gave the pool one, else 204; and the file,
  # which goes on from the bytes already there (`-C -`), is cut short once, or never arrives.
  image.json)
    if [ -e "$STUB/image-reply.json" ]; then body=$(cat "$STUB/image-reply.json"); else code=${STUB_IMAGE_JSON_CODE:-204}; fi ;;
  image)
    have=$(stat -c %s "$out" 2>/dev/null || echo 0)
    if [ -n "${STUB_IMAGE_NEVER_ARRIVES:-}" ]; then
      echo "curl: (28) Operation too slow. Less than 1024 bytes/sec transferred the last 120 seconds" >&2
      printf '000'; exit 28
    fi
    if [ -n "${STUB_IMAGE_IS_CUT:-}" ] && [ "$seen" -lt 1 ]; then
      head -c 4000 "$STUB/pool-image" > "$out"
      echo "curl: (18) transfer closed with outstanding read data remaining" >&2
      printf '200'; exit 18
    fi
    tail -c "+$((have + 1))" "$STUB/pool-image" >> "$out"
    echo "$have" >> "$STUB/image-resumed-from"
    if [ "$have" -gt 0 ]; then printf '206'; else printf '200'; fi
    exit 0 ;;
  *) code=404; body='{"detail": "Not Found"}' ;;
esac
[ -z "$out" ] || printf '%s' "$body" > "$out"
printf '%s' "$code"
"""

INSTALLER = r"""#!/usr/bin/env bash
# A stand-in for the instance installer: says where the instance lives, and records how it was called
# and what it was handed at that moment.
if [ "${*: -1}" = --show-instance ]; then
  printf 'dir=%s\nport=18001\nagent_port=18201\nimage=%s\n' "$STUB/instance" "$STUB_IMAGE_TAG"
  exit 0
fi
if [ "${*: -1}" = --preflight-only ]; then          # the pre-flight: what it was asked with, and what existed then
  printf '%s\n' "$*" > "$STUB/preflight-arguments"
  cp "$STUB/urls" "$STUB/asked-before-preflight"
  cp "$STUB/calls" "$STUB/calls-before-preflight"
  [ ! -e "$STUB/csr-name" ] || touch "$STUB/key-made-before-preflight"
  if [ -n "${STUB_PREFLIGHT_FAILS:-}" ]; then
    echo "install.sh: need at least 60 GB free under /var/lib/docker for a build of the image" >&2
    exit 1
  fi
  echo "pre-flight passed: this host can take the install. Nothing was installed."
  exit 0
fi
if [ "${*: -1}" = --build-only ]; then              # the image build: what it was asked with, and what existed then
  printf '%s\n' "$*" > "$STUB/build-arguments"
  cp "$STUB/urls" "$STUB/asked-before-build"
  [ ! -e "$STUB/csr-name" ] || touch "$STUB/key-made-before-build"
  mkdir -p "$STUB/instance"                         # the build files and the build log land here, whatever follows
  echo "Step 1/21 : FROM python:3.13-slim" > "$STUB/instance/build.log"
  cat > /dev/null                                    # an installer that read its standard input would eat the join script
  if [ -n "${STUB_BUILD_FAILS:-}" ]; then
    echo "install.sh: the image build failed; the full log is $STUB/instance/build.log" >&2
    exit 1
  fi
  : > "$STUB/instance/image.built"
  echo "BUILT acme/kimina:lean-demo-1, and nothing else"
  exit 0
fi
printf '%s\n' "$*" > "$STUB/install-arguments"
printf '%s\n' "$0" > "$STUB/installer-ran-from"
[ ! -e "$STUB/instance/image.built" ] || touch "$STUB/image-built-before-install"
while [ $# -gt 0 ]; do
  case "$1" in
    --tls-dir) cp -R "$2" "$STUB/handed-tls"; stat -c '%a %n' "$2"/* | sed "s|$2/||" | sort > "$STUB/handed-tls-modes"; shift 2 ;;
    --api-key-file) cp "$2" "$STUB/handed-api-key"; stat -c '%a' "$2" > "$STUB/handed-api-key-mode"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "${STUB_INSTALL_LEAVES_NOTHING:-}" ] || mkdir -p "$STUB/instance"
cat > /dev/null                            # an installer that read its standard input would eat the join script
exit "${STUB_INSTALL_FAILS:-0}"
"""

SMALL = {
    "id": '#!/usr/bin/env bash\nif [ "${1:-}" = -u ]; then echo "${STUB_UID:-0}"; else exec /usr/bin/id "$@"; fi\n',
    "hostname": '#!/usr/bin/env bash\necho "${STUB_HOSTNAME:-Lap-Top}"\n',
    "nproc": "#!/usr/bin/env bash\necho 8\n",
    # The free disk beside the instance, where the pool's image file is put while it is loaded (STUB_ROOM, in GB).
    "df": "#!/usr/bin/env bash\nprintf 'Avail\\n%sG\\n' \"${STUB_ROOM:-500}\"\n",
    "sleep": "#!/usr/bin/env bash\nexit 0\n",
    "chown": "#!/usr/bin/env bash\nexit 0\n",
    # `install -o root -g root` needs root; the stand-in drops the owner and does the rest for real.
    "install": '#!/usr/bin/env bash\nkept=()\nwhile [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) kept+=("$1"); shift ;; esac; done\n'
    'exec /usr/bin/install "${kept[@]}"\n',
    # The package managers, never the real ones. A package is "there" only where a test says so (STUB_APT_HAS);
    # each records its call and whether it could read the join script from standard input.
    "apt-get": '#!/usr/bin/env bash\nprintf \'apt-get %s | stdin=%s\\n\' "$*" "$(cat | wc -c)" >> "$STUB/calls"\n'
    'case " $* " in *" update "*) exit 0 ;; esac\npackage=${*: -1}\n'
    'case " ${STUB_APT_HAS:-} " in *" $package "*) : > "$STUB/compose-plugin"; exit 0 ;; esac\n'
    'echo "E: Unable to locate package $package" >&2\nexit 100\n',
    "dnf": '#!/usr/bin/env bash\nprintf \'dnf %s | stdin=%s\\n\' "$*" "$(cat | wc -c)" >> "$STUB/calls"\n'
    'echo "Error: Unable to find a match: ${*: -1}" >&2\nexit 1\n',
    "yum": '#!/usr/bin/env bash\nprintf \'yum %s | stdin=%s\\n\' "$*" "$(cat | wc -c)" >> "$STUB/calls"\nexit 1\n',
}


def bundle() -> str:
    """A join bundle with the REAL pool-lib.sh and join_helper.py, a stand-in installer, the site's values (as
    pack_join_bundle puts them in site/) and just enough of the lean-pool project for build_lean_pool_image to
    read its version."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:

        def add(name: str, content: str, mode: int = 0o644) -> None:
            data = content.encode()
            member = tarfile.TarInfo(f"join/{name}")
            member.size, member.mode = len(data), mode
            archive.addfile(member, io.BytesIO(data))

        add("install.sh", INSTALLER, 0o755)
        add("pool-lib.sh", (INSTALLERS / "pool-lib.sh").read_text())
        add("join_helper.py", (INSTALLERS / "join_helper.py").read_text())
        add("site/site.env", (SITE / "site.env").read_text())
        add("site/instances/demo.env", (SITE / "instances" / "demo.env").read_text())
        add("lean_pool/pyproject.toml", '[project]\nname = "lean-pool"\nversion = "0.1.0"\n')
        add("lean_pool/deploy/Dockerfile", "FROM scratch\n")
    return base64.encodebytes(buffer.getvalue()).decode()


def join_script() -> str:
    settings = helper.ScriptSettings(
        url_base="https://workstation.lan:18110",
        token=TOKEN,
        pin=PIN,
        pool="demo",
        lean_timeout="300",
        server_wait="120",
    )
    return str(helper.join_script(settings, (INSTALLERS / "join-box.sh").read_text(), bundle()))


class Box:
    """One run of the join script on a stand-in box."""

    def __init__(self, tmp_path: Path) -> None:
        self.stub = tmp_path / "stub"
        (self.stub / "bin").mkdir(parents=True)
        for name, content in {"docker": DOCKER, "curl": CURL, **SMALL}.items():
            path = self.stub / "bin" / name
            path.write_text(content)
            path.chmod(0o755)
        self.reply(
            certificate={"certificate": CERTIFICATE, "ca": AUTHORITY, "api_key": API_KEY},
            verdict={
                "admitted": True,
                "detail": "lap-top (lap-top.lan:18001, 3 workers) is serving checks in pool demo",
            },
        )
        (self.stub / "calls").write_text("")
        (self.stub / "urls").write_text("")

    def reply(self, **replies: object) -> None:
        for name, body in replies.items():
            (self.stub / f"{name}-reply.json").write_text(json.dumps(body))

    def run(
        self, *options: str, script: str | None = None, **environment: str
    ) -> subprocess.CompletedProcess[str]:
        """`curl ... | sudo bash -s -- OPTIONS`: the script arrives on standard input."""
        self.result = subprocess.run(
            ["bash", "-s", "--", *options],
            input=join_script() if script is None else script,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=self.stub,
            env={
                **os.environ,
                "PATH": f"{self.stub}/bin:{os.environ['PATH']}",
                "STUB": str(self.stub),
                "STUB_PYTHON": sys.executable,
                "STUB_KEY": FAKE_KEY,
                "STUB_CSR": FAKE_CSR,
                "STUB_IMAGE_TAG": IMAGE_TAG,
                **environment,
            },
        )
        return self.result

    def read(self, name: str) -> str:
        path = self.stub / name
        return path.read_text() if path.exists() else ""

    def pool_ships(self, **changes: object) -> dict[str, object]:
        """The pool of this box ships an image: its manifest, its file and its layer list."""
        digest = hashlib.sha256(POOL_IMAGE).hexdigest()
        manifest = {
            "tag": IMAGE_TAG,
            "image_id": "sha256:" + "9c" * 32,
            "layers": hashlib.sha256(POOL_LAYERS.encode()).hexdigest(),
            "file": f"image-{digest[:12]}.tar",
            "sha256": digest,
            "bytes": len(POOL_IMAGE),
            "machine": os.uname().machine,
            "format": "docker-save",
            "docker": "28.0.4",
            **changes,
        }
        (self.stub / "image-reply.json").write_text(json.dumps(manifest))
        (self.stub / "pool-image").write_bytes(POOL_IMAGE)
        (self.stub / "pool-layers").write_text(POOL_LAYERS)
        return manifest

    @property
    def calls(self) -> list[str]:
        return self.read("calls").splitlines()

    @property
    def asked(self) -> list[str]:
        """What was asked of the pool, in order: the part of each URL after the token."""
        return [url.split(TOKEN, 1)[1] for url in self.read("urls").splitlines()]

    @property
    def work(self) -> Path:
        return Path(self.read("work").strip())

    @property
    def output(self) -> str:
        return self.result.stdout + self.result.stderr


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# --- a join that goes well ---------------------------------------------------------------------------------------


def steps_of(output: str) -> list[str]:
    """The first word of each `==> ` line: `1/10` … `10/10`, and `Building` for the pool's own image."""
    return [line.split()[1] for line in output.splitlines() if line.startswith("==> ")]


TEN_STEPS = [
    "1/10",
    "2/10",
    "3/10",
    "Building",
    "4/10",
    "5/10",
    "6/10",
    "7/10",
    "8/10",
    "9/10",
    "10/10",
]


def test_a_join_runs_its_ten_steps_in_order_and_ends_joined(box):
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    assert box.asked == [
        "/image.json",
        "/csr",
        "/certificate",
        "/certificate",
        "/ready",
        "/verdict",
        "/verdict",
    ]
    assert (
        "the Lean server's image is built here: the pool ships no image in this window"
        in result.stdout
    )
    assert steps_of(result.stdout) == TEN_STEPS
    assert (
        "JOINED: lap-top (lap-top.lan:18001, 3 workers) is serving checks in pool demo"
        in result.stdout
    )
    assert "sudo lean-pool-ctl demo remove-backend lap-top" in result.stdout
    assert (
        f"sudo bash {box.stub}/instance/installer/install.sh --instance demo --uninstall --purge"
        in result.stdout
    )


def test_the_request_names_this_box_its_ports_and_its_workers(box):
    box.run("--workers", "3")
    assert json.loads(box.read("posted-csr.json")) == {
        "name": "lap-top",
        "lean_port": 18001,
        "agent_port": 18201,
        "workers": 3,
        "csr": FAKE_CSR,
    }
    assert (
        box.read("csr-name").strip() == "lap-top"
    )  # the key is made for the same, lower-case name


def test_the_installer_is_called_for_a_tls_pool_member_with_the_pools_key_and_durations(box):
    box.run("--workers", "3")
    work = box.work
    options = [
        "--instance",
        "demo",
        "--no-door",
        "--repls",
        "3",
        "--cpus",
        "3",
        "--mem-limit",
        "16g",
        "--build-jobs",
        "3",
        "--tls-dir",
        f"{work}/tls",
        "--server-name",
        "lap-top",
        "--pool-lean-timeout",
        "300",
        "--pool-server-wait",
        "120",
    ]
    assert shlex.split(box.read("install-arguments")) == [
        *options,
        "--api-key-file",
        f"{work}/api-key",
    ]
    # Its pre-flight and its image build were run with the very same options (and no key: there is none yet).
    assert shlex.split(box.read("preflight-arguments")) == [*options, "--preflight-only"]
    assert shlex.split(box.read("build-arguments")) == [*options, "--build-only"]


def test_what_the_installer_is_handed_is_complete_and_private(box):
    box.run("--workers", "3")
    handed = box.stub / "handed-tls"
    assert (handed / "box.crt").read_text() == CERTIFICATE and (
        handed / "ca.crt"
    ).read_text() == AUTHORITY
    assert (
        handed / "box.pem"
    ).read_text() == CERTIFICATE + FAKE_KEY  # certificate, then key: HAProxy's `crt`
    modes = dict(reversed(line.split()) for line in box.read("handed-tls-modes").splitlines())
    assert modes["box.key"] == "600" and modes["box.pem"] == "600"
    assert (
        box.read("handed-api-key").strip() == API_KEY
        and box.read("handed-api-key-mode").strip() == "600"
    )


def test_options_reach_the_installer(box):
    box.run("--name", "desk-2", "--workers", "5", "--cpus", "4", "--no-firewall")
    arguments = shlex.split(box.read("install-arguments"))
    assert (
        arguments[arguments.index("--repls") + 1] == "5"
        and arguments[arguments.index("--cpus") + 1] == "4"
    )
    assert (
        arguments[arguments.index("--mem-limit") + 1] == "24g"
    )  # 4 GiB a worker and 4 GiB beside them
    assert (
        arguments[arguments.index("--server-name") + 1] == "desk-2" and "--no-firewall" in arguments
    )
    assert "--no-firewall" in shlex.split(box.read("preflight-arguments"))
    assert json.loads(box.read("posted-csr.json"))["name"] == "desk-2"


def test_the_worker_count_is_sized_and_stated_before_the_installer_runs(box):
    result = box.run()
    assert result.returncode == 0, box.output
    workers = json.loads(box.read("posted-csr.json"))["workers"]
    assert 1 <= workers <= 6  # 8 cores - 2, or less by this machine's memory
    stated = next(
        line
        for line in result.stdout.splitlines()
        if "workers: the smaller of 8 cores - 2 = 6" in line
    )
    assert stated.startswith(f"{workers} workers") and "--workers N" in stated
    assert (
        result.stdout.index(stated)
        < result.stdout.index("4/10 Checking")
        < result.stdout.index("5/10 The Lean server's image")
        < result.stdout.index("8/10 Installing")
    )
    assert shlex.split(box.read("install-arguments"))[4] == str(workers)


# --- secrets ------------------------------------------------------------------------------------------------------


def test_every_request_to_the_pool_is_pinned_and_none_shows_the_token(box):
    """The token is part of every URL. It must never be an argument of any program (`ps` shows arguments to
    every user of the box): curl is handed the URL on standard input."""
    box.run("--workers", "3")
    requests = [call for call in box.calls if call.startswith("curl ")]
    assert len(requests) == 7
    for call in requests:
        assert f"--pinnedpubkey {PIN} --config -" in call and " -sSk " in call
        assert "https://" not in call
    assert all(TOKEN not in call for call in box.calls)
    assert TOKEN not in box.output and TOKEN not in box.read("install-arguments")


def test_the_api_key_and_the_private_key_are_never_printed_or_passed_as_arguments(box):
    box.run("--workers", "3")
    for secret in (API_KEY, "VEhFIEJPWCdTIFBSSVZBVEUgS0VZ"):
        assert secret not in box.output
        assert all(secret not in call for call in box.calls)
        assert secret not in box.read("install-arguments")


def test_the_temporary_directory_with_the_key_copy_is_gone_and_the_installer_copy_holds_no_key(box):
    box.run("--workers", "3")
    assert str(box.work) not in ("", ".") and not box.work.exists()
    kept = box.stub / "instance" / "installer"
    assert (
        (kept / "install.sh").exists()
        and (kept / "pool-lib.sh").exists()
        and (kept / "lean_pool" / "pyproject.toml").exists()
    )
    for path in kept.rglob("*"):
        assert (
            path.name not in ("box.key", "box.pem", "api-key", "reply.json")
            and "tls" not in path.parts
        )
        if path.is_file():
            assert "PRIVATE KEY" not in path.read_text() and API_KEY not in path.read_text()
        assert (
            not stat.S_IMODE(path.stat().st_mode) & 0o022
        )  # nobody but root may change the installer


# --- every way out that is not a join -------------------------------------------------------------------------------


def nothing_installed(box: Box) -> bool:
    return not (box.stub / "install-arguments").exists()


def test_a_pool_whose_key_is_not_the_pinned_one_gets_nothing_and_nothing_is_installed(box):
    result = box.run("--workers", "3", STUB_CURL_FAILS_AT="csr")
    assert result.returncode == 1 and "step 7 failed" in result.stderr
    # By then the image is built (step 5): said, with the command that removes it; nothing that needs a key is there.
    assert (
        "On this box: the Lean server's image and" in result.stderr
        and "no key and no certificate" in result.stderr
    )
    assert "--uninstall --purge" in result.stderr and "Nothing was installed" not in result.stderr
    assert box.asked == ["/image.json", "/csr"] and nothing_installed(box) and not box.work.exists()


def test_a_refused_request_says_why_and_installs_nothing(box):
    box.reply(certificate={"reason": "a server named 'lap-top' is already in the pool"})
    result = box.run("--workers", "3", STUB_CERTIFICATE_CODE="409")
    assert result.returncode == 1
    assert (
        "the pool refused this box: a server named 'lap-top' is already in the pool"
        in result.stderr
    )
    assert nothing_installed(box) and not box.work.exists() and "/ready" not in box.asked


def test_a_malformed_request_is_reported_with_the_pools_reason(box):
    result = box.run(
        "--workers",
        "3",
        STUB_CSR_CODE="422",
        STUB_CSR_BODY='{"detail": "workers must be a whole number"}',
    )
    assert (
        result.returncode == 1
        and "the pool answered 422: workers must be a whole number" in result.stderr
    )
    assert nothing_installed(box) and not box.work.exists()


def test_an_answer_without_a_usable_key_installs_nothing(box):
    box.reply(certificate={"certificate": CERTIFICATE, "ca": AUTHORITY, "api_key": None})
    result = box.run("--workers", "3")
    assert result.returncode == 1 and "step 7 failed" in result.stderr
    assert nothing_installed(box) and not box.work.exists()


def test_a_failed_installer_is_never_reported_ready_and_the_key_copy_is_still_deleted(box):
    result = box.run("--workers", "3", STUB_INSTALL_FAILS="1")
    assert (
        result.returncode == 1
        and "step 8 failed" in result.stderr
        and "The pool is unchanged" in result.stderr
    )
    # The pool has signed by now: the window is this box's, and a new attempt needs a new one.
    assert (
        "this join window is used up" in result.stderr
        and "run the NEW command here" in result.stderr
    )
    assert "the image is here and is neither downloaded nor built again" in result.stderr
    assert "/ready" not in box.asked and not box.work.exists()
    assert "--uninstall --purge" in result.stderr  # how to remove what was installed


def test_a_server_that_is_not_admitted_says_what_the_pool_found_and_how_to_remove_it(box):
    box.reply(
        verdict={
            "admitted": False,
            "detail": "admission FAILED for 192.168.50.44:18001: 2 of 12 admission cases misbehaved; the pool is unchanged",
        }
    )
    result = box.run("--workers", "3")
    assert result.returncode == 1
    assert (
        "NOT ADMITTED: admission FAILED for 192.168.50.44:18001: 2 of 12 admission cases misbehaved"
        in result.stderr
    )
    assert "--uninstall --purge" in result.stderr and "JOINED" not in result.stdout
    assert not box.work.exists()


def test_a_window_that_closed_while_the_box_was_building_is_said_plainly(box):
    result = box.run("--workers", "3", STUB_CURL_FAILS_AT="ready")
    assert (
        result.returncode == 1
        and "step 9 failed" in result.stderr
        and "open a new window" in result.stderr
    )
    assert not box.work.exists()


def test_the_installers_pre_flight_runs_before_the_pool_is_asked_for_anything(box):
    """The certificate and the pool's key were handed over BEFORE the installer checked
    free disk, ports and the subnet, so a box that failed there had used up its join window."""
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    # (Whether the pool ships an image is asked first: the disk a box needs depends on it. That binds nothing.)
    assert box.read("asked-before-preflight").split(TOKEN)[1:] == ["/image.json\n"]
    assert not (box.stub / "key-made-before-preflight").exists()
    assert "--api-key-file" not in box.read("preflight-arguments")


def test_a_box_that_fails_the_pre_flight_stops_with_the_installers_own_message_and_has_asked_nothing(
    box,
):
    result = box.run("--workers", "3", STUB_PREFLIGHT_FAILS="1")
    assert result.returncode == 1
    assert (
        "install.sh: need at least 60 GB free under /var/lib/docker" in result.stderr
    )  # the installer's own words
    assert "step 4 failed" in result.stderr and "the pool has signed nothing" in result.stderr
    assert "run the same join command again" in result.stderr  # the window is still open
    assert box.asked == ["/image.json"]  # which binds nothing
    assert not (box.stub / "csr-name").exists()  # no key was even made
    assert nothing_installed(box) and not box.work.exists()
    assert not (box.stub / "build-arguments").exists()  # and nothing was built


# --- the image is built before the pool is asked for anything ------------------------------------------------------------


def test_the_image_is_built_before_a_key_is_made_and_before_the_pool_is_asked_for_anything(box):
    """A join that stops inside the image build AFTER the pool has signed costs a join window."""
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    assert box.read("asked-before-build").split(TOKEN)[1:] == ["/image.json\n"]
    assert not (box.stub / "key-made-before-build").exists()
    assert (
        "--api-key-file" not in box.read("build-arguments")
        and (box.stub / "preflight-arguments").exists()
    )
    assert (box.stub / "image-built-before-install").exists()  # the install found the image there
    calls = box.calls
    key_made = next(index for index, call in enumerate(calls) if "leanpool-pki csr" in call)
    requests = [index for index, call in enumerate(calls) if call.startswith("curl ")]
    assert requests[0] < key_made < requests[1]  # the manifest; then the key; then the request


def test_a_box_whose_image_does_not_build_has_asked_the_pool_nothing_and_joins_with_the_same_command(
    box,
):
    result = box.run("--workers", "3", STUB_BUILD_FAILS="1")
    assert result.returncode == 1 and "step 5 failed" in result.stderr
    assert "install.sh: the image build failed" in result.stderr  # the installer's own words
    assert "The pool has signed nothing, so this join window is still good" in result.stderr
    assert (
        "run the same join command again" in result.stderr
        and "Docker keeps the layers that were built" in result.stderr
    )
    assert box.asked == ["/image.json"]
    assert not (box.stub / "csr-name").exists()  # no key was even made
    assert nothing_installed(box) and not box.work.exists()
    # What the box holds is named, with the ONE command that removes it: the installer copy is already there.
    removal = (
        f"sudo bash {box.stub}/instance/installer/install.sh --instance demo --uninstall --purge"
    )
    assert f"On this box so far: {box.stub}/instance" in result.stderr and removal in result.stderr
    kept = box.stub / "instance" / "installer"
    assert (kept / "install.sh").exists() and (kept / "pool-lib.sh").exists()
    for path in kept.rglob("*"):
        assert path.name not in ("box.key", "box.pem", "api-key") and "tls" not in path.parts
    # The cause put right, the SAME command joins.
    result = box.run("--workers", "3")
    assert result.returncode == 0 and "JOINED: lap-top" in result.stdout, box.output
    assert steps_of(result.stdout) == TEN_STEPS


def test_a_failed_build_on_a_box_that_got_the_compose_plugin_says_that_the_plugin_stays(box):
    result = box.run(
        "--workers",
        "3",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
        STUB_BUILD_FAILS="1",
    )
    assert result.returncode == 1 and "step 5 failed" in result.stderr
    assert (
        "The docker compose plugin stays too (sudo apt-get remove docker-compose-v2 removes it)."
        in result.stderr
    )
    assert box.asked == ["/image.json"]


# --- a box with Docker but without its compose plugin --------------------------------------------------------------------


def package_managers_called(box) -> list[str]:
    return [
        call.split(" | ")[0] for call in box.calls if call.startswith(("apt-get ", "dnf ", "yum "))
    ]


def test_a_box_that_has_the_compose_plugin_calls_no_package_manager(box):
    result = box.run("--workers", "3")
    assert result.returncode == 0 and package_managers_called(box) == []
    assert "docker compose plugin" not in box.output


def test_a_box_without_the_compose_plugin_gets_it_in_step_four_and_joins(box):
    """A join stopped at step 1 on this: Ubuntu's own docker.io ships no compose plugin."""
    result = box.run("--workers", "3", STUB_COMPOSE_MISSING="1", STUB_APT_HAS="docker-compose-v2")
    assert result.returncode == 0, box.output
    assert (
        "installed the docker compose plugin: the apt package docker-compose-v2. "
        "To remove it: sudo apt-get remove docker-compose-v2"
    ) in result.stdout
    assert "JOINED: lap-top" in result.stdout
    # After the installer's pre-flight, before a key is made and before the pool is asked for anything.
    assert not any(
        call.startswith(("apt-get", "dnf", "yum"))
        for call in box.read("calls-before-preflight").splitlines()
    )
    calls = box.calls
    first_package_call = next(
        index for index, call in enumerate(calls) if call.startswith("apt-get ")
    )
    key_made = next(index for index, call in enumerate(calls) if "leanpool-pki csr" in call)
    requests = [index for index, call in enumerate(calls) if call.startswith("curl ")]
    assert (
        requests[0] < first_package_call < key_made < requests[1]
    )  # requests[0] only reads the image manifest
    assert package_managers_called(box) == [
        "apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-plugin",
        "apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-v2",
    ]
    # A package manager that read standard input would have eaten the rest of this very script.
    assert all(call.endswith("stdin=0") for call in calls if call.startswith("apt-get "))
    assert steps_of(result.stdout) == TEN_STEPS


def test_a_box_on_which_the_plugin_cannot_be_added_stops_at_step_four_and_has_asked_the_pool_nothing(
    box,
):
    result = box.run("--workers", "3", STUB_COMPOSE_MISSING="1")
    assert result.returncode == 1 and "step 4 failed" in result.stderr
    assert (
        "this box has Docker without its compose plugin, and the plugin could not be added"
        in result.stderr
    )
    for reason in (
        "apt-get install docker-compose-plugin: E: Unable to locate package docker-compose-plugin",
        "apt-get install docker-compose-v2: E: Unable to locate package docker-compose-v2",
        "dnf install docker-compose-plugin: Error: Unable to find a match: docker-compose-plugin",
    ):
        assert reason in result.stderr, reason
    # The release binary: the download failed (or, on a machine that has a plugin file of its own, was not tried).
    assert (
        "failed: curl: (6) Could not resolve host: github.com" in result.stderr
        or "it was not replaced" in result.stderr
    )
    assert "Nothing else was installed and the pool has signed nothing" in result.stderr
    assert "run the same join command again" in result.stderr  # the window is still open
    assert (box.stub / "preflight-arguments").exists()  # the pre-flight had passed
    assert box.asked == ["/image.json"] and not (box.stub / "csr-name").exists()
    assert nothing_installed(box) and not box.work.exists()


def test_a_box_that_fails_the_pre_flight_gets_no_plugin_installed(box):
    result = box.run(
        "--workers",
        "3",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
        STUB_PREFLIGHT_FAILS="1",
    )
    assert (
        result.returncode == 1
        and "step 4 failed" in result.stderr
        and "Nothing was installed and the pool has signed nothing" in result.stderr
    )
    assert package_managers_called(box) == [] and not (box.stub / "compose-plugin").exists()


def test_a_later_failure_says_that_the_plugin_stays_and_how_to_remove_it(box):
    result = box.run(
        "--workers",
        "3",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-plugin",
        STUB_CURL_FAILS_AT="csr",
    )
    assert result.returncode == 1 and "step 7 failed" in result.stderr
    assert (
        "The docker compose plugin stays too (sudo apt-get remove docker-compose-plugin removes it)."
        in result.stderr
    )
    assert "On this box: the Lean server's image and" in result.stderr
    assert "Nothing was installed" not in result.stderr


def test_snap_docker_is_refused_before_any_attempt_to_install_the_plugin(box):
    result = box.run(
        "--workers",
        "3",
        STUB_DOCKER_ROOT="/var/snap/docker/common/var-lib-docker",
        STUB_COMPOSE_MISSING="1",
    )
    assert (
        result.returncode == 1
        and "step 1 failed" in result.stderr
        and "the snap package, which is not supported" in result.stderr
    )
    assert [call.split()[1] for call in box.calls] == ["info", "info"] and package_managers_called(
        box
    ) == []


def test_docker_from_the_snap_package_is_refused_in_the_first_step(box):
    """With snap-installed Docker the join stopped at step 3 on a missing file. Snap's Docker
    cannot mount /tmp or /srv, so it is named as unsupported before anything is unpacked or built."""
    result = box.run("--workers", "3", STUB_DOCKER_ROOT="/var/snap/docker/common/var-lib-docker")
    assert result.returncode == 1 and "step 1 failed" in result.stderr
    assert (
        "the snap package, which is not supported" in result.stderr
        and "sudo snap remove docker" in result.stderr
    )
    assert "Nothing was installed" in result.stderr
    assert [call.split()[1] for call in box.calls] == [
        "info",
        "info",
    ]  # nothing was built, run or asked
    assert box.read("work") == ""


def test_a_docker_whose_containers_cannot_see_the_unpacked_installer_is_said_plainly(box):
    result = box.run("--workers", "3", STUB_MOUNTS_EMPTY="1")
    assert (
        result.returncode == 1
        and "step 3 failed: a container on this box cannot see" in result.stderr
    )
    assert "not supported" in result.stderr and "Nothing was installed" in result.stderr
    assert "No such file" not in result.stderr  # not the helper's confusing failure
    assert (
        box.asked == []
        and nothing_installed(box)
        and not (box.stub / "preflight-arguments").exists()
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (("--workers", "0"), "--workers must be 1-32"),
        (("--workers", "40"), "--workers must be 1-32"),
        (("--cpus", "x"), "--cpus must be a number"),
        (("--name", "Lap_Top"), "cannot be this box's name in the pool"),
        (("--name", "laptop.lan"), "no dot"),
        (("--name", "x" * 41), "at most 40"),  # one DNS label: what the pool signs
        (("--name",), "--name needs a value"),
        (("--shell",), "unknown option"),
    ],
)
def test_a_bad_option_is_refused_before_anything_is_touched(box, options, message):
    result = box.run(*options)
    assert result.returncode == 1 and message in result.stderr and box.calls == []


def test_a_host_name_that_cannot_be_on_a_certificate_asks_for_a_name(box):
    result = box.run(STUB_HOSTNAME="my_laptop")
    assert result.returncode == 1 and "pass --name NAME" in result.stderr and box.calls == []


def test_without_root_or_without_docker_nothing_happens(box):
    result = box.run("--workers", "3", STUB_UID="1000")
    assert result.returncode == 1 and "must run as root" in result.stderr and box.calls == []
    result = box.run("--workers", "3", STUB_DOCKER_INFO="1")
    assert result.returncode == 1 and "step 1 failed" in result.stderr and box.asked == []


@pytest.mark.parametrize("fraction", [0.05, 0.3, 0.6, 0.9, 0.999])
def test_a_download_that_was_cut_short_runs_nothing(box, fraction):
    """`curl | sudo bash` executes what arrives. The script is functions and five settings until its LAST
    line, so any prefix of it defines things at most."""
    script = join_script()
    box.run("--workers", "3", script=script[: int(len(script) * fraction)])
    assert box.calls == [] and nothing_installed(box)
    whole_but_for_the_last_line = script[: script.rindex('main "$@"')]
    box.run("--workers", "3", script=whole_but_for_the_last_line)
    assert box.calls == [] and nothing_installed(box)


# --- a box that is already in the pool takes this window's installer -----------------------------------------------------

REFRESH = ["--instance", "demo", "--no-door", "--keep-image"]


def in_the_pool(box: Box, firewall: bool = True) -> Path:
    """What a join left on a box: its settings, its certificate, its firewall settings, its installer copy."""
    instance = box.stub / "instance"
    (instance / "tls").mkdir(parents=True)
    (instance / "installer").mkdir()
    (instance / "kimina.env").write_text(
        f"LEAN_SERVER_API_KEY={API_KEY}\nLEAN_SERVER_NAME=lap-top\nLEAN_SERVER_MAX_REPLS=3\n"
    )
    (instance / "tls" / "box.pem").write_text(CERTIFICATE + FAKE_KEY)
    (instance / "installer" / "install.sh").write_text("# the installer of this box's own join\n")
    (instance / "installer" / "only-in-the-older-one").write_text("")
    if firewall:
        (instance / "firewall.conf").write_text("KIMINA_FW_PORT=18001\n")
    return instance


def test_a_refresh_runs_this_windows_installer_as_the_join_ran_it_and_asks_the_pool_for_nothing(
    box,
):
    instance = in_the_pool(box)
    result = box.run("--refresh")
    assert result.returncode == 0, box.output
    assert steps_of(result.stdout) == ["1/3", "2/3", "3/3"]
    # The instance installer of the bundle, once, as the join ran it and told to keep the image. No pre-flight
    # of the join's and no build before it: the installer makes its own checks.
    assert shlex.split(box.read("install-arguments")) == REFRESH
    assert (
        not (box.stub / "preflight-arguments").exists()
        and not (box.stub / "build-arguments").exists()
    )
    # The pool was asked for nothing at all (not even whether it ships an image), and no key was made.
    assert box.asked == [] and not any(call.startswith("curl ") for call in box.calls)
    assert not (box.stub / "csr-name").exists() and not any(
        call.startswith("docker run") for call in box.calls
    )
    # The installer copy on the box is this window's, whole, and nobody but root may change it.
    kept = instance / "installer"
    assert (kept / "install.sh").read_text() == INSTALLER and (kept / "pool-lib.sh").exists()
    assert not (kept / "only-in-the-older-one").exists()
    assert not any(stat.S_IMODE(path.stat().st_mode) & 0o022 for path in kept.rglob("*"))
    # What the box had, it has; and what was unpacked is gone.
    assert (instance / "tls" / "box.pem").read_text() == CERTIFICATE + FAKE_KEY
    assert (instance / "kimina.env").read_text().splitlines()[0] == f"LEAN_SERVER_API_KEY={API_KEY}"
    assert not Path(box.read("installer-ran-from").strip()).parent.parent.exists()
    assert "REFRESHED: 'lap-top'" in result.stdout and "this window is still open" in result.stdout
    assert "sudo kimina-ctl @demo status" in result.stdout
    assert API_KEY not in box.output and TOKEN not in box.output


def test_a_box_that_joined_without_the_firewall_is_refreshed_without_it(box):
    in_the_pool(box, firewall=False)
    assert box.run("--refresh").returncode == 0, box.output
    assert shlex.split(box.read("install-arguments")) == [*REFRESH, "--no-firewall"]


def test_a_refresh_does_not_ask_what_this_host_is_called(box):
    """A join needs a name that can be on a certificate. A refresh keeps the name the box joined under."""
    in_the_pool(box)
    result = box.run("--refresh", STUB_HOSTNAME="my_laptop")
    assert result.returncode == 0 and "REFRESHED: 'lap-top'" in result.stdout, box.output


@pytest.mark.parametrize("left", ["nothing", "settings only", "certificate only"])
def test_a_box_that_holds_no_server_of_the_pool_is_not_refreshed_and_is_told_to_join(box, left):
    if left != "nothing":
        instance = in_the_pool(box)
        (instance / ("tls/box.pem" if left == "settings only" else "kimina.env")).unlink()
    result = box.run("--refresh")
    assert result.returncode == 1 and "this box holds no server of pool demo" in result.stderr, (
        box.output
    )
    assert (
        "run the same command without --refresh" in result.stderr
        and "Nothing was changed" in result.stderr
    )
    assert nothing_installed(box) and box.asked == [] and "REFRESHED" not in result.stdout
    if left != "nothing":
        assert (box.stub / "instance" / "installer" / "only-in-the-older-one").exists()


@pytest.mark.parametrize(
    "options",
    [
        ("--refresh", "--name", "desk-2"),
        ("--workers", "3", "--refresh"),
        ("--refresh", "--cpus", "2"),
        ("--refresh", "--no-firewall"),
        ("--build", "--refresh"),
    ],
)
def test_refresh_goes_with_no_other_option(box, options):
    in_the_pool(box)
    result = box.run(*options)
    assert result.returncode == 1 and "--refresh goes alone" in result.stderr and box.calls == []
    assert nothing_installed(box)


def test_a_refresh_whose_installer_fails_says_so_and_leaves_the_installer_copy_the_box_had(box):
    instance = in_the_pool(box)
    result = box.run("--refresh", STUB_INSTALL_FAILS="1")
    assert result.returncode == 1 and "step 3 failed" in result.stderr, box.output
    assert (
        "The pool's list is unchanged" in result.stderr
        and "run the same command again" in result.stderr
    )
    assert (
        instance / "installer" / "only-in-the-older-one"
    ).exists() and "REFRESHED" not in result.stdout
    assert (
        box.asked == [] and not Path(box.read("installer-ran-from").strip()).parent.parent.exists()
    )


def test_a_join_still_takes_every_option_it_took_and_refresh_is_in_the_usage_text(box):
    result = box.run("--help")
    assert (
        result.returncode == 0
        and "--refresh" in result.stdout
        and "ALREADY in this pool" in result.stdout
    )
    for option in ("--name NAME", "--workers N", "--cpus N", "--no-firewall", "--build"):
        assert option in result.stdout


# --- the bundle a real window serves ------------------------------------------------------------------------------------


def pool_lib(snippet: str) -> subprocess.CompletedProcess[str]:
    script = (
        f"set -euo pipefail\nSRC={shlex.quote(str(INSTALLERS))}\nsay() {{ :; }}\nwarn() {{ :; }}\n"
        f'die() {{ echo "die: $*" >&2; exit 1; }}\n. "$SRC/pool-lib.sh"\nload_site {shlex.quote(str(SITE))}\n{snippet}\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def packed(tmp_path_factory):
    """The real bundle, packed by the real pack_join_bundle from this checkout."""
    out = tmp_path_factory.mktemp("bundle") / "bundle.b64"
    result = pool_lib(f"pack_join_bundle {shlex.quote(str(out))} demo")
    assert result.returncode == 0, result.stderr
    # Left open: the tests read the members' contents from it.
    archive = tarfile.open(fileobj=io.BytesIO(base64.b64decode(out.read_text())), mode="r:gz")  # noqa: SIM115
    return out.read_text(), {member.name: member for member in archive.getmembers()}, archive


def test_the_bundle_holds_the_instance_installer_and_everything_it_asks_for(packed):
    _, members, _ = packed
    # What install.sh's pre-flight wants for `--instance demo --tls-dir ...`, and what the join script itself
    # reads; the site's values for the pool's instance, and its image recipe, in join/site/.
    for name in (
        "install.sh",
        "pool-lib.sh",
        "kimina-firewall",
        "kimina-ctl",
        "join_helper.py",
        "site/site.env",
        "site/instances/demo.env",
        "lean_pool/pyproject.toml",
        "lean_pool/deploy/Dockerfile",
        "lean_pool/README.md",
        "lean_pool/LICENSE",
        "lean_pool/leanpool/pki/cli.py",
        "lean_pool/leanpool/haproxy/box.py",
        "lean_pool/leanpool/agent/cli.py",
        *(
            f"lean_pool/deploy/lean-server/{name}"
            for name in (
                "compose.yaml",
                "compose.agent.yaml",
                "compose.tls.yaml",
                "compose.compiled-search.yaml",
            )
        ),
        *(
            f"lean_pool/deploy/lean-server/compiled-search/{name}"
            for name in ("build.sh", "check.py", "inside.sh", "make.sh", "modules", "repl-native")
        ),
        "lean_pool/deploy/lean-server/read-only-check.sh",
    ):
        assert f"join/{name}" in members, name
    recipe = (
        (SITE / "instances" / "demo.env")
        .read_text()
        .split("INSTANCE_BUILD_FILES=")[1]
        .splitlines()[0]
    )
    for build_file in recipe.split():
        assert f"join/site/{build_file}" in members, build_file
    # No key travels: not the operator's, and not another instance's values.
    assert "join/kimina-ctl.pub" not in members and "join/site/instances/default.env" not in members


def test_the_bundle_holds_no_key_no_settings_and_nothing_a_box_does_not_need(packed):
    text, members, archive = packed
    # The site's values travel (its names, the pool's instance); no setting of an installed server does.
    values = {"join/site/site.env", "join/site/instances/demo.env"}
    for name in values:
        settings = dict(
            line.decode().split("=", 1)
            for line in archive.extractfile(members[name]).read().splitlines()
            if line and not line.startswith(b"#")
        )
        if name.endswith("site.env"):
            assert set(settings) == {
                "SITE_PREFIX",
                "SITE_LABEL",
                "SITE_DOOR_KEY_MARKER",
                "SITE_LAN_DOMAIN",
            }, name
        else:
            assert all(key.startswith("INSTANCE_") for key in settings), name
        assert not [
            value
            for value in settings.values()
            if re.search(r"ssh-|BEGIN|[A-Za-z0-9+/]{32}", value)
        ], name
    for name, member in members.items():
        assert not name.endswith((".key", ".pem", ".env", ".lock")) or name in values, name
        assert "/tests/" not in name, name
        assert ".venv" not in name and "__pycache__" not in name and "/." not in name, name
        assert member.uid == 0 and member.gid == 0
        if member.isfile():
            assert b"PRIVATE KEY-----\nM" not in archive.extractfile(member).read(), (
                name
            )  # no key material
    assert "join/join-box.sh" not in members  # the script's body is served, not unpacked
    assert set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n")
    assert len(text) < 1_000_000


def bundle_version(join: Path) -> subprocess.CompletedProcess[str]:
    """`installer_version demo` as a joined box computes it: from the bundle it unpacked, with the
    bundle's own site."""
    script = (
        f"set -euo pipefail\nSRC={shlex.quote(str(join))}\nsay() {{ :; }}\nwarn() {{ :; }}\n"
        f'die() {{ echo "die: $*" >&2; exit 1; }}\n. "$SRC/pool-lib.sh"\nload_site "$SRC/site"\n'
        "installer_version demo\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


@pytest.fixture
def unpacked(packed, tmp_path):
    """The real bundle, unpacked as a joining box unpacks it: its `join/` directory."""
    _, _, archive = packed
    archive.extractall(tmp_path, filter="data")
    return tmp_path / "join"


def test_a_box_computes_from_its_bundle_the_version_the_pool_computes_from_the_checkout(unpacked):
    here, there = pool_lib("installer_version demo"), bundle_version(unpacked)
    assert here.returncode == 0 and there.returncode == 0, here.stderr + there.stderr
    assert re.fullmatch(r"[0-9a-f]{32}\n", here.stdout), here.stdout
    assert there.stdout == here.stdout


@pytest.mark.parametrize(
    "member",
    [
        "kimina-ctl",
        "lean_pool/deploy/lean-server/compose.agent.yaml",
        "lean_pool/deploy/lean-server/compiled-search/make.sh",
        "lean_pool/deploy/lean-server/read-only-check.sh",
        "site/site.env",
        "site/instances/demo.env",
        "lean_pool/leanpool/agent/cli.py",
        "lean_pool/deploy/Dockerfile",
    ],
)
def test_one_byte_in_any_bundled_file_changes_the_version(unpacked, member):
    before = bundle_version(unpacked).stdout
    path = unpacked / member
    path.write_bytes(path.read_bytes() + b"\n")
    after = bundle_version(unpacked)
    assert after.returncode == 0, after.stderr
    assert re.fullmatch(r"[0-9a-f]{32}\n", after.stdout) and after.stdout != before


def test_a_file_the_bundle_does_not_carry_leaves_the_version_as_it_is(unpacked):
    before = bundle_version(unpacked).stdout
    # The pool's own command runs on the pool's host alone, and Python's byte-code cache is never packed.
    (unpacked / "lean-pool-ctl").write_text("#!/usr/bin/env bash\n")
    (unpacked / "lean_pool/leanpool/__pycache__").mkdir()
    (unpacked / "lean_pool/leanpool/__pycache__/names.cpython-313.pyc").write_bytes(b"\0")
    assert bundle_version(unpacked).stdout == before


def test_a_bundle_missing_a_file_gives_no_version(unpacked):
    (unpacked / "kimina-ctl").unlink()
    result = bundle_version(unpacked)
    assert result.returncode != 0 and result.stdout == ""


def test_the_real_bundle_makes_a_script_bash_accepts(packed, tmp_path):
    text, _, _ = packed
    settings = helper.ScriptSettings(
        url_base="https://workstation.lan:18110",
        token=TOKEN,
        pin=PIN,
        pool="demo",
        lean_timeout="300",
        server_wait="120",
    )
    script = tmp_path / "join.sh"
    script.write_text(helper.join_script(settings, (INSTALLERS / "join-box.sh").read_text(), text))
    assert (
        subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True).returncode == 0
    )


# --- text checks ---------------------------------------------------------------------------------------------------------


def test_no_request_leaves_the_join_script_except_through_the_pinned_call():
    """Text check, to hold what the runs above showed against a later edit: curl appears once as a command,
    inside join_curl, with the pin and with the URL on standard input."""
    code = [line.split("#", 1)[0] for line in (INSTALLERS / "join-box.sh").read_text().splitlines()]
    commands = [
        line
        for line in code
        if "curl " in line
        and "for tool in" not in line
        and "join_curl" not in line
        and "<pin>" not in line
    ]  # the usage text shows the join command
    assert commands == [
        '    | curl -sSk --pinnedpubkey "$JOIN_PIN" --config - --max-time 60 -w \'%{http_code}\' "$@"'
    ]
    assert not any("wget" in line or "http://" in line for line in code)
    # JSON from the pool is never parsed with sed, awk or grep: only by join_helper.py, in the pool image.
    assert not any(re.search(r"\b(sed|awk|grep|jq)\b", line) and "reply" in line for line in code)


# --- the Lean image comes from the pool, and is built only as a fallback --------------------------------------------------


def loaded(box: Box) -> bool:
    return (box.stub / "loaded-bytes").exists()


def built(box: Box) -> bool:
    return (box.stub / "build-arguments").exists()


def test_a_box_loads_the_image_its_pool_ships_and_builds_nothing(box):
    manifest = box.pool_ships()
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    assert steps_of(result.stdout) == TEN_STEPS and "JOINED: lap-top" in result.stdout
    assert (
        f"the Lean server's image comes from the pool: {IMAGE_TAG}, 0.0 GB to download and load"
        in result.stdout
    )
    assert (
        f"loaded {IMAGE_TAG} from the pool" in result.stdout
        and "Nothing is built here." in result.stdout
    )
    assert loaded(box) and not built(box)
    assert box.read("loaded-bytes").strip() == str(
        len(POOL_IMAGE)
    )  # the whole file, as the manifest says
    # The manifest, then the image, and only then the signing request: nothing before it binds the window.
    assert box.asked[:3] == ["/image.json", "/image", "/csr"]
    # The install is told the image is here, by its layer identity, and so is its pre-flight's disk need.
    arguments = shlex.split(box.read("install-arguments"))
    assert arguments[arguments.index("--image-from-pool") + 1] == manifest["layers"]
    preflight = shlex.split(box.read("preflight-arguments"))
    assert (
        preflight[preflight.index("--min-free-gb") + 1] == "4"
        and preflight[-1] == "--preflight-only"
    )
    assert "--image-from-pool" not in preflight
    # The file is gone, and so is everything else that was temporary.
    assert not (box.stub / "instance" / "pool-image.tar.part").exists() and not box.work.exists()
    assert (box.stub / "instance" / "installer" / "install.sh").exists()


def test_the_image_is_fetched_through_the_pinned_request_and_never_shows_the_token(box):
    box.pool_ships()
    box.run("--workers", "3")
    download = next(call for call in box.calls if call.startswith("curl ") and " -C - " in call)
    assert f"--pinnedpubkey {PIN} --config -" in download and " -f " in download
    assert "--max-time 14400" in download and "--speed-limit 1024 --speed-time 120" in download
    assert TOKEN not in download and "https://" not in download


def test_a_download_that_is_cut_short_goes_on_from_the_bytes_it_has(box):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_IMAGE_IS_CUT="1")
    assert result.returncode == 0, box.output
    assert (
        f"the download stopped at 4000 of {len(POOL_IMAGE)} bytes: going on from there (attempt 2 of 5)"
        in result.stdout
    )
    assert box.read("image-resumed-from").split() == [
        "4000"
    ]  # the second request asked for the rest only
    assert (
        loaded(box) and not built(box) and box.read("loaded-bytes").strip() == str(len(POOL_IMAGE))
    )


def test_a_pool_that_ships_no_image_or_an_older_pool_leaves_the_box_to_build(box):
    result = box.run("--workers", "3")
    assert result.returncode == 0 and built(box) and not loaded(box)
    assert (
        "the Lean server's image is built here: the pool ships no image in this window"
        in result.stdout
    )
    assert "/image" not in box.asked and "--min-free-gb" not in box.read("preflight-arguments")
    assert "--image-from-pool" not in box.read("install-arguments")
    older = Box(box.stub.parent / "older")
    result = older.run("--workers", "3", STUB_IMAGE_JSON_CODE="404")
    assert result.returncode == 0 and built(older) and not loaded(older)
    assert (
        "the Lean server's image is built here: this pool's join service does not ship images"
        in result.stdout
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"machine": "riscv64"},
            "the pool's image is for riscv64 machines and this box is " + os.uname().machine,
        ),
        (
            {"tag": "acme/kimina:lean-default-1"},
            "the pool's image is acme/kimina:lean-default-1 and this box's installer installs "
            + IMAGE_TAG,
        ),
    ],
)
def test_an_image_for_another_machine_type_or_another_tag_is_not_taken_and_the_box_builds(
    box, changes, reason
):
    box.pool_ships(**changes)
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    assert f"the Lean server's image is built here: {reason}" in result.stdout
    assert built(box) and not loaded(box) and "/image" not in box.asked


@pytest.mark.parametrize(
    "changes",
    [
        {"file": "../../etc/shadow"},
        {"sha256": "zz"},
        {"bytes": -5},
        {"layers": "short"},
        {"format": "oci-archive"},
        {"bytes": "12"},
    ],
)
def test_a_manifest_that_is_not_one_is_not_followed_and_the_box_builds(box, changes):
    box.pool_ships(**changes)
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    assert (
        "the Lean server's image is built here: the pool's image manifest could not be read"
        in result.stdout
    )
    assert built(box) and not loaded(box) and "/image" not in box.asked


def test_a_box_that_already_holds_the_pools_image_neither_downloads_nor_builds(box):
    manifest = box.pool_ships()
    (box.stub / "image-layers").write_text(POOL_LAYERS)  # the image is already under the tag
    result = box.run("--workers", "3")
    assert result.returncode == 0, box.output
    assert (
        f"this box already holds the pool's image {IMAGE_TAG}: nothing to download and nothing to build"
        in result.stdout
    )
    assert not loaded(box) and not built(box) and "/image" not in box.asked
    arguments = shlex.split(box.read("install-arguments"))
    assert arguments[arguments.index("--image-from-pool") + 1] == manifest["layers"]
    preflight = shlex.split(box.read("preflight-arguments"))
    assert preflight[preflight.index("--min-free-gb") + 1] == "2"


def alarmed(box: Box, result: subprocess.CompletedProcess[str], why: str) -> None:
    """An ALARM: stopped, nothing built in its place, nothing signed, nothing left behind."""
    assert result.returncode == 1 and f"step 5 ALARM: {why}" in result.stderr, box.output
    assert (
        "NOTHING was built in its place" in result.stderr
        and "The pool has signed nothing." in result.stderr
    )
    assert not built(box) and nothing_installed(box)
    assert "/csr" not in box.asked and not (box.stub / "csr-name").exists()
    assert not (box.stub / "instance" / "pool-image.tar.part").exists() and not box.work.exists()


def test_a_file_that_is_not_the_one_the_manifest_names_is_never_loaded(box):
    box.pool_ships()
    (box.stub / "pool-image").write_bytes(POOL_IMAGE[:-1] + b"X")  # the same size, another content
    result = box.run("--workers", "3")
    alarmed(box, result, "the file the pool sent does not have the SHA-256 its manifest names")
    assert not loaded(box) and not any(call.startswith("docker load") for call in box.calls)


@pytest.mark.parametrize(
    "says",
    [
        f"Loaded image: {IMAGE_TAG}\nLoaded image: haproxy:3.0",  # two tags
        "Loaded image: haproxy:3.0",  # another tag
        "Loaded image ID: sha256:" + "5e" * 32,  # no tag at all
    ],
)
def test_a_load_that_brings_in_anything_but_the_one_tag_is_an_alarm_and_what_it_brought_is_removed(
    box, says
):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_LOAD_SAYS=says)
    alarmed(box, result, f"the load brought in something other than the one image {IMAGE_TAG}")
    removed = [call.split()[-1] for call in box.calls if call.startswith("docker image rm ")]
    assert removed == [line.split(": ", 1)[1] for line in says.splitlines()]


def test_an_image_whose_layers_are_not_the_manifests_is_an_alarm_and_is_removed(box):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_LOAD_OTHER_LAYERS="1")
    alarmed(
        box,
        result,
        f"the image now under {IMAGE_TAG} does not have the layers the pool's manifest names",
    )
    assert [call for call in box.calls if call.startswith("docker image rm ")] == [
        f"docker image rm {IMAGE_TAG}"
    ]


def test_a_docker_that_cannot_load_the_pools_image_builds_instead_and_says_why(box):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_LOAD_FAILS="1")
    assert result.returncode == 0, box.output
    assert (
        "building the image here instead: this box's Docker could not load the pool's image: open blobs/sha256: "
        "unsupported image format: this Docker cannot read it (the pool's Docker is 28.0.4, this box's is 27.5.1)"
    ) in result.stdout
    assert built(box) and "JOINED: lap-top" in result.stdout
    assert "--image-from-pool" not in box.read("install-arguments")
    assert not (box.stub / "instance" / "pool-image.tar.part").exists()


def test_a_download_that_never_completes_is_given_up_after_five_attempts_and_the_box_builds(box):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_IMAGE_NEVER_ARRIVES="1")
    assert result.returncode == 0, box.output
    assert box.asked.count("/image") == 5
    assert (
        f"building the image here instead: the download from the pool did not complete in five attempts "
        f"(0 of {len(POOL_IMAGE)} bytes; its last answer: 000)"
    ) in result.stdout
    assert built(box) and not loaded(box) and "JOINED: lap-top" in result.stdout


def test_a_fallback_build_that_fails_still_costs_no_window(box):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_LOAD_FAILS="1", STUB_BUILD_FAILS="1")
    assert result.returncode == 1 and "step 5 failed" in result.stderr
    assert "The pool has signed nothing, so this join window is still good" in result.stderr
    assert "/csr" not in box.asked and not (box.stub / "instance" / "pool-image.tar.part").exists()


def test_a_docker_that_names_the_loaded_image_in_full_is_not_an_alarm(box):
    """A Docker that keeps its images in containerd may say `docker.io/NAME`: the same name."""
    box.pool_ships()
    result = box.run("--workers", "3", STUB_LOAD_SAYS=f"Loaded image: docker.io/{IMAGE_TAG}")
    assert result.returncode == 0, box.output
    assert loaded(box) and not built(box) and "JOINED: lap-top" in result.stdout


def test_a_box_without_room_for_the_file_builds_and_asks_for_no_download(box):
    box.pool_ships()
    result = box.run("--workers", "3", STUB_ROOM="1")
    assert result.returncode == 0, box.output
    assert (
        f"building the image here instead: {box.stub}/instance has 1 GB free and the pool's image file is 0.0 GB"
        in result.stdout
    )
    assert "/image" not in box.asked and built(box) and not loaded(box)
    assert "did not complete in five attempts" not in result.stdout
