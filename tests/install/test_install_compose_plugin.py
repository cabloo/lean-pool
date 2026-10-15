"""A missing docker compose plugin is installed, not reported.

`ensure_compose_plugin` (pool-lib.sh) is the one implementation the join script, install.sh and install-pool.sh
call. It installs a system package or a binary as root, so it cannot run for real here. The REAL function runs
instead, with its host path moved under a fake root exactly as `stand_ins.py` moves the installers',
on a PATH that holds NOTHING but stand-ins and a fixed list of real tools:

  docker           `compose version` works once a package's stand-in left its mark, or the plugin's binary is in
                   place under the fake root (and is not said to be broken)
  apt-get, dnf, yum
                   present only where a test puts them; a package is "there" only where a test says so
                   (STUB_APT_HAS, STUB_APT_HAS_AFTER_UPDATE, STUB_DNF_HAS); each records its arguments and how
                   many bytes it could read from standard input
  curl             records its arguments and writes the test's "release binary" where `-o` says, or fails
  uname, install   a machine type; `install -o root -g root` without needing root (the owner is recorded)

So no test can reach the network or a real package manager: neither is on the PATH. What the stand-ins replace
(real apt, real dnf, the real release download) is covered by an install on a real host. The download itself was
checked by hand when the version was pinned: both binaries' SHA-256 were computed and equal the release's own
checksums.txt.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from stand_ins import (
    HOST_PREFIXES,
    INSTALLERS,
)

PLUGIN = "usr/local/lib/docker/cli-plugins/docker-compose"
# What is left on standard input when the function runs inside `curl ... | sudo bash`: the rest of the script.
REST_OF_THE_SCRIPT = "main --name box-a\n"
RELEASE_BINARY = b"#!/bin/sh\necho 'Docker Compose version (a stand-in)'\n"
# The real tools the function and the stand-ins may use. Anything else is not on the PATH at all.
REAL_TOOLS = ("bash", "cat", "wc", "tail", "tr", "cut", "mktemp", "rm", "rmdir", "sha256sum", "cp")

DOCKER = r"""#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "$STUB/calls"
[ "$*" = "compose version" ] || exit 0
[ -n "${STUB_COMPOSE_PRESENT:-}" ] && exit 0
[ -e "$STUB/package-installed" ] && exit 0
[ -x "$FAKE_ROOT/usr/local/lib/docker/cli-plugins/docker-compose" ] && [ -z "${STUB_BINARY_BROKEN:-}" ] && exit 0
exit 1
"""

APT = r"""#!/usr/bin/env bash
printf 'apt-get %s | frontend=%s | stdin=%s\n' "$*" "${DEBIAN_FRONTEND:-}" "$(cat | wc -c)" >> "$STUB/calls"
has=${STUB_APT_HAS:-}
[ ! -e "$STUB/apt-updated" ] || has="$has ${STUB_APT_HAS_AFTER_UPDATE:-}"
case " $* " in
  *" update "*)
    if [ -n "${STUB_APT_UPDATE_FAILS:-}" ]; then
      echo "E: Failed to fetch http://archive.example/dists/InRelease  Temporary failure resolving 'archive.example'" >&2
      exit 100
    fi
    : > "$STUB/apt-updated"; exit 0 ;;
esac
package=${*: -1}
case " $has " in
  *" $package "*) [ -n "${STUB_PACKAGE_BROKEN:-}" ] || : > "$STUB/package-installed"; echo "Setting up $package ..."; exit 0 ;;
esac
echo "Reading package lists..."
echo "E: Unable to locate package $package" >&2
exit 100
"""

DNF = r"""#!/usr/bin/env bash
printf '%s %s | stdin=%s\n' "${0##*/}" "$*" "$(cat | wc -c)" >> "$STUB/calls"
package=${*: -1}
case " ${STUB_DNF_HAS:-} " in
  *" $package "*) [ -n "${STUB_PACKAGE_BROKEN:-}" ] || : > "$STUB/package-installed"; exit 0 ;;
esac
echo "Error: Unable to find a match: $package" >&2
exit 1
"""

CURL = r"""#!/usr/bin/env bash
printf 'curl %s | stdin=%s\n' "$*" "$(cat | wc -c)" >> "$STUB/calls"
out=""; arguments=("$@")
for ((i = 0; i < ${#arguments[@]}; i++)); do [ "${arguments[i]}" != -o ] || out=${arguments[i + 1]}; done
if [ -n "${STUB_DOWNLOAD_FAILS:-}" ]; then
  printf 'half a bin' > "$out"                       # what a download that broke off leaves
  echo "curl: (6) Could not resolve host: github.com" >&2
  exit 6
fi
cp "$STUB/release-binary" "$out"
"""

INSTALL = r"""#!/usr/bin/env bash
printf 'install %s\n' "$*" >> "$STUB/calls"
kept=()
while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) kept+=("$1"); shift ;; esac; done
exec __REAL_install__ "${kept[@]}"
"""

HARNESS = r"""set -euo pipefail
SRC=__DEPLOY__
say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { echo "die: $*" >&2; exit 1; }
. "$SRC/pool-lib.sh"
# A test's own "release binary" cannot have the pinned checksum: where a test names one, that is the pin.
LEAN_POOL_COMPOSE_SHA256_X86_64=${STUB_PINNED:-$LEAN_POOL_COMPOSE_SHA256_X86_64}
LEAN_POOL_COMPOSE_SHA256_AARCH64=${STUB_PINNED:-$LEAN_POOL_COMPOSE_SHA256_AARCH64}
code=0
ensure_compose_plugin || code=$?
printf 'code=%s\ninstalled=%s\nremoval=%s\nfailure=%s\n' "$code" "$COMPOSE_PLUGIN_INSTALLED" "$COMPOSE_PLUGIN_REMOVAL" "$COMPOSE_PLUGIN_FAILURE"
IFS= read -r rest || true
printf 'rest=%s\n' "$rest"
"""


class Host:
    """One box with Docker and, perhaps, no compose plugin."""

    def __init__(self, directory: Path) -> None:
        self.root, self.stub, self.temporary = (
            directory / "root",
            directory / "stub",
            directory / "tmp",
        )
        (self.root / "usr/local/lib").mkdir(parents=True)
        for path in (self.stub / "bin", self.stub / "tools", self.temporary, directory / "deploy"):
            path.mkdir(parents=True)
        for name in REAL_TOOLS:
            real = shutil.which(name)
            assert real, f"{name} is needed to run these tests"
            (self.stub / "tools" / name).symlink_to(real)
        library = (INSTALLERS / "pool-lib.sh").read_bytes()
        for (
            prefix
        ) in HOST_PREFIXES:  # the plugin's path moves under the fake root; nothing else changes
            library = library.replace(prefix.encode(), b"${FAKE_ROOT}" + prefix.encode())
        (directory / "deploy" / "pool-lib.sh").write_bytes(library)
        self.harness = directory / "harness.sh"
        self.harness.write_text(HARNESS.replace("__DEPLOY__", str(directory / "deploy")))
        self.put("docker", DOCKER)
        self.put("curl", CURL)
        self.put("uname", '#!/usr/bin/env bash\necho "${STUB_MACHINE:-x86_64}"\n')
        self.put(
            "install", INSTALL.replace("__REAL_install__", shutil.which("install") or "install")
        )
        (self.stub / "release-binary").write_bytes(RELEASE_BINARY)
        (self.stub / "calls").write_text("")

    def put(self, name: str, content: str) -> None:
        path = self.stub / "bin" / name
        path.write_text(content)
        path.chmod(0o755)

    def run(self, *managers: str, **environment: str) -> subprocess.CompletedProcess[str]:
        """Run ensure_compose_plugin on a box that has these package managers."""
        for manager in managers:
            self.put(manager, APT if manager == "apt-get" else DNF)
        self.result = subprocess.run(
            ["bash", str(self.harness)],
            input=REST_OF_THE_SCRIPT,
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "PATH": f"{self.stub}/bin:{self.stub}/tools",
                "STUB": str(self.stub),
                "FAKE_ROOT": str(self.root),
                "TMPDIR": str(self.temporary),
                **environment,
            },
        )
        self.said = dict(
            line.split("=", 1)
            for line in self.result.stdout.splitlines()
            if re.match(r"^(code|installed|removal|failure|rest)=", line)
        )
        return self.result

    @property
    def calls(self) -> list[str]:
        return (self.stub / "calls").read_text().splitlines()

    def called(self, tool: str) -> list[str]:
        """What one tool was called with, in order (without the docker stand-in's own lines)."""
        return [call.split(" | ")[0] for call in self.calls if call.startswith(tool + " ")]

    @property
    def plugin(self) -> Path:
        return self.root / PLUGIN

    def nothing_was_left(self) -> bool:
        """No plugin, no directory made for one, no temporary file."""
        return (
            list((self.root / "usr/local/lib").iterdir()) == []
            and list(self.temporary.iterdir()) == []
        )

    def standard_input_was_never_read(self) -> bool:
        tools = [call for call in self.calls if " | stdin=" in call]
        return (
            all(call.endswith("stdin=0") for call in tools)
            and self.said["rest"] == REST_OF_THE_SCRIPT.strip()
        )


def pool_lib_value(name: str, pattern: str = r"\S+") -> str:
    """A value pool-lib.sh sets on a line of its own (`NAME=VALUE`), held to PATTERN."""
    found = re.search(rf"^{name}=({pattern})$", (INSTALLERS / "pool-lib.sh").read_text(), re.M)
    assert found, f"pool-lib.sh sets no {name} of the form {pattern}"
    return found.group(1)


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def pinned() -> str:
    return hashlib.sha256(RELEASE_BINARY).hexdigest()


APT_INSTALL = "apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "
APT_UPDATE = "apt-get -o DPkg::Lock::Timeout=120 update"


# --- nothing to do ---------------------------------------------------------------------------------------------------


def test_a_box_that_has_the_plugin_gets_nothing_installed_and_no_package_manager_is_called(host):
    result = host.run("apt-get", "dnf", STUB_COMPOSE_PRESENT="1")
    assert result.returncode == 0 and host.said["code"] == "0" and host.said["installed"] == ""
    assert host.calls == ["docker compose version"]
    assert "adding it" not in result.stdout and host.nothing_was_left()


# --- the package manager ---------------------------------------------------------------------------------------------


def test_apt_with_dockers_own_repository_installs_docker_compose_plugin(host):
    result = host.run("apt-get", STUB_APT_HAS="docker-compose-plugin docker-compose-v2")
    assert host.said["code"] == "0", result.stdout + result.stderr
    assert host.said["installed"] == "the apt package docker-compose-plugin"
    assert host.said["removal"] == "sudo apt-get remove docker-compose-plugin"
    assert host.called("apt-get") == [
        APT_INSTALL + "docker-compose-plugin"
    ]  # one call: no refresh, no second name
    assert (
        "installed the docker compose plugin: the apt package docker-compose-plugin. To remove it: sudo apt-get remove docker-compose-plugin"
        in result.stdout
    )
    assert all(
        "frontend=noninteractive" in call for call in host.calls if call.startswith("apt-get")
    )
    assert (
        host.called("curl") == []
        and not host.plugin.exists()
        and host.standard_input_was_never_read()
    )


def test_apt_beside_ubuntus_docker_io_installs_docker_compose_v2_without_refreshing_the_lists(host):
    result = host.run("apt-get", STUB_APT_HAS="docker-compose-v2")
    assert host.said["code"] == "0", result.stdout + result.stderr
    assert (
        host.said["installed"] == "the apt package docker-compose-v2"
        and host.said["removal"] == "sudo apt-get remove docker-compose-v2"
    )
    assert host.called("apt-get") == [
        APT_INSTALL + "docker-compose-plugin",
        APT_INSTALL + "docker-compose-v2",
    ]
    assert host.called("curl") == [] and host.standard_input_was_never_read()


def test_the_package_lists_are_refreshed_only_when_neither_package_installs_and_both_are_tried_again(
    host,
):
    result = host.run("apt-get", STUB_APT_HAS_AFTER_UPDATE="docker-compose-v2")
    assert host.said["code"] == "0", result.stdout + result.stderr
    assert host.said["installed"] == "the apt package docker-compose-v2"
    assert host.called("apt-get") == [
        APT_INSTALL + "docker-compose-plugin",
        APT_INSTALL + "docker-compose-v2",
        APT_UPDATE,
        APT_INSTALL + "docker-compose-plugin",
        APT_INSTALL + "docker-compose-v2",
    ]
    assert host.called("curl") == [] and host.standard_input_was_never_read()


@pytest.mark.parametrize("manager", ["dnf", "yum"])
def test_dnf_or_yum_installs_docker_compose_plugin(host, manager):
    result = host.run(manager, STUB_DNF_HAS="docker-compose-plugin")
    assert host.said["code"] == "0", result.stdout + result.stderr
    assert host.said["installed"] == f"the {manager} package docker-compose-plugin"
    assert host.said["removal"] == f"sudo {manager} remove docker-compose-plugin"
    assert host.called(manager) == [f"{manager} install -y docker-compose-plugin"]
    assert host.called("curl") == [] and host.standard_input_was_never_read()


def test_dnf_and_yum_are_one_package_manager_and_only_dnf_is_asked(host):
    host.run("dnf", "yum", STUB_DOWNLOAD_FAILS="1")
    assert (
        host.called("dnf") == ["dnf install -y docker-compose-plugin"] and host.called("yum") == []
    )


# --- the pinned release binary ---------------------------------------------------------------------------------------


def test_without_a_package_the_pinned_release_binary_is_checked_and_put_in_place(host):
    result = host.run("apt-get", STUB_PINNED=pinned())
    assert host.said["code"] == "0", result.stdout + result.stderr
    version = pool_lib_value("LEAN_POOL_COMPOSE_VERSION")
    assert (
        host.said["installed"]
        == f"the release binary {version} from github.com/docker/compose, SHA-256 checked, at {host.plugin}"
    )
    assert host.said["removal"] == f"sudo rm {host.plugin}"
    assert (
        host.plugin.read_bytes() == RELEASE_BINARY and host.plugin.stat().st_mode & 0o777 == 0o755
    )
    # Asked for as root's, with the directories Docker reads plugins from; the package manager was tried first.
    assert f"install -o root -g root -m 0755 {host.temporary}/" in "\n".join(host.calls)
    assert (
        host.called("install")[0]
        == f"install -d -o root -g root -m 0755 {host.plugin.parent.parent} {host.plugin.parent}"
    )
    assert host.called("apt-get")[
        -1
    ] == APT_INSTALL + "docker-compose-v2" and APT_UPDATE in host.called("apt-get")
    download = host.called("curl")
    assert len(download) == 1 and download[0].endswith(
        f"https://github.com/docker/compose/releases/download/{version}/docker-compose-linux-x86_64"
    )
    assert (
        "--proto =https --proto-redir =https" in download[0] and " -f" in download[0]
    )  # HTTPS only, and a 404 is a failure
    assert list(host.temporary.iterdir()) == [] and host.standard_input_was_never_read()


def test_an_arm_box_gets_the_arm_binary(host):
    result = host.run(STUB_PINNED=pinned(), STUB_MACHINE="aarch64")
    assert host.said["code"] == "0", result.stdout + result.stderr
    assert host.called("curl")[0].endswith("/docker-compose-linux-aarch64") and host.plugin.exists()


def test_a_download_with_another_checksum_is_refused_and_nothing_is_left_behind(host):
    """The test's "release binary" does not have the checksum pinned in pool-lib.sh: exactly a tampered download."""
    result = host.run("apt-get")
    assert result.returncode == 0 and host.said["code"] == "1" and host.said["installed"] == ""
    real_pin = pool_lib_value("LEAN_POOL_COMPOSE_SHA256_X86_64", "[0-9a-f]{64}")
    assert (
        "does not have the pinned SHA-256" in host.said["failure"]
        and "it was refused and deleted" in host.said["failure"]
    )
    assert f"it has {pinned()}, pinned is {real_pin}" in host.said["failure"]
    assert host.nothing_was_left() and not any(call.startswith("install ") for call in host.calls)


def test_a_file_already_at_the_plugins_path_is_never_replaced(host):
    host.plugin.parent.mkdir(parents=True)
    host.plugin.write_text(
        "something the owner put here\n"
    )  # not executable: `docker compose` fails
    host.run(STUB_PINNED=pinned())
    assert host.said["code"] == "1" and host.plugin.read_text() == "something the owner put here\n"
    assert (
        f"a file is already at {host.plugin}" in host.said["failure"]
        and "it was not replaced" in host.said["failure"]
    )
    assert host.called("curl") == []


def test_a_binary_that_does_not_work_is_removed_again_with_the_directories_made_for_it(host):
    host.run(STUB_PINNED=pinned(), STUB_BINARY_BROKEN="1")
    assert (
        host.said["code"] == "1"
        and "'docker compose version' still fails: it was removed again" in host.said["failure"]
    )
    assert host.nothing_was_left()


def test_a_machine_type_without_a_pinned_binary_is_said_and_nothing_is_fetched(host):
    host.run(STUB_MACHINE="riscv64")
    assert (
        host.said["code"] == "1"
        and "there is no pinned release binary for this machine type (riscv64)"
        in host.said["failure"]
    )
    assert host.called("curl") == [] and host.nothing_was_left()


# --- every route fails -----------------------------------------------------------------------------------------------


def test_when_every_route_fails_each_reason_is_listed(host):
    result = host.run("apt-get", STUB_APT_UPDATE_FAILS="1", STUB_DOWNLOAD_FAILS="1")
    assert (
        result.returncode == 0
        and host.said["code"] == "1"
        and host.said["installed"] == ""
        and host.said["removal"] == ""
    )
    failure = host.said["failure"]
    assert (
        "apt-get install docker-compose-plugin: E: Unable to locate package docker-compose-plugin"
        in failure
    )
    assert (
        "apt-get install docker-compose-v2: E: Unable to locate package docker-compose-v2"
        in failure
    )
    assert "apt-get update: E: Failed to fetch http://archive.example/dists/InRelease" in failure
    assert "the download of https://github.com/docker/compose/releases/download/" in failure
    assert "failed: curl: (6) Could not resolve host: github.com" in failure
    assert (
        failure.count("Unable to locate package") == 2
    )  # a refresh that failed is not followed by a second try
    assert host.called("apt-get") == [
        APT_INSTALL + "docker-compose-plugin",
        APT_INSTALL + "docker-compose-v2",
        APT_UPDATE,
    ]
    assert (
        host.nothing_was_left() and host.standard_input_was_never_read()
    )  # the broken-off download is gone too


def test_a_box_with_no_known_package_manager_says_so(host):
    host.run(STUB_DOWNLOAD_FAILS="1")
    assert host.said["code"] == "1"
    assert (
        "this box has no package manager this installer knows (apt-get, dnf, yum)"
        in host.said["failure"]
    )


def test_a_package_that_installs_without_giving_a_working_plugin_is_named_as_still_installed(host):
    host.run(
        "apt-get",
        STUB_APT_HAS="docker-compose-plugin docker-compose-v2",
        STUB_PACKAGE_BROKEN="1",
        STUB_DOWNLOAD_FAILS="1",
    )
    assert host.said["code"] == "1"
    assert (
        "apt installed docker-compose-plugin, but 'docker compose version' still fails (the package stays installed)"
        in host.said["failure"]
    )
    assert "apt installed docker-compose-v2, but" in host.said["failure"]


# --- what is pinned --------------------------------------------------------------------------------------------------


def test_the_pin_is_one_version_and_two_different_checksums_and_the_documents_name_that_version():
    library = (INSTALLERS / "pool-lib.sh").read_text()
    version = pool_lib_value("LEAN_POOL_COMPOSE_VERSION", r"v\d+\.\d+\.\d+")
    intel = pool_lib_value("LEAN_POOL_COMPOSE_SHA256_X86_64", "[0-9a-f]{64}")
    arm = pool_lib_value("LEAN_POOL_COMPOSE_SHA256_AARCH64", "[0-9a-f]{64}")
    assert intel != arm
    assert re.search(
        r"^LEAN_POOL_COMPOSE_PLUGIN=/usr/local/lib/docker/cli-plugins/docker-compose$",
        library,
        re.M,
    )
    assert version in (INSTALLERS / "README.md").read_text()


def test_the_three_installers_share_the_one_implementation():
    """Text check: none of them decides about the plugin itself any more."""
    # install.sh keeps the old refusal for one case only: a copy of it without pool-lib.sh beside it.
    without_the_library = 'declare -F ensure_compose_plugin >/dev/null || die "the docker compose plugin is not installed"'
    for name in ("join-box.sh", "install.sh", "install-pool.sh"):
        text = (INSTALLERS / name).read_text()
        assert re.search(r"^\s*ensure_compose_plugin( \\)?$", text, re.M), (
            name
        )  # it is CALLED, not just named
        assert "compose plugin is not installed." not in text.replace(without_the_library, ""), name
        assert 'die "the docker compose plugin is not installed"' not in text.replace(
            without_the_library, ""
        ), name
    assert (INSTALLERS / "pool-lib.sh").read_text().count("ensure_compose_plugin() {") == 1
    # The join's first step does not look at the plugin: pool-lib.sh is not on the box yet.
    join = (INSTALLERS / "join-box.sh").read_text()
    # (The step is `2/10` in a join and `2/3` in a refresh: the script writes `2/$of`.)
    assert "compose version" not in join[: join.index('say "2/$of Unpacking the installer"')]
