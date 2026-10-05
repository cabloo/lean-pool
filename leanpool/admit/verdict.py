"""Judging what a Lean server said about one admission case."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from leanpool.admit.cases import Expectation
from leanpool.kimina import ERROR_SEVERITY, IndefiniteResultError, LeanAnswer, read_lean_answer

_DETAIL_LIMIT = 300


class Observation(enum.StrEnum):
    """What the server's answer amounts to."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNDECIDED = "undecided"


@dataclass(frozen=True)
class Verdict:
    """An observation and the evidence for it (an error message, or why nothing was decided)."""

    observation: Observation
    detail: str = ""


def judge_result(result: Mapping[str, Any]) -> Verdict:
    """Classify one check result as accepted, rejected or undecided.

    * rejected: Lean printed a message of severity ``error``, or the file relies on ``sorry``;
    * accepted: a definitive Lean answer with neither;
    * undecided: the server gave no definitive Lean answer (a Lean timeout, a server error, the
      REPL refusing the command).

    A warning is not a rejection, even one containing the word "failed": a tactic that fails
    inside a combinator that recovers (``first | simp | rfl``) warns while the proof is
    complete. Only error-severity messages and ``sorry`` reject.
    """
    try:
        answer = read_lean_answer(result)
    except IndefiniteResultError as error:
        return undecided(str(error))
    if answer.has_errors:
        return Verdict(Observation.REJECTED, _shorten(_first_error(answer)))
    if answer.uses_sorry:
        return Verdict(Observation.REJECTED, "the file relies on 'sorry'")
    return Verdict(Observation.ACCEPTED)


def undecided(reason: str) -> Verdict:
    """The verdict for a case the server gave no definitive answer to."""
    return Verdict(Observation.UNDECIDED, _shorten(reason))


def expected_observation(expectation: Expectation) -> Observation:
    """What a correct server is observed to do with a case of this kind."""
    return Observation.ACCEPTED if expectation is Expectation.VERIFY else Observation.REJECTED


def behaved(expectation: Expectation, observation: Observation) -> bool:
    """Whether the server did what the case requires.

    Undecided never counts as behaving, for either kind. A ``reject`` case that times out has
    not been rejected, and admitting a server on the strength of answers it did not give is
    how a server that is merely slow or broken would pass.
    """
    return observation is expected_observation(expectation)


def _first_error(answer: LeanAnswer) -> str:
    return next(message.text for message in answer.messages if message.severity == ERROR_SEVERITY)


def _shorten(text: str) -> str:
    return text if len(text) <= _DETAIL_LIMIT else text[: _DETAIL_LIMIT - 1] + "…"
