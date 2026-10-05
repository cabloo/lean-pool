"""The admission test a Lean server must pass before it joins a pool."""

from leanpool.admit.cases import AdmissionCase, CasesError, Expectation, load_cases
from leanpool.admit.header import hoist_imports
from leanpool.admit.runner import (
    AdmissionReport,
    AdmissionSettings,
    CaseOutcome,
    ServerUnreachableError,
    run_admission,
)
from leanpool.admit.tls import AdmissionTls, AdmissionTlsError, load_admission_tls
from leanpool.admit.verdict import Observation, Verdict, judge_result

__all__ = [
    "AdmissionCase",
    "AdmissionReport",
    "AdmissionSettings",
    "AdmissionTls",
    "AdmissionTlsError",
    "CaseOutcome",
    "CasesError",
    "Expectation",
    "Observation",
    "ServerUnreachableError",
    "Verdict",
    "hoist_imports",
    "judge_result",
    "load_admission_tls",
    "load_cases",
    "run_admission",
]
