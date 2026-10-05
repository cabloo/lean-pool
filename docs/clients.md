# Using a pool

A pool speaks the Kimina Lean Server's HTTP interface on one address. A client that works
against a Kimina server works against a pool by changing that address, and need not know there
is a pool. This page is what a client *can* know.

## The interface

| Request | Answered by | What it does |
|---|---|---|
| `POST /api/check` | the cache, or a Lean server | Checks Lean code. Kimina's request and reply, unchanged, with two additions below. |
| `GET /health` | HAProxy itself | 200 while at least one Lean server is up, 503 otherwise, with the [pool's size](#the-pools-size) in the body. Needs no key and never waits behind checks. |
| `GET /status` | the cache | The cache's [counters](cache.md). Needs the key. 503 while the cache is down. |
| anything else | a Lean server | The rest of Kimina's interface passes through, uncached. |

```sh
curl -s http://pool.example:18100/api/check \
    -H "Authorization: Bearer $(cat api-key.txt)" -H 'Content-Type: application/json' \
    -d '{"snippets": [{"id": "attempt-1", "code": "theorem two : 1 + 1 = 2 := by rfl"}], "timeout": 60}'
```

```json
{"results": [{"id": "attempt-1", "time": 0.410432, "response": {"env": 0}}]}
```

That is Lean accepting the proof: a `response` with no error among its `messages` (here, with
no messages at all) and no `sorries`.

The two additions, both optional:

* **`"cached": true`** appears in a result that was answered from the cache's store. Everything
  else in it, `time` included, is the original check's.
* **`"no_cache": true`** in a request skips the cache for that request: no lookup, nothing
  stored, and its own run on a Lean server.

[Cache semantics](cache.md) says exactly which checks are the same check and which answers are
stored.

## From Python

Kimina's own client works unchanged (`kimina-client` 0.2.1 was run against a pool this way):

```python
from kimina_client import KiminaClient

client = KiminaClient(api_url="http://pool.example:18100", api_key="the-pool-api-key")
reply = client.check(["theorem two : 1 + 1 = 2 := by rfl"], timeout=60, batch_size=1)
```

[`examples/pool_client.py`](../examples/pool_client.py) is a small asynchronous client written
for a pool. It sends one file per request, follows the pool's stated size, can mark its work as
background, and never mistakes "the pool did not take this check" for a failed proof. Its tests
are in [`tests/test_example_client.py`](../tests/test_example_client.py). Copy what is useful.

## How to send checks

* **One snippet per request.** The pool gives each Lean server as many requests as it has
  workers. A request with several snippets is split by the cache into one upstream request per
  snippet, and with the cache down it reaches a Lean server whole and occupies several workers
  through one slot. Pass `batch_size=1` to Kimina's client.
* **A little more in flight than the pool has workers**, a fifth or a quarter more, so that no
  worker waits for the client. More than that only waits in HAProxy's queue. The pool says how
  many workers it has ([below](#the-pools-size)).
* **Wait long enough.** A check may wait in the queue and then run. With the default settings
  that is up to 360 s; the generated `haproxy.cfg` states the numbers for yours in its first
  lines ([timeouts](haproxy.md#timeouts)).
* **With [TLS](tls.md)**, trust the pool authority's certificate and nothing else needs to
  change: `curl --cacert ca.crt https://pool.example:18100/health`, or
  `ssl.create_default_context(cafile="ca.crt")` in Python.

## Reading the reply

| Reply | Meaning | What to do |
|---|---|---|
| 200, the result has a `response` | Lean's answer: the code was accepted, or rejected with error `messages` or a `sorry`. | Record it. |
| 200, the result has an `error` | No answer from Lean. Usually a Lean timeout, which Kimina reports this way. The pool neither retries nor stores it. | Record "no answer", not a failed proof. |
| 401 | The API key is missing or wrong. | Fix the client. |
| 422 | The request is malformed. | Fix the client. |
| 429 | A Lean server had no free worker within its own wait. The pool passes it on. | Pause and ask again. |
| 500 | A Lean worker crashed on this check, on up to three servers in turn. | Do not ask again at once: the check itself is the likely cause. |
| 502, 503, 504 | The pool did not take the check: no Lean server is up, the check waited the queue's limit, or the Lean servers could not be reached. | Pause and ask again; record nothing. |

A pool that loses a Lean server or its cache does not show it in these replies: the check is
sent elsewhere ([failover](haproxy.md#failover-among-the-lean-servers),
[losing the cache](haproxy.md#losing-the-cache)).

## Priority

When every worker is busy, checks wait in HAProxy's queue in arrival order, so a
client's share of a busy pool is its share of the requests in flight. A client whose work can
wait says so on each check:

```
X-Lean-Priority: background
```

A waiting background check is taken only when no normal check is waiting. The value is matched in
any letter case; any other value, and no header at all, is a normal check, so an old client and a
mistyped value behave as before. What it does not do:

* It never interrupts a check that is running, and a background check that has a worker keeps it.
  A client that starts while the pool is full of background checks waits for the first of them
  to finish.
* It does not shorten the wait limit. A background check that waits `timeout queue` gets the same
  503 as any check. Pause and ask again; do not record it as an answer. Under a client that
  keeps the pool full, background work makes no progress.
* The cache forwards the header on its second hop. A normal check never waits on a background
  check's flight for the same file: it sends its own request, and later callers join that one.
  The cost is one repeated check, and only when both kinds of client send the same file with the
  same timeout at the same moment.

## The pool's size

Every answer that leaves the public port carries three response headers,
computed by HAProxy as the answer leaves:

| Header | What it counts |
|---|---|
| `X-Lean-Pool-Workers` | workers on the Lean servers that are up now |
| `X-Lean-Pool-Queued` | checks waiting in the queue for a worker, of both priorities |
| `X-Lean-Pool-Servers` | Lean servers that are up now |

`GET /health` carries the same numbers in its body, with the status codes it always had:

```json
{"status":"ok","workers":12,"queued":0,"servers":2}
```

So a client can size itself before its first check, and follow a server that joins or drops
without being restarted. Keep a little more in flight than there are workers (a fifth or a
quarter more), so that no worker waits for the client.

The numbers are **advisory**. Nothing in the pool reads them back: routing, the queue, each
server's limit and the cache are what they are without them. A client must carry on with its own
configured number when a header is missing, empty or not a whole number (a pool that runs an
older configuration sends none), and should never exceed a ceiling of its own, whatever the pool
says. `leanpool.signals` holds the names and two small readers (`capacity_from_headers`,
`capacity_from_health`) that return `None` for anything they cannot read. A server that its usage
agent has drained still counts while it is up: the number is the pool's size, not a promise of an
idle worker.
