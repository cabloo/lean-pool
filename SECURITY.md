# Security

## Reporting a vulnerability

Please report a vulnerability privately, through GitHub's
[private vulnerability reporting](https://github.com/cabloo/lean-pool/security/advisories/new)
for this repository, and not in a public issue. Say what you found and how to reproduce it.

## What a pool protects, and what it does not

**Checking Lean code runs it.** Lean's metaprograms and `#eval` can do whatever the Lean server's
own user can do on its machine. Whoever holds a pool's API key can therefore run code on every
Lean server box of that pool. The pool does not sandbox Lean: that is the job of how the Lean
servers are run (an unprivileged container, no outbound network, resource limits). Give the key
only to clients you would let run code there.

**One key for the whole pool.** Clients send it, the cache checks it in constant time before it
answers anything from its store, and every Lean server checks it again. No command takes a key
or a token as an argument, and none logs one. The installer writes it into a directory of your
choosing only when asked (`--pool-key-copy`), for a program that hands it on: whoever can read
that directory holds the key.

**A second key, screened, for senders trusted less.** The cache can hold one more key. A check
sent with it must first pass [the screen](docs/cache.md#the-screened-key), a list of what a proof
never needs (`#eval`, `run_tac`, `elab`, `IO`, most attributes, imports beyond Mathlib and
Aesop), and is forwarded with the pool's key, so no Lean server holds the second one. The screen
is a list of words, not proof that a check runs no code: give that key only where the Lean
servers are also run so as to hold what passes. A request sent with it may carry only Kimina's
plain fields, a bounded number of snippets and a bounded timeout, and its checks hold a bounded
number of places at the servers. It is taken at one door alone: [the screened
door](docs/cache.md#the-screened-door), a second HTTPS port that leads to the cache and nowhere
else, which the installer publishes on the Docker bridge's address and firewalls to the Docker
bridges.

**Three things are answered without the key.** `GET /health`, and for a Prometheus scraper
`GET /metrics`, `/metrics/cache` and `/metrics/pool`: the names of the pool's servers and
certificates, their states, and counts. No key, no proof and no address is in them, and none
of them reaches a Lean server. Whoever can reach the public port can read them
([what they hold](docs/haproxy.md#metrics-for-prometheus)).

**Plain HTTP is plain.** A pool rendered without TLS sends checks and the API key readable over
the network, and its generated `haproxy.cfg` says so in its first line. Use it on a network you
control.

**With [TLS](docs/tls.md)**, every hop that leaves a machine is TLS 1.3. Clients verify the
pool by its own certificate authority; the proxy and each Lean server box verify each other,
the box by its name in the server list and the proxy by its client certificate, and a box
accepts no other caller. There is no revocation: a lost private key means a new authority.
Private keys are stored unencrypted and protected by file permissions.

**The cache trusts its pin.** A result is given again to any check with the same code, timeout
and pin. If the Lean servers change and the pin does not, old answers are served.

**The join service** hands the pool's API key and a signed certificate to one address per
window: the first that sent a signing request under the window's token, and only after the
operator's side has answered that request. That address can fetch them again while the window
is open; no other address gets anything. The service executes nothing itself: what acts on a
request is the operator's.

[Limitations](docs/limitations.md) lists the rest.
