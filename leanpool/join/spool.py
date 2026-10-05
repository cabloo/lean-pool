"""The spool directory: the only thing the join service and the pool's operator side share.

::

    <spool>/requests/csr.json            written by the service, once per window
    <spool>/requests/ready.json          written by the service, once per window
    <spool>/responses/certificate.json   written by the operator side, read by the service
    <spool>/responses/verdict.json       written by the operator side, read by the service

The service only ever creates the two request files and reads the two response files. It keeps
no state of its own: what has happened in a window is what is in the spool, so a restarted
service carries on where it was.

A request file appears complete or not at all (``leanpool.atomic``) and is never changed once
it exists, so the operator side can act on it the moment it sees it. Response files should be
written the same way (a temporary name, then a rename): one that is not valid JSON yet is
treated as not there yet.
"""

from __future__ import annotations

import enum
import json
import os
from pathlib import Path
from typing import Any

from leanpool.atomic import write_atomically
from leanpool.join.messages import (
    CertificateAnswer,
    InvalidMessageError,
    Verdict,
    parse_certificate_answer,
    parse_json,
    parse_verdict,
)

REQUESTS_DIRECTORY = "requests"
RESPONSES_DIRECTORY = "responses"
SIGNING_REQUEST_FILE = "csr.json"
READY_FILE = "ready.json"
CERTIFICATE_FILE = "certificate.json"
VERDICT_FILE = "verdict.json"
_REQUEST_FILE_MODE = 0o644
_ADDRESS_FIELD = "address"
# What reading a response file gives while the file is not there (any JSON value may be).
_ABSENT = object()


class SpoolError(Exception):
    """The spool cannot be used, or holds something the service cannot make sense of."""


class Stored(enum.Enum):
    """What became of a request that was to be written to the spool."""

    NEW = "new"
    SAME = "same"  # the identical request is already there: a repeat, not a conflict
    DIFFERENT = "different"


class Spool:
    """The request files the service writes and the response files it reads."""

    def __init__(self, directory: Path) -> None:
        self._requests = directory / REQUESTS_DIRECTORY
        self._responses = directory / RESPONSES_DIRECTORY

    def prepare(self) -> None:
        """Make sure requests can be written; fail now rather than on a box's first request."""
        try:
            self._requests.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SpoolError(
                f"cannot create {self._requests}: {error.strerror or error}"
            ) from error
        if not os.access(self._requests, os.W_OK | os.X_OK):
            raise SpoolError(f"cannot write requests into {self._requests}")

    def bound_address(self) -> str | None:
        """The address the window is bound to: the sender of its signing request, if any."""
        record = self._read_request(SIGNING_REQUEST_FILE)
        if record is None:
            return None
        address = record.get(_ADDRESS_FIELD)
        if not isinstance(address, str) or not address:
            raise SpoolError(f"{SIGNING_REQUEST_FILE} in the spool names no address")
        return address

    def store_signing_request(self, record: dict[str, Any]) -> Stored:
        """Write the window's signing request, unless one is already there."""
        return self._store(SIGNING_REQUEST_FILE, record)

    def store_ready(self, record: dict[str, Any]) -> Stored:
        """Write that the box is ready for its admission test, unless that is already there."""
        return self._store(READY_FILE, record)

    def certificate_answer(self) -> CertificateAnswer | None:
        """The pool's answer to the signing request, or None while there is none."""
        body = self._read_response(CERTIFICATE_FILE)
        if body is _ABSENT:
            return None
        try:
            return parse_certificate_answer(body)
        except InvalidMessageError as error:
            raise SpoolError(f"{CERTIFICATE_FILE} in the spool is not usable: {error}") from error

    def verdict(self) -> Verdict | None:
        """The pool's verdict, or None while there is none."""
        body = self._read_response(VERDICT_FILE)
        if body is _ABSENT:
            return None
        try:
            return parse_verdict(body)
        except InvalidMessageError as error:
            raise SpoolError(f"{VERDICT_FILE} in the spool is not usable: {error}") from error

    def _store(self, name: str, record: dict[str, Any]) -> Stored:
        path = self._requests / name
        data = (json.dumps(record, indent=2) + "\n").encode("utf-8")
        try:
            write_atomically(path, data, _REQUEST_FILE_MODE)
        except FileExistsError:
            return Stored.SAME if self._read_request(name) == record else Stored.DIFFERENT
        except OSError as error:
            raise SpoolError(f"cannot write {name}: {error.strerror or error}") from error
        return Stored.NEW

    def _read_request(self, name: str) -> dict[str, Any] | None:
        """Read back a request file this service wrote; a damaged one is an error."""
        try:
            data = (self._requests / name).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SpoolError(f"cannot read {name}: {error.strerror or error}") from error
        try:
            record = parse_json(data)
        except InvalidMessageError:
            raise SpoolError(f"{name} in the spool is not valid JSON") from None
        if not isinstance(record, dict):
            raise SpoolError(f"{name} in the spool is not a JSON object")
        return record

    def _read_response(self, name: str) -> object:
        """Read a response file. One that is missing, or not complete JSON yet, is absent."""
        try:
            data = (self._responses / name).read_bytes()
        except FileNotFoundError:
            return _ABSENT
        except OSError as error:
            raise SpoolError(f"cannot read {name}: {error.strerror or error}") from error
        try:
            return parse_json(data)
        except InvalidMessageError:
            return _ABSENT
