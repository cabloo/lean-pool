# What is tested, and what is not verified

Three kinds of evidence, kept apart on this page: the test suite, which anyone can run; the
record of the one deployment the project was written for; and the list of what neither of them
establishes.

## The test suite

Two kinds of tests, neither of which needs Docker, the network or a Lean server. Lean servers
are in-process fakes that speak Kimina's interface. `uv run pytest` runs about 2,200 test
cases.

### Tests that always run

* the generated configuration, line by line (that each directive [described](haproxy.md) is
  present, with its arguments), and every refusal of the generator and of the server-list editor,
  including that a refused or failed edit leaves the file byte-identical;
* the cache: an identical second check never reaches the upstream; what is and is not stored;
  bypass; single flight; request order; eviction order and the size cap; authentication; that
  the cache does not cap concurrency (150 checks in flight at once);
* the agent: the formula, the reply format, that the CPU share needs two readings, `drain`;
  open slots from made-up readings of a 16-CPU box with 13 workers (other work on 0, 9.6 and
  14.2 CPUs gives 13, 5 and none; the Lean server's own use lowers none), down at once and up
  by one, never `maxconn:0`, the worker count when the control group cannot be read, and
  replies that are what they were, to the character, for an agent not told how to measure;
  slots counted in cores (20 threads on 12 cores, eight of them of two threads, with other work
  on 8.4: 2, not 10; 16 cores of two threads with other work on 12: 6), the topology read from
  the kernel's files, each core with its own number of threads, and left out where it
  describes other CPUs; a box's limit beside other work coming on at one CPU of other work and
  at once, never raising what the cores give, 0 as a drain, and the slots back one a reading
  when the box is free;
* the admission test: verdicts, import hoisting, the report and every exit status. Over TLS,
  against in-process servers that require a client certificate: admission passes when the server
  is dialled by address and named by `--tls-server-name`; another name, another authority, an
  expired certificate, a client certificate the server refuses, a front that closes without
  answering, a port that does not speak TLS 1.3 and a server that stays silent each fail it with
  status 3 and their own message, after one connection; the API key is not in anything sent to
  a front that then closes; some TLS options without the others, `https://` without them and
  `http://` with them are usage errors that send nothing;
* end to end with two fake Lean servers and a stand-in for HAProxy's retry rule: repeats are
  served from the cache, and a failing server is not what the caller sees when the other can
  answer;
* the certificates: what each kind may do, its lifetime, every refusal of `sign-csr`, file modes,
  that nothing is overwritten, and the pin (compared with `openssl`'s own computation where
  `openssl` is installed). A Python `ssl` client built with
  `ssl.create_default_context(cafile=...)` and the strict X.509 rules completes a handshake with
  an `issue-server` certificate by name and by address, and refuses one from a second authority;
  a Python TLS server that requires a client certificate refuses a server certificate;
* the TLS configurations, line by line, every refusal, and the property that with TLS no
  listener off loopback and no line to a Lean server or agent is plain;
* pausing: a paused server's line in the list and in the configuration, with and without TLS,
  that no other server's line moves, and every refusal (the last server taking checks, a list
  of paused servers only); `set-workers` changing one word of one line, and refused for the
  count a server already has;
* the metrics: the rules of the three paths and their order before every routing rule, which is
  what it was; the cache route's text to the character, that it counts what `/status` counts and
  needs no key while `/status` still does; the pool's facts from a list with a paused server and
  three certificates, to the character; a certificate's last moment equal to what `cryptography`
  reads, for a date written with two digits of the year and with four, and a refusal for
  everything that is not a certificate (a certificate cut at every length is refused or read
  right, never anything else); facts over the size limit refused with nothing written;
* the join service: each of its requests, 404 for everything without the token, the
  one-box-per-window rule (from a second loopback address where the machine has one), every
  malformed request, the 16 KiB limit, what is written to the spool and when, and that neither
  the token nor the API key is ever logged. As a real process: it serves HTTPS only and refuses
  TLS 1.2; and, where `curl` is installed, `curl -fsSk --pinnedpubkey` fetches the script with
  the front door's pin, exits 90 having fetched nothing with any other pin, and carries a whole
  join through;
* the documentation: every flag and environment variable a command accepts is in the
  [configuration reference](configuration.md) and none is named there that does not exist; the
  directives, timeouts and messages these pages quote are the ones the code produces; every
  link between the pages leads somewhere;
* the examples: the [demo](../examples/demo)'s stand-in Lean server, and what the real cache
  stores of its answers; that the demo's committed `haproxy.cfg` is what `render` writes for
  its server list; that every image a compose file builds is this project's, that none
  publishes a port that must stay inside the proxy, that each quotes commands that exist, and
  that the TLS configurations those commands render use the certificates where the files mount
  them; the
  [example client](../examples/pool_client.py)'s sizing, retries and verdicts;
* the [compiled search step](compiled-search.md): that the launcher loads the library only
  when it was made from the files that are there, and starts the plain REPL in each other
  case; the build's refusals; the check's judgement over a stand-in Lean, which fails a
  library that is not loaded, answers differently, is handed to a program a tactic starts,
  or is not 3 times faster; and what `make.sh` installs over a stand-in for Docker, when
  the check passes and when anything fails. The real build and check need a Lean with
  Mathlib, and run only where one is present;
* the [installers](../deploy/install/README.md) (`tests/install/`), on a fake machine: copies
  whose host paths point into a fake root directory, and stand-ins for Docker, systemd, the
  firewall's tools and the package managers, whose Docker refuses a file read that a
  container's user could not make. A host with two Lean servers, the pool put in front of one,
  the control commands,
  joins over real HTTPS with the real join service, refreshes, the compose plugin's install,
  the firewall rules, uninstalls; every refusal before root is asked for; an instance whose
  image is pulled by name instead of built; and a box set up by the installers of an older
  version, made by those installers themselves, moved to the compose files of `deploy/` with
  every setting and the cache's results kept, put back as it was when a server does not come
  up, and taken back to the older installers by the steps
  [Deploying a pool](deployment.md#a-box-set-up-by-an-older-installer) gives. Every compose
  command they run names the files of `deploy/`, byte for byte. They need a user who is not
  root, and are skipped when run as root.

### Tests that run only where `haproxy` is installed

`tests/test_haproxy_live.py`, `tests/test_tls_live.py`, `tests/test_metrics_live.py` and the
walkthrough in `tests/test_demo.py`; skipped otherwise. They start a real HAProxy on the
generated configuration, in front of the real cache and fake Lean servers on loopback, and
show what HAProxy does with it:

* a check goes through the cache and a repeat never reaches a Lean server; `/health` and
  `/status` answer as described, with the cache up and down;
* with every worker held, a background check that is already waiting is served after a normal
  check and a check with a mistyped priority that arrived later, through the cache and with the
  cache stopped; with nothing else waiting, background checks are served;
* the workers number on `/health`, on a check and on a cache hit is 8 for two servers of 4, 4
  with one of them stopped and 8 again when it is back; the queued number counts the checks
  waiting; with no server up `/health` is 503 with 0 workers;
* a check that got a 500, 502, 503 or 504 is replayed on the other server, for a short proof and
  for one of 200 kB; one longer than the buffer is forwarded but not replayed;
* a proof that crashes every worker is tried three times and no more; a Lean timeout once;
* a Lean server that is gone costs no check;
* stopping the cache costs no check, alone or under 30 checks at once; checks that were inside
  the cache when it was killed are replayed around it; the cache is used again when it returns;
* a server whose name does not resolve does not stop the proxy, and a server given by address
  beside it is unaffected; a server whose address changes is followed;
* HAProxy applies the usage agent's replies: 50% halves the weight, 1% leaves the server in
  rotation, `drain` drains it, and the next normal reply restores it;
* a server drained by a bare `drain` stays in the workers the pool reports and leaves its
  servers, and takes no check; with every server drained `/health` and a check are both
  answered 503;
* with a queue standing, a server whose agent states 2 open slots holds 2 checks while its
  neighbour holds 4 and two checks wait; with 4 stated it holds 4; drained (`drain 0%`) it stays
  up, finishes the checks it holds and is sent none; `/health`, a check and a cache hit say 6,
  then 8, then 4 workers; a limit stays until another is stated, and `maxconn:0` lifts it;
* a Lean server whose `/health` takes 3 seconds to answer stays up;
* a paused server whose usage agent keeps answering `ready up 100%` stays in maintenance: it is
  sent no check and no health check, and `/health` and every answer count the other server's 4
  workers alone;
* with every worker of two servers holding a check, a reload that pauses one server loses none
  of the 8: they all finish, new checks reach the other server only, and a reload that resumes
  the server gives it its share again.

With TLS (`tests/test_tls_live.py`), the pool proxy and each box's TLS front are real HAProxy
processes on the generated configurations, with throwaway certificates made by `leanpool.pki`:

* a check goes from an HTTPS client through the proxy, the cache and a box's TLS front to a fake
  Lean server, and a repeat is served from the cache; the front door is trusted by its name and
  by its address;
* a client that does not trust the pool's authority is refused at the front door, and a plain
  HTTP request gets no answer;
* a box's front, on its Lean port and on its agent port, lets in the proxy's client certificate
  and refuses a caller with no certificate, with a box's server certificate (its own or another
  box's), with a server certificate in the proxy's name, with a client certificate in another
  name, and with the proxy's name signed by another authority. Nothing from any of them reaches
  the Lean server;
* a box that presents another box's certificate, an expired one, or one from another authority
  is marked down by its health check and gets no check. Every box is at 127.0.0.1 in these
  tests, so only the name in the certificate tells them apart;
* a Lean server that is gone behind a front that still answers costs no check;
* `leanpool-admit` tests a fake Lean server alone through a box's front, dialled by address with
  the box's name; it fails with another box's name, and with a client certificate that is not
  the proxy's (another name of the pool's authority, the box's own server certificate, the
  proxy's name signed by another authority), and nothing from any of those reaches the Lean
  server;
* the usage agent's reading arrives through the tunnel and changes the server's weight in
  HAProxy's statistics (50%, 1%, `drain`, recovery), and a server whose agent is unreachable
  keeps its weight;
* a check that is silent for as long as the Lean timeout allows passes through both proxies
  (with the durations scaled down to seconds);
* where `curl` is installed, `curl -k --pinnedpubkey` with the pin `leanpool-pki pin` prints
  reaches the front door, and with another pin exits 90 having fetched nothing.

The metrics paths (`tests/test_metrics_live.py`), on a real HAProxy in front of the real cache:

* a scraper with no key reads all three paths; the exporter's answer holds every name a
  dashboard of the pool reads, a paused server as `MAINT`, and the checks a server answered; the
  cache's counters are the cache's; the facts are the file, byte for byte; `/status` is still
  401;
* no answer holds the key, a proof, an address or a port of the pool;
* whatever query the scraper sends (`scope=resolvers`, `no-maint`), the exporter's answer has
  the same samples;
* with the cache stopped, `/metrics/cache` is 503 from its first moment and
  `the cache is down` once HAProxy has marked it, a check still passes through the backup, the
  Lean server saw that check and no scrape, and the other two paths and `/health` answer;
* `HEAD` is answered with no body; `POST`, `PUT` and `DELETE` to the three paths, and a `GET`
  of six paths beside them, reach the Lean server as before;
* with every worker held and two checks waiting, all three paths answer at once and the
  exporter reports the queue it did not join;
* a pause and a resume are in the facts and in the exporter's answer once the proxy is reloaded;
* facts of exactly the size limit (128 KiB) are served whole;
* a TLS pool, rendered by `render --pool-metrics --certificates` itself, answers a scraper that
  trusts the pool's authority and holds no key or client certificate, with each certificate's
  last moment as `cryptography` reads it.

The demo's [walkthrough](../examples/demo/walkthrough.sh) is run the same way: its stand-in
Lean servers, usage agents and cache as plain processes behind a real HAProxy on the
configuration rendered for them. Every step must behave as the script says, including a Lean
server and the cache being stopped and started again.

These have been run against HAProxy 3.0.29, the version of the official `haproxy:3.0` image,
and `haproxy -c` accepts every generated file with no warning. It must be an `haproxy` built
with the Prometheus exporter (`USE_PROMEX=1`), as the official images and the distributions'
packages are. On one built without it every test that starts the pool **fails**, saying that
the exporter is missing; they are not skipped, because the pool itself would not start there.

### Continuous integration

On every push to `main` and every pull request the [workflow](../.github/workflows/ci.yml)
runs the lint, the type check and
the tests on Python 3.11, 3.12 and 3.13; runs the HAProxy tests with HAProxy 3.0 installed, where
none may be skipped; builds the image; has Docker validate every compose file; and starts the
demo in Docker and runs its walkthrough against it. The badge on the front page shows the last
run.

## The deployment's record

lean-pool has been in use since 3 October 2026 in the deployment it was written for. That
deployment installs and operates the pool with this project's
[installers](../deploy/install/README.md) and a site directory of its own; what it runs are
this project's commands, this project's image and the configuration
this project generates, on the official `haproxy:3.0` image, in front of Kimina Lean Servers
built from Kimina's source (commit `fb2393d`) with Lean 4.27. These are observations of one
deployment, each made once unless it says otherwise. They are not benchmarks.

**First acceptance, 3 October 2026** (plain HTTP, one Lean server with 8 workers). The same
batch of 40 checks, 8 in flight, four times:

| Run | Seconds | What it showed |
|---|---|---|
| straight to the Lean server | 99.3 | the server's own pace |
| through the pool, first time | 91.1 | 40 misses; the pool is not slower than the server alone |
| through the pool, again | 0.1 | 40 hits; the Lean server's request count did not move |
| through the pool, cache container stopped | 86.6 | all 40 answered by the Lean server; the cache came back with its 40 entries |

In all four runs the verdicts were identical. Adding a server at an address where nothing
listened failed its admission and left the server list byte for byte as it was.

**The same over TLS, 4 October 2026.** The acceptance passed again over HTTPS. Seen from a
client machine:

* a client that trusts only the pool's authority completes a TLS 1.3 handshake with the front
  door, by each of its names and addresses; a client with the system's authorities is refused,
  as is one that offers TLS 1.2; a plain HTTP request gets no answer;
* the Lean server's port and its usage agent's port, behind the box's TLS front, refuse a
  caller without a client certificate (`CERTIFICATE_REQUIRED`) and give plain HTTP no answer;
* the usage agent's reading arrives through the proxy's mutual-TLS tunnel: the server's weight
  moved between 92 and 130 across reads, and fell during the checks;
* for 40 real checks HAProxy's own timers show 0 ms in the queue and 13 ms in all to connect to
  the Lean server, the mutual-TLS handshakes included, out of 102.3 s. The rest is Lean.

**First sustained load, 4 October 2026** (TLS, one Lean server with 8 workers, a host busy
with other work at a load average of 18 to 33 on 32 threads). 34,325 distinct Lean files, 8 in
flight, a Lean limit of 120 s, 68 minutes:

* 34,319 answers. No transport failure and no HTTP 503.
* 9.0 checks per second over the main pass, with a median Lean time of 0.40 s.
* 24 requests were answered 500, all of them for 6 files that crash their Lean worker every
  time. With one server the proxy's retries could only meet the same server again
  ([limitations](limitations.md)).
* The cache counted 967 hits and 34,343 misses, which together are every request sent. Every
  definitive answer was stored; the 21 Lean timeouts and the crashes were not. 967 checks sent
  a second time were all answered from the cache, with the status, messages and time of the
  first answer.
* The proxy marked the cache down once, for under a minute, when its one-second health check
  timed out. It cost no check: the checks in that minute went straight to the Lean server.

**Since then** the pool has grown to four machines, the later ones joining through
`leanpool-join`. On 5 October 2026 `GET /health` answered
`{"status":"ok","workers":37,"queued":0,"servers":4}`, and the cache's counters since its last
start were 403,933 hits, 22,011 checks that joined an identical one in flight and 556,669
misses, with 781,151 results stored in 853 MB and none evicted.

**Boxes that also do other work, 10 to 12 October 2026.** Three of the pool's four machines
run other jobs beside their Lean server. The open slots, a box's own limit, the patient health
check, the pause and the metrics paths were each built against what that showed:

* the measurements quoted on
  [the usage agent's page](usage-agent.md#open-slots-a-box-that-also-does-other-work) (cores
  against threads, and what a check costs the work beside it on two boxes) were taken there,
  each once;
* on 12 October 2026, under a client that kept the pool full, `GET /health` answered
  `{"status":"ok","workers":16,"queued":22,"servers":3}` for four listed servers of 12, 3,
  6 and 9 workers. The 9-worker box was busy with other work and has a limit of 0 beside it:
  its agent had drained it. The 6-worker box has a limit of 1 and counted for 1;
* that day a server's listed worker count was changed (`set-workers`) and the proxy reloaded
  during a run: the 302 answers at the front door in the two minutes around it were all 200;
* the three metrics paths have been scraped there by a Prometheus since 12 October 2026, on
  the official `haproxy:3.0` image.

**Compiled search for linarith, 12 October 2026.** Each of the four Lean server boxes
compiled the step and passed its own check ([what each measured](compiled-search.md#measured)).
On one server alone, 86 checks a run had sent were sent again past the cache with the step
on and then off. Of the 71 that finished both ways no verdict differed, from each other or
from the one stored, and their Lean time was 608 s off and 217 s on. The other three boxes
were switched over while a client kept the pool busy, and every answer was a success
(2xx).

Not part of that deployment, and run once by hand against the demo's pool, whose Lean servers
are stand-ins: Kimina's own Python client (`kimina-client` 0.2.1), unchanged but for the
address. Its single and its batched requests were answered, and repeats came from the cache.

## What none of this establishes

* **There is no benchmark.** The numbers above are what one deployment saw on its own
  workload. How throughput grows with servers, and what the cache saves on another workload,
  have not been measured.
* **HAProxy's memory use** with 256 KiB buffers has not been measured. The 768 MiB figure is
  arithmetic from the manual's two buffers per connection.
* **The 24-day hold was not waited out, and no test covers it.** Once, by hand, on HAProxy
  3.0.29: with the generated holds a server kept its address and kept answering through 25
  seconds without DNS, and with the holds shortened to 5 seconds the same server was taken out
  (as was one whose name only the system's resolver knew).
* The address-change test replaces one line of the generated configuration,
  `parse-resolv-conf`, with a test name server: HAProxy always reads `/etc/resolv.conf` for that
  directive, and a test cannot replace that file.
* **The compose files in `deploy/` have not run on a real host yet.** They are what the
  installers run, and the installers' tests run them on the fake machine; Docker validates every
  combination of them in CI, where only the demo's compose file is started.
* **Kimina's published image has not been used.** The deployment builds Kimina from source at
  one commit. The fakes and the stand-in follow Kimina's interface as read from that source.
  The installers' example site names that image, and pulling it has run only on the fake
  machine.
* **The installers in `deploy/install/` are the deployment's own, in an earlier version**: it
  last ran them before they took the compose files of `deploy/`, with a site of its own, and it
  builds its images from recipes. `site.example/` has not been installed on a real host.
* **The compiled search step's `make.sh` has run only on the fake machine.** The installers
  run it; the deployment ran the same build, check and launcher through its earlier installers,
  and here those three have been run as written against a real Lean, outside Docker. And the
  check compares three proofs: it does not show that the compiled search and the
  interpreted one agree on every input
  ([why that cannot let a wrong proof pass](compiled-search.md#what-it-cannot-change-and-what-it-does)).
* **What TLS costs** in CPU or throughput has not been measured, beyond the timers above.
* **What a check costs the work beside it** was measured on three machines of one deployment
  and differed on each. Nothing here says what it is on yours: measure the box
  ([how](usage-agent.md#open-slots-a-box-that-also-does-other-work)).
* **What a scrape of the exporter costs HAProxy** (about 80 kB of text for a pool of two
  servers without TLS, about 129 kB for four with it) has not been measured.
* **A reboot of the pool's host** has not been tested: whether every container comes back is
  Docker's `restart: unless-stopped`, taken on trust.
* **A real Lean server of the wrong version** has not been refused in the deployment. The
  admission test's rules are covered by unit tests only.
* **The join service** is tested as a service, and through the installers on the fake
  machine. The deployment's joins were carried out by these installers
  ([joining](joining.md)).
