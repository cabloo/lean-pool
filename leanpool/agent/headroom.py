"""Turning a box's measurements into one line of HAProxy's agent-check protocol.

The formula, with every share a number from 0 to 1::

    cpu share     idle CPU ticks / all CPU ticks, between the two most recent readings
    memory share  (available - floor) / floor, limited to 0..1
                  1 when available memory is at least twice the floor, 0 at the floor
    load share    1 - (1-minute load / cores), limited to 0..1      (only when enabled)

    percentage    round(100 * the smallest share), limited to 1..100

    reply   "drain"          available memory is below the floor
            "ready up"       fewer than two CPU readings so far: no percentage is known
            "ready up N%"    otherwise

The smallest share decides because a check needs every resource at once: plenty of memory does
not make up for a CPU that is fully busy. The percentage never drops below 1 because 0 would take
the server out of rotation, and that is reserved for the one condition where a new check would do
harm: memory about to run out (a Lean worker that hits the limit is killed mid-proof).

HAProxy multiplies the server's configured weight by the percentage. ``ready`` and ``up`` are
sent with every normal reply because only the agent can undo its own earlier ``drain``.
"""

from __future__ import annotations

from dataclasses import dataclass

MINIMUM_PERCENT = 1
MAXIMUM_PERCENT = 100
DRAIN_REPLY = "drain\n"
READY_REPLY_WITHOUT_WEIGHT = "ready up\n"


@dataclass(frozen=True)
class BoxState:
    """What is known about the box at the moment a reply is written.

    ``cpu_idle_share`` is None until two CPU readings exist. ``load_per_core`` is None when the
    load average is not in use.
    """

    cpu_idle_share: float | None
    available_memory_bytes: int
    load_per_core: float | None = None


def memory_share(available_bytes: int, floor_bytes: int) -> float:
    """Return how far available memory is above the floor, as a share of the floor.

    Memory should not lower a server's weight while there is plenty of it, only as it runs out,
    so the share stays at 1 down to twice the floor and then falls in a straight line to 0 at
    the floor. One setting (the floor) fixes both where draining starts and where the weight
    starts to fall.
    """
    return _limit_to_unit((available_bytes - floor_bytes) / floor_bytes)


def load_share(load_per_core: float) -> float:
    """Return the share of cores not claimed by the run queue over the last minute."""
    return _limit_to_unit(1.0 - load_per_core)


def headroom_percent(state: BoxState, memory_floor_bytes: int) -> int | None:
    """Return the percentage to report, or None while the CPU share is not yet known."""
    if state.cpu_idle_share is None:
        return None
    shares = [state.cpu_idle_share, memory_share(state.available_memory_bytes, memory_floor_bytes)]
    if state.load_per_core is not None:
        shares.append(load_share(state.load_per_core))
    percent = round(MAXIMUM_PERCENT * min(shares))
    return min(MAXIMUM_PERCENT, max(MINIMUM_PERCENT, percent))


def agent_reply(state: BoxState, memory_floor_bytes: int) -> str:
    """Return the agent-check line for the box's state, ending in a newline."""
    if state.available_memory_bytes < memory_floor_bytes:
        return DRAIN_REPLY
    percent = headroom_percent(state, memory_floor_bytes)
    if percent is None:
        return READY_REPLY_WITHOUT_WEIGHT
    return f"ready up {percent}%\n"


def _limit_to_unit(value: float) -> float:
    return min(1.0, max(0.0, value))
