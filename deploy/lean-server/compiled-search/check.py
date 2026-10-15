#!/usr/bin/env python3
"""Check that the compiled search step is really on, and that it changes nothing but the time.

Runs after build.sh, in the same container of the Kimina image (make.sh starts both through
inside.sh), before the server is started with compose.compiled-search.yaml. It starts a worker
twice the way Kimina does (``lake env PROGRAM`` in the server's Lean project, the imports first,
then each check against them): once with nothing to load, once with the library. Standard library
only; see docs/compiled-search.md.

It passes when all of these hold:

1. the library is in the second process's memory and not in the first's (the kernel's own
   record, /proc/PID/maps): the launcher loads it exactly when it should;
2. every check gets the same answer from both: a proof ``nlinarith`` closes, one it cannot close
   (the same error, word for word), and one whose tactic starts another program (``bv_decide``,
   a SAT solver);
3. a program started from inside Lean finds no LD_PRELOAD in its environment, in either process;
4. the search step itself, timed by Lean's own profiler, takes at most a third of the time with
   the library that it takes without (8 to 10 times less where this was written).

It prints one line that starts ``COMPILED SEARCH STEP: ok`` or ``COMPILED SEARCH STEP: FAILED``,
and exits 0 or 1.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LIBRARY = "linarith-search.so"
IMPORTS = "import Mathlib\nimport Aesop"
# The checks below were written for this file. The two inequalities carry more hints than they
# need on purpose: nlinarith multiplies the hints pairwise, and the search over the products is the
# step being timed.
PROFILED = (
    "set_option maxHeartbeats 0\n"
    "set_option trace.profiler true\n"
    "set_option trace.profiler.threshold 10\n\n"
)
HINTS = (
    "  nlinarith [sq_nonneg (x - y), sq_nonneg (y - z), sq_nonneg (z - x), sq_nonneg (x + y - z),\n"
    "    sq_nonneg (y + z - x), sq_nonneg (z + x - y), sq_nonneg (x + y + z), mul_nonneg hx hy,\n"
    "    mul_nonneg hy hz, mul_nonneg hz hx, sq_nonneg (x - 1), sq_nonneg (y - 1), "
    "sq_nonneg (z - 1)]\n"
)
CHECKS = {
    "holds": PROFILED
    + "theorem compiled_search_holds (x y z : ℝ) (hx : 0 ≤ x) (hy : 0 ≤ y) (hz : 0 ≤ z) :\n"  # noqa: RUF001
    + "    x ^ 3 + y ^ 3 + z ^ 3 ≥ 3 * x * y * z := by\n"
    + HINTS,
    "fails": PROFILED
    + "theorem compiled_search_fails (x y z : ℝ) (hx : 0 ≤ x) (hy : 0 ≤ y) (hz : 0 ≤ z) :\n"  # noqa: RUF001
    + "    x ^ 3 + y ^ 3 + z ^ 3 ≥ 4 * x * y * z := by\n"
    + HINTS,
    "starts a program": (
        "theorem compiled_search_solver (a b : BitVec 8) : a &&& b = b &&& a := by\n  bv_decide\n"
    ),
    "environment": (
        "#eval show IO Unit from do\n"
        '  let out ← IO.Process.output { cmd := "printenv", args := #["LD_PRELOAD"] }\n'
        '  IO.println s!"inherited=[{out.stdout.trim}] exit={out.exitCode}"\n'
    ),
}
ORACLE = re.compile(r"\[linarith\] \[([0-9.]+)\][^\n]*Invoking oracle")
FASTER = 3.0


class CheckFailedError(Exception):
    """The check failed; the message says why."""


def descendants(pid: int) -> list[int]:
    """Every process below ``pid`` (a worker is ``lake env PROGRAM``: Lean is lake's child)."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            parents[int(entry.name)] = int(stat.rsplit(")", 1)[1].split()[1])
    found, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        for child, its_parent in parents.items():
            if its_parent == parent:
                found.append(child)
                frontier.append(child)
    return found


def holds_library(pid: int) -> bool:
    """Whether ``pid`` or a process below it has the library mapped into its memory."""
    for process in [pid, *descendants(pid)]:
        try:
            if LIBRARY in Path(f"/proc/{process}/maps").read_text():
                return True
        except OSError:
            continue
    return False


class Worker:
    """One worker started the way Kimina starts one, spoken to in the REPL's own protocol."""

    def __init__(self, launcher: str, repl: str, project: str, native: str, limit: float) -> None:
        environment = dict(os.environ, LEAN_NATIVE_DIR=native, LEAN_NATIVE_REPL=repl)
        environment.pop("LD_PRELOAD", None)
        self.limit = limit
        self.process = subprocess.Popen(
            ["lake", "env", launcher],
            cwd=project,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def ask(self, command: dict[str, Any]) -> dict[str, Any]:
        """Send one command and wait, at most the limit, for its whole answer."""
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        try:
            self.process.stdin.write((json.dumps(command) + "\n\n").encode())
            self.process.stdin.flush()
        except BrokenPipeError:
            raise CheckFailedError(
                f"the Lean process ended before it was asked anything: {self.last_words()}"
            ) from None
        deadline = time.monotonic() + self.limit
        data = b""
        while True:
            left = deadline - time.monotonic()
            ready = select.select([self.process.stdout], [], [], left)[0] if left > 0 else []
            if not ready:
                raise CheckFailedError(f"no answer from Lean in {self.limit:.0f} s")
            chunk = os.read(self.process.stdout.fileno(), 1 << 16)
            if not chunk:
                raise CheckFailedError(f"the Lean process ended: {self.last_words()}")
            data += chunk
            if data.endswith(b"\n\n"):
                try:
                    answer: dict[str, Any] = json.loads(data.decode())
                except json.JSONDecodeError:
                    continue
                return answer

    def last_words(self) -> str:
        """The last lines the process wrote to its error stream, or its exit status."""
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)
        assert self.process.stderr is not None
        os.set_blocking(self.process.stderr.fileno(), False)
        words = (self.process.stderr.read() or b"").decode(errors="replace").strip().splitlines()
        return " | ".join(words[-3:])[:400] or f"exit status {self.process.returncode}"

    def stop(self) -> None:
        """End the worker and everything it started."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()


def session(launcher: str, repl: str, project: str, native: str, limit: float) -> dict[str, Any]:
    """One worker: the imports, then every check.

    Returns what each check said, and whether the library was loaded.
    """
    worker = Worker(launcher, repl, project, native, limit)
    try:
        imported = worker.ask({"cmd": IMPORTS})
        errors = [m for m in imported.get("messages", []) if m.get("severity") == "error"]
        if imported.get("env") != 0 or errors:
            raise CheckFailedError(f"the imports did not load: {json.dumps(imported)[:300]}")
        loaded = holds_library(worker.process.pid)
        said = {}
        for name, body in CHECKS.items():
            answer = worker.ask({"cmd": body, "env": 0})
            if "messages" not in answer and "message" in answer:
                raise CheckFailedError(
                    f"Lean refused the check '{name}': {str(answer['message'])[:300]}"
                )
            messages = answer.get("messages", [])
            text = "\n".join(str(m.get("data", "")) for m in messages)
            said[name] = {
                "errors": [
                    str(m.get("data", "")) for m in messages if m.get("severity") == "error"
                ],
                "search_seconds": sum(float(t) for t in ORACLE.findall(text)),
                "searches": len(ORACLE.findall(text)),
                "printed": [
                    str(m.get("data", ""))
                    for m in messages
                    if m.get("severity") == "info" and "inherited=" in str(m.get("data", ""))
                ],
            }
        return {"loaded": loaded, "said": said}
    finally:
        worker.stop()


def judge(plain: dict[str, Any], compiled: dict[str, Any]) -> tuple[float, float]:
    """Raise ``CheckFailedError`` unless conditions 1 to 3 hold.

    Returns the search's seconds without the library and with it.
    """
    if plain["loaded"]:
        raise CheckFailedError(
            "the library is in the memory of the process started with nothing to load"
        )
    if not compiled["loaded"]:
        raise CheckFailedError(
            "the launcher did not load the library (it is not in the Lean process's memory)"
        )
    for which, result in (("as the image runs it", plain), ("with the library", compiled)):
        said = result["said"]
        if said["holds"]["errors"]:
            first = said["holds"]["errors"][0][:200]
            raise CheckFailedError(f"{which}, the proof that holds was refused: {first}")
        failed = said["fails"]["errors"]
        if not failed or not all("linarith failed" in error for error in failed):
            raise CheckFailedError(
                f"{which}, the proof that cannot hold did not fail the way it does: {failed[:1]}"
            )
        if said["starts a program"]["errors"]:
            first = said["starts a program"]["errors"][0][:200]
            raise CheckFailedError(f"{which}, a tactic that starts another program failed: {first}")
        if said["environment"]["printed"] != ["inherited=[] exit=1"]:
            raise CheckFailedError(
                f"{which}, a program started from Lean inherits LD_PRELOAD or did not run: "
                f"{said['environment']}"
            )
    for name in CHECKS:
        if plain["said"][name]["errors"] != compiled["said"][name]["errors"]:
            raise CheckFailedError(
                f"the check '{name}' got a different answer with the library than without"
            )
    if plain["said"]["holds"]["searches"] < 1 or plain["said"]["fails"]["searches"] < 1:
        raise CheckFailedError(
            "Lean's profiler did not report the search step, so it could not be timed"
        )
    before = sum(plain["said"][name]["search_seconds"] for name in ("holds", "fails"))
    after = sum(compiled["said"][name]["search_seconds"] for name in ("holds", "fails"))
    return before, after


def main() -> int:
    """Run both workers, judge them, and print the one line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--launcher", required=True, help="repl-native, as the server will start it"
    )
    parser.add_argument("--native-dir", required=True, help="where build.sh wrote the library")
    parser.add_argument(
        "--repl", help="the image's REPL (default: the native directory's repl-path)"
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("LEAN_SERVER_PROJECT_DIR"),
        help="the server's Lean project (default: LEAN_SERVER_PROJECT_DIR)",
    )
    parser.add_argument("--limit", type=float, default=600.0, help="seconds to wait for one answer")
    arguments = parser.parse_args()
    if not arguments.project:
        parser.error("--project (or LEAN_SERVER_PROJECT_DIR) is required")
    repl = arguments.repl
    if not repl:
        path = Path(arguments.native_dir, "repl-path")
        repl = path.read_text().strip() if path.is_file() else ""
    if not repl:
        parser.error("--repl is required when the native directory holds no repl-path")
    try:
        before = after = 0.0
        for attempt in (1, 2):  # the timing alone is tried twice: other work can fall on one run
            plain = session(
                arguments.launcher,
                repl,
                arguments.project,
                "/nonexistent/lean-native",
                arguments.limit,
            )
            compiled = session(
                arguments.launcher, repl, arguments.project, arguments.native_dir, arguments.limit
            )
            before, after = judge(plain, compiled)
            if after * FASTER <= before:
                break
            if attempt == 2:
                raise CheckFailedError(
                    f"the search step took {before:.1f} s as the image runs it and {after:.1f} s "
                    f"with the library, twice in a row: not the {FASTER:.0f} times less it "
                    "must be, so the compiled code is not what ran"
                )
    except CheckFailedError as failure:
        print(f"COMPILED SEARCH STEP: FAILED - {failure}")
        return 1
    times = f"{before / after:.1f} times less" if after > 0 else "too short to time"
    print(
        f"COMPILED SEARCH STEP: ok - linarith's search took {before:.1f} s as the image runs it "
        f"and {after:.1f} s compiled ({times}); the same answers; a program a tactic starts does "
        "not inherit the library"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
