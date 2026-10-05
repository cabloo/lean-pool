"""The admission tests' example cases, an honest fake Lean server, and the command as a call."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fake_lean_server import Behaviour, SnippetReply, lean_answer

from leanpool.admit.cli import main

GOOD_PROOF = "/- A license. -/\nimport Mathlib\n\ntheorem two : 1 + 1 = 2 := by rfl\n"
WRONG_PROOF = "import Mathlib\n\ntheorem wrong : 1 + 1 = 3 := by rfl\n"
SORRY_PROOF = "import Mathlib\n\ntheorem unfinished : 1 + 1 = 2 := by sorry\n"


def honest_lean(snippet_code: str) -> dict[str, Any]:
    """What a correct Lean server says about the three example files."""
    if "1 + 1 = 3" in snippet_code:
        return lean_answer(("error", "The rfl tactic failed: 2 is not definitionally equal to 3"))
    if "sorry" in snippet_code:
        return lean_answer(("warning", "declaration uses 'sorry'"), sorries=1)
    return lean_answer()


def answering(decide: Callable[[str], SnippetReply]) -> Behaviour:
    """A fake Lean server's behaviour that answers each snippet by its code."""

    async def behaviour(snippet: dict[str, Any]) -> SnippetReply:
        return decide(snippet["code"])

    return behaviour


def write_cases(directory: Path) -> Path:
    """Write the three example cases: one that verifies and two that must be rejected."""
    (directory / "verify").mkdir(parents=True)
    (directory / "reject").mkdir()
    (directory / "verify" / "two.lean").write_text(GOOD_PROOF)
    (directory / "reject" / "wrong.lean").write_text(WRONG_PROOF)
    (directory / "reject" / "unfinished.lean").write_text(SORRY_PROOF)
    return directory


async def admit(
    server_url: str,
    cases_directory: Path,
    key_file: Path | None,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> tuple[int, dict[str, Any]]:
    """Run the command in a thread (it owns its event loop) and return its status and report."""
    arguments = ["--server", server_url, "--cases", str(cases_directory), *extra]
    if key_file is not None:
        arguments += ["--api-key-file", str(key_file)]
    capsys.readouterr()
    exit_status = await asyncio.to_thread(main, arguments, {})
    report: dict[str, Any] = json.loads(capsys.readouterr().out)
    return exit_status, report
