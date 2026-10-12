"""Turning a box's measurements into one line of HAProxy's agent-check protocol.

The percentage, with every share a number from 0 to 1::

    cpu share     idle CPU ticks / all CPU ticks, between the two most recent readings
    memory share  (available - floor) / floor, limited to 0..1
                  1 when available memory is at least twice the floor, 0 at the floor
    load share    1 - (1-minute load / cores), limited to 0..1      (only when enabled)

    percentage    round(100 * the smallest share), limited to 1..100

The smallest share decides because a check needs every resource at once: plenty of memory does
not make up for a CPU that is fully busy. The percentage never drops below 1 because 0 would take
the server out of rotation, and that is reserved for the conditions where a new check would do
harm.

HAProxy multiplies the server's configured weight by the percentage. A weight is a server's
SHARE of the next check, and it is read only when two servers could take that check: while every
worker of every server is busy, a worker that comes free is handed the next waiting check
whatever its server's weight. So an agent that is told how to measure it also states the box's
OPEN SLOTS, the number of checks the box can run now beside whatever else runs on it::

    others      the CPUs the box had busy - the CPUs the Lean server itself used
                (both between the two most recent readings; never below 0)
    taken       the reserve + others, in physical CORES, the cores with most threads first
    fits        floor(the threads of the cores not taken), limited to 0..the server's workers

    beside      (a setting of the box; not set: no such limit) while others is 1 CPU or
                more, fits is no more than this

    slots       fall to ``fits`` at once and rise by at most one per reading

CORES, not hyperthreads. Other work that keeps 8 threads busy holds 8 cores, because the scheduler
gives each thread a core of its own while there are any. A check started on the second thread of
one of those cores slows the work on its first thread, and no CPU priority prevents that: the two
threads share the core's machinery. So a check may use a thread only of a core the other work
does not use, and every thread of such a core. Where the cores differ (a desktop with fast cores
of two threads and slow cores of one) the other work is taken to hold the cores with most
threads, which are the fast ones the scheduler fills first. Measured on such a box, 20 threads on
12 cores, with 8 single-threaded tasks: counted in threads, 10 checks "fitted" and the tasks ran
a third slower; counted in cores, 2 fit (the slow cores the tasks leave, less the reserve). On a
box with one thread a core the two counts are the same.

BESIDE OTHER WORK, a measured number and not a count. Free cores do not say what a box's other
work can carry: on that same box, four tasks with a core each and five cores to spare ran 26%
slower with nine checks beside them, more than six further tasks slowed them (13%). So a box
that also runs other work is given a limit of its own, found by timing that work with the Lean
server taking checks and paused. While something else keeps a CPU or more busy the agent states
no more slots than that; when the box is free again the count above stands alone, and the slots
come back one a reading.

HAProxy takes ``maxconn:N`` from an agent as that server's limit on concurrent checks, and keeps
the last limit it was told until it is told another. So an agent that states slots states them
in EVERY reply that can carry them. ``maxconn:0`` is never written: to HAProxy a limit of 0 means
no limit at all.

The reply::

    "drain"                       available memory is below the floor, no slots stated
    "drain 0%"                    slots are stated and are 0, or memory is below the floor
    "ready up"                    fewer than two CPU readings so far: nothing is known
    "ready up N%"                 no slots stated
    "ready up N% maxconn:S"       S open slots, 1 or more

``drain`` leaves the server up: it finishes the checks it has and is sent no new one. The ``0%``
beside it sets the server's weight to 0, which is what the pool's own count of open slots reads
to leave a drained server out (HAProxy's ``srv_is_up`` is true for a drained server). ``ready``
and ``up`` are sent with every other reply because only the agent can undo its own ``drain``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

MINIMUM_PERCENT = 1
MAXIMUM_PERCENT = 100
DRAIN_REPLY = "drain\n"
# The drain of an agent that states slots: the weight goes to 0 with it.
DRAIN_REPLY_WITH_SLOTS = "drain 0%\n"
READY_REPLY_WITHOUT_WEIGHT = "ready up\n"
# A sum of measured shares can land a hair under a whole number (15 - 9.999999999 CPUs).
_ROUNDING_SLACK = 1e-9
# A box is taken to run other work once everything but the Lean server keeps this many CPUs busy:
# one task's worth. Under it the box is the Lean server's own.
OTHER_WORK_CPUS = 1.0


@dataclass(frozen=True)
class BoxState:
    """What is known about the box at the moment a reply is written.

    ``cpu_idle_share`` is None until two CPU readings exist. ``load_per_core`` is None when the
    load average is not in use. ``open_slots`` is None for an agent that states no slots (it was
    not told how to measure them, or has no measurement yet).
    """

    cpu_idle_share: float | None
    available_memory_bytes: int
    load_per_core: float | None = None
    open_slots: int | None = None


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


def fitting_slots(
    cores: int,
    reserve_cores: float,
    busy_cpus: float,
    lean_cpus: float,
    workers: int,
    threads_of_each_core: Sequence[int] | None = None,
    beside_other_work: int | None = None,
) -> int:
    """Return how many checks fit on the box beside its other work.

    ``cores`` is the box's logical CPUs (its threads) and ``threads_of_each_core`` how many of
    them each physical core has; None, or a list that is not of those CPUs, means one thread a
    core. ``busy_cpus`` is what the whole box had busy and ``lean_cpus`` what the Lean server
    itself used, over the same interval. Their difference is everything else on the box, taken to
    hold that many CORES, and with the reserve the cores with most threads first. The threads of
    the cores left are room for checks, one each; of a core taken in part, that part of its
    threads. The Lean server's own use is taken out so that a box busy with nothing but checks
    keeps every slot.

    ``beside_other_work`` is the box's measured limit on checks while it runs other work; None is
    no such limit. It applies while everything but the Lean server keeps ``OTHER_WORK_CPUS`` or
    more busy, and never raises what the cores give.
    """
    if (
        not threads_of_each_core
        or min(threads_of_each_core) < 1
        or sum(threads_of_each_core) != cores
    ):
        threads_of_each_core = (1,) * cores
    others = max(0.0, busy_cpus - lean_cpus)
    taken = reserve_cores + others
    room = 0.0
    for threads in sorted(threads_of_each_core, reverse=True):
        left = 1.0 - min(1.0, taken)
        taken = max(0.0, taken - 1.0)
        room += threads * left
    fits = min(workers, max(0, math.floor(room + _ROUNDING_SLACK)))
    if beside_other_work is not None and others + _ROUNDING_SLACK >= OTHER_WORK_CPUS:
        fits = min(fits, beside_other_work)
    return fits


def next_slots(stated: int | None, fits: int) -> int:
    """Return the slots to state now, given the slots stated before and what fits now.

    Down at once, so a burst of other work is met within one reading; up by one, so a pause in
    that work is not taken for free room. With nothing stated before, what fits.
    """
    if stated is None or fits <= stated:
        return fits
    return stated + 1


def agent_reply(state: BoxState, memory_floor_bytes: int) -> str:
    """Return the agent-check line for the box's state, ending in a newline."""
    if state.available_memory_bytes < memory_floor_bytes:
        return DRAIN_REPLY if state.open_slots is None else DRAIN_REPLY_WITH_SLOTS
    if state.open_slots == 0:
        return DRAIN_REPLY_WITH_SLOTS
    words = ["ready", "up"]
    percent = headroom_percent(state, memory_floor_bytes)
    if percent is not None:
        words.append(f"{percent}%")
    if state.open_slots is not None:
        words.append(f"maxconn:{state.open_slots}")
    return " ".join(words) + "\n"


def _limit_to_unit(value: float) -> float:
    return min(1.0, max(0.0, value))
