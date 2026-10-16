# Cache semantics

Every check goes to the cache first. A check it has seen is answered from its store; the rest
are forwarded to the Lean servers through the proxy. The rule everything below follows from:

The cache must never change what a client measures. It only avoids recomputing an answer Lean
would give again.

**The key** is the SHA-256 of the JSON array `[pin, code, timeout]`:

* `pin` is the configured string naming the pool's Lean and Mathlib versions;
* `code` is the snippet's code, byte for byte;
* `timeout` is the request's Lean timeout in whole seconds (`60` and `60.0` are the same;
  a request without a timeout has its own key).

The snippet's `id` is not part of the key. Neither are `debug`, `reuse` or any other field.

**Stored:** only a definitive Lean answer, which is a result that has a `response` with Lean's
messages and no `error`. That includes proofs Lean rejected: a wrong proof is wrong every time.
What is stored is the result without its `id`: `time`, `response` and `diagnostics`.

**Never stored**, because a retry could change the answer:

* a result with an `error`: a Lean timeout (`Lean REPL command timed out ...`, which Kimina sends
  as HTTP 200) or a failed worker;
* a result where the REPL rejected the command before Lean judged the code (a bare
  `{"message": ...}` response);
* a result with a Lean message containing a resource-exhaustion pattern (by default
  `out of memory` or `stack overflow`, any case, any severity);
* any reply that is not HTTP 200, and any reply that is not exactly one result for the snippet.

**A hit** is returned under the caller's own `id`, with the original check's `time` and
`diagnostics`, and marked `"cached": true`. A miss is returned as the Lean server sent it, with
no `cached` field. `diagnostics` follow the caller's own `debug` flag as they do on Kimina
(the cache always asks the Lean server for them, so a stored answer can serve either kind of
caller).

**Bypass:** `"no_cache": true` in a request skips both the lookup and the store, and the request
is forwarded as it was sent, minus `no_cache` itself. A bypassed check always gets its own run on
a Lean server: it never waits for an identical check in flight. A request with a non-null
`infotree` is bypassed too, because the key does not cover that option.

**Single flight:** identical checks in flight at the same time wait for one upstream answer.
Each caller gets it under its own `id`. An answer that came from a Lean server this way is not
marked `cached`.

**Several snippets in one request** are split: each is looked up, forwarded and stored on its
own, and the results come back in request order. Like Kimina, the cache answers a whole request
with one status: if any snippet could not be answered, the reply is that failure, with the
upstream's own status and body. Snippets that did get a definitive answer are stored, so the
client's retry is cheap.

**Failures** reach the client unchanged: an HTTP error from the Lean servers is returned with
its own status and body, so a client's retry rules for 429 and 5xx work as before. If the Lean
servers cannot be reached the cache answers 502, and 504 if they do not answer within the
upstream timeout. Nothing from a failure is stored.

**Authentication:** the cache compares the request's Bearer token with its configured key, in
constant time, before it serves anything from `/api/check` or `/status`. It has to do this
itself: a request answered from the store never reaches a Lean server that would check the key.
On a miss the caller's `Authorization` header is forwarded. `/health` and `/metrics/cache` are
open: neither holds a key, a proof or an address.

**Other replies of the cache itself:** 401 without the key, 422 for a malformed request
(not a JSON object, no snippets, duplicate ids, a `timeout` that is not a whole number of
seconds, a `debug` or `no_cache` that is not a boolean) or one the screen refuses (below), 508
for a request that already carries the loop-guard header.

**The store** is one SQLite file in write-ahead-log mode. All SQLite work runs on a dedicated
thread, never on the event loop. The size cap counts the bytes of keys and payloads; when a
write takes the store above it, the least recently used entries are evicted (reading an entry
counts as using it). The file on disk is larger than that count by SQLite's own overhead and its
write-ahead log; `/status` reports both. A store that fails (a full disk) costs the cache, not
the check: the check is forwarded and answered, uncached.

**`GET /status`** returns

```json
{"hits": 0, "misses": 0, "coalesced": 0, "bypasses": 0, "in_flight": 0,
 "stored_entries": 0, "stored_bytes": 0, "file_bytes": 0, "evictions": 0,
 "maximum_bytes": 4294967296}
```

Every snippet counts in exactly one of `hits` (answered from the store), `misses` (forwarded),
`coalesced` (waited for an identical check in flight) and `bypasses`. `in_flight` is the number
of checks waiting for the Lean servers right now. Asked through the pool's public port while
the cache is down, `/status` is answered by HAProxy: 503 `{"status":"the cache is down"}`.

**`GET /metrics/cache`** returns the same ten numbers as Prometheus text
(`text/plain; version=0.0.4`), **without the key**: it is the one route of the cache beside
`/health` that asks for none, and it holds counts and sizes and nothing else. The counters
`leanpool_cache_hits_total`, `leanpool_cache_misses_total`, `leanpool_cache_coalesced_total`,
`leanpool_cache_bypasses_total` and `leanpool_cache_evictions_total` count since the cache
process started and start again at 0 when it does; the gauges are `leanpool_cache_in_flight`,
`leanpool_cache_stored_entries`, `leanpool_cache_stored_bytes`, `leanpool_cache_file_bytes` and
`leanpool_cache_maximum_bytes`. [Metrics for Prometheus](haproxy.md#metrics-for-prometheus)
says how the pool's public port answers it.

## The screened key

A pool can give the cache a second key (`--screened-api-key-file`), for senders it trusts less
than its own: machines whose owner it does not know. Checking Lean code runs it
([security](../SECURITY.md)), and the screen takes the easy ways of doing that away from them.

- **A request sent with it keeps to a short form.** It carries `snippets`, `timeout`, `debug`
  and `reuse` alone (`reuse` true and `no_cache` false where they are given), at most
  `--screened-in-flight` snippets, and a timeout of at most `--screened-max-timeout` seconds.
  Anything else is answered 422 and reaches nothing. So it cannot skip the store, ask for a
  costlier answer (`infotree`, `reuse` false), or change what an answer stored for the pool's own
  checks holds.
- **Every snippet is read first.** Every snippet of the request must pass, or the whole
  request is answered 422, `{"detail": "refused by the pool's screen: snippet 'ID' holds ..."}`,
  and nothing of it is looked up, forwarded or stored.
- **One that passes is served like any other**, from the store or the servers, and shares stored
  answers with checks sent with the pool's key. A screened check joins a flight the pool's key
  started; a check sent with the pool's key never waits on one a screened sender started, which
  may be refused (below), and starts its own. It is forwarded with the pool's key, so no Lean
  server holds the second one. `/status` takes the pool's key alone.
- **What its checks may cost.** A check its senders start at the servers takes one of
  `--screened-in-flight` places while it is there, and as many more checks may wait for a place.
  Past that a check is answered 503 `{"detail":"the screened door is full"}`, which a client
  retries. A check keeps its place until it ends, even after its sender has hung up, since it runs
  on and its answer is stored. An answer from the store, or a check already under way, takes no
  place. The pool's own checks take none either.
- **It is taken at the [screened door](#the-screened-door) alone**, and the pool's key everywhere
  else: either key at the other's door is answered 401. That needs the proxy rendered with the door
  (`--screened-door-port`), which is when the front door takes the door's header away from what it
  receives; give the cache the second key only behind such a proxy.
- **The rules** (`leanpool/screen.py`) hold what a proof and its statement never need: `import`
  only `Mathlib`, `Aesop` and their modules; no `#` followed by a name but `#print`, `#check`,
  `#reduce` and `#synth`, wherever it stands (so the notation `#s`, a finset's size, is refused
  too: `s.card` passes); none of the words that run a program or define a way to (`run_tac`,
  `elab`, `macro`, `syntax`, `initialize`, `simproc`, `unsafe`, `implemented_by`, `extern`,
  `native_decide`, `IO` and others); and attributes from a short list (`simp`, `norm_cast`, `ext`, `instance`, ...)
  only, since an attribute can register code. The whole text is read, comments and strings
  included, each word as a whole word: a word in a comment costs one refused check.
- **Where it is set:** by the installer, with the screened door (below), which also gives the cache
  its two limits: the pool's workers, and the pool's Lean timeout.
- **While the cache is down** the screened door answers 503, and the front door sends checks
  straight to the Lean servers ([losing the cache](haproxy.md#losing-the-cache)), which know only
  the pool's key: a check sent with the second key is never checked unscreened.
- **What it is not:** proof that a check runs no code. It is a list of words over a language
  whose files can define syntax, and a determined sender may get round it. Run the Lean servers
  so that what passes is held too: the installers put each worker in a
  [sandbox of its own](worker-sandbox.md).

## The screened door

The second key's one way in: a second HTTPS port of the pool's proxy (`--screened-door-port`;
18120 by the installer) that leads to the cache and nowhere else.

- **It tells the cache.** The door sets `X-Lean-Pool-Door: screened` on every request, and the
  front door takes that header away from every request it receives. The cache takes the second key
  only with the header and the pool's key only without it. So a machine given the second key
  cannot use the front door, and the pool's own key is no use at this one.
- **Checks alone.** `POST /api/check` goes to the cache. `/health` and the capacity headers are the
  front door's, so they tell anyone who reaches the door the pool's size and its installer's
  version, without a key. Anything else, `/status` included, is answered 404 by the proxy (and the
  cache would refuse `/status` from this door too).
- **No way round the cache.** Its only backend is the cache server, with no backup: while the
  cache is down, stopped (`cache-stop`) or being made again (by `screened-key new`, or by an install
  run that changes it), the door answers 503 with HAProxy's own page, and a client retries. The
  front door would send a check straight to the Lean servers then, past the screen.
- **Its limits.** Let W be the workers of the listed servers that are not paused, when the proxy's
  config was rendered. At most W requests from the door are in the cache at once, and W more wait;
  past that, 503 `{"detail":"the screened door is full"}`, and a request still waiting at the proxy's
  queue timeout gets HAProxy's own 503. The door keeps at most 2W + 8 connections open, so its
  senders cannot use up the proxy's own. A request may carry several snippets, so the bound on the
  checks themselves is the cache's (above), which the installer sets to W as of its run: a server
  that joins later raises the door's W at once, and the cache's at the next install run.
- **Where it is published.** `install.sh --proxy NAME --screened-door` publishes it on the docker0
  bridge's address alone (172.17.0.1 on most hosts), which the containers and the programs of the
  pool's host reach and the LAN does not. A firewall rule (`lean-pool-firewall@door-NAME`) lets in
  what arrives from a docker bridge's interface (`docker0`, `br-*`) and from loopback alone, so a LAN
  host that routes to that address is turned away, whatever its address. The door's certificate is
  the front door's, which already names that address. A program on the pool's host passes the door
  on to the machines that use it: one that opens an SSH tunnel to each, say.
- **The key** is made by the installer once (`screened-api-key` beside the pool's key, readable by
  the cache's group alone) and kept: a re-run keeps it, and so does a closed door.
  `sudo lean-pool-ctl NAME screened-key new` makes a new one, the door open or closed. Open, it
  makes the cache again with it and checks that a new cache container runs; the old key is refused
  from then on. Closed, the new key is the one taken when the door opens. No Lean server, joined
  box or join bundle ever holds it, and no command prints it. Closing the door does not retire a
  key: opened again, it takes the same one. `screened-key new` retires it.
- **Its copy.** With `--screened-key-copy DIR`, the installer and `screened-key new` also write the
  key (`api-key`, 0600) and the pool authority's certificate (`ca.crt`, 0644) into DIR, for the
  program that hands them on, owned by DIR's owner. DIR is kept once given, and goes with the door:
  it is refused while the door is closed. Its path is walked from `/` one directory at a time, and
  none of them may be a link, so nobody who owns a directory on the way can send the copy
  elsewhere. Its parent must exist. A DIR that does not is made, 0700, with its parent's owner, so
  that parent should belong to the program's user. Each file is made new beside the old one and
  moved over it, never written by name, so a link planted in DIR changes nothing outside it, and a
  file that cannot be moved into place is removed. A copy that cannot be written does not fail the
  install: its summary says so (`ITS KEY'S COPY WAS NOT WRITTEN`), and
  `sudo lean-pool-ctl NAME screened-key copy` writes it again. A new DIR leaves the old one's files
  where they are: remove them, or run `screened-key new`.
- **Closed** (the default, and `--no-screened-door`): the port, its firewall rule and the cache's
  second key are taken away. The key's file and its copy are kept for when the door opens again.
- **What a sender at the door can do:** send checks that pass the screen, within the limits above.
  They run on the pool's servers like any other, each worker in its [sandbox](worker-sandbox.md),
  and their answers are stored and shared. A program that passes the screen and stays inside the
  sandbox can still change what that same worker answers later checks, the pool's own included,
  until the worker is recycled; such an answer is stored and served like any other
  ([what the sandbox does not hold](worker-sandbox.md#what-it-does-not-hold)).
