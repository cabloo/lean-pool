# lean-pool

[![CI](https://github.com/cabloo/lean-pool/actions/workflows/ci.yml/badge.svg)](https://github.com/cabloo/lean-pool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

**One address for a pool of Lean 4 proof checkers.**

lean-pool turns any number of
[Kimina Lean Servers](https://github.com/project-numina/kimina-lean-server), on machines of
different sizes, into one endpoint that speaks Kimina's own API. HAProxy does the routing.
lean-pool adds what a generic load balancer cannot:

* a **shared result cache** that knows which of Lean's answers are safe to give again;
* **load balancing by real headroom**: every machine reports its idle CPU and free memory, and
  receives checks in proportion;
* **failover that never changes a result**: a check is sent to another server only when no
  answer from Lean came back;
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

## Why

Checking proofs is usually the slowest step of training or evaluating a Lean prover: a GPU
writes candidate proofs faster than Lean can check them. Kimina Lean Server checks them well,
on one machine. Its deployment guide scales out with a cloud load balancer in front of a
managed group of identical VMs. That leaves gaps, and they are widest when the hardware
is whatever you have: a workstation, two desktops and a laptop, each also used for other work.

| The gap | What lean-pool does |
|---|---|
| **The same proof is checked again and again.** Provers resubmit the same proof of the same statement across samples, epochs and reruns. Kimina has no cache between servers, and an ordinary HTTP cache cannot be one: a check is a `POST` identified by its body, every attempt carries its own id, and a Lean timeout comes back as HTTP 200. | The [cache](docs/cache.md) answers a repeat from its store, under the caller's own id, and lets identical checks in flight share one worker. It stores only what Lean would say again: a timeout, a crashed worker or an out-of-memory failure is passed on and never stored. |
| **Machines differ, and are busy with other things.** A balancer that knows nothing sends a 4-core laptop as much as a 32-core workstation, and keeps sending to a box that has started to swap. | Each server gets [at most as many checks as it has workers](docs/haproxy.md), so checks wait in one queue instead of timing out on a busy box. A small [usage agent](docs/usage-agent.md) on each box reports idle CPU and available memory, and HAProxy scales that box's share by it, down to draining a box that runs out of memory. |
| **Retries can change what you measure.** A balancer that retries a slow check elsewhere turns "timed out" into "passed on a luckier box", and pass rates start to depend on the size of the pool. | A Lean timeout is an answer and is [never retried](docs/haproxy.md#failover-among-the-lean-servers). Only a check that got no answer from Lean (a lost connection, a crashed worker, a gateway error) goes to another server, at most twice. |
| **A server on the wrong version answers confidently and wrongly**, and behind a balancer nothing shows which answers were its. | A server is [tested alone](docs/admission.md) against proofs that must verify and near misses that must not, before it is added. |

The pool is also designed to lose parts without losing checks. The cache is optional at run
time: stop it and checks flow straight to the Lean servers, uncached. A Lean server that goes
away costs the checks it held a retry, not a failure.

### Measured in use

lean-pool was written for a prover-training workload and has carried it since October 2026, at
the time of writing on four machines with 37 Lean workers and TLS on every hop. From that one
deployment, not from a benchmark:

* **43% of checks never needed a Lean worker.** Of 982,613 checks counted by the cache since
  it last started, 403,933 were answered from its store and 22,011 joined an identical check
  already in flight. The store held 781,151 results in 853 MB.
* **A repeated batch costs nothing.** A batch of 40 proofs took 91.1 s through the pool the
  first time and 0.1 s the second. With the cache stopped, the same batch was answered by the
  Lean server in 86.6 s, every check, with no client change.
* **The proxy costs nothing measurable.** For one such batch over TLS, HAProxy's own timers
  show 0 ms in its queue and 13 ms in all to connect to the Lean server, mutual TLS handshakes
  included, out of 102 s. The rest is Lean.

What has and has not been verified is listed, without rounding up, in
[docs/verification.md](docs/verification.md).

## See it work in a minute

The demo runs a whole pool with two stand-in Lean servers, so there is no Lean or Mathlib image
to build. You need Docker with compose, and `curl`.

```sh
git clone https://github.com/cabloo/lean-pool.git && cd lean-pool
docker compose -f examples/demo/compose.yaml up --build -d
examples/demo/walkthrough.sh
```

The walkthrough sends checks and shows what the pool does with each: a first check takes the
stand-in's 2 seconds, the same proof again comes back in milliseconds marked `"cached": true`,
a Lean timeout is passed on and never stored, and stopping a Lean server or the cache costs no
check. It stops at the first step that does not behave as described.

```
== 3. The same proof as another attempt: answered from the cache, under the new id
   HTTP 200 in 0.001701 s
   {"results": [{"id": "attempt-2", "time": 2.0, "response": {...}, "cached": true}]}
```

The stand-ins speak Kimina's interface and judge nothing ([what they are](examples/demo)).
Everything else in the demo is what a real pool runs. Stop it with
`docker compose -f examples/demo/compose.yaml down -v`.

## Run a real pool

You need one or more machines running Kimina Lean Server with the same Lean and Mathlib and the
same API key, and one machine for the pool (it can be one of them). In short:

```sh
# On each Lean server box: Kimina, and the usage agent beside it.
cd deploy/lean-server
printf 'LEAN_SERVER_API_KEY=%s\nLEAN_SERVER_MAX_REPLS=%s\n' 'the-pool-api-key' 8 > .env
docker compose up -d --build

# On the pool box: list the servers, render HAProxy's configuration, start the pool.
cd deploy/pool
uv run leanpool-haproxy-config add --servers servers lean-a lean-a.example:8000 8
uv run leanpool-haproxy-config add --servers servers lean-b lean-b.example:8000 4
mkdir -p haproxy
uv run leanpool-haproxy-config render --servers servers > haproxy/haproxy.cfg
printf '%s\n' 'the-pool-api-key' > api-key.txt
echo 'LEANPOOL_CACHE_PIN=lean4.27.0-mathlib4.27.0' > .env    # name YOUR versions
docker compose up -d --build
```

[docs/deployment.md](docs/deployment.md) is the full guide: testing each server before it
joins, adding and removing servers, reloading without dropping a check, and what to watch.
That quickstart is plain HTTP; [docs/tls.md](docs/tls.md) encrypts every hop.

## Use it

Exactly like a single Kimina server:

```sh
curl -s http://pool.example:18100/api/check \
    -H "Authorization: Bearer $(cat api-key.txt)" -H 'Content-Type: application/json' \
    -d '{"snippets": [{"id": "attempt-1", "code": "theorem two : 1 + 1 = 2 := by rfl"}], "timeout": 60}'
```

Kimina's own Python client works unchanged, pointed at the pool:

```python
from kimina_client import KiminaClient

client = KiminaClient(api_url="http://pool.example:18100", api_key="the-pool-api-key")
client.check("theorem two : 1 + 1 = 2 := by rfl")
```

A client that knows it is talking to a pool can do two things more: mark its work as
[background](docs/clients.md#priority) with one header, so that it never delays a normal check,
and [size itself](docs/clients.md#the-pools-size) from what every answer says about the pool
(`X-Lean-Pool-Workers: 37`). [examples/pool_client.py](examples/pool_client.py) does both in
about 250 lines.

## Documentation

| | |
|---|---|
| [Deploying a pool](docs/deployment.md) | From nothing to a running pool, and how to operate it. |
| [Using a pool](docs/clients.md) | The API, priorities, the pool's stated size, an example client. |
| [TLS](docs/tls.md) | The pool's own certificate authority; what is encrypted and who holds which key. |
| [Joining over the network](docs/joining.md) | Letting a new box request its certificate over HTTPS. |
| [Cache semantics](docs/cache.md) | The key, what is stored and what never is, bypass, single flight. |
| [The usage agent](docs/usage-agent.md) | The weight formula, and what HAProxy does with it. |
| [The generated HAProxy configuration](docs/haproxy.md) | Failover, buffers, name resolution, timeouts, losing the cache. |
| [Admission](docs/admission.md) | Testing a Lean server alone before it joins. |
| [Configuration reference](docs/configuration.md) | Every flag and environment variable of the six commands. |
| [What is tested, and what is not verified](docs/verification.md) | The test suite, the deployment's record, and the gaps. |
| [Limitations](docs/limitations.md) | What the pool does not do. |

## What is in the box

Six small commands, each usable alone. HAProxy itself is used as it ships; lean-pool writes
its configuration.

| Command | What it does |
|---|---|
| `leanpool-haproxy-config` | Keeps the server list and renders a complete, commented `haproxy.cfg` from it (and the configuration of a box's TLS front). Standard library only. |
| `leanpool-cache` | The result cache: aiohttp in front of one SQLite file, with a size cap and least-recently-used eviction. |
| `leanpool-agent` | The usage agent a Lean server box runs. Reads `/proc`, answers HAProxy's agent check. Standard library only. |
| `leanpool-admit` | The admission test. |
| `leanpool-pki` | The pool's certificate authority: issues, signs and inspects certificates. |
| `leanpool-join` | An HTTPS service through which a new box asks to join. It moves four messages and executes nothing. |

The code is typed (`mypy --strict`), linted, and tested by about 1,200 tests, among them ones
that run a real HAProxy on the generated configuration and take servers and the cache away
under load.

## Status

Version 0.1: in daily use by its author, young as a public project. The interfaces described in
the documentation are what the tests hold it to; they may still change before 1.0, and
[CHANGELOG.md](CHANGELOG.md) will say when they do. Bug reports and questions are welcome in
the [issue tracker](https://github.com/cabloo/lean-pool/issues); see
[CONTRIBUTING.md](CONTRIBUTING.md) to work on it and [SECURITY.md](SECURITY.md) to report a
vulnerability.

## Credits and license

lean-pool exists to sit in front of the
[Kimina Lean Server](https://github.com/project-numina/kimina-lean-server) by Project Numina
(MIT license), which does the actual work of checking Lean. This project contains none of its
code; it speaks its HTTP interface. The proxy is [HAProxy](https://www.haproxy.org/).

lean-pool is released under the [MIT license](LICENSE).
