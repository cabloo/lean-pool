"""Which results may be stored: only an answer that Lean would give again."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from leanpool.kimina import IndefiniteResultError, read_lean_answer

DEFAULT_EXHAUSTION_PATTERNS = ("out of memory", "stack overflow")


def is_storable(result: Mapping[str, Any], exhaustion_patterns: Sequence[str]) -> bool:
    """Return whether a result is a definitive Lean answer that a retry could not change.

    Stored: Lean's own output for the code, whether it accepted the proof or reported errors.
    A wrong proof is wrong every time, and failed attempts are most of what a prover sends.

    Never stored, because the same check could come out differently next time:

    * a result with an ``error``: a Lean timeout (the box may have been busy) or a crashed worker;
    * a result where the REPL rejected the command before Lean judged the code;
    * an answer in which any message reports resource exhaustion (``exhaustion_patterns``,
      matched without regard to case): the worker ran out of memory or stack, which depends on
      what else the box was doing, not on the proof.

    Storing a wrong "failed" would silently turn a provable statement into a permanent failure;
    not storing only costs a recheck. So every doubt is resolved towards not storing.
    """
    try:
        answer = read_lean_answer(result)
    except IndefiniteResultError:
        return False
    lowered_patterns = [pattern.lower() for pattern in exhaustion_patterns]
    return not any(
        pattern in message.text.lower()
        for message in answer.messages
        for pattern in lowered_patterns
    )
