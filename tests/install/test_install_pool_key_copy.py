"""The pool's own key's copy, installed on a fake machine (stand_ins.py; docs/deployment.md).

The machine is the tests' site with `demo`'s pool. It is installed again with `--pool-key-copy DIR`, DIR where a
program on the host hands the pool's key to boxes of its own (a directory under one that program's user owns). What
is shown: no copy unless asked for; asked, the key and the authority are written at every run as the screened key's
copy is, by the same writer, and `lean-pool-ctl NAME pool-key copy` writes them again; the key is never printed; and
the copy is never let into the screened key's copy's directory, nor one inside or around it, whichever names it.
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from stand_ins import (
    ADMIT_CASES,
    Machine,
    installed_instances,
    rewritten_installers,
    tools_run_from_source,
)

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
]

POOL = "/srv/lean-pool-demo"
HAND_OUT = "/srv/hand-out"
COPY = f"{HAND_OUT}/lean-pool-home"
SCREENED_COPY = f"{HAND_OUT}/lean-pool"


def failed(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout[-3000:] + result.stderr[-3000:]


def settings(machine: Machine) -> dict[str, str]:
    lines = machine.path(f"{POOL}/pool.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def control(machine: Machine, *arguments: str) -> subprocess.CompletedProcess[str]:
    return machine.bash_in(None, machine.path("/usr/local/sbin/lean-pool-ctl"), "demo", *arguments)


def install(machine: Machine, *options: str) -> subprocess.CompletedProcess[str]:
    return machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, *options)


def key_of(machine: Machine) -> str:
    return machine.path(f"{POOL}/api-key").read_text().strip()


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture(scope="module")
def plain(tmp_path_factory, deploy):
    """`demo`'s pool installed without a copy: `install.sh --proxy demo`."""
    machine = installed_instances(tmp_path_factory.mktemp("plain") / "host", deploy)
    machine.path(HAND_OUT).mkdir(parents=True)
    result = install(machine)
    assert result.returncode == 0, failed(result)
    return SimpleNamespace(machine=machine, result=result)


@pytest.fixture(scope="module")
def copied(tmp_path_factory, plain):
    """That pool, installed again with a copy of its key."""
    machine = plain.machine.copy_to(tmp_path_factory.mktemp("copied") / "host")
    result = install(machine, "--pool-key-copy", str(machine.path(COPY)))
    return SimpleNamespace(machine=machine, result=result)


@pytest.fixture
def pool(tmp_path, copied):
    """A pool with a copy of its key, of the test's own, to change."""
    assert copied.result.returncode == 0, failed(copied.result)
    machine = copied.machine.copy_to(tmp_path / "host")
    # The pool's settings name the copy's directory by its full path, which is the first machine's.
    env = machine.path(f"{POOL}/pool.env")
    env.write_text(env.read_text().replace(str(copied.machine.path(COPY)), str(machine.path(COPY))))
    return machine


@pytest.fixture
def untouched(tmp_path, plain):
    machine = plain.machine.copy_to(tmp_path / "host")
    return SimpleNamespace(machine=machine, settings=machine.path(f"{POOL}/pool.env").read_bytes())


# --- none unless asked for -------------------------------------------------------------------------------------------


def test_without_the_option_no_copy_is_named_written_or_shown(plain):
    machine = plain.machine
    assert "LEANPOOL_POOL_KEY_COPY" not in settings(machine)
    assert not machine.path(COPY).exists()
    assert "  pool key " not in plain.result.stdout
    assert "the pool's key: copied" not in control(machine, "status").stdout


# --- asked for ------------------------------------------------------------------------------------------------------


def test_the_copy_holds_the_pools_key_and_the_authority_owned_by_its_directorys_owner(copied):
    machine = copied.machine
    assert copied.result.returncode == 0, failed(copied.result)
    assert "POOL INSTALLED AND VERIFIED" in copied.result.stdout
    copy = machine.path(COPY)
    assert (copy / "api-key").read_text().strip() == key_of(machine)
    assert (copy / "ca.crt").read_bytes() == machine.path(f"{POOL}/certs/ca.crt").read_bytes()
    assert copy.stat().st_mode & 0o777 == 0o700
    assert (copy / "api-key").stat().st_mode & 0o777 == 0o600
    assert (copy / "ca.crt").stat().st_mode & 0o777 == 0o644
    owner = copy.parent.stat()
    for path in (copy, copy / "api-key", copy / "ca.crt"):
        assert (path.stat().st_uid, path.stat().st_gid) == (owner.st_uid, owner.st_gid)
    assert sorted(path.name for path in copy.iterdir()) == ["api-key", "ca.crt"]
    assert settings(machine)["LEANPOOL_POOL_KEY_COPY"] == str(copy)


def test_the_summary_and_status_say_where_the_copy_is_and_never_print_the_key(copied):
    machine = copied.machine
    copy = machine.path(COPY)
    assert (
        f"  pool key    the pool's key and the authority's certificate are copied to {copy}"
        in copied.result.stdout
    ), copied.result.stdout
    status = control(machine, "status")
    assert status.returncode == 0 and f"--- the pool's key: copied to {copy}" in status.stdout
    key = key_of(machine)
    for said in (copied.result.stdout, copied.result.stderr, status.stdout, status.stderr):
        assert key not in said


def test_a_re_run_without_the_option_keeps_its_directory_and_writes_the_copy_again(pool):
    (pool.path(COPY) / "api-key").unlink()
    result = install(pool)
    assert result.returncode == 0, failed(result)
    assert settings(pool)["LEANPOOL_POOL_KEY_COPY"] == str(pool.path(COPY))
    assert (pool.path(COPY) / "api-key").read_text().strip() == key_of(pool)


def test_the_verb_writes_the_copy_again(pool):
    (pool.path(COPY) / "api-key").write_text("stale\n")
    result = control(pool, "pool-key", "copy")
    assert result.returncode == 0, failed(result)
    assert (pool.path(COPY) / "api-key").read_text().strip() == key_of(pool)
    assert key_of(pool) not in result.stdout + result.stderr


def test_the_verb_takes_copy_alone(pool):
    for arguments in ((), ("new",), ("copy", "copy")):
        result = control(pool, "pool-key", *arguments)
        assert result.returncode == 2 and "pool-key copy" in result.stderr, arguments


def test_the_verb_on_a_pool_that_names_no_copy_is_refused(untouched):
    result = control(untouched.machine, "pool-key", "copy")
    assert result.returncode != 0 and "names no directory for its key's copy" in result.stderr
    assert not untouched.machine.path(COPY).exists()


def test_the_copy_is_never_written_through_a_link_planted_in_its_directory(pool, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("left alone\n")
    victim.chmod(0o640)
    copy = pool.path(COPY)
    (copy / "api-key").unlink()
    (copy / "api-key").symlink_to(victim)
    result = control(pool, "pool-key", "copy")
    assert result.returncode == 0, failed(result)
    assert victim.read_text() == "left alone\n" and victim.stat().st_mode & 0o777 == 0o640
    assert not (copy / "api-key").is_symlink()


def test_a_copy_that_cannot_be_written_is_a_warning_not_a_failed_install(pool, tmp_path):
    # The directory above the copy's is gone: the copy cannot be made, and the pool is installed all the same.
    pool.path(HAND_OUT).rename(tmp_path / "moved")
    result = install(pool)
    assert result.returncode == 0, failed(result)
    assert "the pool's key's copy was not written" in result.stderr
    assert f"  pool key    ITS COPY WAS NOT WRITTEN to {pool.path(COPY)}" in result.stdout


# --- never beside the screened key's copy ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pool_copy", "screened_copy"),
    [
        (SCREENED_COPY, SCREENED_COPY),
        (f"{SCREENED_COPY}/home", SCREENED_COPY),
        (HAND_OUT, SCREENED_COPY),
    ],
    ids=["the same", "inside it", "around it"],
)
def test_a_copy_beside_the_screened_keys_given_in_one_run_is_refused(
    untouched, pool_copy, screened_copy
):
    machine = untouched.machine
    result = install(
        machine,
        "--screened-door",
        "--screened-key-copy",
        str(machine.path(screened_copy)),
        "--pool-key-copy",
        str(machine.path(pool_copy)),
    )
    assert result.returncode != 0 and "would be handed out with the screened one" in result.stderr
    assert machine.path(f"{POOL}/pool.env").read_bytes() == untouched.settings
    assert not machine.path(f"{POOL}/screened-api-key").exists()


def test_a_copy_in_the_directory_the_pool_keeps_for_the_screened_key_is_refused(untouched):
    # A closed door keeps its copy's directory, which still holds the screened key's files.
    machine = untouched.machine
    env = machine.path(f"{POOL}/pool.env")
    env.write_text(env.read_text() + f"LEANPOOL_SCREENED_KEY_COPY={machine.path(SCREENED_COPY)}\n")
    kept = env.read_bytes()
    result = install(machine, "--pool-key-copy", str(machine.path(SCREENED_COPY)))
    assert result.returncode != 0 and "would be handed out with the screened one" in result.stderr
    assert env.read_bytes() == kept
    assert not machine.path(SCREENED_COPY).exists()


def test_the_verb_refuses_a_pool_whose_settings_put_the_two_copies_together(pool):
    env = pool.path(f"{POOL}/pool.env")
    env.write_text(env.read_text() + f"LEANPOOL_SCREENED_KEY_COPY={pool.path(COPY)}\n")
    (pool.path(COPY) / "api-key").write_text("before\n")
    result = control(pool, "pool-key", "copy")
    assert result.returncode != 0 and "would be handed out with the screened one" in result.stderr
    assert (pool.path(COPY) / "api-key").read_text() == "before\n"


def test_a_copy_directory_that_is_not_a_plain_absolute_path_is_refused(untouched):
    for directory in ("relative/dir", "/srv/../etc", "/srv/a dir", "/srv/.", "/srv/x/", "/srv//x"):
        result = install(untouched.machine, "--pool-key-copy", directory)
        assert result.returncode != 0 and "--pool-key-copy must be an absolute" in result.stderr, (
            directory
        )
    assert untouched.machine.path(f"{POOL}/pool.env").read_bytes() == untouched.settings
    assert not Path(untouched.machine.path(COPY)).exists()
