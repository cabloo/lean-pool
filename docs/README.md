# Documentation

Start with the [front page](../README.md) for what lean-pool is and why, and with the
[demo](../examples/demo) to see one run.

**Setting up and running**

* [Deploying a pool](deployment.md): from nothing to a running pool, and how to operate it.
* [TLS](tls.md): the pool's own certificate authority; what is encrypted and who holds which key.
* [Joining over the network](joining.md): letting a new box request its certificate over HTTPS.
* [Admission](admission.md): testing a Lean server alone before it joins.
* [Configuration reference](configuration.md): every flag and environment variable.

**Using**

* [Using a pool](clients.md): the interface, how to send checks, how to read replies,
  priorities and the pool's stated size.

**How it works**

* [Cache semantics](cache.md): the key, what is stored and what never is, bypass, single flight.
* [The usage agent](usage-agent.md): the weight formula, and what HAProxy does with it.
* [The generated HAProxy configuration](haproxy.md): failover, buffers, name resolution,
  timeouts, losing the cache, and the TLS variants.

**How far to trust it**

* [What is tested, and what is not verified](verification.md)
* [Limitations](limitations.md)

## The words used here

| Word | Meaning |
|---|---|
| **check** | One piece of Lean code sent to be checked: a snippet of a `POST /api/check`. |
| **verdict** | Lean's judgement of a check: accepted, or rejected with errors or a `sorry`. Also called a definitive answer. The only thing the cache stores. |
| **timeout** | Lean ran out of the time the client allowed. It is a reply, and no verdict: passed on once, never retried, never stored. |
| **no answer from Lean** | The check got nothing Lean said: a lost connection, a crashed worker (HTTP 500), a gateway error (502, 503, 504). Only this is sent to another server. |
| **Lean server** | One Kimina Lean Server. |
| **workers** | The checks a Lean server runs at once (Kimina's `LEAN_SERVER_MAX_REPLS`). |
| **Lean server box**, or **box** | A machine that runs a Lean server and its usage agent. |
| **pool box**, or **the pool's host** | The machine that runs the proxy and the cache. |
| **proxy** | The HAProxy on the pool box. |
| **the cache's pin** | The string that names the pool's Lean and Mathlib versions; part of every cache key. |
| **front door** | The proxy's public port, and with TLS its certificate (`front.pem`). |
| **TLS front** | With TLS, the HAProxy in front of the Lean server and the agent on a Lean server box. Not the front door. |
| **checkers** | The Lean servers as the proxy's configuration names them; the **checkers door** is the proxy's loopback listener through which the cache reaches them. |
| **a certificate's pin** | The fingerprint of a public key that a joining box checks with `curl --pinnedpubkey`. Unrelated to the cache's pin. |
