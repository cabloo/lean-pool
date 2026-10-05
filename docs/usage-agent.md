# The usage agent

Every Lean server box runs `leanpool-agent` beside its Lean server. It tells HAProxy how much
headroom the box has, and HAProxy scales the share of new checks the box receives by it. A box
that is busy with something else gets fewer checks; a box that runs out of memory gets none
until it recovers. The agent reads `/proc`, needs no privilege and uses the standard library
only.

## The weight formula

HAProxy connects to the agent every 5 seconds (`agent-check`), reads one line and closes. The
agent answers from its latest sample; a background sampler reads `/proc` every
`--sample-interval` seconds.

```
cpu share     = idle CPU time / all CPU time, between the two most recent readings of /proc/stat
                (time waiting for disk counts as idle)
memory share  = (MemAvailable - floor) / floor, limited to 0..1
                (1 when available memory is at least twice the floor, 0 at the floor)
load share    = 1 - (1-minute load average / cores), limited to 0..1     only with --use-load-average

percentage    = round(100 x the smallest share), limited to 1..100

reply         = "drain"           if MemAvailable < floor
                "ready up"        if there are not yet two CPU readings an interval apart
                "ready up N%"     otherwise
```

* **The CPU share is the whole box's idle share.** It counts every process on the box, so it
  includes the Lean server's own load: a box that is busy only because it is checking proofs
  reports a low percentage too. The agent cannot tell the pool's work from anyone else's.
  Weights are relative, so boxes that are equally busy with checks stay equally weighted; what
  the figure adds is that a box also busy with something else gets fewer new checks.
* The CPU share always comes from two readings. `/proc/stat` counts time since boot, so a single
  reading says nothing about now; until the second reading the agent reports no percentage and
  HAProxy leaves the weight as it is.
* The smallest share decides, because a check needs every resource at once.
* The percentage never drops below 1. Taking a server out of rotation is reserved for memory:
  below the floor the agent answers `drain`, and the box gets no new checks until memory
  recovers (the next normal reply starts with `ready`, which cancels the drain).
* If the agent cannot be reached, HAProxy keeps the server's last weight.

HAProxy sets the server's weight to `configured weight x N / 100` in whole numbers, and a weight
of 0 takes the server out of rotation. For that reason the generated configuration does not use
the worker count itself as the weight: it scales all weights by one common factor so that the
largest server gets as close to 256 (HAProxy's maximum) as possible. A 4-worker and an 8-worker
server get weights 128 and 256, not 4 and 8. The ratios between servers, which is all that
least-connections balancing reads, are the worker counts' ratios; the scaling only keeps a low
percentage from rounding down to zero.
