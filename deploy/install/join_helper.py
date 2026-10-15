#!/usr/bin/env python3
"""join_helper.py - every decision of a lean-pool join, as pure functions behind a small command line.

install-pool.sh installs it to /usr/local/lib/<SITE_PREFIX>/join_helper.py (root-owned), where
lean-pool-ctl's `join-open` runs it with the host's python3. It is also part of the join bundle: on a
new box the join script runs it with the POOL IMAGE's Python, because a new box may have neither
python3 nor jq. Standard library only, except `certificate-names`, which needs `cryptography` (the
pool image has it).

Bash does the orchestration (Docker, files, waiting). Whatever decides something is here, so that it
is tested without root and without Docker (tests/install/test_install_join_helper.py):

  on the pool's host   request | certificate-response | refuse | ready | verdict | lookup |
                       choose-address | admission-detail | script |
                       certificate-names (in the pool image) | image-manifest | image-export
  on the new box       workers | csr-body | certificate | verdict-of | reason | image-plan

Nothing here prints a private key, the pool's API key or a window's token; none of them is ever
taken as an argument (a command line is visible to every user of the machine): they are read from
files.
"""  # noqa: E501

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

# The door's own patterns for `add-backend` (kimina-ctl and lean-pool-ctl); a test holds the three
# files to the same text. A join request is validated with them before anything is signed or added.
# A server's name is also the name on its certificate: ONE DNS label in lower case, at most 40
# characters.
# No dot: `NAME.lan` and the like are names of hosts, and one of them is the pool's own front door.
SERVER_NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$"
SERVER_ADDRESS_PATTERN = r"^[A-Za-z0-9.-]{1,120}:[0-9]{2,5}$"
COUNT_PATTERN = r"^[0-9]{1,3}$"
PORT_PATTERN = r"^[0-9]{2,5}$"
LAN_DOMAIN_PATTERN = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
# Reserved whatever the pool's certificates say: every machine answers to it.
ALWAYS_RESERVED = ("localhost",)

MAXIMUM_WORKERS = 256
MAXIMUM_PORT = 65535
MAXIMUM_REQUEST_BYTES = 64 * 1024
MAXIMUM_CSR_BYTES = 16 * 1024
MAXIMUM_REPLY_BYTES = 256 * 1024
BYTES_PER_WORKER = 4 * 1024**3
RESERVED_CORES = 2
TEXT_LIMIT = 300
NOT_ADMITTED = 4  # `verdict-of`: a readable verdict that says no (1 is "no readable verdict")
NOT_FOR_THIS_BOX = (
    4  # `image-plan`: a readable manifest of an image this box cannot use (1: unreadable)
)

# The Lean server image a pool's host exports for joining boxes. The manifest is written on
# the pool's host and read on a box, where it is text from another machine like any other.
IMAGE_FILE_PATTERN = r"^image-[0-9a-f]{12}\.tar$"  # what leanpool-join serves, and nothing else
IMAGE_TAG_PATTERN = r"^[a-z0-9][a-z0-9._/-]{0,120}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
IMAGE_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MACHINE_PATTERN = r"^[A-Za-z0-9_]{1,24}$"  # `uname -m`
IMAGE_FORMAT = "docker-save"
MAXIMUM_IMAGE_BYTES = 200 * 1024**3
MAXIMUM_MANIFEST_BYTES = 16 * 1024
IMAGE_MANIFEST_FIELDS = (
    "tag",
    "image_id",
    "layers",
    "file",
    "sha256",
    "bytes",
    "machine",
    "format",
    "docker",
)

REQUEST_FIELDS = ("name", "lean_port", "agent_port", "workers", "csr", "address")
CSR_BODY_FIELDS = ("name", "lean_port", "agent_port", "workers", "csr")
_CSR_LABELS = ("CERTIFICATE REQUEST", "NEW CERTIFICATE REQUEST")
_CERTIFICATE_LABEL = "CERTIFICATE"
_API_KEY_PATTERN = r"^[A-Za-z0-9]{16,128}$"  # what install.sh --api-key-file accepts
_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{22,128}$"  # what leanpool-join accepts
_PIN_PATTERN = r"^sha256//[A-Za-z0-9+/]{43}=$"  # what `leanpool-pki pin` prints
_POOL_PATTERN = r"^[a-z0-9]{1,16}$"
_SECONDS_PATTERN = r"^[0-9]{1,5}$"
_BUNDLE_PATTERN = r"^[A-Za-z0-9+/=\n]+$"
_BUNDLE_MARKER = "LEANPOOL_BUNDLE_END"


class Refused(ValueError):  # noqa: N818
    """A request, a reply or a setting was refused. The message says why, and holds no secret."""


# ------------------------------------------------------------------------------------------- text


def one_line(text: object, limit: int = TEXT_LIMIT) -> str:
    """Whatever came from the other machine, made safe to print: one line of printable ASCII,
    capped.

    Nothing else reaches a terminal or a reply: no escape sequence, no control character, and no
    letter of another alphabet that reads like a Latin one.
    """
    if not isinstance(text, str):
        return "(not text)"
    cleaned = "".join(character if " " <= character <= "~" else " " for character in text)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _matches(pattern: str, text: str) -> bool:
    return re.fullmatch(pattern.strip("^$"), text) is not None


def _whole_number(value: object, what: str, pattern: str, low: int, high: int) -> int:
    """A JSON whole number that the door's pattern for it and its range both accept."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise Refused(f"{what} must be a whole number")
    if not _matches(pattern, str(value)) or not low <= value <= high:
        raise Refused(f"{what} must be between {low} and {high}")
    return value


def is_single_pem(text: object, labels: tuple[str, ...], limit: int) -> bool:
    """Whether ``text`` is exactly one PEM block with one of ``labels``, within ``limit`` bytes."""
    if not isinstance(text, str) or not text.isascii() or len(text) > limit:
        return False
    stripped = text.strip()
    if stripped.count("-----BEGIN ") != 1:
        return False
    return any(
        stripped.startswith(f"-----BEGIN {label}-----\n")
        and stripped.endswith(f"\n-----END {label}-----")
        for label in labels
    )


# ------------------------------------------------------------------------------ the pool's host


@dataclass(frozen=True)
class JoinRequest:
    """A box's request to join, every field validated."""

    name: str
    address: str
    lean_port: int
    agent_port: int
    workers: int
    csr: str


def listed_servers(servers_text: str) -> list[tuple[str, str]]:
    """The (name, host:port) pairs of a server list; comments and blank lines are skipped."""
    servers = []
    for line in servers_text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2:
            servers.append((fields[0], fields[1]))
    return servers


def reserved_names(text: str) -> set[str]:
    """The names no box may join under, from `certificate-names` output and the pool's settings: one
    a line.

    They are the pool's OWN names (on the front door's certificate, the join host, the proxy's
    client certificate), read from what is installed. A certificate for one of them would pass for
    the pool itself to every client that trusts the pool's authority. Compared without regard to
    letter case.
    """
    return {line.strip().lower() for line in text.splitlines() if line.strip()} | set(
        ALWAYS_RESERVED
    )


def validate_request(record: object, servers_text: str, reserved: set[str]) -> JoinRequest:
    """Validate what the join service wrote for a box's signing request, before anything is signed.

    Every field is checked with the door's patterns for `add-backend`; the name must be one DNS
    label, must not be one of the pool's own names (``reserved``), and neither the name nor the
    address may already be in the server list. A refusal never quotes a value that failed its
    pattern.
    """
    if not isinstance(record, dict) or set(record) != set(REQUEST_FIELDS):
        raise Refused(f"a join request must hold exactly: {', '.join(REQUEST_FIELDS)}")
    name, address, csr = record["name"], record["address"], record["csr"]
    if not isinstance(name, str) or not _matches(SERVER_NAME_PATTERN, name):
        raise Refused(
            "the server name is also the name on its certificate, so it must be ONE DNS label in "
            "lower case: letters, digits and '-', no dot, at most 40 characters"
        )
    if name.lower() in {reserved_name.lower() for reserved_name in reserved} | set(ALWAYS_RESERVED):
        raise Refused(
            f"'{name}' is a name of the pool itself (its front door, its join host or its proxy) "
            f"or of every machine: a certificate for it would pass for the pool. Join under "
            f"another --name"
        )
    lean_port = _whole_number(record["lean_port"], "lean_port", PORT_PATTERN, 1, MAXIMUM_PORT)
    agent_port = _whole_number(record["agent_port"], "agent_port", PORT_PATTERN, 1, MAXIMUM_PORT)
    workers = _whole_number(record["workers"], "workers", COUNT_PATTERN, 1, MAXIMUM_WORKERS)
    if lean_port == agent_port:
        raise Refused("lean_port and agent_port must differ")
    if not isinstance(address, str) or not _is_ipv4(address):
        raise Refused("the caller's address is not an IPv4 address")
    if not _matches(SERVER_ADDRESS_PATTERN, f"{address}:{lean_port}"):
        raise Refused("the caller's address and port are not a HOST:PORT the door accepts")
    if not is_single_pem(csr, _CSR_LABELS, MAXIMUM_CSR_BYTES):
        raise Refused("csr must be one PEM signing request")
    for listed_name, listed_address in listed_servers(servers_text):
        if listed_name.lower() == name.lower():
            raise Refused(
                f"a server named '{name}' is already in the pool; remove it first "
                f"(lean-pool-ctl POOL remove-backend {name}) or join under another --name"
            )
        if listed_address == f"{address}:{lean_port}":
            raise Refused(
                f"{address}:{lean_port} is already in the pool as '{one_line(listed_name, 40)}'"
            )
    return JoinRequest(
        name=name,
        address=address,
        lean_port=lean_port,
        agent_port=agent_port,
        workers=workers,
        csr=csr,
    )


def _is_ipv4(text: str) -> bool:
    try:
        return not ipaddress.IPv4Address(text).is_unspecified
    except ipaddress.AddressValueError:
        return False


def validate_ready(record: object, caller: str) -> None:
    """The ready notice must come from the address the signing request came from."""
    if not isinstance(record, dict) or record.get("address") != caller:
        raise Refused("the ready notice did not come from the address that asked to join")


def certificate_response(certificate: str, authority: str) -> dict[str, str]:
    """What `responses/certificate.json` holds once the request is signed (the service adds the API
    key)."""
    for what, text in (
        ("the signed certificate", certificate),
        ("the authority's certificate", authority),
    ):
        if not is_single_pem(text, (_CERTIFICATE_LABEL,), MAXIMUM_REPLY_BYTES):
            raise Refused(f"{what} is not one PEM certificate")
    return {"certificate": certificate, "ca": authority}


def refusal_response(reason: str) -> dict[str, str]:
    """What `responses/certificate.json` holds when the request is refused."""
    return {"reason": one_line(reason) or "refused"}


def verdict_response(admitted: bool, detail: str) -> dict[str, object]:
    """What `responses/verdict.json` holds at the end of the admission test."""
    return {"admitted": bool(admitted), "detail": one_line(detail, 600)}


def parse_lookup(text: str) -> dict[str, set[str]]:
    """Read `lookup` output back: one `NAME ADDRESS...` line per name."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        fields = line.split()
        if fields:
            found.setdefault(fields[0], set()).update(fields[1:])
    return found


def choose_address(
    name: str,
    caller: str,
    host_lookup: dict[str, set[str]],
    proxy_lookup: dict[str, set[str]],
    domain: str,
) -> tuple[str, str]:
    """Where the pool will dial the box: a name if one leads to the caller, otherwise the caller's
    address.

    `NAME.DOMAIN` (the LAN's domain, the site's SITE_LAN_DOMAIN) and then `NAME` are tried. A name
    is used only if BOTH this host's resolver (the admission test dials through it) and the proxy's
    (HAProxy dials through it; it cannot see this host's /etc/hosts) map it to the address the
    request came from. Returns (host, note); the note is a warning when the address had to be used,
    because an address can change.
    """
    if not re.fullmatch(LAN_DOMAIN_PATTERN, domain):
        raise Refused(f"the LAN's domain must be a DNS name in lower case, not {domain!r}")
    for candidate in (f"{name}.{domain}", name):
        if caller in host_lookup.get(candidate, set()) and caller in proxy_lookup.get(
            candidate, set()
        ):
            return candidate, f"{candidate} resolves to {caller}, here and for the proxy"
    return caller, (
        f"WARNING: neither {name}.{domain} nor {name} resolves to {caller} for both this host "
        f"and the proxy, so the server is listed by its address, which can change (DHCP); give "
        f"the box a fixed address or a LAN name, then remove it and join again"
    )


def lookup(names: list[str]) -> dict[str, set[str]]:
    """The IPv4 addresses each name resolves to, through the resolver of whoever runs this."""
    found: dict[str, set[str]] = {}
    for name in names:
        try:
            answers = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            answers = []
        found[name] = {answer[4][0] for answer in answers}
    return found


def certificate_names(pem: bytes) -> list[str]:
    """The names a certificate answers to: its common name and its alternative DNS names.

    Needs the `cryptography` package, so it is run with the POOL IMAGE's Python (the host may not
    have it). A file holding a certificate followed by its key works: the first certificate is read.
    """
    from cryptography import x509  # only here: everything else in this file is standard library

    certificate = x509.load_pem_x509_certificate(pem)
    names = [
        attribute.value
        for attribute in certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        if isinstance(attribute.value, str)
    ]
    try:
        alternative = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return names
    return names + list(alternative.get_values_for_type(x509.DNSName))


def admission_detail(report_text: str, exit_code: int) -> str:
    """One line saying how `leanpool-admit` ended, in its own words where it gave any.

    Status 3 is "unreachable, or the TLS handshake failed", and the report's `error` says which
    (wrong name, wrong authority, expired, client certificate refused): that text is passed on
    verbatim.
    """
    try:
        report = json.loads(report_text)
    except ValueError:
        report = None
    if not isinstance(report, dict):
        return f"the admission test gave no report (status {exit_code})"
    if isinstance(report.get("error"), str):
        return one_line(report["error"], 600)
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    total, misbehaved = counts.get("cases"), counts.get("misbehaved")
    if exit_code == 0 and report.get("admitted") is True:
        return f"all {total} admission cases behaved"
    cases = [
        case
        for case in report.get("cases") or []
        if isinstance(case, dict) and case.get("behaved") is False
    ]
    shown = "; ".join(
        f"{one_line(case.get('case'), 80)} expected {one_line(case.get('expected'), 20)}, "
        f"observed {one_line(case.get('observed'), 20)}"
        + (f" ({one_line(case.get('detail'), 120)})" if case.get("detail") else "")
        for case in cases[:3]
    )
    more = f"; and {len(cases) - 3} more" if len(cases) > 3 else ""
    return f"{misbehaved} of {total} admission cases misbehaved (status {exit_code}): {shown}{more}"


@dataclass(frozen=True)
class ScriptSettings:
    """What a window's join script is made with. The token is part of the URL, and nothing
    else is secret."""

    url_base: str
    token: str
    pin: str
    pool: str
    lean_timeout: str
    server_wait: str


def join_script(settings: ScriptSettings, body: str, bundle: str) -> str:
    """The script a new box downloads: five settings, the body (join-box.sh) and the installer
    bundle.

    Every value is written into shell code, so each is held to a pattern that leaves no room for a
    quote. The script's LAST line is the call of `main`, so a download cut short runs nothing.
    """
    url = re.fullmatch(r"https://([A-Za-z0-9.-]{1,120}):([0-9]{2,5})", settings.url_base)
    if url is None or not 1 <= int(url.group(2)) <= MAXIMUM_PORT:
        raise Refused("the pool's join address must be https://HOST:PORT")
    for what, pattern, value in (
        ("the token", _TOKEN_PATTERN, settings.token),
        ("the pin", _PIN_PATTERN, settings.pin),
        ("the pool's name", _POOL_PATTERN, settings.pool),
        ("the Lean timeout", _SECONDS_PATTERN, settings.lean_timeout),
        ("the server wait", _SECONDS_PATTERN, settings.server_wait),
    ):
        if not _matches(pattern, value):
            raise Refused(f"{what} is malformed")  # never quoted: the token is a secret
    if "\nmain() {" not in body or _BUNDLE_MARKER in body:
        raise Refused(
            "the join script's body must define main() and must not contain the bundle marker"
        )
    if not _matches(_BUNDLE_PATTERN, bundle) or not bundle.strip():
        raise Refused("the bundle must be base64 text")
    return "".join(
        [
            "#!/usr/bin/env bash\n",
            f"# The join script of lean-pool '{settings.pool}', made for ONE join window by "
            "`lean-pool-ctl join-open`.\n",
            "# It holds that window's token and no key. Run it on the new box exactly as join-open "
            "printed it:\n",
            "#   curl -fsSk --pinnedpubkey '<pin>' https://<pool host>:<port>/j/<token> "
            "| sudo bash\n",
            f"JOIN_URL='{settings.url_base}/j/{settings.token}'\n",
            f"JOIN_PIN='{settings.pin}'\n",
            f"JOIN_POOL='{settings.pool}'\n",
            f"JOIN_LEAN_TIMEOUT='{settings.lean_timeout}'\n",
            f"JOIN_SERVER_WAIT='{settings.server_wait}'\n",
            body if body.endswith("\n") else body + "\n",
            "# The installer bundle: a base64 tar of the instance installer and the lean-pool "
            "project. No key in it.\n",
            "bundle() {\n",
            f"cat <<'{_BUNDLE_MARKER}'\n",
            bundle if bundle.endswith("\n") else bundle + "\n",
            f"{_BUNDLE_MARKER}\n",
            "}\n",
            'main "$@"\n',
        ]
    )


# ---------------------------------------------------------------------------------- the new box


def default_workers(cores: int, available_kib: int) -> tuple[int, str]:
    """The worker count of a joining box: the smaller of (cores - 2) and (available RAM / 4 GiB), at
    least 1."""
    by_cores = cores - RESERVED_CORES
    by_memory = (available_kib * 1024) // BYTES_PER_WORKER
    workers = max(1, min(by_cores, by_memory))
    gib = available_kib / 1024**2
    return workers, (
        f"{workers} workers: the smaller of {cores} cores - {RESERVED_CORES} = {by_cores} and "
        f"{gib:.1f} GiB available / 4 GiB = {by_memory}, at least 1"
    )


def csr_body(
    name: str, lean_port: int, agent_port: int, workers: int, csr: str
) -> dict[str, object]:
    """The body of `POST /j/<token>/csr`: exactly the five fields the join service takes."""
    if not _matches(SERVER_NAME_PATTERN, name):
        raise Refused(
            "the server name must be one DNS label in lower case (no dot), at most 40 characters"
        )
    if not is_single_pem(csr, _CSR_LABELS, MAXIMUM_CSR_BYTES):
        raise Refused("the signing request is not one PEM signing request")
    body = {
        "name": name,
        "lean_port": _whole_number(lean_port, "lean_port", PORT_PATTERN, 1, MAXIMUM_PORT),
        "agent_port": _whole_number(agent_port, "agent_port", PORT_PATTERN, 1, MAXIMUM_PORT),
        "workers": _whole_number(workers, "workers", COUNT_PATTERN, 1, MAXIMUM_WORKERS),
        "csr": csr,
    }
    assert tuple(body) == CSR_BODY_FIELDS
    return body


def split_certificate_reply(reply: object) -> tuple[str, str, str]:
    """The pool's answer to a signing request: (certificate, the authority's certificate, the
    pool's API key)."""
    if not isinstance(reply, dict):
        raise Refused("the pool's answer is not a JSON object")
    certificate, authority, api_key = (
        reply.get("certificate"),
        reply.get("ca"),
        reply.get("api_key"),
    )
    for what, text in (("certificate", certificate), ("ca", authority)):
        if not is_single_pem(text, (_CERTIFICATE_LABEL,), MAXIMUM_REPLY_BYTES):
            raise Refused(f"'{what}' in the pool's answer is not one PEM certificate")
    if not isinstance(api_key, str) or not _matches(_API_KEY_PATTERN, api_key):
        raise Refused(
            "the pool's answer holds no usable API key (16-128 letters and digits)"
        )  # never quoted
    return certificate, authority, api_key


def read_verdict(reply: object) -> tuple[bool, str]:
    """The pool's verdict: (admitted, what was found)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("admitted"), bool):
        raise Refused("the pool's verdict is not {admitted, detail}")
    return reply["admitted"], one_line(reply.get("detail", ""), 600)


def reason_of(reply_text: str) -> str:
    """Why the pool refused, from the body of an error reply (`reason` or `detail`)."""
    try:
        reply = json.loads(reply_text)
    except ValueError:
        return "no reason given"
    if isinstance(reply, dict):
        for field in ("reason", "detail"):
            if isinstance(reply.get(field), str) and reply[field]:
                return one_line(reply[field])
    return "no reason given"


# ------------------------------------------------------------------------ the image a pool ships


@dataclass(frozen=True)
class ImageManifest:
    """What a pool's host says about the Lean server image it exported for joining boxes.

    ``layers`` is the image's LAYER IDENTITY: the SHA-256 of the layer list Docker reports for it.
    It is what two hosts compare, because Docker's own image ID (``image_id``) is the digest of the
    image's config under one image store and of its manifest under the other. ``sha256`` and
    ``size`` are the file's.
    """

    tag: str
    image_id: str
    layers: str
    file: str
    sha256: str
    size: int
    machine: str
    docker: str

    def record(self) -> dict[str, object]:
        """The manifest as the JSON object the pool's host writes, its fields in their set order."""
        record = {
            "tag": self.tag,
            "image_id": self.image_id,
            "layers": self.layers,
            "file": self.file,
            "sha256": self.sha256,
            "bytes": self.size,
            "machine": self.machine,
            "format": IMAGE_FORMAT,
            "docker": self.docker,
        }
        assert tuple(record) == IMAGE_MANIFEST_FIELDS
        return record

    @property
    def gigabytes(self) -> str:
        """The image file's size in gigabytes, to one decimal, for people to read."""
        return f"{self.size / 1e9:.1f} GB"


def validate_image_manifest(record: object) -> ImageManifest:
    """Read a manifest as untrusted text: every field by its pattern, and the file's name by its
    digest."""
    if not isinstance(record, dict):
        raise Refused("the image manifest is not a JSON object")
    patterns = {
        "tag": IMAGE_TAG_PATTERN,
        "image_id": IMAGE_ID_PATTERN,
        "layers": SHA256_PATTERN,
        "file": IMAGE_FILE_PATTERN,
        "sha256": SHA256_PATTERN,
        "machine": MACHINE_PATTERN,
    }
    for field, pattern in patterns.items():
        value = record.get(field)
        if not isinstance(value, str) or not _matches(pattern, value):
            raise Refused(f"'{field}' in the image manifest is not what it must be")
    if record.get("format") != IMAGE_FORMAT:
        raise Refused(f"the image manifest's format is not '{IMAGE_FORMAT}'")
    size = record.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAXIMUM_IMAGE_BYTES:
        raise Refused("'bytes' in the image manifest is not a size an image can have")
    if record["file"] != f"image-{record['sha256'][:12]}.tar":
        raise Refused("the image manifest's file is not named after its SHA-256")
    return ImageManifest(
        tag=record["tag"],
        image_id=record["image_id"],
        layers=record["layers"],
        file=record["file"],
        sha256=record["sha256"],
        size=size,
        machine=record["machine"],
        docker=one_line(record.get("docker", ""), 40) or "unknown",
    )


def exported_image(manifest: ImageManifest, directory: Path, image_id: str) -> None:
    """On the pool's host: the export is of the image the host holds NOW, and its file is there,
    whole."""
    if manifest.image_id != image_id:
        raise Refused(
            "the export is of an image this host no longer holds under that tag "
            "(the host's image changed)"
        )
    try:
        status = (directory / manifest.file).lstat()
    except OSError:
        raise Refused(f"the exported file {manifest.file} is not there") from None
    if not stat.S_ISREG(status.st_mode) or status.st_size != manifest.size:
        raise Refused(
            f"the exported file {manifest.file} is not a file of the {manifest.size} bytes "
            f"the manifest records"
        )


def image_plan(manifest: ImageManifest, machine: str, tag: str) -> str | None:
    """On a new box: None when this box can load the pool's image, else why it must build its
    own."""
    if manifest.machine != machine:
        return (
            f"the pool's image is for {manifest.machine} machines and this box is "
            f"{one_line(machine, 24)}"
        )
    if manifest.tag != tag:
        return (
            f"the pool's image is {manifest.tag} and this box's installer installs "
            f"{one_line(tag, 200)}"
        )
    return None


def gib_rounded_up(size: int) -> int:
    """A size in bytes as a whole number of GiB, rounded up."""
    return -(-size // 1024**3)


# ------------------------------------------------------------------------------------------ files


def read_untrusted_file(path: Path, limit: int) -> str:
    """Read a file another party wrote: a regular file (never a link), of bounded size, as text."""
    try:
        # Never through a link, and never waiting on a pipe someone put in the file's place.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise Refused(f"cannot read {path.name}: {error.strerror or error}") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Refused(f"{path.name} is not a regular file")
        data = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise Refused(f"{path.name} is larger than {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{path.name} is not text") from None


def read_json_file(path: Path, limit: int) -> object:
    """Read a file another party wrote (see `read_untrusted_file`) and parse it as JSON."""
    text = read_untrusted_file(path, limit)
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise Refused(f"{path.name} is not valid JSON") from None


def write_file(path: Path, text: str, mode: int) -> None:
    """Put ``text`` at ``path`` in one step, with ``mode`` from the moment the file exists."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)  # the mode asked for, whatever the umask
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_response(spool: Path, name: str, body: dict[str, object]) -> None:
    """Write a response file the way the join service wants it: complete or absent, never
    half-written."""
    write_file(spool / "responses" / name, json.dumps(body, indent=2) + "\n", 0o644)


# ----------------------------------------------------------------------------------- command line


def _command_request(options: argparse.Namespace) -> int:
    try:
        record = read_json_file(options.spool / "requests" / "csr.json", MAXIMUM_REQUEST_BYTES)
        reserved = reserved_names(options.reserved.read_text(encoding="utf-8"))
        if len(reserved) <= len(ALWAYS_RESERVED):
            raise Refused(
                "the pool's own names could not be read, so no name can be shown not to be one of "
                "them"
            )
        request = validate_request(record, options.servers.read_text(encoding="utf-8"), reserved)
        write_file(options.csr_out, request.csr, 0o600)
    except Refused as error:
        print(str(error))  # the reason, for the refusal that goes back to the box
        return 1
    print(request.name, request.address, request.lean_port, request.agent_port, request.workers)
    return 0


def _command_certificate_response(options: argparse.Namespace) -> int:
    body = certificate_response(
        options.certificate.read_text(encoding="ascii"), options.ca.read_text(encoding="ascii")
    )
    write_response(options.spool, "certificate.json", dict(body))
    return 0


def _command_refuse(options: argparse.Namespace) -> int:
    write_response(options.spool, "certificate.json", dict(refusal_response(options.reason)))
    return 0


def _command_ready(options: argparse.Namespace) -> int:
    validate_ready(
        read_json_file(options.spool / "requests" / "ready.json", MAXIMUM_REQUEST_BYTES),
        options.caller,
    )
    return 0


def _command_verdict(options: argparse.Namespace) -> int:
    write_response(
        options.spool, "verdict.json", verdict_response(options.admitted == "yes", options.detail)
    )
    return 0


def _command_lookup(options: argparse.Namespace) -> int:
    for name, addresses in lookup(options.names).items():
        print(name, *sorted(addresses))
    return 0


def _command_choose_address(options: argparse.Namespace) -> int:
    host, note = choose_address(
        options.name,
        options.caller,
        parse_lookup(options.host_lookup),
        parse_lookup(options.proxy_lookup),
        options.domain,
    )
    print(host)
    print(note)
    return 0


def _command_certificate_names(options: argparse.Namespace) -> int:
    for path in options.certificates:
        for name in certificate_names(Path(path).read_bytes()):
            print(one_line(name, 253))
    return 0


def _command_admission_detail(options: argparse.Namespace) -> int:
    print(admission_detail(sys.stdin.read(), options.exit_code))
    return 0


def _command_script(options: argparse.Namespace) -> int:
    settings = ScriptSettings(
        url_base=options.url_base,
        token=options.token_file.read_text(encoding="ascii").strip(),
        pin=options.pin,
        pool=options.pool,
        lean_timeout=options.lean_timeout,
        server_wait=options.server_wait,
    )
    script = join_script(
        settings,
        options.body.read_text(encoding="utf-8"),
        options.bundle.read_text(encoding="ascii"),
    )
    write_file(
        options.out, script, 0o600
    )  # it holds the token; the caller hands it to the join service's group
    return 0


def _command_workers(options: argparse.Namespace) -> int:
    workers, explanation = default_workers(options.cores, options.available_kib)
    print(workers)
    print(explanation)
    return 0


def _command_csr_body(options: argparse.Namespace) -> int:
    body = csr_body(
        options.name,
        options.lean_port,
        options.agent_port,
        options.workers,
        read_untrusted_file(options.csr, MAXIMUM_CSR_BYTES),
    )
    write_file(options.out, json.dumps(body) + "\n", 0o600)
    return 0


def _command_certificate(options: argparse.Namespace) -> int:
    certificate, authority, api_key = split_certificate_reply(
        read_json_file(options.reply, MAXIMUM_REPLY_BYTES)
    )
    write_file(options.out_dir / "box.crt", certificate, 0o644)
    write_file(options.out_dir / "ca.crt", authority, 0o644)
    write_file(options.api_key_out, api_key + "\n", 0o600)
    return 0


def _command_verdict_of(options: argparse.Namespace) -> int:
    """Print what the pool found. Status 0: admitted; NOT_ADMITTED: not admitted;
    1: no readable verdict."""
    admitted, detail = read_verdict(read_json_file(options.reply, MAXIMUM_REPLY_BYTES))
    print(detail or ("admitted" if admitted else "no detail given"))
    return 0 if admitted else NOT_ADMITTED


def _command_reason(options: argparse.Namespace) -> int:
    try:
        print(reason_of(read_untrusted_file(options.reply, MAXIMUM_REPLY_BYTES)))
    except Refused:
        print("no reason given")
    return 0


def _command_image_manifest(options: argparse.Namespace) -> int:
    """On the pool's host: write the manifest of the export, checked as a box will check it."""
    record = {
        "tag": options.tag,
        "image_id": options.image_id,
        "layers": options.layers,
        "file": options.file,
        "sha256": options.sha256,
        "bytes": options.bytes,
        "machine": options.machine,
        "format": IMAGE_FORMAT,
        "docker": options.docker,
    }
    manifest = validate_image_manifest(record)
    write_file(options.out, json.dumps(manifest.record(), indent=2) + "\n", 0o640)
    return 0


def _command_image_export(options: argparse.Namespace) -> int:
    """On the pool's host: say what the export holds, if it is of the image the host holds now."""
    manifest = validate_image_manifest(read_json_file(options.manifest, MAXIMUM_MANIFEST_BYTES))
    exported_image(manifest, options.directory, options.image_id)
    for line in (
        manifest.tag,
        manifest.file,
        manifest.size,
        manifest.machine,
        manifest.gigabytes,
        manifest.docker,
    ):
        print(line)
    return 0


def _command_image_plan(options: argparse.Namespace) -> int:
    """On a new box. Status 0: load the pool's image (its file, SHA-256, bytes, layer identity,
    whole GiB, Docker version and size, one a line); NOT_FOR_THIS_BOX: build, for the reason
    printed; 1: unreadable."""
    manifest = validate_image_manifest(read_json_file(options.reply, MAXIMUM_MANIFEST_BYTES))
    reason = image_plan(manifest, options.machine, options.tag)
    if reason is not None:
        print(reason)
        return NOT_FOR_THIS_BOX
    for line in (
        manifest.file,
        manifest.sha256,
        manifest.size,
        manifest.layers,
        gib_rounded_up(manifest.size),
        manifest.docker,
        manifest.gigabytes,
    ):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line: one required-flags subcommand for each decision this file makes."""
    parser = argparse.ArgumentParser(
        prog="join_helper.py", description=__doc__.splitlines()[0], allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def command(
        verb: str, handler, /, **arguments: dict
    ) -> None:  # each keyword is one required --flag
        sub = commands.add_parser(verb, allow_abbrev=False)
        sub.set_defaults(handler=handler)
        for flag, settings in arguments.items():
            sub.add_argument("--" + flag.replace("_", "-"), required=True, **settings)

    path, text, number = {"type": Path}, {"type": str}, {"type": int}
    command("request", _command_request, spool=path, servers=path, reserved=path, csr_out=path)
    command(
        "certificate-response", _command_certificate_response, spool=path, certificate=path, ca=path
    )
    command("refuse", _command_refuse, spool=path, reason=text)
    command("ready", _command_ready, spool=path, caller=text)
    command(
        "verdict", _command_verdict, spool=path, admitted={"choices": ("yes", "no")}, detail=text
    )
    command(
        "choose-address",
        _command_choose_address,
        name=text,
        caller=text,
        host_lookup=text,
        proxy_lookup=text,
        domain=text,
    )
    command("admission-detail", _command_admission_detail, exit_code=number)
    command(
        "script",
        _command_script,
        url_base=text,
        token_file=path,
        pin=text,
        pool=text,
        lean_timeout=text,
        server_wait=text,
        body=path,
        bundle=path,
        out=path,
    )
    command("workers", _command_workers, cores=number, available_kib=number)
    command(
        "csr-body",
        _command_csr_body,
        name=text,
        lean_port=number,
        agent_port=number,
        workers=number,
        csr=path,
        out=path,
    )
    command("certificate", _command_certificate, reply=path, out_dir=path, api_key_out=path)
    command("verdict-of", _command_verdict_of, reply=path)
    command("reason", _command_reason, reply=path)
    command(
        "image-manifest",
        _command_image_manifest,
        tag=text,
        image_id=text,
        layers=text,
        file=text,
        sha256=text,
        bytes=number,
        machine=text,
        docker=text,
        out=path,
    )
    command("image-export", _command_image_export, manifest=path, directory=path, image_id=text)
    command("image-plan", _command_image_plan, reply=path, machine=text, tag=text)
    names = commands.add_parser("lookup", allow_abbrev=False)
    names.set_defaults(handler=_command_lookup)
    names.add_argument("names", nargs="+")
    certificates = commands.add_parser("certificate-names", allow_abbrev=False)
    certificates.set_defaults(handler=_command_certificate_names)
    certificates.add_argument("certificates", nargs="+")
    return parser


def main(arguments: list[str] | None = None) -> int:
    """Run one subcommand; a refusal or a bad file is one line on stderr and status 1."""
    options = build_parser().parse_args(arguments)
    try:
        return options.handler(options)
    except Refused as error:
        print(f"join_helper: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"join_helper: {error.strerror or error}: {error.filename or ''}", file=sys.stderr)
        return 1
    except ValueError as error:  # a file that is not what it should be (not a certificate)
        print(f"join_helper: {one_line(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
