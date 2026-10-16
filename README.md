# lean-pool

[![CI](https://github.com/cabloo/lean-pool/actions/workflows/ci.yml/badge.svg)](https://github.com/cabloo/lean-pool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

**One address for a pool of Lean 4 proof checkers.**

The [Kimina Lean Server](https://github.com/project-numina/kimina-lean-server) is an HTTP
server that checks Lean 4 code, on one machine. lean-pool turns any number of them, on machines
of different sizes, into one endpoint that speaks Kimina's own API. HAProxy does the routing.
lean-pool generates its configuration and adds what a load balancer does not do on its own:

* a **shared result cache** that knows which of Lean's answers are safe to give again;
* **load balancing by real headroom**: every machine reports its idle CPU and available
  memory, and receives checks in proportion; one that is also busy with other work is sent
  only the checks that fit beside it;
* **failover that leaves Lean's verdicts alone**: a check goes to another server only when it
  got no answer from Lean (a lost connection, a crashed worker, a gateway error);
* an **admission test** that keeps a server with the wrong Lean or Mathlib out of the pool;
* optionally, **TLS on every hop** from the pool's own certificate authority.

Clients change one thing: the address.

```
 clients                 +--------------------- the pool box -----------------------+
 (provers, graders)      |                                                          |
      |                  |  HAProxy :18100                                          |
      +---- checks ----->|    |                                                     |
                         |    +--> cache --------> hit:  answered from its store    |
                         |    |      |                                              |
                         |    |      +-- miss ---> HAProxy's loopback door --+      |
                         |    |                                              |      |
                         |    +-- the cache is down -------------------------+      |
                         |                                                   |      |
                         +---------------------------------------------------|------+
                                                                             |
               least connections; never more checks per server than it has workers;
               weight = workers x the headroom its usage agent reports
                                                                             |
                      +--------------------------+---------------------------+
                      v                          v                           v
               Kimina  @ lean-a           Kimina  @ lean-b            Kimina  @ ...
               + usage agent              + usage agent
```

A server's **workers** are the checks it runs at once (Kimina's `LEAN_SERVER_MAX_REPLS`).

## Why

Checking proofs is often the bottleneck of training or evaluating a Lean prover: a GPU writes
candidate proofs faster than Lean can check them. Kimina checks them well, on one machine. Its
deployment guide scales out with a cloud load balancer in front of a managed group of identical
VMs. That leaves gaps, and they are widest when the hardware is whatever you have: a
workstation, two desktops and a laptop, each also used for other work.

| The gap | What lean-pool does |
|---|---|
| **The same proof is checked again and again.** Provers resubmit the same proof of the same statement across samples, epochs and reruns. Kimina has no cache between servers, and an ordinary HTTP cache cannot be one: a check is a `POST` identified by its body, every attempt carries its own id, and a Lean timeout comes back as HTTP 200. | The [cache](docs/cache.md) answers a repeat from its store, under the caller's own id, and lets identical checks in flight share one worker. It stores only a verdict Lean would give again: a timeout, a crashed worker or an out-of-memory failure is passed on and never stored. |
| **Machines differ, and are busy with other things.** A balancer that knows nothing sends a 4-core laptop as much as a 32-core workstation, and keeps sending to a box that has started to swap. | Each server gets [at most as many checks as it has workers](docs/haproxy.md), and the rest wait in one queue instead of timing out on a busy box. A small [usage agent](docs/usage-agent.md) on each box reports idle CPU and available memory, and HAProxy scales that box's share by it, down to draining a box that runs out of memory. A box that also runs other work states [how many checks fit beside it](docs/usage-agent.md#open-slots-a-box-that-also-does-other-work), counted in cores, and is sent no more. |
| **Retries can change what you measure.** A balancer that retries a slow check elsewhere turns "timed out" into "passed on a faster box". | A Lean timeout is passed on as it is and [never retried](docs/haproxy.md#failover-among-the-lean-servers). Only a check that got no answer from Lean (a lost connection, a crashed worker, a gateway error) goes to another server, at most twice. |
| **A server on the wrong version answers confidently and wrongly**, and behind a balancer its answers look like everyone else's. | A server is [tested alone](docs/admission.md) against proofs that must verify and near misses that must not, before it is added. |

The pool is also built to lose parts without losing checks. The cache is optional at run time:
stop it and checks flow straight to the Lean servers, uncached. A Lean server that goes away
costs the checks it held a retry, not a failure.

**When it is not for you.** lean-pool speaks Kimina's API and nothing else. With a single Lean
server it still gives you the cache, the queue and the admission test, but nothing to balance.
If you run identical, autoscaled cloud VMs and rarely send the same proof twice, Kimina's own
deployment guide is the simpler answer.

### Measured in use

lean-pool has been in use since 3 October 2026 in the one deployment it was written for, a
prover-training workload. What that pool has shown so far, each observed once, none of it a
benchmark:

* **43% of checks never needed a Lean worker.** Read on 5 October 2026 from a pool of four
  machines and 37 workers: of the 982,613 checks the cache had counted since it last started,
  at most a day and a half earlier, 403,933 were answered from its store and 22,011 joined an
  identical check already in flight. The store held 781,151 results in 853 MB.
* **A repeated batch costs nothing.** On 3 October 2026, with one 8-worker server: a batch of
  40 proofs took 91.1 s through the pool the first time and 0.1 s the second. With the cache
  stopped, the same batch was answered by the Lean server in 86.6 s, every check, with no
  client change.
* **No overhead of the proxy was visible.** Straight to that server the batch took 99.3 s, so
  the pool was not slower than the server alone, within the variation between single runs. On
  4 October 2026, over TLS, HAProxy's own timers for 40 checks show 0 ms in its queue and 13 ms
  in all to connect to the Lean server, mutual TLS handshakes included, out of 102 s.
* **Less Lean time, and no verdict changed.** On 12 October 2026 each Lean server box
  compiled the one step of `linarith` where most of the deployment's Lean time went
  ([compiled search](docs/compiled-search.md)). Of 86 checks a run had sent, sent again to
  one server both ways, the 71 that finished both ways took 608 s of Lean time with the
  step off and 217 s with it on.

How throughput grows with machines has not been measured.
[docs/verification.md](docs/verification.md) has the full record and what remains unverified.

## See it work in a minute

The demo runs a whole pool with two stand-in Lean servers, so there is no Lean or Mathlib image
to build. You need Docker with compose, and `bash`, `curl` and `awk` for the walkthrough.

```sh
git clone https://github.com/cabloo/lean-pool.git && cd lean-pool
docker compose -f examples/demo/compose.yaml up --build -d
examples/demo/walkthrough.sh
```

The walkthrough sends checks and shows what the pool does with each: a first check takes the
stand-in's 2 seconds, the same proof again comes back in milliseconds marked `"cached": true`,
a Lean timeout is passed on and never stored, and the pool goes on answering while a Lean
server, and then the cache, is stopped. It stops at the first step that does not behave as
described.

```
== 3. The same proof as another attempt: answered from the cache, under the new id
   HTTP 200 in 0.001701 s
   {"results": [{"id": "attempt-2", "time": 2.0, "response": {...}, "cached": true}]}
```

The stand-ins speak Kimina's interface and judge nothing ([what they are](examples/demo)).
Everything else in the demo is what a real pool runs. Stop it with
`docker compose -f examples/demo/compose.yaml down -v`.

## Run a real pool

The [installers](deploy/install/README.md) set up a pool on Linux machines with Docker and root,
TLS throughout. Copy the [example site](deploy/install/site.example), which names the Lean server
image every box runs, and on the pool's host, in a clone of this repository:

```sh
cd deploy/install
sudo bash install.sh --site ~/my-site --instance upstream
sudo bash install.sh --site ~/my-site --proxy upstream --admit-cases ../../examples/admission-cases
```

The first line installs a Lean server, the second the pool in front of it. Each further box
joins with one command, which `sudo lean-pool-ctl upstream join-open` prints for it.
[docs/deployment.md](docs/deployment.md) is the guide: the site directory, joining, operating,
upgrading, sizing, and what to watch.

**Whoever holds the API key can run code on every Lean server box**, because checking Lean code
runs it: [SECURITY.md](SECURITY.md) is the next read.

## Use it

Exactly like a single Kimina server. A pool the installers set up answers over HTTPS under its
own authority, whose certificate `sudo lean-pool-ctl upstream ca > ca.crt` prints:

```sh
curl -s --cacert ca.crt https://pool.example:18100/api/check \
    -H "Authorization: Bearer $(cat api-key.txt)" -H 'Content-Type: application/json' \
    -d '{"snippets": [{"id": "attempt-1", "code": "theorem two : 1 + 1 = 2 := by rfl"}], "timeout": 60}'
```

Kimina's own Python client works unchanged, pointed at the pool; here the
[demo](examples/demo)'s, which is plain HTTP ([TLS](docs/tls.md#clients) says what a Python
client needs for an HTTPS pool):

```python
from kimina_client import KiminaClient

client = KiminaClient(api_url="http://localhost:18100", api_key="...")
client.check("theorem two : 1 + 1 = 2 := by rfl")
```

A client that knows it is talking to a pool can do two things more: mark its work as
[background](docs/clients.md#priority) with one header, so that it never delays a normal check,
and [size itself](docs/clients.md#the-pools-size) from what every answer says about the pool
(`X-Lean-Pool-Workers: 37`). [examples/pool_client.py](examples/pool_client.py) does both in
about 250 lines.

The same address answers a Prometheus scraper, without the key: what every server is doing,
the cache's counters, and each certificate's last day
([metrics](docs/haproxy.md#metrics-for-prometheus)).

## Documentation

| | |
|---|---|
| [Deploying a pool](docs/deployment.md) | From nothing to a running pool, and how to operate it. |
| [Using a pool](docs/clients.md) | The API, how to read each reply, priorities, the pool's stated size. |
| [TLS](docs/tls.md) | The pool's own certificate authority: what is encrypted and who holds which key. |
| [Joining over the network](docs/joining.md) | What happens when a new box joins, and the rules the join service keeps. |
| [Cache semantics](docs/cache.md) | The key, what is stored and what never is, bypass, single flight, and the screened door: a second way in, through the screen, for machines you do not own. |
| [The usage agent](docs/usage-agent.md) | The weight formula, what HAProxy does with it, and open slots for a box that also does other work. |
| [The generated HAProxy configuration](docs/haproxy.md) | Pausing a server, the health check, failover, buffers, name resolution, timeouts, losing the cache, metrics for Prometheus. |
| [Admission](docs/admission.md) | Testing a Lean server alone before it joins. |
| [Compiled search for linarith](docs/compiled-search.md) | An optional step on each Lean server box that makes Lean itself faster on the proofs that take it longest, and changes no verdict. |
| [The worker sandbox](docs/worker-sandbox.md) | An optional step on each Lean server box: every worker in a sandbox of its own, so the code a check runs cannot reach into the other workers' pipes or memory, nor read the server's key. |
| [Configuration reference](docs/configuration.md) | Every flag and environment variable of the six commands. |
| [What is tested, and what is not verified](docs/verification.md) | The test suite, the deployment's record, and the gaps. |
| [Limitations](docs/limitations.md) | What the pool does not do. |

The words these pages use for the pool's parts are in the
[documentation's index](docs/README.md#the-words-used-here).

## The six commands

Each is usable alone. HAProxy itself is used as it ships (version 3.0); lean-pool writes its
configuration.

| Command | What it does |
|---|---|
| `leanpool-haproxy-config` | Keeps the server list and renders a complete, commented `haproxy.cfg` from it (and the configuration of a box's TLS front). Standard library only. |
| `leanpool-cache` | The result cache: aiohttp in front of one SQLite file, with a size cap and least-recently-used eviction. |
| `leanpool-agent` | The usage agent a Lean server box runs. Reads `/proc`, answers HAProxy's agent check. Standard library only. |
| `leanpool-admit` | The admission test. |
| `leanpool-pki` | The pool's certificate authority: issues, signs and inspects certificates. |
| `leanpool-join` | An HTTPS service that carries a new box's signing request to the pool and its certificate back. It executes nothing: the script a box runs and whatever acts on a request are yours. |

The code is typed (`mypy --strict`), linted, and held to about 2,200 test cases, among them
ones that run a real HAProxy on the generated configuration and take servers and the cache away
under load, and ones that run the installers on a fake machine.

## Status

Version 0.1, new as a public project. It has been tested against one build of Kimina (from
source, with Lean 4.27) and HAProxy 3.0. The interfaces described in the documentation are what
the tests hold it to; they may still change before 1.0, and [CHANGELOG.md](CHANGELOG.md) will
say when they do. Bug reports and questions are welcome in the
[issue tracker](https://github.com/cabloo/lean-pool/issues); see
[CONTRIBUTING.md](CONTRIBUTING.md) to work on it and [SECURITY.md](SECURITY.md) to report a
vulnerability.

## Credits and license

Written by [Zane Hooper](https://github.com/cabloo).

lean-pool exists to sit in front of the
[Kimina Lean Server](https://github.com/project-numina/kimina-lean-server) by Project Numina
(MIT license), which does the actual work of checking Lean. This project contains none of its
code; it speaks its HTTP interface. The proxy is [HAProxy](https://www.haproxy.org/).

lean-pool is released under the [MIT license](LICENSE).
