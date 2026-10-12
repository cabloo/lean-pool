# The usage agent

Every Lean server box runs `leanpool-agent` beside its Lean server. It tells HAProxy how much
headroom the box has, and HAProxy scales the share of new checks the box receives by it. A box
that is busy with something else gets fewer checks; a box that runs out of memory gets none
until it recovers. Told how to measure it, the agent also states how many checks fit beside
the box's other work, and the box is sent no more than that
([Open slots](#open-slots-a-box-that-also-does-other-work)). The agent reads `/proc` (and, for
open slots, the kernel's files on control groups and CPU topology), needs no privilege and
uses the standard library only.

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
* The percentage never drops below 1. Taking a box out of rotation is reserved for the
  conditions where a new check would do harm. Memory: below the floor the agent answers
  `drain`, and the box gets no new checks until memory recovers (the next normal reply starts
  with `ready`, which cancels the drain). And, for an agent that states open slots, no room for
  a check beside the box's other work
  ([Open slots](#open-slots-a-box-that-also-does-other-work)). A percentage can still take a
  small server of an uneven pool out, through rounding (below).
* If the agent cannot be reached, HAProxy keeps the server's last weight.

HAProxy sets the server's weight to `configured weight x N / 100` in whole numbers, and a weight
of 0 takes the server out of rotation. For that reason the generated configuration does not use
the worker count itself as the weight: it scales all weights by one common factor so that the
largest server gets as close to 256 (HAProxy's maximum) as possible. A 4-worker and an 8-worker
server get weights 128 and 256, not 4 and 8. The ratios between servers, which is all that
least-connections balancing reads, are the worker counts' ratios; the scaling only keeps a low
percentage from rounding down to zero. It can guarantee that for the largest server only. A
server with less than about 40% of the largest server's workers has a configured weight below
100, and reaches weight 0 when its agent reports less than `100 / weight` percent: beside a
32-worker server, a 4-worker one (weight 32) is out of rotation at 1 to 3 percent.

## Open slots: a box that also does other work

A weight is a server's **share** of the next check, and least-connections balancing reads it only
when two servers could take that check. A client that keeps the pool full (more requests in
flight than there are workers, which is how the pool is meant to be used) leaves a queue standing
at the proxy, and then a worker that comes free is handed the next waiting check whatever its
server's weight. So a weight cannot keep work off a box that is busy with something else: at 1%
the box still receives a check for every worker it has.

An agent that is told how to measure it therefore also states the box's **open slots**, the
number of checks the box can run now beside whatever else runs on it, and HAProxy takes that as
the server's limit on concurrent checks:

```
others      = the CPUs the box had busy - the CPUs the Lean server itself used
              (both between the two most recent readings; never below 0)
taken       = reserve + others, in physical cores, the cores with most threads first
fits        = floor(the threads of the cores not taken), limited to 0..the server's workers
              and, while others is 1 CPU or more, to the box's limit beside other work (if set)

slots    = fits, at once, when fits is lower than the slots stated before;
           the slots stated before + 1 when fits is higher

reply    = "ready up N% maxconn:S"   S open slots, 1 or more
           "drain 0%"                no slot, or memory below the floor
```

* **What the agent must be given:** the server's worker count (`--workers`) and the `cpu.stat`
  file of a control group that holds the Lean server and nothing else (`--lean-cpu-stat-path`,
  cgroup v2). The Lean server's own use is taken out of the box's, so a box that is busy with
  nothing but checks keeps every slot: only other work lowers them. With either setting missing
  the agent's replies are, to the character, what they were before slots existed.
* **Cores, not hyperthreads.** Other work that keeps 8 threads busy holds 8 cores: the scheduler
  gives each thread a core of its own while there are any. A check started on the second thread
  of one of those cores slows the work on its first thread, and no CPU priority prevents that,
  because the two threads share the core's machinery. So a check may use a thread only of a core
  the other work leaves, and every thread of such a core. Where a box's cores differ (fast cores
  of two threads, slow cores of one) the other work and the reserve are taken to hold the cores
  with most threads, which are the fast ones the scheduler fills first. This was measured, not
  assumed: on such a box, 20 threads on 12 cores, with 8 single-threaded tasks, counting in
  threads let 10 checks "fit", and each task then took about 150 seconds for work that took about
  100 with no checks beside it. Counted in cores, 2 fit: the tasks hold the eight two-thread
  cores, and of the four one-thread cores one is the reserve's. The agent reads the cores from
  `/sys/devices/system/cpu/cpu*/topology` (in a container that is the host's); where it cannot,
  or where that describes other CPUs than `/proc/stat` counts, it takes every thread for a core.
  On a box with one thread a core the two counts are the same.
* **Beside other work, a measured limit.** The count of cores says how many checks have a core
  to run on. It does not say what the box's other work can carry: on a box of 12 cores, four
  tasks with a core each and five cores to spare ran 26% slower with nine checks beside them (79.5
  seconds against 63 for the same work), which is more than six further tasks slowed them (13%).
  Checks are heavy neighbours whatever core they run on. So a box that shares gets a number of
  its own, `--slots-beside-other-work`: while anything but the Lean server keeps a CPU or more
  busy, the agent states no more slots than that. With the box to itself the count stands alone,
  and the slots come back one a reading. Find the number by timing the other work with the Lean
  server taking checks and [paused](haproxy.md#a-paused-server), in turns of ten minutes (taking,
  paused, taking, paused, taking), and reading each turn against its two neighbours, because a
  box's pace drifts and a single before-and-after cannot tell the drift from the checks. Time
  the work that runs on that box, not on its neighbour. On one night one box cost its other
  work about 3% a check (26% for nine) and another about 10% a check (1.8 times for eight): the
  number is the box's, not a constant.
* **Down at once, up by one.** A burst of other work is met within one reading. A pause in that
  work is not taken for free room: the slots climb back one per reading (5 seconds).
* **No slot is `drain`, never `maxconn:0`.** To HAProxy a limit of 0 means no limit at all. A
  drained server stays up, finishes the checks it holds and is sent no new one. The `0%` beside
  the `drain` sets the server's weight to 0, which is how the pool's own count leaves the server
  out: HAProxy's `srv_is_up` is true for a drained server.
* **A limit stays until another is stated.** HAProxy keeps the last `maxconn` an agent told it.
  So an agent that states slots states them in every reply, and when it cannot read the Lean
  server's `cpu.stat` (the server is being made again, the file is not there) it states the
  whole worker count: silence would leave an earlier, lower limit standing on a server nobody
  measures. A reading in which a counter went backwards (the control group was made again) keeps
  the slots stated before.
* **Who wins the CPU is not the agent's matter.** The slots say how many checks a box is sent.
  They do not make the box's other work run first when both want a CPU; that is for the
  operating system (run the Lean server at a low CPU weight). The two go together: the low
  weight protects the other work even when a reading is stale, and the slots keep checks from
  being sent where they would crawl to their timeout.

### Giving the agent what it needs

Both settings are the box's to arrange, and the compose files in [`deploy/`](../deploy) do not
arrange them: an agent started from those answers with a weight alone.

* **A control group for the Lean server alone.** With Docker under systemd, start the Lean
  server's container in a slice of its own (Compose: `cgroup_parent: leanpool-main.slice`). Its
  `cpu.stat` is then `/sys/fs/cgroup/leanpool.slice/leanpool-main.slice/cpu.stat` on the box,
  and a unit file for `leanpool.slice` with `CPUWeight=1` gives everything under it the lowest
  CPU weight there is.
* **A view of the box's control groups.** In a container the agent sees them when the box's
  tree is mounted read-only (`/sys/fs/cgroup:/host-cgroup:ro`, with the path given as
  `/host-cgroup/leanpool.slice/leanpool-main.slice/cpu.stat`), or when the container shares the
  host's cgroup namespace (Compose: `cgroup: host`; `/sys/fs/cgroup` is then the host's tree,
  read-only). `/proc` and the CPU topology under `/sys` are the host's in a container already.

That is how the deployment this project was written for runs it
([its record](verification.md#the-deployments-record)); its installer, which writes the slice
and these settings, is specific to it and is not shipped.
