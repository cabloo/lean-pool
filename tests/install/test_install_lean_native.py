"""The three programs that make linarith's search step compiled code, each run on its own.

  lean-native-repl       the launcher a worker is started through
  lean-native-build.sh   makes the library, inside the Lean server's image
  lean-native-check.py   says whether the library is loaded and changes nothing but the time

Without Lean, the launcher and the check's judgement run against stand-ins written here, and the build's
refusals need none. The last part runs the build and the check against a REAL Lean workspace and skips
without one: it needs `lake` on PATH, LEAN_SERVER_PROJECT_DIR (a workspace with Mathlib's build cache) and
LEAN_NATIVE_REPL (the REPL binary), which is what the Lean server's image has. How the installer uses the
three is test_install_compiled_search.py's.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLERS = Path(__file__).resolve().parents[2] / "deploy" / "install"
LAUNCHER, BUILD, CHECK, MODULES = (
    INSTALLERS / name
    for name in (
        "lean-native-repl",
        "lean-native-build.sh",
        "lean-native-check.py",
        "lean-native-modules",
    )
)
OK = (
    "COMPILED SEARCH STEP: ok - linarith's search took 18.0 s as the image runs it and 2.0 s compiled (9.0 times less); "
    "the same answers; a program a tactic starts does not inherit the library"
)


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
    """The hash the build writes and the launcher takes again: over `sha256sum FILE...`'s own lines."""
    lines = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n" for path in sources
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def native_directory(directory: Path) -> Path:
    """A directory as the build leaves it, around a library that is no library (nothing here loads it as one)."""
    directory.mkdir(parents=True)
    sources = [directory / "source" / name for name in ("Gauss.c", "Datatypes.c", "lean-toolchain")]
    for path in sources:
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"what {path.name} holds\n")
    (directory / "linarith-search.so").write_text("the compiled search step\n")
    (directory / "sources").write_text("".join(f"{path}\n" for path in sources))
    (directory / "built-from").write_text(built_from(sources) + "\n")
    return directory


# --- the launcher -------------------------------------------------------------------------------------------------


@pytest.fixture
def started(tmp_path):
    """Start the launcher over a REPL that says how it was started; returns what it said."""
    repl = tmp_path / "repl"
    repl.write_text('#!/bin/sh\necho "preload=[${LD_PRELOAD:-}] arguments=[$*]"\nexit 7\n')
    repl.chmod(0o755)
    native = native_directory(tmp_path / "native")

    def start(*arguments: str) -> str:
        result = subprocess.run(
            ["sh", str(LAUNCHER), *arguments],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "LEAN_NATIVE_REPL": str(repl),
                "LEAN_NATIVE_DIR": str(native),
            },
        )
        assert result.returncode == 7  # the REPL's own status, whichever way it was started
        return result.stdout.strip()

    start.native = native  # type: ignore[attr-defined]  # what the tests read beside the call
    return start


def test_the_launcher_loads_the_library_made_from_the_files_that_are_there(started):
    assert started() == f"preload=[{started.native}/linarith-search.so] arguments=[]"
    assert (
        started("--one", "two")
        == f"preload=[{started.native}/linarith-search.so] arguments=[--one two]"
    )


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
        "no directory",
    ],
)
def test_the_launcher_starts_the_plain_repl_whenever_the_library_is_not_the_one_for_these_files(
    started, what
):
    native = started.native
    if what == "a source changed":  # the image was updated under a library made before
        (native / "source" / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
    elif what == "a source gone":
        (native / "source" / "Gauss.c").unlink()
    elif what == "another hash":
        (native / "built-from").write_text("0" * 64 + "\n")
    elif what == "an empty hash":
        (native / "built-from").write_text("")
    elif what == "no hash":  # how the build leaves it while it writes
        (native / "built-from").unlink()
    elif what == "no list":
        (native / "sources").unlink()
    elif what == "no library":
        (native / "linarith-search.so").unlink()
    else:
        shutil.rmtree(native)
    assert started("--one") == "preload=[] arguments=[--one]"


def test_the_launcher_defaults_to_where_the_image_and_the_compose_file_put_things():
    text = LAUNCHER.read_text()
    assert (
        "real=${LEAN_NATIVE_REPL:-/repl/.lake/build/bin/repl}" in text
        and "here=${LEAN_NATIVE_DIR:-/opt/lean-native}" in text
    )
    assert os.access(LAUNCHER, os.X_OK) and os.access(BUILD, os.X_OK)


# --- the build's refusals (all before anything of Lean is run) ----------------------------------------------------


@pytest.fixture
def build(tmp_path):
    """Run a copy of the build script (beside a list the test may change) with an environment of the test's."""
    here = tmp_path / "src"
    here.mkdir()
    shutil.copy(BUILD, here / BUILD.name)
    shutil.copy(MODULES, here / MODULES.name)
    out, project = tmp_path / "out", tmp_path / "project"
    out.mkdir()
    project.mkdir()

    def run(*arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(here / BUILD.name), *arguments],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], **environment},
        )

    run.here, run.out, run.project = here, out, project  # type: ignore[attr-defined]
    return run


def test_the_build_names_seven_modules_of_the_search_step():
    lines = listed_modules()
    assert len(lines) == 7 and {package for package, _ in lines} == {"mathlib", "batteries"}
    assert (
        sum(
            module.startswith("Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm")
            for _, module in lines
        )
        == 5
    )


def test_the_build_refuses_without_a_workspace_or_a_place_to_write(build):
    result = build()
    assert result.returncode != 0 and "usage: lean-native-build.sh OUT_DIRECTORY" in result.stderr
    result = build(str(build.out))
    assert result.returncode == 2 and "LEAN_SERVER_PROJECT_DIR is not set" in result.stderr
    result = build(str(build.out / "absent"), LEAN_SERVER_PROJECT_DIR=str(build.project))
    assert result.returncode == 2 and f"no directory {build.out}/absent" in result.stderr
    assert list(build.out.iterdir()) == []


def test_the_build_writes_nothing_for_an_image_without_the_c_files(build):
    result = build(str(build.out), LEAN_SERVER_PROJECT_DIR=str(build.project))
    assert result.returncode == 3
    assert (
        "this image holds no C file for Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm/Datatypes of mathlib"
        in result.stderr
    )
    assert result.stderr.strip().endswith(": nothing was built") and list(build.out.iterdir()) == []
    # ... nor for one whose toolchain it could not name in the hash.
    for package, module in listed_modules():
        file = (
            build.project
            / ".lake"
            / "packages"
            / package
            / ".lake"
            / "build"
            / "ir"
            / f"{module}.c"
        )
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("")
    result = build(str(build.out), LEAN_SERVER_PROJECT_DIR=str(build.project))
    assert (
        result.returncode == 3
        and f"no {build.project}/lean-toolchain" in result.stderr
        and list(build.out.iterdir()) == []
    )


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
def test_the_build_refuses_a_list_it_cannot_read(build, listed, said):
    (build.here / MODULES.name).write_text(listed)
    result = build(str(build.out), LEAN_SERVER_PROJECT_DIR=str(build.project))
    assert result.returncode == 2 and said in result.stderr and list(build.out.iterdir()) == []


# --- the check's judgement, over a stand-in for Lean --------------------------------------------------------------

LAKE = '#!/bin/sh\n# `lake env PROGRAM`: the program, in the workspace\'s environment.\n[ "$1" = env ] && shift\nexec "$@"\n'

REPL = r'''#!/usr/bin/env python3
"""A REPL that answers the check's four commands the way Lean does, and can be told to misbehave (FAKE_REPL).
What the real library does to the process, this does to itself: it is mapped into memory, and it takes
LD_PRELOAD out of the environment."""
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
trace = "" if mode == "no-profile" else f"[Elab.command] [{seconds}] theorem\n  [linarith] [{seconds}] Invoking oracle\n"
for line in sys.stdin:
    if not line.strip():
        continue
    command = json.loads(line)["cmd"]
    messages = []
    if "compiled_search_holds" in command:
        messages = [{"severity": "info", "data": trace}]
        if library and mode == "refuses":
            messages.append({"severity": "error", "data": "linarith failed to find a contradiction"})
    elif "compiled_search_fails" in command:
        error = "linarith failed to find a contradiction" + (" (and something else)" if library and mode == "other-answer" else "")
        messages = [{"severity": "info", "data": trace}, {"severity": "error", "data": error}]
    elif "printenv" in command:
        ran = subprocess.run(["printenv", "LD_PRELOAD"], capture_output=True, text=True)
        messages = [{"severity": "info", "data": f"inherited=[{ran.stdout.strip()}] exit={ran.returncode}"}]
    sys.stdout.write(json.dumps({"env": 0 if command.startswith("import") else 1, "messages": messages}) + "\n\n")
    sys.stdout.flush()
'''


@pytest.fixture
def check(tmp_path):
    """Run the check over the REAL launcher, a stand-in `lake` and a stand-in REPL."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name, text in (("lake", LAKE), ("repl", REPL)):
        (bin_directory / name).write_text(text)
        (bin_directory / name).chmod(0o755)
    native = native_directory(tmp_path / "native")

    def run(mode: str = "ok") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CHECK),
                "--launcher",
                str(LAUNCHER),
                "--native-dir",
                str(native),
                "--project",
                str(tmp_path),
                "--limit",
                "30",
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{bin_directory}:{os.environ['PATH']}",
                "LEAN_NATIVE_REPL": str(bin_directory / "repl"),
                "FAKE_REPL": mode,
            },
        )

    run.native = native  # type: ignore[attr-defined]
    return run


def test_the_check_passes_a_library_that_is_loaded_answers_the_same_and_is_faster(check):
    result = check()
    assert (result.returncode, result.stdout.strip()) == (0, OK)


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
            "with the library, the proof that holds was refused: linarith failed to find a contradiction",
        ),
        (
            "not-faster",
            "the search step took 18.0 s as the image runs it and 18.0 s with the library, twice in a row: not the 3 times less it must be",
        ),
        ("no-profile", "Lean's profiler did not report the search step, so it could not be timed"),
        ("does-not-start", "the Lean process ended"),
    ],
)
def test_the_check_fails_a_library_that_changes_anything_but_the_time(check, mode, said):
    result = check(mode)
    assert (
        result.returncode == 1
        and result.stdout.startswith("COMPILED SEARCH STEP: FAILED - ")
        and said in result.stdout
    )
    assert len(result.stdout.splitlines()) == 1


def test_the_check_fails_when_the_launcher_would_not_load_the_library(check):
    (check.native / "built-from").write_text("0" * 64 + "\n")
    result = check()
    assert (result.returncode, result.stdout.strip()) == (
        1,
        "COMPILED SEARCH STEP: FAILED - the launcher did not load the library (it is not in the Lean process's memory)",
    )


# --- with a real Lean ---------------------------------------------------------------------------------------------

PROJECT = os.environ.get("LEAN_SERVER_PROJECT_DIR", "")
REAL_REPL = os.environ.get("LEAN_NATIVE_REPL", "")
with_lean = pytest.mark.skipif(
    not (
        shutil.which("lake")
        and PROJECT
        and REAL_REPL
        and os.access(REAL_REPL, os.X_OK)
        and Path(PROJECT, ".lake/packages/mathlib/.lake/build/ir/Mathlib/Data/Ineq.c").is_file()
    ),
    reason="needs a real Lean: `lake` on PATH, LEAN_SERVER_PROJECT_DIR with Mathlib's build cache, LEAN_NATIVE_REPL",
)


def really_built(directory: Path, change=None) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the build script (or a changed copy of it) for real, into a directory laid out as the installer's."""
    src, native = directory / "src", directory / "native"
    src.mkdir(parents=True)
    native.mkdir()
    text = BUILD.read_text()
    if change:
        changed = text.replace(*change)
        assert changed != text
        text = changed
    (src / BUILD.name).write_text(text)
    shutil.copy(MODULES, src / MODULES.name)
    shutil.copy(LAUNCHER, native / "repl-native")
    return subprocess.run(
        ["bash", str(src / BUILD.name), str(native)], capture_output=True, text=True, timeout=1800
    ), native


def really_checked(native: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--launcher",
            str(native / "repl-native"),
            "--native-dir",
            str(native),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    result, native = really_built(tmp_path_factory.mktemp("real"))
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return result, native


@with_lean
def test_a_real_build_makes_one_library_of_the_seven_modules_and_ties_it_to_its_sources(real):
    result, native = real
    assert {path.name for path in native.iterdir()} == {
        "repl-native",
        "linarith-search.so",
        "sources",
        "built-from",
    }
    assert (
        "COMPILED: 7 modules into " in result.stdout
        and "imported modules are not compiled" in result.stdout
    )
    sources = [Path(line) for line in (native / "sources").read_text().splitlines()]
    assert (
        len(sources) == 8
        and sources[-1] == Path(PROJECT) / "lean-toolchain"
        and all(path.is_file() for path in sources)
    )
    assert (native / "built-from").read_text().strip() == built_from(sources)
    # Lean finds a compiled module by its initializer: the seven asked for are visible, and no other is.
    names = subprocess.run(
        ["nm", "-D", "--defined-only", str(native / "linarith-search.so")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    exported = sorted(
        line.split()[-1]
        for line in names.splitlines()
        if line.split()[-1].startswith("initialize_")
    )
    wanted = sorted(
        f"initialize_{package}_{module}".replace("/", "_") for package, module in listed_modules()
    )
    assert exported == wanted and len(exported) == 7
    assert [
        line for line in result.stdout.splitlines() if line.startswith("  not compiled: ")
    ]  # and the stubbed ones are named


@with_lean
def test_a_real_library_passes_the_check_and_one_the_launcher_refuses_does_not(real, tmp_path):
    _, native = real
    result = really_checked(native)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("COMPILED SEARCH STEP: ok - linarith's search took ")
    assert result.stdout.strip().endswith(
        "the same answers; a program a tactic starts does not inherit the library"
    )
    # The same library under a hash that is not its sources': the launcher starts the plain REPL, and the check says so.
    other = tmp_path / "native"
    shutil.copytree(native, other)
    (other / "built-from").write_text("0" * 64 + "\n")
    result = really_checked(other)
    assert (result.returncode, result.stdout.strip()) == (
        1,
        "COMPILED SEARCH STEP: FAILED - the launcher did not load the library (it is not in the Lean process's memory)",
    )


@with_lean
def test_a_real_library_that_leaves_ld_preload_in_the_environment_fails_the_check(tmp_path):
    """The positive control for the check: without the one line that forgets LD_PRELOAD, every program a tactic
    starts is handed a library only a Lean process can load."""
    line = """  echo '__attribute__((constructor)) static void lean_native_forget_preload(void) { unsetenv("LD_PRELOAD"); }'\n"""
    result, native = really_built(tmp_path, change=(line, ""))
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    result = really_checked(native)
    assert result.returncode == 1 and result.stdout.startswith(
        "COMPILED SEARCH STEP: FAILED - with the library, "
    ), result.stdout
    assert (
        "a tactic that starts another program failed" in result.stdout
        or "inherits LD_PRELOAD or did not run" in result.stdout
    )


@with_lean
def test_a_real_build_refuses_a_library_that_shows_an_initializer_it_did_not_compile(tmp_path):
    """Were a do-nothing initializer visible, Lean would take that module for a compiled one and skip its own
    start-up code."""
    result, native = really_built(
        tmp_path, change=('visibility(\\"hidden\\")', 'visibility(\\"default\\")')
    )
    assert result.returncode == 4, result.stdout[-2000:] + result.stderr[-2000:]
    assert (
        "the library does not export exactly the initializers of the modules asked for"
        in result.stderr
    )
    assert (
        "exported and not asked for:" in result.stderr
        and "initialize_" in result.stderr.split("exported and not asked for:")[1]
    )
    assert {path.name for path in native.iterdir()} == {"repl-native"}
