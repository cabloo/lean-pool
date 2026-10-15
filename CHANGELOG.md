# Changelog

Notable changes to lean-pool. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): before 1.0, a minor version may
change an interface, and this file says when one does.

## Unreleased

A week of running the pool on boxes that also do other work.

### Changed

- **One set of compose files.** The installers now run the compose files of `deploy/` exactly as
  they are, and their own copies are gone. Each Lean server and each pool is one compose project
  (`lean-server`, `lean-server-NAME`, `lean-pool-NAME`), its containers have Compose's names, and
  each setting in `kimina.env` and `pool.env` is named for what reads it (`LEAN_SERVER_...`,
  `LEANPOOL_...`; [the settings](deploy/README.md#settings)). The first run of the installers on
  a box an older version set up moves the whole box, keeping every setting and the cache's
  results, and keeps the older files beside the new ones as `*.before-compose-names`
  ([the move, and the way back](docs/deployment.md#a-box-set-up-by-an-older-installer)). A pool
  set up before TLS is not moved: the installer refuses it before it changes anything.
- **The compose files, run by hand:** the Lean server's image is `LEAN_SERVER_IMAGE` (was
  `KIMINA_IMAGE`); the usage agent is the overlay `compose.agent.yaml`, and TLS is the overlay
  `compose.tls.yaml` after it (`-f compose.yaml -f compose.agent.yaml -f compose.tls.yaml`); the
  pool reads its key from `api-key` (was `api-key.txt`), runs the `lean-pool` image built
  beforehand, and with TLS mounts `./tls` (was `./pki/proxy`). `compose.dns.yaml` gives the
  proxy a DNS server. Each file's header says what it expects.
- **The documentation describes the installers.** [Deploying a pool](docs/deployment.md),
  [TLS](docs/tls.md) and [joining](docs/joining.md) no longer walk through a pool set up by
  hand.
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

- **The installers** (`deploy/install/`): the scripts the deployment installs its Lean servers,
  its pool and its joining boxes with, now in this repository, with their tests
  (`tests/install/`, on a fake machine). A deployment's own values live in a site directory
  given with `--site`; `site.example/` is an example site with one instance, which pulls
  Kimina's published image: an instance file may name an image to pull (`INSTANCE_IMAGE`)
  instead of a recipe to build
  ([deploy/install/README.md](deploy/install/README.md)).
- **Open slots.** A usage agent that is given the Lean server's worker count and its control
  group's `cpu.stat` states how many checks fit beside the box's other work, counted in cores,
  and HAProxy sends the box no more (`--workers`, `--lean-cpu-stat-path`, `--cpu-reserve`,
  `--cpu-topology-path`). A box may also carry a measured limit of its own for while other work
  runs on it (`--slots-beside-other-work`). An agent given neither answers as before.
- **The installer's version** in every box's answers: the installers give each usage agent the
  md5 of everything a join bundle carries (`--installer-version`), the agent ends every reply
  with `# version V`, `lean-pool-ctl NAME status` shows it for each server, and the pool's
  `GET /health` ends with its own host's (`render --installer-version`)
  ([the installer's version](docs/usage-agent.md#the-installers-version)).
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
