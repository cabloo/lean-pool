"""The compiled search step for linarith (docs/compiled-search.md), without Docker or Lean.

``deploy/lean-server/compiled-search/`` holds five programs:

* ``repl-native``, the launcher each worker is started through;
* ``build.sh``, which makes the library inside the Kimina image;
* ``check.py``, which says whether the library is loaded and changes nothing but the time;
* ``inside.sh``, which runs the three in the container;
* ``make.sh``, which runs that container on the box and installs what passed.

Here the launcher starts a stand-in REPL, the check judges a stand-in Lean, ``make.sh`` drives a
stand-in Docker, and the build is held to its refusals, which come before any Lean is run. The
real build and check run against the box's own Lean each time ``make.sh`` runs; what they measured
where this was written is in docs/compiled-search.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT = Path(__file__).parent.parent
LEAN_SERVER = PROJECT / "deploy" / "lean-server"
HERE = LEAN_SERVER / "compiled-search"
LAUNCHER = HERE / "repl-native"
BUILD = HERE / "build.sh"
CHECK = HERE / "check.py"
INSIDE = HERE / "inside.sh"
MAKE = HERE / "make.sh"
MODULES = HERE / "modules"
OK = (
    "COMPILED SEARCH STEP: ok - linarith's search took 18.0 s as the image runs it and 2.0 s "
    "compiled (9.0 times less); the same answers; a program a tactic starts does not inherit "
    "the library"
)
ON = OK.replace("COMPILED SEARCH STEP: ok - ", "COMPILED SEARCH STEP: on - ")


def listed_modules() -> list[tuple[str, str]]:
    """The (package, module path) pairs of the list, read as the build reads it."""
    lines = [
        line.split()
        for line in MODULES.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert all(len(line) == 2 for line in lines)
    return [(package, module) for package, module in lines]


def built_from(sources: list[Path]) -> str:
    """The hash the build writes and the launcher takes again, over ``sha256sum``'s lines."""
    lines = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n" for path in sources
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def native_directory(directory: Path, repl: Path) -> Path:
    """A directory as make.sh leaves it, around a library that is no library (none is loaded)."""
    directory.mkdir(parents=True)
    sources = [directory / "source" / name for name in ("Gauss.c", "Datatypes.c", "lean-toolchain")]
    for path in sources:
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"what {path.name} holds\n")
    (directory / "linarith-search.so").write_text("the compiled search step\n")
    (directory / "sources").write_text("".join(f"{path}\n" for path in sources))
    (directory / "built-from").write_text(built_from(sources) + "\n")
    (directory / "repl-path").write_text(f"{repl}\n")
    shutil.copy(LAUNCHER, directory / "repl-native")
    return directory


def executable(path: Path, text: str) -> Path:
    """Write a program the tests start."""
    path.write_text(text)
    path.chmod(0o755)
    return path


def environment(**more: str) -> dict[str, str]:
    """An environment with nothing of the test runner's in it but PATH."""
    return {"PATH": os.environ["PATH"], **more}


# --- the launcher -------------------------------------------------------------------------------


@dataclass
class Launcher:
    """The launcher where make.sh puts it, over a REPL that says how it was started."""

    native: Path
    repl: Path

    def start(self, *arguments: str, **more: str) -> str:
        """Start the launcher; return what the REPL said it was given."""
        result = subprocess.run(
            ["sh", str(self.native / "repl-native"), *arguments],
            capture_output=True,
            text=True,
            env=environment(**more),
            check=False,
        )
        assert result.returncode == 7, result.stderr  # the REPL's own status, whichever way
        return result.stdout.strip()


@pytest.fixture
def launcher(tmp_path: Path) -> Launcher:
    """A native directory as make.sh leaves it, around a stand-in REPL."""
    repl = executable(
        tmp_path / "repl",
        '#!/bin/sh\necho "preload=[${LD_PRELOAD:-}] arguments=[$*] as=[$0]"\nexit 7\n',
    )
    return Launcher(native_directory(tmp_path / "native", repl), repl)


def test_the_launcher_loads_the_library_made_from_the_files_that_are_there(
    launcher: Launcher,
) -> None:
    library = launcher.native / "linarith-search.so"

    assert launcher.start() == f"preload=[{library}] arguments=[] as=[{launcher.repl}]"
    assert launcher.start("--one", "two") == (
        f"preload=[{library}] arguments=[--one two] as=[{launcher.repl}]"
    )


def test_the_launcher_starts_the_repl_it_is_told_over_the_one_recorded(
    launcher: Launcher, tmp_path: Path
) -> None:
    other = executable(tmp_path / "other", '#!/bin/sh\necho "the other one"\nexit 7\n')

    assert launcher.start(LEAN_NATIVE_REPL=str(other)) == "the other one"


@pytest.mark.parametrize(
    "what",
    [
        "a source changed",
        "a source gone",
        "another hash",
        "an empty hash",
        "no hash",
        "no list",
        "no library",
    ],
)
def test_the_launcher_starts_the_plain_repl_unless_the_library_is_the_one_for_these_files(
    launcher: Launcher, what: str
) -> None:
    native = launcher.native
    if what == "a source changed":  # the image was updated under a library made before
        (native / "source" / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
    elif what == "a source gone":
        (native / "source" / "Gauss.c").unlink()
    elif what == "another hash":
        (native / "built-from").write_text("0" * 64 + "\n")
    elif what == "an empty hash":
        (native / "built-from").write_text("")
    elif what == "no hash":  # how make.sh leaves it while it writes
        (native / "built-from").unlink()
    elif what == "no list":
        (native / "sources").unlink()
    else:
        (native / "linarith-search.so").unlink()

    assert launcher.start("--one") == f"preload=[] arguments=[--one] as=[{launcher.repl}]"


def test_the_launcher_without_a_repl_to_start_says_so(launcher: Launcher) -> None:
    (launcher.native / "repl-path").unlink()

    result = subprocess.run(
        ["sh", str(launcher.native / "repl-native")],
        capture_output=True,
        text=True,
        env=environment(),
        check=False,
    )

    assert result.returncode == 127
    assert "no REPL to start" in result.stderr
    assert "run compiled-search/make.sh" in result.stderr


# --- the build's refusals (all before anything of Lean is run) ---------------------------------


@dataclass
class Build:
    """A copy of the build script beside a list the test may change."""

    here: Path
    out: Path
    project: Path

    def run(self, *arguments: str, **more: str) -> subprocess.CompletedProcess[str]:
        """Run the copy with an environment of the test's."""
        return subprocess.run(
            ["bash", str(self.here / BUILD.name), *arguments],
            capture_output=True,
            text=True,
            env=environment(**more),
            check=False,
        )


@pytest.fixture
def build(tmp_path: Path) -> Build:
    """The build script and its list, an empty place to write, and an empty Lean project."""
    built = Build(tmp_path / "src", tmp_path / "out", tmp_path / "project")
    for directory in (built.here, built.out, built.project):
        directory.mkdir()
    shutil.copy(BUILD, built.here / BUILD.name)
    shutil.copy(MODULES, built.here / MODULES.name)
    return built


def test_the_build_names_seven_modules_of_the_search_step() -> None:
    lines = listed_modules()

    assert len(lines) == 7
    assert {package for package, _ in lines} == {"mathlib", "batteries"}
    simplex = "Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm"
    assert sum(module.startswith(simplex) for _, module in lines) == 5


def test_the_build_refuses_without_a_project_or_a_place_to_write(build: Build) -> None:
    result = build.run()
    assert result.returncode != 0
    assert "usage: build.sh OUT_DIRECTORY" in result.stderr

    result = build.run(str(build.out))
    assert result.returncode == 2
    assert "LEAN_SERVER_PROJECT_DIR is not set" in result.stderr

    result = build.run(str(build.out / "absent"), LEAN_SERVER_PROJECT_DIR=str(build.project))
    assert result.returncode == 2
    assert f"no directory {build.out}/absent" in result.stderr
    assert list(build.out.iterdir()) == []


def test_the_build_writes_nothing_for_an_image_without_the_c_files(build: Build) -> None:
    result = build.run(str(build.out), LEAN_SERVER_PROJECT_DIR=str(build.project))

    assert result.returncode == 3
    assert (
        "this image holds no C file for "
        "Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm/Datatypes of mathlib"
    ) in result.stderr
    assert result.stderr.strip().endswith(": nothing was built")
    assert list(build.out.iterdir()) == []


def test_the_build_writes_nothing_without_the_toolchain_it_hashes(build: Build) -> None:
    for package, module in listed_modules():
        ir = build.project / ".lake" / "packages" / package / ".lake" / "build" / "ir"
        (ir / f"{module}.c").parent.mkdir(parents=True, exist_ok=True)
        (ir / f"{module}.c").write_text("")

    result = build.run(str(build.out), LEAN_SERVER_PROJECT_DIR=str(build.project))

    assert result.returncode == 3
    assert f"no {build.project}/lean-toolchain" in result.stderr
    assert list(build.out.iterdir()) == []


@pytest.mark.parametrize(
    ("listed", "said"),
    [
        ("mathlib\n", "is not 'PACKAGE MODULE/PATH': mathlib"),
        (
            "mathlib Mathlib/Data/Ineq and more\n",
            "is not 'PACKAGE MODULE/PATH': mathlib Mathlib/Data/Ineq and more",
        ),
        ("# nothing but a comment\n\n", "names no module"),
    ],
)
def test_the_build_refuses_a_list_it_cannot_read(build: Build, listed: str, said: str) -> None:
    (build.here / MODULES.name).write_text(listed)

    result = build.run(str(build.out), LEAN_SERVER_PROJECT_DIR=str(build.project))

    assert result.returncode == 2
    assert said in result.stderr
    assert list(build.out.iterdir()) == []


# --- the check's judgement, over a stand-in for Lean --------------------------------------------

LAKE = """#!/bin/sh
# `lake env PROGRAM`: the program, in the project's environment.
[ "$1" = env ] && shift
exec "$@"
"""

REPL = r'''#!/usr/bin/env python3
"""A REPL that answers the check's four commands the way Lean does, and misbehaves on request
(FAKE_REPL). What the real library does to the process, this does to itself: it is mapped into
memory, and it takes LD_PRELOAD out of the environment."""
import json, mmap, os, subprocess, sys

mode = os.environ.get("FAKE_REPL", "ok")
library = os.environ.get("LD_PRELOAD", "")
if library:
    handle = open(library, "rb")
    mapped = mmap.mmap(handle.fileno(), 0, prot=mmap.PROT_READ)
    if mode != "keeps-preload":
        del os.environ["LD_PRELOAD"]
    if mode == "does-not-start":
        sys.exit(127)
seconds = "1.0" if library and mode != "not-faster" else "9.0"
trace = "" if mode == "no-profile" else (
    f"[Elab.command] [{seconds}] theorem\n  [linarith] [{seconds}] Invoking oracle\n"
)
for line in sys.stdin:
    if not line.strip():
        continue
    command = json.loads(line)["cmd"]
    messages = []
    if "compiled_search_holds" in command:
        messages = [{"severity": "info", "data": trace}]
        if library and mode == "refuses":
            refused = "linarith failed to find a contradiction"
            messages.append({"severity": "error", "data": refused})
    elif "compiled_search_fails" in command:
        error = "linarith failed to find a contradiction"
        if library and mode == "other-answer":
            error += " (and something else)"
        messages = [{"severity": "info", "data": trace}, {"severity": "error", "data": error}]
    elif "printenv" in command:
        ran = subprocess.run(["printenv", "LD_PRELOAD"], capture_output=True, text=True)
        said = f"inherited=[{ran.stdout.strip()}] exit={ran.returncode}"
        messages = [{"severity": "info", "data": said}]
    env = 0 if command.startswith("import") else 1
    sys.stdout.write(json.dumps({"env": env, "messages": messages}) + "\n\n")
    sys.stdout.flush()
'''


@dataclass
class Check:
    """The check over the REAL launcher, a stand-in ``lake`` and a stand-in REPL."""

    native: Path
    bin_directory: Path
    project: Path

    def run(self, mode: str = "ok", *more: str) -> subprocess.CompletedProcess[str]:
        """Run the check, with the stand-in REPL told how to behave."""
        return subprocess.run(
            [
                sys.executable,
                str(CHECK),
                "--launcher",
                str(self.native / "repl-native"),
                "--native-dir",
                str(self.native),
                "--project",
                str(self.project),
                "--limit",
                "30",
                *more,
            ],
            capture_output=True,
            text=True,
            env=environment(PATH=f"{self.bin_directory}:{os.environ['PATH']}", FAKE_REPL=mode),
            check=False,
        )


@pytest.fixture
def check(tmp_path: Path) -> Check:
    """A native directory around the stand-in REPL, with the stand-in ``lake`` on PATH."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable(bin_directory / "lake", LAKE)
    repl = executable(bin_directory / "repl", REPL)
    return Check(native_directory(tmp_path / "native", repl), bin_directory, tmp_path)


def test_the_check_passes_a_library_that_is_loaded_answers_the_same_and_is_faster(
    check: Check,
) -> None:
    result = check.run()

    assert (result.returncode, result.stdout.strip()) == (0, OK)


def test_the_check_takes_the_repl_from_its_flag_or_from_the_native_directory(
    check: Check,
) -> None:
    (check.native / "repl-path").unlink()

    assert check.run("ok", "--repl", str(check.bin_directory / "repl")).stdout.strip() == OK
    refused = check.run()
    assert refused.returncode == 2
    assert "--repl is required" in refused.stderr


@pytest.mark.parametrize(
    ("mode", "said"),
    [
        (
            "keeps-preload",
            "with the library, a program started from Lean inherits LD_PRELOAD or did not run: ",
        ),
        ("other-answer", "the check 'fails' got a different answer with the library than without"),
        (
            "refuses",
            "with the library, the proof that holds was refused: "
            "linarith failed to find a contradiction",
        ),
        (
            "not-faster",
            "the search step took 18.0 s as the image runs it and 18.0 s with the library, "
            "twice in a row: not the 3 times less it must be",
        ),
        ("no-profile", "Lean's profiler did not report the search step, so it could not be timed"),
        ("does-not-start", "the Lean process ended"),
    ],
)
def test_the_check_fails_a_library_that_changes_anything_but_the_time(
    check: Check, mode: str, said: str
) -> None:
    result = check.run(mode)

    assert result.returncode == 1
    assert result.stdout.startswith("COMPILED SEARCH STEP: FAILED - ")
    assert said in result.stdout
    assert len(result.stdout.splitlines()) == 1


def test_the_check_fails_when_the_launcher_would_not_load_the_library(check: Check) -> None:
    (check.native / "built-from").write_text("0" * 64 + "\n")

    result = check.run()

    assert (result.returncode, result.stdout.strip()) == (
        1,
        "COMPILED SEARCH STEP: FAILED - the launcher did not load the library "
        "(it is not in the Lean process's memory)",
    )


# --- make.sh, over a stand-in for Docker --------------------------------------------------------

DOCKER = r'''#!/usr/bin/env python3
"""The docker CLI as make.sh uses it (run, cp, rm -f). FAKE_DOCKER says how the container's
build and check go; FAKE_DOCKER_ROOT holds the containers and a record of every call."""
import json, os, shutil, sys
from pathlib import Path

root = Path(os.environ["FAKE_DOCKER_ROOT"])
mode = os.environ.get("FAKE_DOCKER", "ok")
with open(root / "calls.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
command, arguments = sys.argv[1], sys.argv[2:]
if command == "run":
    if mode == "no-image":
        sys.exit("Unable to find image 'missing:1' locally")
    name = arguments[arguments.index("--name") + 1]
    source = next(a.split(":")[0] for a in arguments if a.endswith(":/lean-native-src:ro"))
    out = root / name / "opt" / "lean-native"
    out.mkdir(parents=True)
    (out / "repl-path").write_text("/repl/.lake/build/bin/repl\n")
    shutil.copy(Path(source) / "repl-native", out / "repl-native")
    if mode == "no-c-files":
        sys.exit("build.sh: this image holds no C file for Mathlib/Data/Ineq: nothing was built")
    (out / "linarith-search.so").write_text("the library\n")
    (out / "sources").write_text("/mathlib4/lean-toolchain\n")
    (out / "built-from").write_text("0" * 64 + "\n")
    print("COMPILED: 7 modules into /opt/lean-native/linarith-search.so")
    if mode == "check-fails":
        print("COMPILED SEARCH STEP: FAILED - the check 'fails' got a different answer")
        sys.exit(1)
    print(os.environ["FAKE_DOCKER_SAYS"])
elif command == "cp":
    name, path = arguments[0].split(":", 1)
    origin = root / name / path.lstrip("/")
    if not origin.is_dir():
        sys.exit("Error response from daemon: No such container: " + name)
    shutil.copytree(origin, arguments[1], dirs_exist_ok=True)
elif command == "rm":
    shutil.rmtree(root / arguments[-1], ignore_errors=True)
else:
    sys.exit("stand-in docker: unexpected " + " ".join(sys.argv[1:]))
'''


@dataclass
class Box:
    """A Lean server box's ``deploy/lean-server`` directory, with a stand-in Docker on PATH."""

    directory: Path
    bin_directory: Path
    docker_root: Path

    @property
    def native(self) -> Path:
        """Where make.sh installs the step."""
        return self.directory / "native"

    def make(self, mode: str = "ok", *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run make.sh, with the stand-in Docker told how the container goes."""
        return subprocess.run(
            ["bash", str(self.directory / "compiled-search" / "make.sh"), *arguments],
            capture_output=True,
            text=True,
            env=environment(
                PATH=f"{self.bin_directory}:{os.environ['PATH']}",
                FAKE_DOCKER=mode,
                FAKE_DOCKER_ROOT=str(self.docker_root),
                FAKE_DOCKER_SAYS=OK,
            ),
            check=False,
        )

    def calls(self) -> list[list[str]]:
        """Every Docker command make.sh ran, in order."""
        lines = (self.docker_root / "calls.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def run(self) -> list[str]:
        """The one ``docker run`` make.sh ran."""
        (run,) = [call for call in self.calls() if call[0] == "run"]
        return run

    def installed(self) -> list[str]:
        """What is in ``native/``."""
        return sorted(path.name for path in self.native.iterdir())


@pytest.fixture
def box(tmp_path: Path) -> Box:
    """A copy of ``compiled-search/`` in a box's directory, and the stand-in Docker."""
    directory = tmp_path / "lean-server"
    shutil.copytree(HERE, directory / "compiled-search")
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable(bin_directory / "docker", DOCKER)
    docker_root = tmp_path / "docker"
    docker_root.mkdir()
    return Box(directory, bin_directory, docker_root)


def mode_of(path: Path) -> int:
    """A file's permission bits."""
    return stat.S_IMODE(path.stat().st_mode)


def test_make_installs_a_library_that_passed_its_check(box: Box) -> None:
    result = box.make()

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == ON
    assert box.installed() == [
        "built-from",
        "linarith-search.so",
        "repl-native",
        "repl-path",
        "sources",
        "state",
    ]
    native = box.native
    assert (native / "state").read_text() == ON.replace("COMPILED SEARCH STEP: on - ", "on ") + "\n"
    assert (native / "repl-native").read_bytes() == LAUNCHER.read_bytes()
    assert (native / "repl-path").read_text() == "/repl/.lake/build/bin/repl\n"
    assert mode_of(native) == 0o755
    assert mode_of(native / "repl-native") == 0o755
    assert mode_of(native / "linarith-search.so") == 0o644
    assert OK in (box.directory / "native.log").read_text()
    assert not list(box.directory.glob("native.work.*"))
    assert [path.name for path in box.docker_root.iterdir()] == ["calls.jsonl"]


def test_make_runs_the_build_in_a_container_with_no_network_and_no_privileges(box: Box) -> None:
    box.make()

    run = box.run()
    words = " ".join(run)
    assert "--pull never" in words  # the box's own image, never one fetched for the build
    assert "--network none" in words
    assert "--cap-drop ALL" in words
    assert "--security-opt no-new-privileges:true" in words
    mounts = [run[index + 1] for index, word in enumerate(run) if word == "-v"]
    assert mounts == [f"{box.directory / 'compiled-search'}:/lean-native-src:ro"]
    assert run[-3:] == [
        "projectnumina/kimina-lean-server:2.0.0",
        "/lean-native-src/inside.sh",
        "/opt/lean-native",
    ]
    assert box.calls()[-1] == ["rm", "-f", run[run.index("--name") + 1]]


@pytest.mark.parametrize(
    ("dot_env", "arguments", "image"),
    [
        (None, (), "projectnumina/kimina-lean-server:2.0.0"),
        ("LEAN_SERVER_MAX_REPLS=4\nKIMINA_IMAGE=example/kimina:4.27\n", (), "example/kimina:4.27"),
        ("KIMINA_IMAGE='example/kimina:quoted'\n", (), "example/kimina:quoted"),
        ("KIMINA_IMAGE=example/kimina:4.27\n", ("example/other:1",), "example/other:1"),
    ],
)
def test_make_builds_from_the_image_the_box_runs(
    box: Box, dot_env: str | None, arguments: tuple[str, ...], image: str
) -> None:
    if dot_env is not None:
        (box.directory / ".env").write_text(dot_env)

    box.make("ok", *arguments)

    run = box.run()
    assert run[run.index("--entrypoint") + 2] == image


def test_make_installs_only_the_launcher_when_the_check_fails(box: Box) -> None:
    result = box.make("check-fails")

    assert result.returncode == 1
    assert result.stdout.splitlines()[-1].startswith(
        "COMPILED SEARCH STEP: OFF - the check 'fails' got a different answer. "
        "With compose.compiled-search.yaml the server runs the plain REPL, as before."
    )
    assert box.installed() == ["repl-native", "repl-path", "state"]
    assert (box.native / "state").read_text() == "off the check 'fails' got a different answer\n"


def test_a_box_loses_the_library_when_a_later_make_fails(box: Box) -> None:
    assert box.make().returncode == 0
    assert (box.native / "linarith-search.so").is_file()

    assert box.make("check-fails").returncode == 1

    assert box.installed() == ["repl-native", "repl-path", "state"]
    assert (box.native / "state").read_text().startswith("off ")


def test_make_says_what_stopped_a_build_that_never_reached_the_check(box: Box) -> None:
    result = box.make("no-c-files")

    assert result.returncode == 1
    assert (box.native / "state").read_text() == (
        "off nothing was built: build.sh: this image holds no C file for Mathlib/Data/Ineq: "
        "nothing was built\n"
    )
    assert box.installed() == ["repl-native", "repl-path", "state"]


def test_make_without_the_image_installs_nothing_to_start_with(box: Box) -> None:
    result = box.make("no-image")

    assert result.returncode == 1
    assert result.stdout.splitlines()[-1].startswith(
        "COMPILED SEARCH STEP: OFF - nothing was built: Unable to find image 'missing:1' locally. "
        "Do not start the server with compose.compiled-search.yaml"
    )
    assert box.installed() == ["state"]


def test_make_clears_what_an_interrupted_run_left(box: Box) -> None:
    left = box.directory / "native.work.abcdef"
    left.mkdir()
    (left / "linarith-search.so").write_text("half a library\n")

    box.make()

    assert not left.exists()


# --- the files agree with each other ------------------------------------------------------------


def shell_value(path: Path, name: str) -> str:
    """The value a script assigns to ``name`` on a line of its own."""
    (value,) = re.findall(rf"^{name}=(\S+)", path.read_text(), re.MULTILINE)
    return str(value)


def kimina_service(path: Path) -> dict[str, Any]:
    """The ``kimina`` service of a compose file."""
    services: dict[str, Any] = yaml.safe_load(path.read_text())["services"]
    kimina: dict[str, Any] = services["kimina"]
    return kimina


def test_the_compose_file_mounts_what_make_sh_installs_and_starts_the_launcher() -> None:
    override = LEAN_SERVER / "compose.compiled-search.yaml"
    mount = shell_value(MAKE, "MOUNT")

    assert list(yaml.safe_load(override.read_text())["services"]) == ["kimina"]
    assert kimina_service(override) == {
        "environment": {"LEAN_SERVER_REPL_PATH": f"{mount}/repl-native"},
        "volumes": [
            {
                "type": "bind",
                "source": "./native",
                "target": mount,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
    }
    assert shell_value(MAKE, "NATIVE") == "$BOX/native"


def test_make_sh_defaults_to_the_image_compose_yaml_runs() -> None:
    image = kimina_service(LEAN_SERVER / "compose.yaml")["image"]

    assert image == f"${{KIMINA_IMAGE:-{shell_value(MAKE, 'DEFAULT_IMAGE')}}}"


def test_the_programs_are_executable_and_inside_sh_runs_all_three() -> None:
    for path in (LAUNCHER, BUILD, CHECK, INSIDE, MAKE):
        assert os.access(path, os.X_OK), path
    inside = INSIDE.read_text()
    assert 'install -m 0755 "$HERE/repl-native" "$OUT/repl-native"' in inside
    assert 'bash "$HERE/build.sh" "$OUT"' in inside
    assert 'python3 "$HERE/check.py" --launcher "$OUT/repl-native" --native-dir "$OUT"' in inside


def test_what_make_sh_writes_on_a_box_is_ignored_by_git() -> None:
    ignored = (PROJECT / ".gitignore").read_text().splitlines()

    for path in (
        "deploy/lean-server/native/",
        "deploy/lean-server/native.work.*",
        "deploy/lean-server/native.log",
    ):
        assert path in ignored


def test_the_documentation_quotes_the_line_make_sh_prints(check: Check) -> None:
    page = (PROJECT / "docs" / "compiled-search.md").read_text()
    quoted = re.search(r"^COMPILED SEARCH STEP: on - .*$", page, re.MULTILINE)

    assert quoted is not None
    printed = check.run().stdout.strip().replace(": ok - ", ": on - ")
    numbers = re.compile(r"[0-9]+\.[0-9]+")
    assert numbers.sub("N", quoted.group(0)) == numbers.sub("N", printed)
