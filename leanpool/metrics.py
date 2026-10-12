"""What the pool tells a Prometheus scraper: the three paths, and the text they answer in.

The front door answers three paths without the API key, as it answers ``GET /health``:

* ``/metrics``: HAProxy's own exporter (servers, backends, the process);
* ``/metrics/cache``: the cache's counters, made by the cache service;
* ``/metrics/pool``: what the server list and the certificates say, written to a file when the
  configuration is rendered.

The last two are written here, in the Prometheus text format (version 0.0.4): for each metric a
``# HELP`` line, a ``# TYPE`` line and its samples. Every value is a whole number. Standard
library only, because the HAProxy tooling writes one of them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

METRICS_PATH = "/metrics"
CACHE_METRICS_PATH = "/metrics/cache"
POOL_METRICS_PATH = "/metrics/pool"
# What HAProxy's exporter says of its own answer; the other two routes say the same.
CONTENT_TYPE = "text/plain; version=0.0.4"

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


@dataclass(frozen=True)
class Sample:
    """One value of a metric, with the labels that tell it from the metric's other values."""

    value: int
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Metric:
    """One metric: its name, what it is, a line of help, and its samples."""

    name: str
    kind: Literal["counter", "gauge"]
    help: str
    samples: Sequence[Sample]


def render_metrics(metrics: Iterable[Metric]) -> str:
    """Return the metrics as Prometheus text. A metric with no sample is left out whole."""
    lines: list[str] = []
    for metric in metrics:
        if not _METRIC_NAME.fullmatch(metric.name):
            raise ValueError(f"{metric.name!r} is not a metric name")
        if not metric.samples:
            continue
        lines.append(f"# HELP {metric.name} {_escaped_help(metric.help)}")
        lines.append(f"# TYPE {metric.name} {metric.kind}")
        lines += [
            f"{metric.name}{_labels(sample.labels)} {sample.value}" for sample in metric.samples
        ]
    return "".join(f"{line}\n" for line in lines)


def _labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    pairs = []
    for name, value in labels.items():
        if not _LABEL_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a label name")
        pairs.append(f'{name}="{_escaped_label(value)}"')
    return "{" + ",".join(pairs) + "}"


def _escaped_label(value: str) -> str:
    """A label's value as the format writes it: a backslash, a quote and a line end escaped."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escaped_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")
