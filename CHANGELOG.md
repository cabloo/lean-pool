# Changelog

Notable changes to lean-pool. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html): before 1.0, a minor version may
change an interface, and this file says when one does.

## 0.1.0 - 2026-10-05

The first public release. lean-pool was developed inside a private repository and had been
running a pool there for two days; this is that code, with documentation, a demo and
deployment examples written for publication.

### Added

- `leanpool-haproxy-config`: the server list and the generator of a pool proxy's `haproxy.cfg`
  (cache-first routing, least-connections balancing capped at each server's workers, failover
  that never retries a Lean timeout, lossless loss of the cache, run-time name resolution), and
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
