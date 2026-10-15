"""``deploy/lean-server/read-only-check.sh``: would a Lean server keep working read-only?

The script reads what a running server has written (``docker diff``) and its mounts (``docker
inspect``); here a stand-in Docker says both. What a real server writes while it serves checks, and
how the installer acts on the answer, are the installer tests' (tests/install/).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parent.parent / "deploy" / "lean-server" / "read-only-check.sh"

DOCKER = r'''#!/usr/bin/env python3
"""The docker CLI as read-only-check.sh uses it (diff, inspect). FAKE_DIFF is what `diff` prints,
FAKE_MOUNTS the mount points `inspect` lists; FAKE_DIFF_FAILS and FAKE_INSPECT_FAILS make them
fail. Every call is kept in FAKE_CALLS."""
import json, os, sys

with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1] == "diff":
    if os.environ.get("FAKE_DIFF_FAILS"):
        print("Error response from daemon: " + os.environ["FAKE_DIFF_FAILS"], file=sys.stderr)
        sys.exit(1)
    print(os.environ["FAKE_DIFF"])
elif sys.argv[1] == "inspect":
    if os.environ.get("FAKE_INSPECT_FAILS"):
        sys.exit("Error: No such object: " + sys.argv[-1])
    print("".join(mount + " " for mount in os.environ["FAKE_MOUNTS"].split()))
else:
    sys.exit("stand-in docker: unexpected " + " ".join(sys.argv[1:]))
'''

# What a Lean server that has served checks with a writable root shows: files in /tmp, Python's
# byte-code cache, git's index of a Lake package, Docker's init, and its own mount points; every
# directory above a change is listed as changed too.
SERVED = """C /root
C /root/kimina-lean-server
C /root/kimina-lean-server/server
A /root/kimina-lean-server/server/__pycache__
A /root/kimina-lean-server/server/__pycache__/repl.cpython-313.pyc
C /tmp
A /tmp/tmpq8x1v2c4
C /lean-project
C /lean-project/.lake
C /lean-project/.lake/packages
C /lean-project/.lake/packages/mathlib
C /lean-project/.lake/packages/mathlib/.git
C /lean-project/.lake/packages/mathlib/.git/index
C /usr
C /usr/sbin
A /usr/sbin/docker-init
C /etc
A /etc/hosts
A /etc/hostname
A /etc/resolv.conf
C /opt
A /opt/lean-native
A /opt/lean-native/linarith-search.so"""
MOUNTS = "/opt/lean-native"


def checked(
    tmp_path: Path, *arguments: str, diff: str = SERVED, **more: str
) -> subprocess.CompletedProcess[str]:
    """Run the script against the stand-in Docker."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    docker = bin_directory / "docker"
    docker.write_text(DOCKER)
    docker.chmod(0o755)
    environment = {
        "PATH": f"{bin_directory}:{os.environ['PATH']}",
        "FAKE_CALLS": str(tmp_path / "calls.jsonl"),
        "FAKE_DIFF": diff,
        "FAKE_MOUNTS": MOUNTS,
        **more,
    }
    return subprocess.run(
        ["bash", str(CHECK), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def calls(tmp_path: Path) -> list[list[str]]:
    """Every Docker command the script ran."""
    path = tmp_path / "calls.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line) for line in lines]


def test_a_server_that_wrote_only_where_it_may_keeps_working_read_only(tmp_path: Path) -> None:
    result = checked(tmp_path, "lean-server-demo-kimina-1")

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert calls(tmp_path) == [
        ["diff", "lean-server-demo-kimina-1"],
        ["inspect", "-f", "{{range .Mounts}}{{.Destination}} {{end}}", "lean-server-demo-kimina-1"],
    ]


def test_every_path_a_read_only_root_would_refuse_is_named_once_in_order(tmp_path: Path) -> None:
    elsewhere = (
        "C /lean-project/.lake/lakefile.olean.lock\nD /usr/lib/python3/old.py\n"
        "A /opt/lean-native-other\nA /var/cache/one\nA /var/cache/one/two"
    )

    result = checked(tmp_path, "box", diff=f"{SERVED}\n{elsewhere}")

    assert result.returncode == 1
    # A directory listed only because something under it changed is not named: the change is. A
    # path that merely starts like a mount point is not under it.
    assert result.stdout.splitlines() == [
        "/lean-project/.lake/lakefile.olean.lock",
        "/opt/lean-native-other",
        "/usr/lib/python3/old.py",
        "/var/cache/one/two",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/a/b",
        "/opt/lean-native/x",
        "/srv/data/file",
        "/a/__pycache__/b.cpython-313.pyc",
        "/a/b.pyc",
        "/p/.lake/packages/batteries/.git/index",
    ],
)
def test_what_is_not_counted(tmp_path: Path, path: str) -> None:
    result = checked(
        tmp_path, "box", diff=f"{SERVED}\nA {path}", FAKE_MOUNTS="/opt/lean-native /srv/data"
    )

    assert (result.returncode, result.stdout) == (0, "")


def test_git_other_files_of_a_lake_package_are_counted(tmp_path: Path) -> None:
    result = checked(
        tmp_path, "box", diff=f"{SERVED}\nA /lean-project/.lake/packages/mathlib/.git/ORIG_HEAD"
    )

    assert result.stdout.splitlines() == ["/lean-project/.lake/packages/mathlib/.git/ORIG_HEAD"]


def test_the_diff_is_kept_where_it_is_asked_for(tmp_path: Path) -> None:
    kept = tmp_path / "read-only.diff"

    assert checked(tmp_path, "--diff", str(kept), "box").returncode == 0

    assert kept.read_text() == SERVED + "\n"


def test_a_container_docker_cannot_read_is_no_answer(tmp_path: Path) -> None:
    kept = tmp_path / "read-only.diff"

    result = checked(tmp_path, "--diff", str(kept), "box", FAKE_DIFF_FAILS="the layer is gone")

    assert result.returncode == 2
    assert result.stdout == ""
    assert (
        result.stderr.splitlines()[-1]
        == "docker diff: Error response from daemon: the layer is gone"
    )
    assert kept.read_text() == "Error response from daemon: the layer is gone\n"


def test_mounts_docker_cannot_read_are_no_answer(tmp_path: Path) -> None:
    result = checked(tmp_path, "box", FAKE_INSPECT_FAILS="1")

    assert result.returncode == 2
    assert result.stderr.splitlines()[-1] == "docker inspect: could not read the mounts of box"


@pytest.mark.parametrize(
    "arguments", [(), ("",), ("--diff",), ("--diff", "", "box"), ("--force", "box"), ("a", "b")]
)
def test_it_refuses_what_it_does_not_understand(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    result = checked(tmp_path, *arguments)

    assert result.returncode == 2
    assert result.stderr == "usage: read-only-check.sh [--diff FILE] CONTAINER\n"
    assert calls(tmp_path) == []


def test_it_is_a_program() -> None:
    assert os.access(CHECK, os.X_OK)


def test_only_lines_of_docker_diff_are_read_and_a_path_is_under_a_mount_only_below_it(
    tmp_path: Path,
) -> None:
    diff = "\n".join(
        [
            "C /root",
            "C /root/kimina-lean-server",
            "A /root/kimina-lean-server/server/__pycache__",
            "A /root/kimina-lean-server/server/__pycache__/x.cpython-313.pyc",
            "A /root/stray.pyc",
            "C /tmp",
            "A /tmp/a",
            "A /tmpfile",
            "C /opt",
            "A /opt/lean-native",
            "A /opt/lean-native-other",
            "D /etc/motd",
            "C /var",
            "C /var/log",
            "A /var/log/x.log",
            "not a line of docker diff",
            # Only the index of a Lake package itself; nothing else of git, and no other index.
            "C /p/.lake/packages/mathlib/.git",
            "C /p/.lake/packages/mathlib/.git/index",
            "C /p/.lake/packages/mathlib/.git/config",
            "C /repo/.git/index",
            "C /p/.lake/packages/a/b/.git/index",
        ]
    )

    result = checked(tmp_path, "box", diff=diff)

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "/etc/motd",
        "/opt/lean-native-other",
        "/p/.lake/packages/a/b/.git/index",
        "/p/.lake/packages/mathlib/.git/config",
        "/repo/.git/index",
        "/tmpfile",
        "/var/log/x.log",
    ]
