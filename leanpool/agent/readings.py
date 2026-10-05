"""Reading a Linux box's CPU, memory and load figures from ``/proc``."""

from __future__ import annotations

import re
from dataclasses import dataclass

_AGGREGATE_CPU_LABEL = "cpu"
_CORE_LABEL_PATTERN = re.compile(r"cpu\d+")
# /proc/stat's columns after the label: user nice system idle iowait irq softirq steal guest
# guest_nice. Guest time is already counted inside user and nice, so only the first eight add up
# to the total.
_IDLE_COLUMN = 3
_IOWAIT_COLUMN = 4
_COUNTED_COLUMNS = 8
_BYTES_PER_KIBIBYTE = 1024


class ReadingError(ValueError):
    """A ``/proc`` file did not have the expected content."""


@dataclass(frozen=True)
class CpuTimes:
    """CPU time counters since boot, summed over all cores, in the kernel's ticks.

    The counters only ever grow, so one reading says nothing about how busy the box is now;
    the share of idle time is the difference between two readings (see ``idle_share``).
    """

    idle_ticks: int
    total_ticks: int
    cores: int


def parse_cpu_times(stat_text: str) -> CpuTimes:
    """Read the aggregate ``cpu`` line and the core count from ``/proc/stat``.

    Time spent waiting for disk (iowait) counts as idle: the CPU is free to run a check then.
    """
    aggregate: list[int] | None = None
    cores = 0
    for line in stat_text.splitlines():
        label, _, columns = line.partition(" ")
        if label == _AGGREGATE_CPU_LABEL:
            aggregate = _parse_tick_columns(columns)
        elif _CORE_LABEL_PATTERN.fullmatch(label):
            cores += 1
    if aggregate is None or cores == 0:
        raise ReadingError("no 'cpu' lines in the stat file")
    return CpuTimes(
        idle_ticks=aggregate[_IDLE_COLUMN] + aggregate[_IOWAIT_COLUMN],
        total_ticks=sum(aggregate),
        cores=cores,
    )


def idle_share(previous: CpuTimes, current: CpuTimes) -> float | None:
    """Return the fraction of CPU time that was idle between two readings, from 0 to 1.

    None when no time passed between the readings (or the counters went backwards, as after a
    restore from a snapshot): there is then no interval to measure, and reporting a number
    anyway would be reporting a single reading.
    """
    elapsed_ticks = current.total_ticks - previous.total_ticks
    if elapsed_ticks <= 0:
        return None
    idle_ticks = current.idle_ticks - previous.idle_ticks
    return min(1.0, max(0.0, idle_ticks / elapsed_ticks))


def parse_available_memory_bytes(meminfo_text: str) -> int:
    """Read ``MemAvailable`` from ``/proc/meminfo``, in bytes.

    ``MemAvailable`` is the kernel's own estimate of how much memory a new process could use
    without swapping; ``MemFree`` would count reclaimable page cache as used.
    """
    for line in meminfo_text.splitlines():
        label, _, value = line.partition(":")
        if label == "MemAvailable":
            return _parse_kibibytes(value) * _BYTES_PER_KIBIBYTE
    raise ReadingError("no 'MemAvailable' line in the meminfo file")


def parse_one_minute_load(loadavg_text: str) -> float:
    """Read the 1-minute load average, the first field of ``/proc/loadavg``."""
    fields = loadavg_text.split()
    try:
        return float(fields[0])
    except (IndexError, ValueError):
        raise ReadingError("the loadavg file does not start with a number") from None


def _parse_tick_columns(columns: str) -> list[int]:
    try:
        ticks = [int(column) for column in columns.split()[:_COUNTED_COLUMNS]]
    except ValueError:
        raise ReadingError("the 'cpu' line of the stat file is not numeric") from None
    if len(ticks) <= _IDLE_COLUMN:
        raise ReadingError("the 'cpu' line of the stat file has too few columns")
    # Kernels before 2.6.24 report fewer columns; the missing ones are time that was not counted.
    return ticks + [0] * (_COUNTED_COLUMNS - len(ticks))


def _parse_kibibytes(value: str) -> int:
    fields = value.split()
    if len(fields) != 2 or fields[1] != "kB" or not fields[0].isdigit():
        raise ReadingError(f"cannot read the MemAvailable value {value.strip()!r}")
    return int(fields[0])
