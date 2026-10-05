# Limitations

What the pool does not do, and where it can surprise. Nothing here is hidden behaviour: each
item follows from a rule described on another page.

* There is no installer. A pool is set up and changed with the commands and compose files
  described in [Deploying a pool](deployment.md); the scripts that automate this for the
  deployment the project was written for are specific to it and are not shipped.
* The pool speaks Kimina's interface and nothing else. It does not run Lean, and it does not
  build or manage the Lean servers.
* The pool box is a single point of failure: while it is down the pool is down, although each
  Lean server still answers a client that calls it directly.
* On machines of different speeds, whether a check close to its time limit finishes depends on
  which box ran it. The pool does not hide that and does not add to it: a timeout is passed on
  once, never retried and never stored, so the same check sent again may land on another box
  and finish. Likewise a check that crashes a worker on a small box (out of memory, say) is
  tried on another server, which may be a larger one.
* While every Lean server is drained by its usage agent (each is below its memory floor),
  the pool answers `GET /health` and every check with 503 although the servers are up.
* The cache shares the proxy's network namespace. If the proxy's container is restarted, the
  cache must be restarted after it, and until then the pool runs uncached.
* The cache trusts the pin. If the Lean servers' image changes and the pin does not, old answers
  are served. Change the pin with the image.
* Resource exhaustion is recognised by message text. A nondeterministic failure that Lean reports
  in other words would be stored; add a pattern for it, or send `no_cache`.
* One cache process per database file.
* The usage agent's CPU figure is the whole box's idle share, so it includes the Lean server's
  own load: a pool that is busy with its own checks reports low percentages on every box.
  Weights are relative, so this still balances; it does not measure how much of the load is
  someone else's.
* The percentage is applied in whole numbers. A server whose configured weight is below 100
  (one with less than about 40% of the largest server's workers) reaches weight 0, and gets no
  new checks, when its agent reports less than `100 / weight` percent.
* The agent reads `/proc`, so it runs on Linux only. In a container it sees the host's CPU and
  memory, which is what is wanted, but not a memory limit set on the Lean server's own cgroup.
* A check larger than HAProxy's buffer (256 KiB by default, headers included) is not replayed
  after a server or the cache received it and failed; its caller sees the failure. It is still
  retried when the connection could not be made.
* HAProxy waits for the whole request, up to one buffer, before it forwards anything. A client
  that takes more than 10 seconds to send it gets a 408.
* A check that was inside the cache when the cache stopped may be run twice on the Lean servers:
  once for the cache's own forwarded request, once for HAProxy's replay.
* One failed connection to the cache takes it out of use for about two seconds, even if it is
  alive. So does one health check that takes longer than a second, which a very busy host can
  cause: checks go around the cache, uncached, until it is seen healthy again.
* A Lean server answers 500 for more than a crashed worker (any error it could not handle), and
  each such check is tried on up to three workers before the caller sees the 500.
* A check is tried on at most three servers. A Lean server that fails every check at once but
  still answers `/health` stays in rotation, and least-connections balancing sends it new checks
  first because it is never busy. One or two such servers cost checks a retry or two; with
  three, some checks fail although a healthy server exists.
* A 429 from a Lean server (no free worker within its own wait) is returned to the client, not
  retried: HAProxy cannot retry on that status.
* A server name that only the system's resolver knows (`/etc/hosts`, a search domain, mDNS) is
  taken out by HAProxy after 24 days ([Server names](haproxy.md#server-names)). A mistyped name no longer
  stops the proxy from starting: it shows as a server that is down.
* `maxconn` counts requests. A request with several snippets that reaches a Lean server directly
  (when the cache is down) occupies several workers but one slot, and through the cache it
  becomes one upstream request per snippet, all at once. Send one snippet per request.
* IPv6 literals are not supported in the server list.
* Two people editing the server list at the same moment can lose one edit; there is no lock.
* A `reject` case passes on any error-severity message, whatever it says.
* TLS ends at a box's front. Between the front and the Lean server beside it checks are plain:
  whoever can reach that network on the box can read them and can call the Lean server.
* There is no revocation. A certificate is good until it expires; taking a box out of the server
  list stops the proxy from dialling it, and nothing more.
* Private keys are stored unencrypted, protected by file permissions alone.
* The front door's certificate must name every host name and address clients use for the pool.
  A client that dials another one is refused, correctly.
* With TLS, two servers on one box that share one usage agent are each asked through their own
  tunnel, and each tunnel checks the agent port's certificate against its own server's name: that
  certificate must carry both names.
* With TLS, removing a server moves the tunnel ports of the servers listed after it.
* The join service presents the front door's certificate, so it must be able to read the front
  door's private key.
* A join window's token is part of the command a new box runs, so it is visible in that box's
  process list and shell history. It opens one window for one box and is useless afterwards.
* The join service binds a window to the address a box calls from, and writes that address to
  the spool. It must therefore see the real one: in a container, use the host's network or a
  network that keeps source addresses (a port published through a userland proxy shows every
  caller at the bridge's gateway). A box behind address translation is seen at the translator's
  address, which every machine behind it shares.
