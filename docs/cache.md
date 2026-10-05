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
Each caller gets it under its own `id`. These answers are not marked `cached`.

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
On a miss the caller's `Authorization` header is forwarded. `/health` is open.

**Other replies of the cache itself:** 401 without the key, 422 for a malformed request
(not a JSON object, no snippets, duplicate ids, a `timeout` that is not a whole number of
seconds, a `debug` or `no_cache` that is not a boolean), 508 for a request that already carries
the loop-guard header.

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
