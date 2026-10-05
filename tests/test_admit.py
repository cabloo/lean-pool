"""The admission test: verdicts, cases, and the command against in-process fake servers."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from admit_support import WRONG_PROOF, admit, answering, honest_lean
from aiohttp import web
from fake_lean_server import (
    SnippetReply,
    always,
    failing_with,
    lean_answer,
    lean_timeout,
    repl_error,
)
from support import API_KEY, StartLeanServer

from leanpool.admit import (
    AdmissionSettings,
    CasesError,
    Expectation,
    Observation,
    judge_result,
    load_cases,
    run_admission,
)
from leanpool.admit.cli import main
from leanpool.admit.verdict import behaved


def test_a_clean_answer_is_accepted() -> None:
    verdict = judge_result(lean_answer(("info", "'two' does not depend on any axioms")))
    assert verdict.observation is Observation.ACCEPTED


def test_an_error_message_is_a_rejection_and_is_quoted() -> None:
    verdict = judge_result(lean_answer(("warning", "unused"), ("error", "unsolved goals")))
    assert (verdict.observation, verdict.detail) == (Observation.REJECTED, "unsolved goals")


def test_a_sorry_is_a_rejection_whichever_way_lean_reports_it() -> None:
    by_warning = judge_result(lean_answer(("warning", "declaration uses 'sorry'")))
    by_list = judge_result(lean_answer(sorries=1))
    assert by_warning.observation is by_list.observation is Observation.REJECTED


@pytest.mark.parametrize(
    "text",
    [
        "aesop: failed to prove the goal after exhaustive search.",
        "linarith failed to find a contradiction",
        "unused variable `h`",
    ],
)
def test_a_warning_is_not_a_rejection_even_when_it_says_failed(text: str) -> None:
    assert judge_result(lean_answer(("warning", text))).observation is Observation.ACCEPTED


def test_the_word_sorry_in_an_info_message_is_not_a_sorry() -> None:
    answer = lean_answer(("info", "declaration uses 'sorry'"))
    assert judge_result(answer).observation is Observation.ACCEPTED


@pytest.mark.parametrize(
    "result",
    [
        lean_timeout(),
        {"time": 0.1, "error": "server_error: the worker died"},
        repl_error(),
        {"time": 0.1},
    ],
)
def test_no_definitive_answer_is_undecided(result: dict[str, Any]) -> None:
    verdict = judge_result(result)
    assert verdict.observation is Observation.UNDECIDED
    assert verdict.detail


def test_undecided_never_counts_as_behaving() -> None:
    assert behaved(Expectation.VERIFY, Observation.ACCEPTED)
    assert behaved(Expectation.REJECT, Observation.REJECTED)
    for expectation in Expectation:
        assert not behaved(expectation, Observation.UNDECIDED)
    assert not behaved(Expectation.VERIFY, Observation.REJECTED)
    assert not behaved(Expectation.REJECT, Observation.ACCEPTED)


def test_a_long_error_is_shortened_in_the_report() -> None:
    verdict = judge_result(lean_answer(("error", "x" * 5_000)))
    assert len(verdict.detail) == 300


def test_cases_are_loaded_in_name_order_with_their_imports_hoisted(cases_directory: Path) -> None:
    cases = load_cases(cases_directory)
    assert [(case.name, case.expectation) for case in cases] == [
        ("verify/two.lean", Expectation.VERIFY),
        ("reject/unfinished.lean", Expectation.REJECT),
        ("reject/wrong.lean", Expectation.REJECT),
    ]
    assert cases[0].code.startswith("import Mathlib\n/- A license. -/\n")
    assert cases[2].code == WRONG_PROOF


@pytest.mark.parametrize("missing", ["verify", "reject"])
def test_both_kinds_of_case_are_required(cases_directory: Path, missing: str) -> None:
    for path in (cases_directory / missing).iterdir():
        path.unlink()
    with pytest.raises(CasesError, match=f"{missing}: an admission test needs at least one"):
        load_cases(cases_directory)


def test_files_that_are_not_lean_files_are_not_cases(cases_directory: Path) -> None:
    (cases_directory / "verify" / "README.md").write_text("notes")
    assert len(load_cases(cases_directory)) == 3


async def test_an_honest_server_is_admitted(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lean = await start_lean_server(answering(honest_lean))
    exit_status, report = await admit(lean.url, cases_directory, key_file, capsys)

    assert exit_status == 0
    assert report["admitted"] is True
    assert report["server"] == lean.url
    assert report["counts"] == {
        "cases": 3,
        "behaved": 3,
        "misbehaved": 0,
        "verify": 1,
        "reject": 2,
        "accepted": 1,
        "rejected": 2,
        "undecided": 0,
    }
    assert report["checks_per_second"] > 0
    assert report["elapsed_seconds"] >= 0
    assert [case["case"] for case in report["cases"]] == [
        "verify/two.lean",
        "reject/unfinished.lean",
        "reject/wrong.lean",
    ]
    assert report["cases"][0] == {
        "case": "verify/two.lean",
        "expected": "accepted",
        "observed": "accepted",
        "behaved": True,
        "seconds": report["cases"][0]["seconds"],
        "lean_seconds": 0.25,
        "detail": "",
    }
    assert report["cases"][2]["detail"].startswith("The rfl tactic failed")


async def test_requests_are_plain_single_snippet_kimina_checks_with_the_key_from_the_file(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lean = await start_lean_server(answering(honest_lean))
    _, report = await admit(lean.url, cases_directory, key_file, capsys, "--timeout", "45")

    assert len(lean.requests) == 3
    sent_code: dict[str, str] = {}
    for request in lean.requests:
        assert set(request.body) == {"snippets", "timeout"}
        assert request.body["timeout"] == 45
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        (snippet,) = request.body["snippets"]
        sent_code[snippet["id"]] = snippet["code"]
    assert API_KEY not in json.dumps(report)
    assert sent_code["verify/two.lean"].startswith("import Mathlib\n")
    assert report["timeout_seconds"] == 45


async def test_a_server_without_a_key_is_tested_without_one(
    start_lean_server: StartLeanServer, cases_directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lean = await start_lean_server(answering(honest_lean), api_key=None)
    exit_status, _ = await admit(lean.url, cases_directory, None, capsys)
    assert exit_status == 0
    assert "Authorization" not in lean.requests[0].headers


async def test_a_server_that_rejects_a_correct_proof_is_refused(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wrong Mathlib: every file fails on its import."""
    lean = await start_lean_server(
        always(lean_answer(("error", "unknown module prefix 'Mathlib'")))
    )
    exit_status, report = await admit(lean.url, cases_directory, key_file, capsys)
    assert exit_status == 1
    assert report["admitted"] is False
    assert report["counts"]["misbehaved"] == 1
    assert report["cases"][0]["behaved"] is False
    assert report["cases"][0]["detail"] == "unknown module prefix 'Mathlib'"


async def test_a_server_that_accepts_everything_is_refused(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lean = await start_lean_server(always(lean_answer()))
    exit_status, report = await admit(lean.url, cases_directory, key_file, capsys)
    assert exit_status == 1
    assert report["counts"]["misbehaved"] == 2
    assert [case["behaved"] for case in report["cases"]] == [True, False, False]


async def test_a_reject_case_that_times_out_fails_admission_rather_than_passing_it(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def times_out_on_the_wrong_proof(code: str) -> SnippetReply:
        return lean_timeout() if "1 + 1 = 3" in code else honest_lean(code)

    lean = await start_lean_server(answering(times_out_on_the_wrong_proof))
    exit_status, report = await admit(lean.url, cases_directory, key_file, capsys)
    assert exit_status == 1
    timed_out = report["cases"][2]
    assert (timed_out["case"], timed_out["observed"], timed_out["behaved"]) == (
        "reject/wrong.lean",
        "undecided",
        False,
    )
    assert "timed out" in timed_out["detail"]
    assert report["counts"]["undecided"] == 1


@pytest.mark.parametrize("status", [500, 401, 429])
async def test_a_server_error_is_undecided_and_fails_admission(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    lean = await start_lean_server(failing_with(status, "the worker died"))
    exit_status, report = await admit(lean.url, cases_directory, key_file, capsys)
    assert exit_status == 1
    assert report["counts"]["undecided"] == 3
    assert report["cases"][0]["detail"] == f"HTTP {status}: the worker died"
    assert len(lean.requests) == 3  # each case is sent once: nothing is retried


async def test_a_recovered_tactic_failure_warning_does_not_fail_a_verify_case(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def warns_on_the_good_proof(code: str) -> SnippetReply:
        if "1 + 1 = 2 := by rfl" in code:
            return lean_answer(("warning", "aesop: failed to prove the goal"))
        return honest_lean(code)

    lean = await start_lean_server(answering(warns_on_the_good_proof))
    exit_status, _ = await admit(lean.url, cases_directory, key_file, capsys)
    assert exit_status == 0


async def test_a_reply_for_another_snippet_is_undecided(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def answers_for_someone_else(_snippet: dict[str, Any]) -> SnippetReply:
        return web.json_response({"results": [{"id": "someone-else", **lean_answer()}]})

    lean = await start_lean_server(answers_for_someone_else)
    exit_status, report = await admit(lean.url, cases_directory, key_file, capsys)
    assert exit_status == 1
    assert "different snippet" in report["cases"][0]["detail"]


async def test_a_server_that_cannot_be_reached_exits_with_status_three(
    start_lean_server: StartLeanServer,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lean = await start_lean_server()
    url = lean.url
    await lean.close()
    exit_status, report = await admit(url, cases_directory, key_file, capsys)
    assert exit_status == 3
    assert report["admitted"] is False
    assert "did not answer" in report["error"]
    assert lean.requests == []


async def test_concurrency_bounds_the_checks_in_flight(
    start_lean_server: StartLeanServer, tmp_path: Path
) -> None:
    async def slow(_snippet: dict[str, Any]) -> SnippetReply:
        await asyncio.sleep(0.05)
        return lean_answer()

    directory = tmp_path / "many"
    (directory / "verify").mkdir(parents=True)
    (directory / "reject").mkdir()
    for number in range(12):
        (directory / "verify" / f"case{number:02}.lean").write_text(f"-- {number}\n")
    (directory / "reject" / "wrong.lean").write_text(WRONG_PROOF)

    lean = await start_lean_server(slow)
    settings = AdmissionSettings(lean.url, API_KEY, timeout_seconds=60, concurrency=3)
    report = await run_admission(settings, load_cases(directory))
    assert lean.peak_in_flight == 3
    assert len(report.outcomes) == 13
    assert report.to_json()["concurrency"] == 3


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (["--cases", "{cases}"], "--server is required"),
        (["--server", "http://lean.example:8000"], "--cases is required"),
        (["--server", "lean.example:8000", "--cases", "{cases}"], "http:// or https://"),
        (["--server", "http://lean.example:8000", "--cases", "{missing}"], "no \\*.lean file"),
        (
            ["--server", "http://lean.example:8000", "--cases", "{cases}", "--timeout", "0"],
            "at least 1",
        ),
        (
            ["--server", "http://lean.example:8000", "--cases", "{cases}", "--concurrency", "x"],
            "not a whole number",
        ),
        (
            ["--server", "http://lean.example:8000", "--cases", "{cases}", "--api-key", "secret"],
            "unrecognized arguments",
        ),
        (
            [
                "--server",
                "http://lean.example:8000",
                "--cases",
                "{cases}",
                "--api-key-file",
                "{missing}",
            ],
            "cannot read the API key file",
        ),
    ],
)
def test_bad_arguments_exit_with_status_two(
    cases_directory: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    reason: str,
) -> None:
    filled = [
        argument.format(cases=cases_directory, missing=tmp_path / "missing")
        for argument in arguments
    ]
    with pytest.raises(SystemExit) as exit_information:
        main(filled, {})
    assert exit_information.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert re.search(reason, captured.err)


def test_an_empty_key_file_is_a_bad_argument(
    cases_directory: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty-key"
    empty.write_text("\n")
    arguments = ["--server", "http://lean.example:8000", "--cases", str(cases_directory)]
    with pytest.raises(SystemExit) as exit_information:
        main([*arguments, "--api-key-file", str(empty)], {})
    assert exit_information.value.code == 2
    assert "the API key is empty" in capsys.readouterr().err


def test_settings_can_come_from_the_environment(
    cases_directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = {
        "LEANPOOL_ADMIT_SERVER": "not-a-url",
        "LEANPOOL_ADMIT_CASES": str(cases_directory),
    }
    with pytest.raises(SystemExit):
        main([], environment)
    assert "got 'not-a-url'" in capsys.readouterr().err
