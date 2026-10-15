# Changelog

Notable changes to lean-pool. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): before 1.0, a minor version may
change an interface, and this file says when one does.

## Unreleased

A week of running the pool on boxes that also do other work.

### Changed

- **What the pool says its size is.** `X-Lean-Pool-Workers`, and `workers` in the body of
  `GET /health`, are now the open slots on the servers taking checks: each server's worker
  count, or the lower number its usage agent states. They were the workers on servers that
  are up. A client that follows the number now shrinks with a box that is busy.
- **The health check of a Lean server is patient:** every 5 seconds, 15 seconds to answer, down
  after 4 misses in a row, up after 1 answer. A busy server that answers `/health` slowly is
  no longer taken out and handed a full burst when it returns.
- **HAProxy must be built with its Prometheus exporter** (`USE_PROMEX=1`), as the official
  images are: the generated configuration uses it.
- A `GET` of `/metrics`, `/metrics/cache` and `/metrics/pool` at the public port no longer
  reaches a Lean server (Kimina answers 404 to all three).

### Added

- **Open slots.** A usage agent that is given the Lean server's worker count and its control
  group's `cpu.stat` states how many checks fit beside the box's other work, counted in cores,
  and HAProxy sends the box no more (`--workers`, `--lean-cpu-stat-path`, `--cpu-reserve`,
  `--cpu-topology-path`). A box may also carry a measured limit of its own for while other work
  runs on it (`--slots-beside-other-work`). An agent given neither answers as before.
- `leanpool-haproxy-config pause` and `resume`: a server that stays in the list and is sent
  nothing, across reloads and restarts.
- `leanpool-haproxy-config set-workers`: change the worker count a server is listed with.
- Metrics for Prometheus at the public port, without the key: `/metrics` (HAProxy's own
  exporter), `/metrics/cache` (the cache's counters) and `/metrics/pool` (listed workers,
  paused servers and the last day of each certificate, written by
  `render --pool-metrics FILE [--certificates DIR]`).
- **Compiled search for linarith**, an optional step on each Lean server box
  (`deploy/lean-server/compiled-search/` and `compose.compiled-search.yaml`). The box
  compiles the search step of `linarith` and `nlinarith` from the C files its own Kimina
  image holds, checks that it changes nothing but the time, and has every worker load it.
  In the deployment it was written for: 8 to 10 times faster on four machines, and no
  verdict changed ([docs/compiled-search.md](docs/compiled-search.md)).

## 0.1.0 - 2026-10-05

The first public release. lean-pool was developed inside a private repository and had been
running a pool there for two days; this is that code, with documentation, a demo and
deployment examples written for publication.

### Added

- `leanpool-haproxy-config`: the server list and the generator of a pool proxy's `haproxy.cfg`
  (cache-first routing, least-connections balancing capped at each server's workers, failover
  that never retries a Lean timeout, no check lost when the cache is, run-time name resolution), and
  of a Lean server box's TLS front.
- `leanpool-cache`: the shared result cache, with single flight for identical checks, a size
  cap with least-recently-used eviction, and a `no_cache` bypass.
- `leanpool-agent`: the usage agent that scales a server's weight by its idle CPU and available
  memory, and drains it when memory runs out.
- `leanpool-admit`: the admission test for one Lean server, directly or through its TLS front.
- `leanpool-pki`: a pool's certificate authority.
- `leanpool-join`: the HTTPS service through which a new box asks to join.
- Priorities (`X-Lean-Priority: background`) and the pool's stated size on every answer
  (`X-Lean-Pool-Workers`, `X-Lean-Pool-Queued`, `X-Lean-Pool-Servers`, and the body of
  `GET /health`).
- A demo with stand-in Lean servers and a walkthrough, an example client, starter admission
  cases, and compose files for a pool box and a Lean server box, plain and with TLS.
