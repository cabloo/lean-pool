"""The admission cases: Lean files a server must accept, and Lean files it must reject."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from leanpool.admit.header import hoist_imports

_LEAN_FILES = "*.lean"


class CasesError(ValueError):
    """The cases directory cannot be used for an admission test."""


class Expectation(enum.StrEnum):
    """What a correct server does with a case. The value is the case's directory name."""

    VERIFY = "verify"
    REJECT = "reject"


@dataclass(frozen=True)
class AdmissionCase:
    """One Lean file and what a correct server must say about it.

    ``name`` is the file's path inside the cases directory (``verify/two_plus_two.lean``); it is
    also the snippet id sent to the server. ``code`` already has its imports hoisted.
    """

    name: str
    expectation: Expectation
    code: str


def load_cases(directory: Path) -> tuple[AdmissionCase, ...]:
    """Read ``verify/*.lean`` and ``reject/*.lean`` from ``directory``, in name order.

    Both kinds are required. A test with only ``verify`` cases would admit a server that accepts
    everything, and one with only ``reject`` cases a server that rejects everything: each kind
    is the control that shows the other kind's result means something.
    """
    cases = tuple(
        case for expectation in Expectation for case in _load_kind(directory, expectation)
    )
    for expectation in Expectation:
        if all(case.expectation is not expectation for case in cases):
            raise CasesError(
                f"no {_LEAN_FILES} file in {directory / expectation.value}: an admission test "
                "needs at least one case to verify and one to reject"
            )
    return cases


def _load_kind(directory: Path, expectation: Expectation) -> list[AdmissionCase]:
    paths = sorted((directory / expectation.value).glob(_LEAN_FILES))
    return [_load_case(path, expectation) for path in paths]


def _load_case(path: Path, expectation: Expectation) -> AdmissionCase:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CasesError(f"cannot read the case {path}: {error}") from error
    return AdmissionCase(
        name=f"{expectation.value}/{path.name}",
        expectation=expectation,
        code=hoist_imports(source),
    )
