# The generated HAProxy configuration

HAProxy is used as it ships; everything the pool asks of it is in one generated file. This page
says what is in that file and why. The file itself is commented throughout, and it helps to
read one beside this page: [the demo's](../examples/demo/haproxy.cfg) is a complete example.

`leanpool-haproxy-config render` writes a complete `haproxy.cfg` in HAProxy 3.0 syntax:

* `global`: `tune.bufsize` and `maxconn` (see [Buffers and memory](#buffers-and-memory)).
* `resolvers pool_dns`: how server names are looked up while the proxy runs (see
  [Server names](#server-names)).
* `frontend lean_pool` on the public port. `GET /health` is answered by HAProxy itself from
  `nbsrv(checkers)`: 200 if at least one Lean server is taking checks, 503 otherwise, so a health probe
  never queues behind proofs and does not depend on the cache. `default_backend cache`;
  `use_backend checkers` when the cache server is down, when the request carries the loop-guard
  header, and for every path other than `/api/check` (so the rest of Kimina's interface still
  works, uncached). `/status` is the cache's, and is answered 503 by HAProxy while the cache is
  down. The body of `/health` and three response headers on every answer carry the open slots
  on servers taking checks, the checks queued and the servers taking checks. The first is
  summed per listed server: the checks it holds plus the room under its limit (`srv_conn` and
  `srv_conn_free`; HAProxy has no fetch for the limit itself), counted when the server is up
  and its weight is not 0 (`srv_is_up`, `srv_weight`). The others are `queue(checkers)` and
  `nbsrv(checkers)`. HAProxy's `nbsrv` leaves out a server that is drained or at weight 0, and
  `srv_is_up` does not: a server drained by an agent that states
  [open slots](usage-agent.md#open-slots-a-box-that-also-does-other-work) (`drain 0%`) leaves
  both numbers, and one drained by a bare `drain` leaves the servers and stays in the first.
  A `GET` of `/metrics`, `/metrics/cache` and `/metrics/pool` is answered for a Prometheus
  scraper, without the key (see [Metrics for Prometheus](#metrics-for-prometheus)).
* `frontend checkers_door` on `127.0.0.1` only: sets the loop-guard header, uses `checkers`.
* `backend cache`: the cache server, health-checked every second and marked down on its first
  failed connection, with no connection cap; and the checkers door as its `backup` server (see
  [Losing the cache](#losing-the-cache)).
* `backend cache_metrics`: the cache server alone, for `GET /metrics/cache`. No backup and no
  retry; `track cache/cache` gives it the cache server's state without a second health check.
* `backend checkers`: `balance leastconn`; per server `check`, `maxconn <workers>`,
  `weight <workers x factor>` and `agent-check agent-port <port> agent-inter 5s`;
  `option httpchk GET /health` with `timeout check 15s` and
  `default-server inter 5s fall 4 rise 1` (see [The health check](#the-health-check)); `retries 2`,
  `retry-on conn-failure empty-response 500 502 503 504` and `option redispatch 1` (see
  [Failover](#failover-among-the-lean-servers)); and
  `http-request set-priority-class int(100)` for a check that carries
  `X-Lean-Priority: background`, which puts it behind every normal check in this backend's queue.
* `listen stats` on `127.0.0.1` only.

## A paused server

`leanpool-haproxy-config pause --servers FILE NAME` keeps a server in the list and stops the
pool sending it anything; `resume` undoes it. Like `add` and `remove`, both edit the list alone:
render again, validate, and reload HAProxy. A paused server:

* is declared `disabled` in `backend checkers`, with **no** `agent-check`, and with TLS it has
  no agent tunnel. It is sent no check, no health check and no agent check, and HAProxy's
  statistics show it as `MAINT`;
* is not up, so it adds nothing to the workers and servers the pool reports
  ([the pool's size](clients.md#the-pools-size)): a client that follows the pool's size shrinks
  with it;
* stays paused across a reload and a restart, because the pause is in the list and not in the
  running proxy;
* changes nothing about the other servers: their lines in the list and in the configuration
  are what they were (weights are scaled by the largest *listed* server, and every agent tunnel
  keeps its port).

A reload does not drop a check: HAProxy's old process finishes the checks it holds, including
those running on the paused server and those already waiting in its queue, and takes no new
one. The last server taking checks cannot be paused, and a list of paused servers only is
refused: a pool with no server answers nothing.

The agent check is left out on purpose. The usage agent says `ready` with every normal reply,
because only an agent can undo its own earlier `drain`, and HAProxy takes `ready` from an agent
as the end of a maintenance. On HAProxy 3.0.29 a `disabled` server that keeps its agent check is
back in rotation within one agent interval. For the same reason, do not pause a server with
HAProxy's runtime `set server ... state maint`: the agent undoes that too, and a reload
forgets it.

Before resuming a server that was paused because it misbehaved, run the
[admission test](admission.md) against it alone.

## The health check

Every Lean server is asked `GET /health` every 5 seconds, has 15 seconds to answer, is called
down after 4 misses in a row and up again after 1 answer. That is patient on purpose. A Lean
server that shares its box with other work answers `/health` slowly while it is busy, and taking
it out for that helps nobody: the checks it holds go on running, and when it comes back it is
handed a full burst. How many checks a busy box is sent is the usage agent's matter
([open slots](usage-agent.md#open-slots-a-box-that-also-does-other-work)), not the health
check's.

The price is a slower notice of a server that has really stopped: about 20 seconds when its port
refuses connections, up to about 100 when it accepts them and says nothing. No check is lost to
that. One sent meanwhile to a port that refuses goes to another server (next section), and one
sent to a server that hangs ends as the proxy's own gateway error, which a client asks again.

## Failover among the Lean servers

A check that failed **without Lean's answer** is sent again, to a different server when one has
a free worker (`option redispatch 1`):

* the connection could not be made (`conn-failure`), or was closed with no reply
  (`empty-response`): the server went away;
* the server answered 500, which is what Kimina answers when a worker crashed, or a gateway
  error (502, 503, 504).

`retries 2` allows at most two retries. A proof that crashes every worker it meets is therefore
stopped after three workers, and the caller then gets the 500.

**A Lean timeout is never retried.** Kimina reports it as HTTP 200 with the error in the body,
so to HAProxy it is a normal reply, and that is deliberate:

* it is Lean's answer for the time the client allowed, not a failure of the server;
* a second try would hold another worker for the full timeout, exactly when the pool is slowest;
* it would change measurements: a proof that timed out on one box and passed on a luckier one
  would make pass rates depend on how many servers the pool has.

For the same reasons HAProxy's own `response-timeout` (no reply within `timeout server`) is left
out of `retry-on`.

HAProxy can replay a request only if it had the whole of it, headers and body, in one buffer
when it first sent it. Two settings make that true for long proofs: the buffer is 256 KiB instead
of HAProxy's default 16 kB (`--max-request-bytes`), and `option http-buffer-request` makes
HAProxy wait for the complete request before it chooses a server. A request that does not fit
is still forwarded, and still retried when the connection could not be made (nothing had been
sent), but it is not replayed after a server received it.

## Buffers and memory

Larger buffers cost memory, so the generated `maxconn` is lowered with them. The worst case,
with every connection holding full buffers:

```
maxconn x buffers per connection x tune.bufsize
 1024   x          3             x   262144 bytes   =   768 MiB
```

Two buffers per connection is the figure in HAProxy's manual (request and response); the third
is the copy of the request kept for a replay. The manual's advice is to lower `maxconn` by the
factor `tune.bufsize` is raised, so change one with the other in mind; the generated file states
this arithmetic for the values it was rendered with.

A check that goes through the cache holds two of these connections (the client's and the
cache's), so the default allows about 500 checks in flight or waiting in HAProxy's queue.

## Server names

A server may be given by name or by IPv4 address.

* **At start**, a name is resolved by the system's resolver. If it does not resolve, that
  server starts as down and the proxy starts all the same (`init-addr last,libc,none`).
* **While running**, HAProxy asks the name servers in its `/etc/resolv.conf` for every server
  name again each 10 seconds (`resolvers pool_dns`, `parse-resolv-conf`,
  `timeout resolve 10s`), so a box whose address changed, such as one on DHCP, is followed, and
  a server that started unresolved comes up once its name resolves.
* **A failed lookup does not take a server out.** DNS says where a server is; only its health
  check says whether it is up. The server keeps its last address through a DNS outage for 24
  days (`hold nx`, `hold refused`, `hold timeout`, `hold other`), close to the longest period
  HAProxy accepts.
* IPv4 is preferred when a name has both kinds of address (`resolve-prefer ipv4`), because Kimina
  listens on IPv4 by default.
* A server given as an IPv4 address has no name to look up; none of this applies to it.

One trap: HAProxy's own DNS client only asks name servers. It does not read `/etc/hosts`, apply
search domains or use mDNS. A name that only the system's resolver knows resolves at start, is
never refreshed, and after 24 days of failed lookups HAProxy takes that server out. Give such a
server by IPv4 address, or use a name the DNS server answers as written.

## Losing the cache

Losing the cache is meant to cost no check:

* The first failed connection to the cache marks it down at once
  (`observe layer4 error-limit 1 on-error mark-down`) instead of waiting for its health check.
* The request that met the dead cache is sent again (`retry-on conn-failure empty-response`,
  `option redispatch`) to the `backup` server of the same backend, which is the checkers door:
  it gets the loop-guard header there and goes on to a Lean server. HAProxy pauses about one
  second before that retry.
* A request the cache had already received when it died is replayed the same way, if it fits in
  one buffer.
* From then on `use_backend checkers if !cache_up` sends checks straight to the Lean servers.
  `cache_up` is `srv_is_up(cache/cache)`, the cache server itself: `nbsrv(cache)` would count
  the backup and never report the cache as down.
* The cache is used again after two successful health checks, one second apart.

The price: a single failed connection to a cache that is alive also takes it out, for about two
seconds, during which checks are answered uncached.

## Timeouts

Timeouts are derived from the largest Lean timeout clients send, so the proxy never cuts a check
the Lean server would still have answered:

| Timeout | Value | With the defaults |
|---|---|---|
| `timeout server` (checkers) | server wait + 2 x Lean timeout + margin. Kimina allows the header import and the body the full Lean timeout each. | 210 s |
| `timeout queue` | 2 x Lean timeout + margin, unless `--queue-timeout` is given | 150 s |
| `timeout server` (cache) and `timeout client` | queue + checkers + margin | 390 s |

The cache's `--upstream-timeout` must be at least queue + checkers (360 s with the defaults);
the generated file states the number in its first lines.

## Statistics

HAProxy's statistics are on the loopback port only. From the cache container, which shares the
proxy's network namespace in the [compose example](../deploy/pool/compose.yaml), in
`deploy/pool`:

```sh
docker compose exec cache \
    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18103/;csv').read().decode())"
```

## Metrics for Prometheus

A scraper on another machine has neither the pool's key nor a place on the proxy's loopback. So
the public port answers three more paths, read-only and **without the key**, as it answers
`GET /health`. All three are `text/plain; version=0.0.4`.

| Path | Answered by | What it holds |
|---|---|---|
| `GET /metrics` | HAProxy's own Prometheus exporter, in the frontend | What each server, backend and frontend is doing, and the process: `haproxy_server_status{proxy,server,state}`, `haproxy_server_current_sessions`, `haproxy_server_limit_sessions`, `haproxy_backend_current_queue` and the rest of the exporter's names. |
| `GET /metrics/cache` | the cache service, through `backend cache_metrics` | The cache's counters and sizes ([Cache semantics](cache.md)). |
| `GET /metrics/pool` | HAProxy, from a file beside the configuration | What the server list and the certificates said when the configuration was rendered: `leanpool_server_listed_workers{server}`, `leanpool_server_paused{server}` (1 or 0) and `leanpool_certificate_not_after_seconds{certificate}` (seconds since 1970). |

```sh
curl --cacert ca.crt https://pool.example:18100/metrics
```

**None of them queues or reaches a Lean server.** `/metrics` and `/metrics/pool` end in the
frontend. `/metrics/cache` goes to the cache server and to nothing else: `backend cache` has a
backup that leads to the Lean servers, which is right for a check and wrong for the cache's
counters, so this path has a backend of its own with the cache server alone. While the cache
server is down HAProxy answers 503 `the cache is down` itself; a scrape that meets a cache that
died that instant gets HAProxy's own 503, with no retry.

**GET and HEAD only, on the exact path.** Any other method and any other path (`/metrics/`,
`/metrics/anything`) is routed as before these routes existed, which for everything but
`/api/check` means to a Lean server. Before them, a `GET` of these three paths went to a Lean
server too, so the front door now shadows whatever a Lean server answers there. Kimina serves
none of the three (it answers 404).

**The exporter's answer is fixed by the configuration.** The scraper's query string is replaced
(`http-request set-query scope=global&scope=frontend&scope=backend&scope=server`) before the
exporter reads it. Left to the caller, `scope=resolvers` would return each name server's
address as a label, and `no-maint` would hide a paused server. A paused server is
`haproxy_server_status{...,state="MAINT"} 1`. HAProxy's counters restart at 0 on every reload
and `haproxy_process_start_time_seconds` moves with it; with TLS the agent tunnels appear as
proxies of their own (`agent_tunnel_NAME`), and the cache server appears twice, under
`proxy="cache"` and `proxy="cache_metrics"`.

**No key, no proof and no address** is in any of the three: server and certificate names from
the list, and numbers. Whoever can reach the public port can read them.

**The pool's facts are a file.** `render --pool-metrics FILE [--certificates DIR]` writes it
from the same server list as the configuration, and the configuration then holds

```
global
    default-path config
frontend lean_pool
    http-request return status 200 content-type "text/plain; version=0.0.4" file pool.metrics if METH_GET is_pool_metrics
```

HAProxy reads the file when it reads the configuration, at start and at every reload, and
refuses a configuration whose file is missing: install the file **before** the configuration
that names it, beside it, readable by HAProxy's user. The server list stays the single source:
after `pause`, `resume`, `set-workers`, `add` or `remove`, render again (which writes both),
validate, install both, reload. HAProxy keeps the file in one buffer, so `render` refuses facts
larger than half of `--max-request-bytes` (128 KiB with the default; a server takes about a
hundred bytes) and writes nothing. Without `--pool-metrics` the configuration has no rule for
`/metrics/pool` and that path goes where it went before.

A certificate is named by its path under `--certificates` (`ca.crt`, `servers/lean-a.crt`), and
its date is read with the standard library alone (the generator has no third-party
dependency): the one field, straight from the certificate's encoding, with no signature
checked. A `*.crt` file that is not a certificate gets **no sample** (not a zero), and `render`
says so on standard error and goes on.

**HAProxy must be built with the exporter** (`USE_PROMEX=1`; `haproxy -vv` then lists
`prometheus-exporter` under `Available services`). The official `haproxy` images are built with
it; ask any other build. One built without it refuses the generated configuration, with or
without `--pool-metrics`.

Every scrape is logged by HAProxy like any other request.

## With TLS

Given the three TLS files ([TLS](tls.md)), `render` writes the same configuration with these
differences, and nothing else changes:

* its first lines say what is encrypted. A configuration rendered without TLS starts with
  `# UNENCRYPTED:` instead;
* `global`: `ssl-default-bind-options ssl-min-ver TLSv1.3` and
  `ssl-default-server-options ssl-min-ver TLSv1.3`, so that a line a later edit adds is not
  below TLS 1.3 either. Every generated TLS line also says `ssl-min-ver TLSv1.3` itself;
* `frontend lean_pool`: `bind :18100 ssl crt <front door> ssl-min-ver TLSv1.3`;
* `backend checkers`: each server line gains
  `ssl verify required ca-file <authority> crt <client certificate>`, `sni str(<name>)` and
  `verifyhost <name>`, where `<name>` is the server's name in the list in lower case, and
  `check check-ssl check-sni <name>` so that the health check is the same mutual TLS;
* the agent check of each server becomes
  `agent-check agent-addr 127.0.0.1 agent-port <tunnel port>`;
* after every other section, `defaults agent_tunnels` (TCP mode, 10 s timeouts, only failures
  logged) and one `listen agent_tunnel_<name>` per server: bound to `127.0.0.1:<tunnel port>`,
  with one server, the box's agent port, reached with the same mutual TLS options and the same
  name check as the Lean server.

**Tunnel ports** are the first free ports counting up from `--agent-tunnel-port` (18300), in the
order of the server list, skipping the pool's own ports (public, checkers, statistics and the
cache's). The same list and options always give the same ports, and a server appended to the
list changes no other server's port. Removing a server moves the ports of the servers after it;
the listeners and the agent checks that use them are always rendered together.

If a box's agent cannot be reached, the tunnel closes the agent check's connection without a
reply. HAProxy ignores an empty reply, so the server keeps its last weight, as it does without
TLS.

With TLS these are refused, each because it would leave a hop unencrypted or an identity
unclear: a cache that is not on a loopback address; a server name that is not a DNS name; two
server names that differ only in letter case; a certificate path that is not a plain absolute
path; tunnels that do not fit below port 65535.

## A box's TLS front

`render-box` writes the configuration of the HAProxy that stands in front of one box's Lean
server and usage agent:

* `defaults`: `mode tcp`. The front carries bytes and never reads a request, so nothing about a
  check (its size, its headers, how long it takes) is limited or changed there. A Lean server
  that is gone shows to the pool proxy as a connection closed without a reply, which it retries
  on another server like a refused connection;
* `listen lean` and `listen agent`, each with
  `bind :<port> ssl crt <box certificate> ssl-min-ver TLSv1.3 ca-file <authority> verify required`:
  a caller without a certificate signed by the pool's authority is refused by the handshake;
* in both, `tcp-request session reject unless { ssl_c_s_dn(cn) -m str <proxy client name> }`
  before anything is forwarded;
* in both, one plain `server`: the Lean server and the agent, by `HOST:PORT`. Their names are
  looked up again while the front runs, as the pool proxy does for its servers, so a container
  that was restarted is found at its new address.

While Lean works on a check nothing is sent in either direction, so in TCP mode the front's
`timeout client` and `timeout server` must outlast the slowest check. They are the pool proxy's
own limit for a Lean server plus the margin once more (240 s with the defaults, against the
proxy's 210 s): the proxy always gives up first, and the front never cuts a check the proxy
would still have waited for. Render the front with the pool's `--lean-timeout`,
`--server-wait` and `--margin`. The agent's listener has 10 s timeouts.
