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
or a token as an argument, and none logs one.

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

**The join service** hands a new box the pool's API key and a signed certificate, once, to the
first address that presents the window's token. It executes nothing itself; what acts on a
request is the operator's.

[Limitations](docs/limitations.md) lists the rest.
