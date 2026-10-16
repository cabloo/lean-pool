# Limitations

What the pool does not do, and where it can surprise. Nothing here is hidden behaviour: each
item follows from a rule described on another page.

* The [installers](../deploy/install/README.md) are for Linux hosts with Docker and systemd,
  and run as root. Anywhere else, the compose files of `deploy/` run as each one's header says,
  with this project's commands; no page walks through that
  ([without the installers](deployment.md#without-the-installers)).
* The pool speaks Kimina's interface and nothing else. It does not run Lean, and it does not
  build or manage the Lean servers.
* The pool box is a single point of failure: while it is down the pool is down, although each
  Lean server still answers a client that calls it directly.
* On machines of different speeds, whether a check close to its time limit finishes depends on
  which box ran it. The pool does not hide that and does not add to it: a timeout is passed on
  once, never retried and never stored, so the same check sent again may land on another box
  and finish. Likewise a check that crashes a worker on a small box (out of memory, say) is
  tried on another server, which may be a larger one.
* While every Lean server is drained by its usage agent (each is below its memory floor, or
  has no room for a check beside its box's other work), the pool has no server to give a check
  to: it answers `GET /health` and every check with 503 at once, although the servers are up.
  It does not queue. A client asks again.
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
  someone else's. [Open slots](usage-agent.md#open-slots-a-box-that-also-does-other-work) do,
  and need what the weight does not: cgroup v2, and a control group that holds the Lean server
  and nothing else.
* Open slots count CPUs only. A check is taken to need one thread; memory lowers the weight
  and, at the floor, drains.
* What other work can carry is not computed. `--slots-beside-other-work` is a number someone
  measured on that box, for the work that ran there that day; other work may be more or less
  sensitive, and a box given no number shares by the count of cores alone, which was measured to
  slow lightly loaded work by a quarter.
* Other work is taken to hold one core for each thread it keeps busy. Work that packs two of its
  own threads onto one core leaves more cores than the agent counts, so the count errs toward
  fewer checks. On a box that mixes cores of two threads and cores of one, other work is taken
  to hold the two-thread cores first: right for the usual desktop, whose two-thread cores are its
  fast ones, and erring toward fewer checks anywhere else. Contention for memory or cache between
  a check and other work on different cores is not counted at all.
* A server's limit (its open slots, or its worker count) is exact for checks that arrive one
  after another. HAProxy 3.0.29 places checks on several threads at once, and of checks that
  arrive in the same instant one can go over: of 450 lots of eight held checks sent at once to
  two servers of four workers, three left a server holding five, and four left a check waiting
  in a full server's own queue while the other had a worker free. Either ends when a check on
  that server ends.
* HAProxy keeps a limit an agent stated, and forgets it on a reload: a reloaded proxy holds
  every server to its worker count again until that server's agent next answers (5 seconds).
* A check that is running is never stopped. When a box's other work grows, the checks it holds
  finish slowly, and one of them may reach its Lean timeout where it would not have on a free
  box; new checks stop arriving within one reading.
* A Lean server that has stopped is noticed after 4 missed health checks: about 20 seconds when
  its port refuses connections, up to about 100 when it accepts them and says nothing
  ([the health check](haproxy.md#the-health-check)).
* The generated configuration needs an HAProxy built with its Prometheus exporter
  (`USE_PROMEX=1`), as the official images are. One built without it refuses the configuration.
* The three [metrics paths](haproxy.md#metrics-for-prometheus) are answered to anyone who can
  reach the public port, without the key: server and certificate names, and numbers. A `GET` of
  those three paths no longer reaches a Lean server.
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
* The screened door's limits count requests and checks, not senders: one sender with the screened
  key can take all of them, and while it does, every other sender at that door is answered 503. Its
  checks share the pool's workers with everyone's, at whatever priority it asks for, so at most as
  many as the pool has workers can be its at once. The cache's count of those workers is the one of
  the pool's last install run.
* A screened sender's checks share the store with the pool's: many unique checks with long
  answers push the pool's own stored answers out sooner, and an answer's `cached: true` tells a
  sender whether that exact text, with that timeout, was checked before.
* The firewall of the screened door tells Docker's bridges from others named `br-*` as the host has
  them when its rule is applied: a bridge of that name made later is let in until the rule is
  applied again.
* The screened door hands its key to no one: a program on the pool's host must pass the door and
  the key on, and whoever holds the key can send checks through the door until a new key is made.
  Closing the door does not retire it; `screened-key new` does.
* A check sent through the screened door that passes the screen and stays inside its worker's
  sandbox can still change what that worker answers later checks, the pool's own included, until
  the worker is recycled, and such an answer is stored and served from the cache
  ([what the sandbox does not hold](worker-sandbox.md#what-it-does-not-hold)).
* The screened door's `/health` and capacity headers tell whoever reaches it the pool's size and
  its installer's version, without a key, as the front door's do.
