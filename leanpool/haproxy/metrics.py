"""The pool's facts for ``GET /metrics/pool``: what the server list and the certificates say.

HAProxy's own exporter knows what each server is doing and nothing of what the pool was told
about it: the worker count a server was listed with, whether it was paused on purpose, and when
a certificate ends. Those are fixed when the configuration is rendered, so they are written
then, to a file HAProxy serves as it is. The server list stays the single source: the file is
made from the same list as the configuration, by the same command, and replaced with it.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from leanpool.haproxy.certificate_dates import CertificateDateError, not_after_seconds
from leanpool.haproxy.config import PoolSettingsError
from leanpool.haproxy.files import write_file_atomically
from leanpool.haproxy.servers import LeanServer
from leanpool.metrics import Metric, Sample, render_metrics

CERTIFICATE_SUFFIX = ".crt"
# A certificate with a long chain of names is a few kilobytes; nothing larger is read.
_LARGEST_CERTIFICATE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CertificateDates:
    """When each certificate of a directory ends, and which could not be read, with the reason.

    A certificate is named by its path under the directory (``servers/lean-a.crt``).
    """

    not_after: dict[str, int] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)


def read_certificate_dates(directory: Path) -> CertificateDates:
    """Read the end of validity of every ``*.crt`` file under ``directory``, by name.

    A file that is not a certificate is reported in ``unreadable`` and has no date: one
    damaged copy of a public certificate must not stop the pool from being rendered. A
    directory that is not there is a mistyped path, and is refused.
    """
    if not directory.is_dir():
        raise PoolSettingsError(f"the certificates directory {directory} does not exist")
    dates = CertificateDates()
    for path in sorted(directory.rglob(f"*{CERTIFICATE_SUFFIX}")):
        if not path.is_file():
            continue
        name = path.relative_to(directory).as_posix()
        try:
            if path.stat().st_size > _LARGEST_CERTIFICATE_BYTES:
                raise CertificateDateError("it is too large to be a certificate")
            text = path.read_text(encoding="utf-8", errors="replace")
            dates.not_after[name] = not_after_seconds(text)
        except (CertificateDateError, OSError) as error:
            dates.unreadable[name] = str(error)
    return dates


def render_pool_metrics(
    servers: Sequence[LeanServer], certificate_not_after: Mapping[str, int] | None = None
) -> str:
    """Return the pool's facts as Prometheus text: two numbers a server, one a certificate.

    The servers are written in the order of the list and the certificates in the order given.
    Without certificates their metric is left out whole.
    """
    return render_metrics(
        [
            Metric(
                "leanpool_server_listed_workers",
                "gauge",
                "Workers the server list gives a Lean server.",
                [Sample(server.workers, {"server": server.name}) for server in servers],
            ),
            Metric(
                "leanpool_server_paused",
                "gauge",
                "Whether the server list marks a Lean server as paused (1) or not (0).",
                [Sample(int(server.paused), {"server": server.name}) for server in servers],
            ),
            Metric(
                "leanpool_certificate_not_after_seconds",
                "gauge",
                "The end of a certificate's validity, in seconds since 1970.",
                [
                    Sample(seconds, {"certificate": name})
                    for name, seconds in (certificate_not_after or {}).items()
                ],
            ),
        ]
    )


def write_pool_metrics(path: Path, text: str, maximum_bytes: int) -> None:
    """Put the pool's facts at ``path`` in one step, or refuse a text HAProxy could not serve.

    HAProxy keeps the file in one buffer and refuses a configuration whose file is larger, so
    a text over ``maximum_bytes`` is refused here, before anything is written.
    """
    size = len(text.encode("utf-8"))
    if size > maximum_bytes:
        raise PoolSettingsError(
            f"the pool's metrics take {size} bytes, more than the {maximum_bytes} HAProxy is "
            "given to serve them from (half its buffer): raise the buffer "
            "(--max-request-bytes). Nothing was written"
        )
    write_file_atomically(path, text)
