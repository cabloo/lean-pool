"""What a joining box sends and what the pool answers, each validated before it is passed on.

The join service executes nothing: it writes what a box sent into its spool directory and
serves what the pool's operator side put there. Validating here keeps nonsense out of the spool
and gives a box a clear refusal at once. It is not what the pool relies on: whoever acts on a
request validates every field again.

Everything in this module is a pure function of parsed JSON, so each rule is tested without a
server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from leanpool.haproxy.servers import LeanServer, ServerListError
from leanpool.haproxy.tls import TlsSettingsError, tls_identity

SIGNING_REQUEST_FIELDS = ("name", "lean_port", "agent_port", "workers", "csr")
_PEM_LABELS = ("CERTIFICATE REQUEST", "NEW CERTIFICATE REQUEST")
# Only checked by the server-list rules, never dialled: a documentation address (RFC 5737).
_PLACEHOLDER_HOST = "192.0.2.1"


class InvalidMessageError(ValueError):
    """A box's request, or the pool's answer in the spool, does not have the expected shape."""


@dataclass(frozen=True)
class SigningRequest:
    """What a box asks to join with: its server's name, ports and workers, and its request."""

    name: str
    lean_port: int
    agent_port: int
    workers: int
    csr: str

    def record(self, address: str) -> dict[str, Any]:
        """The request as it is written to the spool, with the address it came from."""
        return {
            "name": self.name,
            "lean_port": self.lean_port,
            "agent_port": self.agent_port,
            "workers": self.workers,
            "csr": self.csr,
            "address": address,
        }


@dataclass(frozen=True)
class CertificateAnswer:
    """The pool's answer to a signing request: a certificate with the authority's, or a refusal."""

    certificate: str | None = None
    authority: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Verdict:
    """The outcome of the admission test: whether the server joined, and what was found."""

    admitted: bool
    detail: str


def parse_json(text: bytes) -> object:
    """Read JSON, refusing anything that is not valid JSON (or is nested too deeply to read)."""
    try:
        parsed: object = json.loads(text)
    except (ValueError, RecursionError):
        raise InvalidMessageError("the body is not valid JSON") from None
    return parsed


def parse_signing_request(body: object) -> SigningRequest:
    """Validate a box's signing request: exactly the five fields, each of the right kind.

    The name, the ports and the worker count obey the rules of the server list, because that is
    where they are headed; the name must also be one a certificate can carry.
    """
    if not isinstance(body, dict) or set(body) != set(SIGNING_REQUEST_FIELDS):
        raise InvalidMessageError(
            f"the request must be a JSON object with exactly {', '.join(SIGNING_REQUEST_FIELDS)}"
        )
    name, csr = body["name"], body["csr"]
    if not isinstance(name, str):
        raise InvalidMessageError("name must be a string")
    numbers = {field: body[field] for field in ("lean_port", "agent_port", "workers")}
    for field, value in numbers.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidMessageError(f"{field} must be a whole number")
    try:
        LeanServer(
            name=name,
            host=_PLACEHOLDER_HOST,
            port=numbers["lean_port"],
            workers=numbers["workers"],
            agent_port=numbers["agent_port"],
        )
        tls_identity(name)
    except (ServerListError, TlsSettingsError) as error:
        raise InvalidMessageError(str(error)) from error
    if numbers["lean_port"] == numbers["agent_port"]:
        raise InvalidMessageError("lean_port and agent_port must differ")
    return SigningRequest(name=name, csr=_validate_request_text(csr), **numbers)


def parse_certificate_answer(body: object) -> CertificateAnswer:
    """Validate the pool's answer: ``{certificate, ca}`` when signed, ``{reason}`` when refused."""
    if isinstance(body, dict) and _is_text(body.get("reason")):
        return CertificateAnswer(reason=body["reason"])
    if isinstance(body, dict) and _is_text(body.get("certificate")) and _is_text(body.get("ca")):
        return CertificateAnswer(certificate=body["certificate"], authority=body["ca"])
    raise InvalidMessageError("expected {certificate, ca} or {reason}, each a non-empty string")


def parse_verdict(body: object) -> Verdict:
    """Validate the pool's verdict: ``{admitted, detail}``."""
    if (
        isinstance(body, dict)
        and isinstance(body.get("admitted"), bool)
        and isinstance(body.get("detail"), str)
    ):
        return Verdict(admitted=body["admitted"], detail=body["detail"])
    raise InvalidMessageError("expected {admitted, detail}: a boolean and a string")


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _validate_request_text(csr: object) -> str:
    """Refuse anything that is not one PEM signing request. Whether it is a good one is for
    the pool's certificate authority to judge when it is asked to sign it.
    """
    if not isinstance(csr, str) or not csr.isascii():
        raise InvalidMessageError("csr must be a PEM signing request")
    text = csr.strip()
    is_one_request = any(
        text.startswith(f"-----BEGIN {label}-----\n") and text.endswith(f"\n-----END {label}-----")
        for label in _PEM_LABELS
    )
    if not is_one_request or text.count("-----BEGIN ") != 1:
        raise InvalidMessageError("csr must be one PEM signing request")
    return csr
