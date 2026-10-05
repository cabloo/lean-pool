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
