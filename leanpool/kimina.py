"""Reading one element of a Kimina Lean Server ``/api/check`` reply.

Kimina answers ``{"results": [result, ...]}``. A result carries exactly one of:

* ``error``: the server could not give Lean's answer. A Lean timeout arrives here (as HTTP 200),
  with the text ``Lean REPL command timed out in N seconds`` or ``Lean REPL header command
  timed out in N seconds``.
* ``response``: what the Lean REPL said. Either Lean's own output (``messages``, ``sorries``,
  ``env``) or, when the REPL itself rejected the command, a bare ``{"message": "..."}``.

Only the first shape of ``response`` is Lean's verdict on the code. Both the cache (what may be
stored) and the admission test (what counts as decided) need that distinction, so it lives here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ERROR_SEVERITY = "error"
WARNING_SEVERITY = "warning"
SORRY_WARNING_TEXT = "declaration uses 'sorry'"


class IndefiniteResultError(ValueError):
    """The result is not a definitive Lean answer. The message says what it is instead."""


@dataclass(frozen=True)
class LeanMessage:
    """One message Lean printed while checking a file."""

    severity: str
    text: str


@dataclass(frozen=True)
class LeanAnswer:
    """Lean's own output for one checked file."""

    messages: tuple[LeanMessage, ...]
    has_sorries: bool

    @property
    def has_errors(self) -> bool:
        """Whether Lean printed at least one message of severity ``error``."""
        return any(message.severity == ERROR_SEVERITY for message in self.messages)

    @property
    def uses_sorry(self) -> bool:
        """Whether Lean accepted the file only by assuming a ``sorry``.

        Lean reports a sorry twice, as an entry in ``sorries`` and as a warning; either is enough.
        """
        return self.has_sorries or any(
            message.severity == WARNING_SEVERITY and SORRY_WARNING_TEXT in message.text
            for message in self.messages
        )


def read_lean_answer(result: Mapping[str, Any]) -> LeanAnswer:
    """Return Lean's output from one result, or raise ``IndefiniteResultError``.

    A result is indefinite when the server reported an ``error`` (a timeout or a crashed
    worker), when it has no ``response``, or when the REPL rejected the command before Lean
    judged the code. None of those says anything about the code itself, and a retry could change
    every one of them.
    """
    error = result.get("error")
    if error is not None:
        raise IndefiniteResultError(f"the server reported an error: {error}")
    response = result.get("response")
    if not isinstance(response, Mapping):
        raise IndefiniteResultError("the result has neither an error nor a response")
    if "message" in response and "messages" not in response:
        raise IndefiniteResultError(f"the REPL rejected the command: {response['message']}")
    return LeanAnswer(
        messages=_read_messages(response.get("messages")),
        has_sorries=bool(response.get("sorries")),
    )


def _read_messages(raw_messages: object) -> tuple[LeanMessage, ...]:
    if raw_messages is None:
        return ()
    if not isinstance(raw_messages, list):
        raise IndefiniteResultError("the response's messages are not a list")
    return tuple(_read_message(raw_message) for raw_message in raw_messages)


def _read_message(raw_message: object) -> LeanMessage:
    if not isinstance(raw_message, Mapping):
        raise IndefiniteResultError("a message in the response is not an object")
    return LeanMessage(
        severity=str(raw_message.get("severity", "")),
        text=str(raw_message.get("data", "")),
    )
