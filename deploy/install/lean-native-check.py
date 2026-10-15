#!/usr/bin/env python3
"""lean-native-check.py - is the compiled search step really on, and does it change nothing but the time?

Runs after lean-native-build.sh, in the same container of the Lean server's image, BEFORE the
server is told to start its workers through the launcher (`lean-native-repl`). It starts a worker
twice the way the server does (`lake env PROGRAM` in the server's workspace, the imports first, then
each check against them): once with nothing to load, once with the library. Standard library only.

It passes when all of these hold:
  1. the library is in the second process's memory and not in the first's (the kernel's own record,
     /proc/PID/maps): the launcher loads it exactly when it should;
  2. every check gets the same answer from both: a proof `nlinarith` closes, one it cannot close
     (the same error, word for word), and one whose tactic starts another program (`bv_decide`, a
     SAT solver);
  3. a program started from inside Lean finds no LD_PRELOAD in its environment, in either process;
  4. the search step itself, timed by Lean's own profiler, takes at most a third of the time with
     the library that it takes without.

It prints one line that starts `COMPILED SEARCH STEP: ok` or `COMPILED SEARCH STEP: FAILED`, and
exits 0 or 1.
"""  # noqa: E501

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

LIBRARY = "linarith-search.so"
IMPORTS = "import Mathlib\nimport Aesop"
# The checks below are ours (written for this file), not anybody's proof attempt. The two
# inequalities carry more hints than they need on purpose: nlinarith multiplies the hints pairwise,
# and the search over the products is the step being timed.
PROFILED = (
    "set_option maxHeartbeats 0\nset_option trace.profiler true\n"
    "set_option trace.profiler.threshold 10\n\n"
)
HINTS = (
    "  nlinarith [sq_nonneg (x - y), sq_nonneg (y - z), sq_nonneg (z - x), sq_nonneg (x + y - z),\n"
    "    sq_nonneg (y + z - x), sq_nonneg (z + x - y), sq_nonneg (x + y + z), mul_nonneg hx hy, "
    "mul_nonneg hy hz,\n"
    "    mul_nonneg hz hx, sq_nonneg (x - 1), sq_nonneg (y - 1), sq_nonneg (z - 1)]\n"
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


class Failed(Exception):  # noqa: N818
    """A check of the compiled search step did not hold; the message says which, and why."""


def descendants(pid: int) -> list[int]:
    """Every process below `pid` (the worker is `lake env PROGRAM`: the Lean process is lake's
    child)."""
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
    """Whether the library is in the memory of `pid` or of any process below it."""
    for process in [pid, *descendants(pid)]:
        try:
            if LIBRARY in Path(f"/proc/{process}/maps").read_text():
                return True
        except OSError:
            continue
    return False


class Worker:
    """One Lean worker, started the way the server starts it, that answers one command at a time."""

    def __init__(self, launcher: str, project: str, native_dir: str, limit: float) -> None:
        environment = dict(os.environ, LEAN_NATIVE_DIR=native_dir)
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

    def ask(self, command: dict) -> dict:
        """Send one command to the worker and return its JSON answer, or raise `Failed`."""
        assert self.process.stdin is not None and self.process.stdout is not None  # noqa: PT018
        try:
            self.process.stdin.write((json.dumps(command) + "\n\n").encode())
            self.process.stdin.flush()
        except BrokenPipeError:
            raise Failed(
                f"the Lean process ended before it was asked anything: {self.last_words()}"
            ) from None
        deadline = time.monotonic() + self.limit
        data = b""
        while True:
            left = deadline - time.monotonic()
            ready = (
                select.select([self.process.stdout], [], [], max(left, 0))[0] if left > 0 else []
            )
            if not ready:
                raise Failed(f"no answer from Lean in {self.limit:.0f} s")
            chunk = os.read(self.process.stdout.fileno(), 1 << 16)
            if not chunk:
                raise Failed(f"the Lean process ended: {self.last_words()}")
            data += chunk
            if data.endswith(b"\n\n"):
                try:
                    return json.loads(data.decode())
                except json.JSONDecodeError:
                    continue

    def last_words(self) -> str:
        """What the worker's process last said on stderr, or how it ended."""
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)
        assert self.process.stderr is not None
        os.set_blocking(self.process.stderr.fileno(), False)
        words = (self.process.stderr.read() or b"").decode(errors="replace").strip().splitlines()
        return " | ".join(words[-3:])[:400] or f"exit status {self.process.returncode}"

    def stop(self) -> None:
        """Kill the worker and everything it started, and wait for it."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()


def session(launcher: str, project: str, native_dir: str, limit: float) -> dict:
    """One worker: the imports, then every check. What each check said, and whether the library was
    loaded."""
    worker = Worker(launcher, project, native_dir, limit)
    try:
        imported = worker.ask({"cmd": IMPORTS})
        if imported.get("env") != 0 or any(
            m.get("severity") == "error" for m in imported.get("messages", [])
        ):
            raise Failed(f"the imports did not load: {json.dumps(imported)[:300]}")
        loaded = holds_library(worker.process.pid)
        said = {}
        for name, body in CHECKS.items():
            answer = worker.ask({"cmd": body, "env": 0})
            if "messages" not in answer and "message" in answer:
                raise Failed(f"Lean refused the check '{name}': {str(answer['message'])[:300]}")
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


def judge(plain: dict, compiled: dict) -> tuple[float, float]:
    """Compare the two sessions: raise `Failed` unless they agree; return the two search times."""
    if plain["loaded"]:
        raise Failed(
            "the library is in the memory of the process that was started with nothing to load"
        )
    if not compiled["loaded"]:
        raise Failed(
            "the launcher did not load the library (it is not in the Lean process's memory)"
        )
    for which, result in (("as the image runs it", plain), ("with the library", compiled)):
        said = result["said"]
        if said["holds"]["errors"]:
            raise Failed(
                f"{which}, the proof that holds was refused: {said['holds']['errors'][0][:200]}"
            )
        if not said["fails"]["errors"] or not all(
            "linarith failed" in e for e in said["fails"]["errors"]
        ):
            raise Failed(
                f"{which}, the proof that cannot hold did not fail the way it does: "
                f"{said['fails']['errors'][:1]}"
            )
        if said["starts a program"]["errors"]:
            raise Failed(
                f"{which}, a tactic that starts another program failed: "
                f"{said['starts a program']['errors'][0][:200]}"
            )
        if said["environment"]["printed"] != ["inherited=[] exit=1"]:
            raise Failed(
                f"{which}, a program started from Lean inherits LD_PRELOAD or did not run: "
                f"{said['environment']}"
            )
    for name in CHECKS:
        if plain["said"][name]["errors"] != compiled["said"][name]["errors"]:
            raise Failed(f"the check '{name}' got a different answer with the library than without")
    before = sum(plain["said"][name]["search_seconds"] for name in ("holds", "fails"))
    after = sum(compiled["said"][name]["search_seconds"] for name in ("holds", "fails"))
    if plain["said"]["holds"]["searches"] < 1 or plain["said"]["fails"]["searches"] < 1:
        raise Failed("Lean's profiler did not report the search step, so it could not be timed")
    return before, after


def main() -> int:
    """Run the two sessions and judge them; print the one result line and return 0 or 1."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--launcher",
        required=True,
        help="the launcher (lean-native-repl), as the server will start it",
    )
    parser.add_argument(
        "--native-dir",
        required=True,
        help="the directory lean-native-build.sh wrote the library into",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("LEAN_SERVER_PROJECT_DIR"),
        help="the server's workspace",
    )
    parser.add_argument("--limit", type=float, default=600.0, help="seconds to wait for one answer")
    arguments = parser.parse_args()
    if not arguments.project:
        parser.error("--project (or LEAN_SERVER_PROJECT_DIR) is required")
    try:
        before = after = 0.0
        for attempt in (
            1,
            2,
        ):  # the timing alone is tried twice: other work on the box can fall on one run
            plain = session(
                arguments.launcher, arguments.project, "/nonexistent/lean-native", arguments.limit
            )
            compiled = session(
                arguments.launcher, arguments.project, arguments.native_dir, arguments.limit
            )
            before, after = judge(plain, compiled)
            if after * FASTER <= before:
                break
            if attempt == 2:
                raise Failed(
                    f"the search step took {before:.1f} s as the image runs it and {after:.1f} s "
                    f"with the library, twice in a row: "
                    f"not the {FASTER:.0f} times less it must be, so the compiled code is not "
                    f"what ran"
                )
    except Failed as failure:
        print(f"COMPILED SEARCH STEP: FAILED - {failure}")
        return 1
    times = f"{before / after:.1f} times less" if after > 0 else "too short to time"
    print(
        f"COMPILED SEARCH STEP: ok - linarith's search took {before:.1f} s as the image runs it "
        f"and {after:.1f} s compiled "
        f"({times}); the same answers; a program a tactic starts does not inherit the library"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
