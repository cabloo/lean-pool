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

- **The worker sandbox** ([docs](docs/worker-sandbox.md)): every Lean worker in a Landlock domain
  of its own (`deploy/lean-server/worker-sandbox/`, `compose.worker-sandbox.yaml`). The code a
  check runs can no longer reach another worker's pipes, memory or environment, or Kimina's;
  write anywhere but `/tmp`; open TCP (Landlock 4); or signal another worker (Landlock 6). Each
  worker starts without the server's key. `make.sh` asks a container of the box's own image
  whether the sandbox can be made there, and a worker that cannot be sandboxed does not start.
  The installers turn it on where that probe passes and keep it where a real check then passes
  (else one protection is taken away at a time, the sandbox first); `kimina-ctl [@NAME] sandbox
  on|off` switches it, and `status` counts the running workers that carry its mark.
- **A second, screened key for the cache** (`--screened-api-key-file`): for senders trusted less
  than the pool's own. Their checks must first pass [the screen](docs/cache.md#the-screened-key)
  (`leanpool/screen.py`: no `#eval`, `run_tac`, `elab`, `IO`, most attributes or imports beyond
  Mathlib and Aesop), or the whole request is answered 422 and reaches nothing; those that pass
  are served like any other and forwarded with the pool's key, so no Lean server holds the
  second one. Such a request carries Kimina's plain fields alone, at most `--screened-in-flight`
  snippets, a timeout of at most `--screened-max-timeout` seconds (and that one when it names
  none) and a body of at most `--screened-max-request-bytes`, and the checks its senders start
  hold at most `--screened-in-flight` places at the servers, with as many waiting. The screen runs
  off the cache's event loop.
- **The screened door** ([docs](docs/cache.md#the-screened-door)): the second key's one way in, a
  second HTTPS port of the proxy (`--screened-door-port`) that leads to the cache alone, with no
  backup, and marks what it passes (`X-Lean-Pool-Door`). It speaks HTTP/1.1 alone, and refuses on
  its headers a body of no stated length (411) or longer than one buffer (413), so its senders
  cannot use up the proxy's memory. `install.sh --proxy NAME --screened-door`
  opens it on port 18120 of the docker0 bridge's address, firewalled to the docker bridges (a
  bridge named `br-*` that is not Docker's is turned away by name), makes
  the second key once and keeps it, and with `--screened-key-copy DIR` writes it, with the pool
  authority's certificate, into DIR for the program that hands it on. `lean-pool-ctl NAME
  screened-key new|copy` makes a new key or writes the copy again.
- **A copy of the pool's own key** ([docs](docs/deployment.md#a-copy-of-the-pools-key)):
  `install.sh --proxy NAME --pool-key-copy DIR` writes the pool's key and the authority's
  certificate into DIR at every run, as the screened key's copy is written, for a program on the
  host that hands it to boxes of its own; never into the screened key's copy's directory, nor
  one inside or around it, nor one that holds the screened key, nor a pool's own directory.
  `lean-pool-ctl NAME pool-key copy` writes it again; `--no-pool-key-copy` and
  `--no-screened-key-copy` stop a copy. Either copy is written only where no directory of its
  path can be written by anyone but root and the copy's owner.
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

### Fixed

- **An install on a busy box.** At the lowest CPU weight, a Lean server just made on a box whose
  CPUs other work keeps busy got almost no CPU, and its first check ran past its time limit: the
  install failed, a move to Compose's names was put back, and the worker sandbox and the read-only
  root were taken away for nothing. The installers now run their own checks, and a joining box
  the pool's admission test, at the box's ordinary CPU weight. They put the lowest back after,
  whether they succeed, fail or are stopped; an installer killed outright leaves the ordinary
  weight until the next install on the box
  ([a box that also does other work](docs/deployment.md#a-box-that-also-does-other-work)).

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
