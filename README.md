# lean-pool

One address for a pool of [Kimina Lean Servers](https://github.com/project-numina/kimina-lean-server):
HAProxy in front, a shared result cache tried first, and load balancing that follows each box's
real CPU and memory headroom.

## Why

Checking Lean 4 proofs is usually the slowest step of training or evaluating a prover: the GPU
produces candidate proofs faster than Lean can check them. Kimina Lean Server checks them well,
but it is a single-node service. Its own deployment guide scales out by putting a generic load
balancer in front of identical servers, and it has no shared cache, so a proof submitted twice is
checked twice. Provers submit the same proof for the same statement often.

lean-pool is that missing layer, kept as small as possible:

| Part | What it does | Whose code |
|---|---|---|
| **HAProxy** | the address clients call; cache-first routing, load balancing, queueing, health checks, failover | off the shelf; lean-pool generates its configuration |
| **cache** (`leanpool-cache`) | answers an identical check from its store; forwards a miss to the Lean servers | this project |
| **usage agent** (`leanpool-agent`) | one per Lean server box; tells HAProxy how much CPU and memory headroom the box has | this project |
| **admission test** (`leanpool-admit`) | proves a Lean server gives the right answers before it joins a pool | this project |
| **certificates** (`leanpool-pki`) | optional: the pool's own certificate authority, so that every hop between machines is TLS and a Lean server answers only its pool ([TLS](#tls)) | this project |
| **join service** (`leanpool-join`) | optional: lets a new Lean server box ask to join over HTTPS, and executes nothing itself | this project |

Load balancing is HAProxy's job because a generic balancer already has the right rule: Kimina
reports a Lean timeout as HTTP 200 with an error in the body, so it is passed through, and only
a failure that is not Lean's answer (a lost connection, a crashed worker, a gateway error) is
tried again on another server. The cache is this project's job because no
generic HTTP cache can do it: a check is a `POST` identified by its body, every attempt carries
its own id, and only something that understands Kimina's replies can see that a 200 is a timeout
that must not be stored.

Clients need no change beyond the address: the pool speaks Kimina's `POST /api/check` and
`GET /health`.

## Architecture

```
 clients                 +--------------------- the proxy box ----------------------+
 (provers, graders)      |                                                          |
      |                  |  HAProxy :18100   frontend lean_pool                     |
      +---- checks ----->|    |   GET /health is answered here:                     |
                         |    |   200 if at least one Lean server is up, else 503   |
                         |    |                                                     |
                         |    +--> backend cache --> leanpool-cache  127.0.0.1:18102|
                         |    |                         |                           |
                         |    |                   hit:  answered from SQLite        |
                         |    |                   miss: forwarded                   |
                         |    |                         v                           |
                         |    |    HAProxy 127.0.0.1:18101  frontend checkers_door  |
                         |    |                         |                           |
                         |    +-- cache is down ------->+--> backend checkers       |
                         |                                      |                   |
                         +--------------------------------------|-------------------+
                                                                |
                 least connections; never more checks per server than it has workers;
                 weight = workers x the percentage its usage agent reports
                                                                |
                        +---------------------------+-----------+---------------+
                        v                           v                           v
                 Kimina  @ lean-a            Kimina  @ lean-b            Kimina  @ ...
                 leanpool-agent :18200       leanpool-agent :18200
```

* Every check goes to HAProxy on the public port. Clients know no other address.
* HAProxy sends a check to the cache. On a miss the cache sends it back into HAProxy on a
  loopback-only listener that leads to the Lean servers. That second hop carries a header
  (`X-Lean-Pool-Hop`) so it can never be routed to the cache again.
* The cache is optional at run time: while it is down, HAProxy sends checks straight to the Lean
  servers. They keep flowing, uncached, with no client change. A check that was on its way to
  the cache at the moment it went down is sent again by HAProxy, around the cache.
* A check that fails without Lean's answer is sent to another Lean server; a Lean timeout is an
  answer and is not.
* The API key passes through: clients send it, the cache checks it and forwards it, each Lean
  server checks it.

## Quickstart

You need Docker with compose, [uv](https://docs.astral.sh/uv/) (or any Python 3.11+ environment
with this project installed) and one or more running Kimina Lean Servers that share an API key.

**1. On each Lean server box, run the usage agent.** It reads the box's own `/proc`, so run it on
the box, in a container or directly:

```sh
docker build -f deploy/Dockerfile -t lean-pool .
docker run -d --name leanpool-agent --restart unless-stopped -p 18200:18200 lean-pool leanpool-agent
```

**2. Check each Lean server before adding it** (see [Admission](#admission-leanpool-admit)):

```sh
uv run leanpool-admit --server http://lean-a.example:8000 --api-key-file api-key.txt \
    --cases cases/ --timeout 60 --concurrency 4
```

**3. On the proxy box, list the servers, render the configuration, start the proxy and the cache:**

```sh
cd deploy
uv run leanpool-haproxy-config add --servers servers lean-a lean-a.example:8000 4
uv run leanpool-haproxy-config add --servers servers lean-b lean-b.example:8000 8
uv run leanpool-haproxy-config render --servers servers > haproxy.cfg

printf '%s\n' 'the-pool-api-key' > api-key.txt   # readable by uid 10001, the cache's user
# edit LEANPOOL_CACHE_PIN in compose.example.yaml to name your Lean and Mathlib versions
docker compose -f compose.example.yaml up -d
```

**4. Use it** exactly like a single Kimina server:

```sh
curl -s http://localhost:18100/health
curl -s http://localhost:18100/api/check \
    -H "Authorization: Bearer $(cat api-key.txt)" -H 'Content-Type: application/json' \
    -d '{"snippets": [{"id": "attempt-1", "code": "theorem two : 1 + 1 = 2 := by rfl"}], "timeout": 60}'
curl -s http://localhost:18100/status -H "Authorization: Bearer $(cat api-key.txt)"
```

Send one snippet per request and size the client's concurrency to the pool's total worker count.
The pool says it: `GET /health` carries it in its body and every answer carries it in a response
header (see [Background work and the pool's size](#background-work-and-the-pools-size)). More
only waits in HAProxy's queue.

**Changing the pool later:** `add` or `remove` a server, `render` again, and reload HAProxy
(`docker compose -f compose.example.yaml kill -s HUP haproxy`). Validate a new configuration
before installing it: `haproxy -c -f haproxy.cfg`.

**This quickstart is unencrypted.** Checks and the API key cross the network in plain HTTP, and
the generated `haproxy.cfg` says so in its first line. To encrypt the pool, see [TLS](#tls).

## TLS

Given three certificate files, `leanpool-haproxy-config render` writes a configuration in which
every hop that leaves a machine is encrypted, TLS 1.3 or later, and both ends of the hop between
the proxy and a Lean server prove who they are. The certificates come from the pool's own
certificate authority, made with `leanpool-pki`. Nothing outside the pool is involved.

```
 clients ---HTTPS---> HAProxy :18100 --> cache --> HAProxy 127.0.0.1:18101     (the proxy box;
 (trust the pool's                                        |                     loopback hops
  authority)                                         mutual TLS                 stay plain)
                                                          |
                    +-------------------------------------+---------- a Lean server box ---+
                    |                                     v                                |
                    |   TLS front (HAProxy)  :8000  --plain, inside the box-->  Kimina     |
                    |   TLS front (HAProxy)  :18200 --plain, inside the box-->  agent      |
                    +----------------------------------------------------------------------+
```

### What is encrypted

| Hop | How |
|---|---|
| a client to the proxy's public port | HTTPS. The proxy presents the front door's certificate and the client checks it against the pool authority's certificate. The API key travels inside. |
| the proxy to each Lean server, checks and health checks alike | Mutual TLS. The box presents its certificate, and the proxy checks that the pool's authority signed it and that it is for **the server's name in the server list**. The proxy presents its client certificate, and the box accepts no other. |
| the proxy to each usage agent | The same mutual TLS. HAProxy's agent check cannot speak TLS, so it asks a loopback listener of the proxy itself (one per server), and that listener carries the exchange to the box. |
| the proxy to the cache, the cache to the proxy's checkers door, statistics | Plain, on loopback, inside the proxy's own network namespace. With TLS `render` refuses a cache that is not on a loopback address. |
| a box's TLS front to the Lean server and the agent beside it | Plain. Keep it inside the box (an internal Docker network, or loopback), and do not publish the Lean server's own port any more. |

### Who holds which key

| Key | Where it is | What it is for |
|---|---|---|
| the authority's (`ca.key`) | on the pool's host, and nowhere else | Signing this pool's certificates. No container needs it. |
| the front door's | on the pool's host, readable by the proxy | Being the pool to its clients. |
| the proxy's client key | on the pool's host, readable by the proxy | Calling the Lean servers. |
| each box's | on that box, where it was made; it never leaves | Being that box, by name, to the proxy. |

The authority's certificate (`ca.crt`) is public: every client needs it, and so does every box.

* **A server's identity is its name, not its address.** A box's certificate is issued for the
  name the server has in the server list, and the proxy checks that name (`sni str(<name>)`,
  `verifyhost <name>`) whatever host name or address it dials. Addresses change (DHCP), and
  HAProxy's `verifyhost` does not match an IP address in a certificate.
* **A box's certificate cannot call another box.** It is a server certificate, and TLS refuses a
  server certificate presented by a client. A box's front also turns away every client
  certificate but the proxy's own, by name
  (`tcp-request session reject unless { ssl_c_s_dn(cn) -m str <proxy client name> }`).
* **There is no revocation list.** A box that leaves the pool keeps a certificate that proves
  only its own name, and the proxy no longer dials it. If a private key is lost to someone else,
  make a new authority and issue everything again.
* Certificates last 5 years and the authority 10; `leanpool-pki expiry` prints the days left.

### Setting it up

On the pool's host, make the authority, the front door's certificate (with every name and
address clients use for the pool) and the proxy's client certificate:

```sh
leanpool-pki init --ca-dir pki/ca
leanpool-pki issue-server --ca-dir pki/ca --out-dir pki/proxy --file-stem front \
    --name pool.example --ip 192.0.2.1
leanpool-pki issue-client --ca-dir pki/ca --out-dir pki/proxy --file-stem proxy-client \
    --name lean-pool-proxy
cp pki/ca/ca.crt pki/proxy/
```

Mount `pki/proxy` read-only in the proxy's container, say at `/etc/leanpool/tls`, readable by
the user HAProxy runs as, and render with the paths as that container sees them:

```sh
leanpool-haproxy-config render --servers servers \
    --tls-front-door-pem /etc/leanpool/tls/front.pem \
    --tls-ca-file /etc/leanpool/tls/ca.crt \
    --tls-client-pem /etc/leanpool/tls/proxy-client.pem > haproxy.cfg
```

On each Lean server box, make the box's key and a signing request for the name the server will
have in the server list (`lean-a` here). The request goes to the pool's host; the key stays:

```sh
leanpool-pki csr --out-dir tls --name lean-a             # on the box
leanpool-pki sign-csr --ca-dir pki/ca --csr lean-a.csr --out lean-a.crt \
    --allow-dns lean-a                                   # on the pool's host
cat lean-a.crt tls/lean-a.key > tls/box.pem              # on the box again, with umask 077
```

Then put the TLS front where the Lean server and the agent used to be published. It is the same
HAProxy image as the proxy, given `box.pem` and a copy of the authority's `ca.crt`; the Lean
server and the agent sit behind it on a network that does not leave the box:

```sh
leanpool-haproxy-config render-box \
    --tls-server-pem /etc/leanpool/tls/box.pem --tls-ca-file /etc/leanpool/tls/ca.crt \
    --proxy-client-name lean-pool-proxy \
    --lean-upstream kimina:8000 --agent-upstream agent:18200 > box-haproxy.cfg
```

Before the box is added to the server list, test its Lean server alone through the front, from
the pool's host, as the proxy will reach it (see
[Through a box's TLS front](#through-a-boxs-tls-front)):

```sh
leanpool-admit --server https://192.0.2.10:8000 --api-key-file api-key.txt --cases cases/ \
    --tls-ca-file pki/ca/ca.crt --tls-client-pem pki/proxy/proxy-client.pem \
    --tls-server-name lean-a
```

Clients keep their API key and get the authority's certificate beside it:

```sh
curl --cacert ca.crt https://pool.example:18100/health
```

In Python, `ssl.create_default_context(cafile="ca.crt")` is all a client needs, with host name
checking on and under the strict X.509 rules that are the default from Python 3.13.

### Joining over the network (`leanpool-join`)

Carrying a signing request to the pool's host and a certificate back by hand is fine for one
box. `leanpool-join` does the carrying for a box that can reach the pool's host. For one join
window it serves a script and passes four messages between the new box and a spool directory.

**It executes nothing.** Whoever watches the spool on the pool's host (an operator, or a script
of yours) decides: validates what the box sent, signs with `leanpool-pki sign-csr`, runs
`leanpool-admit` against the box alone, adds it to the server list. The service only moves the
files, so it can run as an unprivileged, read-only container that holds no authority key.

A window is one random token and one box:

| Request | Answer |
|---|---|
| `GET /j/<token>` | 200 and the join script. |
| `POST /j/<token>/csr` with JSON `{name, lean_port, agent_port, workers, csr}` | 202. Written to `requests/csr.json` together with the caller's address. The first one binds the window to that address. |
| `GET /j/<token>/certificate` | 204 while there is no `responses/certificate.json`; 200 `{certificate, ca, api_key}` once that file holds `{certificate, ca}`; 409 `{reason}` if it holds `{reason}`. |
| `POST /j/<token>/ready` | 202. Written to `requests/ready.json` with the caller's address. |
| `GET /j/<token>/verdict` | 204 while there is no `responses/verdict.json`; 200 `{admitted, detail}` once there is. |
| `GET /j/<token>/image.json` | 200 and the manifest of the Lean server image this window ships, as the pool's host wrote it; 204 when it ships none. Binds nothing. |
| `GET /j/<token>/image` | 200 and the image file, or 206 and the part a `Range` asks for (a download that stopped goes on with `curl -C -`); 404 when the window ships none. Binds nothing. |

**Shipping the image.** Building a Lean server's image takes a new box minutes to a quarter of an
hour, and the pool's host already has one. With `--image-directory` the service also serves that
image: the directory holds `manifest.json` and the image file it names, a plain
`image-<12 hex>.tar` (`docker save`) of exactly the size the manifest records in `bytes`.
Anything else in the directory is never served, and a manifest that names another kind of file,
a link, or a file of another size ships nothing. The service sends the file from disk in chunks
of 256 KiB and never reads it whole, so it stays within a small container whatever the image's
size. It decides nothing about the image: the box checks the file's SHA-256 against the manifest
before it loads it. The two requests bind nothing, so a box may fetch the image, fail, and come
back in the same window.

The command a new box runs is one line. The box does not have the pool's authority yet, so it
cannot check the certificate's chain (`-k`); it checks the front door's public key instead,
which `leanpool-pki pin` prints on the pool's host:

```sh
curl -fsSk --pinnedpubkey 'sha256//...' https://pool.example:18110/j/<token> | sudo bash
```

curl compares the server's key with the pin before it sends anything, and exits 90 if they
differ. Pin every later request of the join script the same way: the script, the box's signing
request, its certificate and the pool's API key then all travel inside TLS to the machine that
holds the front door's key, and to no other.

* **HTTPS only**, TLS 1.3 or later, with the front door's certificate. There is no plain mode.
* **Without the token, nothing.** A request whose path does not carry the token is answered 404,
  as is a request for anything but the five above, so someone without the token cannot tell
  whether a window is open. The token is compared in constant time. It is 22 to 128 letters,
  digits, `-` and `_`: 128 random bits are 22 characters in URL-safe base64, 32 in hexadecimal.
* **One box per window.** Once a signing request has arrived, every request from another
  address is answered 409, whatever it asks. The address is the connection's own, never a
  header. The same signing request sent again is accepted; a different one is answered 409, so
  what the spool holds never changes under whoever is acting on it.
* **In order.** `ready` is answered 409 until a certificate has been issued, and the certificate
  and the verdict are handed out only to the box the window is bound to: an answer left in the
  spool is given to nobody before a signing request has arrived.
* **Small and checked.** A request body is at most 16 KiB (413 above). A signing request must be
  a JSON object with exactly the five fields; the name, the ports and the worker count obey the
  rules of the server list, the name must be one a certificate can carry, and `csr` must be one
  PEM signing request. Anything else is answered 422 and nothing is written. Whether the
  signing request is a good one is not judged here: `leanpool-pki sign-csr` does that, for the
  names it is told to allow.
* **The spool.** A request file appears complete or not at all, and is never changed once it
  exists. Write response files the same way, under a temporary name and then renamed; one that
  is not valid JSON yet is treated as not there, and one that is valid JSON of the wrong shape
  is answered 500. The service keeps no state of its own, so a restarted service carries on from
  what the spool holds. Give every window an empty spool and a new token.
* **Nothing secret is logged.** The token is part of every path, so the service has no access
  log, and what it logs names the caller's address and what happened: never a path, a body, the
  token or the API key.

## Configuration

Every command is configured the same way: a flag wins, then its environment variable, then the
default. `--help` on any command prints this reference.

### `leanpool-cache`

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--pin` | `LEANPOOL_CACHE_PIN` | required | A string naming the pool's Lean and Mathlib versions. Part of every cache key: change it whenever the Lean servers' image changes. |
| `--api-key-file` | `LEANPOOL_CACHE_API_KEY_FILE` | none | A file holding the pool's API key. |
| (no flag) | `LEANPOOL_CACHE_API_KEY` | none | The key itself. There is deliberately no flag for it: command lines are visible to every user of the machine. Give the file or the variable, not both. |
| `--allow-unauthenticated` | `LEANPOOL_CACHE_ALLOW_UNAUTHENTICATED` | off | Serve without checking a key, for a pool whose Lean servers have none. Without a key and without this, the cache refuses to start. |
| `--database` | `LEANPOOL_CACHE_DATABASE` | `leanpool-cache.sqlite3` (the image sets `/var/lib/leanpool/cache.sqlite3`) | The SQLite file holding the stored results. |
| `--max-bytes` | `LEANPOOL_CACHE_MAX_BYTES` | `4294967296` (4 GiB) | The size cap. Above it the least recently used results are evicted. |
| `--host` | `LEANPOOL_CACHE_HOST` | `127.0.0.1` | The address to listen on. |
| `--port` | `LEANPOOL_CACHE_PORT` | `18102` | The port to listen on. |
| `--upstream-url` | `LEANPOOL_CACHE_UPSTREAM_URL` | `http://127.0.0.1:18101` | Where a miss is forwarded: HAProxy's loopback listener. |
| `--upstream-timeout` | `LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS` | `1800` | How long to wait for the Lean servers. Must cover HAProxy's queue wait plus the slowest check; the generated `haproxy.cfg` states the minimum in its first lines. |
| `--max-request-bytes` | `LEANPOOL_CACHE_MAX_REQUEST_BYTES` | `16777216` (16 MiB) | The largest request body accepted. |
| `--exhaustion-pattern` (repeatable) | `LEANPOOL_CACHE_EXHAUSTION_PATTERNS` (comma-separated) | `out of memory`, `stack overflow` | A result with a Lean message containing one of these (any case) is never stored. |
| `--hop-header` | `LEANPOOL_HOP_HEADER` | `X-Lean-Pool-Hop` | The loop-guard header. Must match the proxy's. |

### `leanpool-agent`

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--host` | `LEANPOOL_AGENT_HOST` | `0.0.0.0` | The address to listen on. |
| `--port` | `LEANPOOL_AGENT_PORT` | `18200` | The port to listen on. |
| `--sample-interval` | `LEANPOOL_AGENT_SAMPLE_INTERVAL_SECONDS` | `5.0` | Seconds between two readings. The CPU share is measured over this interval. |
| `--memory-floor-mib` | `LEANPOOL_AGENT_MEMORY_FLOOR_MIB` | `2048` | Available memory below which the agent answers `drain`. The percentage starts to fall at twice this. |
| `--use-load-average` / `--no-use-load-average` | `LEANPOOL_AGENT_USE_LOAD_AVERAGE` | off | Also limit the percentage by the 1-minute load average per core. |
| `--stat-path` | `LEANPOOL_AGENT_STAT_PATH` | `/proc/stat` | Where CPU times are read. |
| `--meminfo-path` | `LEANPOOL_AGENT_MEMINFO_PATH` | `/proc/meminfo` | Where available memory is read. |
| `--loadavg-path` | `LEANPOOL_AGENT_LOADAVG_PATH` | `/proc/loadavg` | Where the load average is read. |

### `leanpool-haproxy-config`

```
leanpool-haproxy-config add        --servers FILE NAME HOST:PORT WORKERS [--agent-port PORT]
leanpool-haproxy-config remove     --servers FILE NAME
leanpool-haproxy-config list       --servers FILE
leanpool-haproxy-config render     --servers FILE [options] > haproxy.cfg
leanpool-haproxy-config render-box [options] > box-haproxy.cfg
```

`--servers` may also be given as `LEANPOOL_HAPROXY_SERVERS`. `add` takes `--agent-port` when the
box's usage agent does not listen on 18200. `list` prints the validated servers and the pool's
total worker count. `render` writes the pool proxy's configuration; `render-box` writes the
configuration of one Lean server box's TLS front (see [TLS](#tls)) and takes no server list.
Exit status: 0 on success, 1 when the input was refused (nothing is written), 2 on a usage
error.

Options of `render`:

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--public-port` | `LEANPOOL_HAPROXY_PUBLIC_PORT` | `18100` | The port clients send every check to. |
| `--checkers-port` | `LEANPOOL_HAPROXY_CHECKERS_PORT` | `18101` | The loopback port the cache forwards a miss to. |
| `--cache-address` | `LEANPOOL_HAPROXY_CACHE_ADDRESS` | `127.0.0.1:18102` | `HOST:PORT` of the cache. |
| `--stats-port` | `LEANPOOL_HAPROXY_STATS_PORT` | `18103` | The loopback port of HAProxy's statistics page. |
| `--lean-timeout` | `LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS` | `60` | The largest Lean `timeout` any client sends with a check. |
| `--server-wait` | `LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS` | `60` | How long a Lean server waits for a free worker (Kimina's `LEAN_SERVER_MAX_WAIT`). |
| `--margin` | `LEANPOOL_HAPROXY_MARGIN_SECONDS` | `30` | Added on top of each derived timeout. |
| `--queue-timeout` | `LEANPOOL_HAPROXY_QUEUE_TIMEOUT_SECONDS` | `2 x lean timeout + margin` | How long a check may wait in the proxy for a free worker. Must be above the Lean timeout. |
| `--max-request-bytes` | `LEANPOOL_HAPROXY_MAX_REQUEST_BYTES` | `262144` (256 KiB) | HAProxy's buffer size (`tune.bufsize`): the largest request, headers and body together, that can be replayed on another server after a failure. At least 16384. |
| `--maximum-connections` | `LEANPOOL_HAPROXY_MAXIMUM_CONNECTIONS` | `1024` | HAProxy's global connection limit (`maxconn`). A check through the cache holds two connections, and clients waiting in the queue count. Lower it when you raise the buffer size. |
| `--hop-header` | `LEANPOOL_HOP_HEADER` | `X-Lean-Pool-Hop` | The loop-guard header. Must match the cache's. |
| `--tls-front-door-pem` | `LEANPOOL_HAPROXY_TLS_FRONT_DOOR_PEM` | none | The front door's certificate and key in one file. Give this and the next two to encrypt the pool; give none of them for plain HTTP. Some without the others is refused. |
| `--tls-ca-file` | `LEANPOOL_HAPROXY_TLS_CA_FILE` | none | The pool authority's certificate. Every Lean server's certificate is checked against it. |
| `--tls-client-pem` | `LEANPOOL_HAPROXY_TLS_CLIENT_PEM` | none | The proxy's client certificate and key in one file, presented to every Lean server. |
| `--agent-tunnel-port` | `LEANPOOL_HAPROXY_AGENT_TUNNEL_PORT` | `18300` | With TLS, the first of the loopback ports the usage agents are asked through: one per server, counting up. |

The three TLS files are paths as HAProxy sees them (inside its container). They are written into
the configuration, not read by `render`, so they are absolute paths of letters, digits, `.`,
`_`, `-` and `/`.

Options of `render-box`:

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--tls-server-pem` | `LEANPOOL_HAPROXY_BOX_TLS_SERVER_PEM` | required | The box's certificate and key in one file, as HAProxy sees the path. |
| `--tls-ca-file` | `LEANPOOL_HAPROXY_TLS_CA_FILE` | required | The pool authority's certificate. A caller must present a certificate it signed. |
| `--proxy-client-name` | `LEANPOOL_HAPROXY_BOX_PROXY_CLIENT_NAME` | required | The name of the pool proxy's client certificate (its common name). No other caller is accepted. |
| `--lean-upstream` | `LEANPOOL_HAPROXY_BOX_LEAN_UPSTREAM` | required | `HOST:PORT` of the Lean server behind the front, reached in plain TCP. |
| `--agent-upstream` | `LEANPOOL_HAPROXY_BOX_AGENT_UPSTREAM` | required | `HOST:PORT` of the usage agent behind the front. |
| `--lean-port` | `LEANPOOL_HAPROXY_BOX_LEAN_PORT` | `8000` | The port the front serves the Lean server on: the one the pool's server list names. |
| `--agent-port` | `LEANPOOL_HAPROXY_BOX_AGENT_PORT` | `18200` | The port the front serves the usage agent on. |
| `--lean-timeout`, `--server-wait`, `--margin` | as for `render` | `60`, `60`, `30` | The pool's values: the front's timeouts are derived from them. |
| `--maximum-connections` | `LEANPOOL_HAPROXY_MAXIMUM_CONNECTIONS` | `1024` | HAProxy's global connection limit (`maxconn`). |

The server list is a text file, one server per line, `NAME HOST:PORT WORKERS [AGENT_PORT]`; `#`
starts a comment ([example](deploy/servers.example)). Every field is validated strictly, because
every field ends up inside `haproxy.cfg`:

* `NAME`: letters, digits, `_`, `.`, `-`; starts with a letter or digit; at most 63 characters; unique.
  With TLS the name is what the server's certificate is checked against, so it must also be a DNS
  name (no `_`, no label starting or ending with `-`, not only digits and dots), and two names may
  not differ only in letter case.
* `HOST`: an IPv4 address or an RFC 1123 host name (no underscores, no IPv6 literals); `HOST:PORT` unique.
* `PORT`, `AGENT_PORT`: 1 to 65535, plain decimal digits. `AGENT_PORT` is 18200 when left out.
* `WORKERS`: 1 to 256, the server's `LEAN_SERVER_MAX_REPLS`.

`add` and `remove` validate the whole resulting list before writing anything, and write through
a temporary file renamed over the original: a refused edit leaves the file byte-identical, and a
reader never sees a half-written list. `render` refuses a malformed list and an empty one.

### `leanpool-admit`

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--server` | `LEANPOOL_ADMIT_SERVER` | required | The Lean server's URL, e.g. `http://lean-a.example:8000`. The server itself, not the pool. With the three TLS options, `https://HOST:PORT`: the box's TLS front. |
| `--api-key-file` | `LEANPOOL_ADMIT_API_KEY_FILE` | none | A file holding the server's API key. Leave out for a server without one. |
| `--cases` | `LEANPOOL_ADMIT_CASES` | required | A directory with `verify/*.lean` and `reject/*.lean`. |
| `--timeout` | `LEANPOOL_ADMIT_TIMEOUT_SECONDS` | `60` | The Lean timeout sent with every check. |
| `--concurrency` | `LEANPOOL_ADMIT_CONCURRENCY` | `4` | How many checks are in flight at once. |
| `--tls-ca-file` | `LEANPOOL_ADMIT_TLS_CA_FILE` | none | The pool authority's certificate. Only it is trusted then: not the system's authorities. Give this and the next two to test a server behind its box's TLS front; give none of them for plain HTTP. Some without the others is a usage error. |
| `--tls-client-pem` | `LEANPOOL_ADMIT_TLS_CLIENT_PEM` | none | The pool proxy's client certificate and key in one file: the only caller a box's TLS front lets in. |
| `--tls-server-name` | `LEANPOOL_ADMIT_TLS_SERVER_NAME` | none | The name the server's certificate must carry: its name in the pool's server list. It is sent as the SNI and checked against the certificate, whatever host or address `--server` dials. |

### `leanpool-pki`

```
leanpool-pki init         --ca-dir DIR [--name TEXT] [--days DAYS]
leanpool-pki issue-server --ca-dir DIR --out-dir DIR --name NAME [--dns NAME]... [--ip ADDRESS]...
                          [--file-stem STEM] [--days DAYS] [--replace]
leanpool-pki issue-client --ca-dir DIR --out-dir DIR --name NAME
                          [--file-stem STEM] [--days DAYS] [--replace]
leanpool-pki csr          --out-dir DIR --name NAME [--dns NAME]... [--ip ADDRESS]...
                          [--file-stem STEM] [--replace]
leanpool-pki sign-csr     --ca-dir DIR --csr FILE --out FILE --allow-dns NAME [--allow-dns NAME]...
                          [--allow-ip ADDRESS]... [--days DAYS] [--replace]
leanpool-pki pin          CERTIFICATE
leanpool-pki expiry       CERTIFICATE
```

| Command | What it does | Files it writes |
|---|---|---|
| `init` | Makes a pool's certificate authority: an elliptic-curve P-256 key and a self-signed certificate valid for 10 years that may sign certificates and nothing else. | `ca.crt` and `ca.key` in `--ca-dir` |
| `issue-server` | Makes a key and a server certificate for `--name`, also valid for each `--dns` name and `--ip` address. | `<stem>.crt`, `<stem>.key`, `<stem>.pem` in `--out-dir` |
| `issue-client` | Makes a key and a client certificate for `--name`. | the same three |
| `csr` | Makes a key and a signing request for it, on the machine that will keep the key. | `<stem>.csr`, `<stem>.key` in `--out-dir` |
| `sign-csr` | Signs a request as a server certificate for exactly the `--allow-dns` and `--allow-ip` names. | `--out` |
| `pin` | Prints the certificate's public-key pin, `sha256//...`, the value `curl --pinnedpubkey` takes. | none |
| `expiry` | Prints the whole days the certificate has left; negative once it has expired. | none |

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--ca-dir` | `LEANPOOL_PKI_CA_DIR` | required | The directory holding the authority's `ca.crt` and `ca.key`. `init` creates it (mode 0700) when it is missing. |
| `--name` | none | required (`init`: `lean-pool CA`) | The DNS name the certificate is issued to: its common name, and always one of its names. At most 64 characters. For `init`, the authority's own name, any printable text. |
| `--dns` (repeatable) | none | none | A further DNS name the certificate is valid for. |
| `--ip` (repeatable) | none | none | An IPv4 or IPv6 address the certificate is valid for. |
| `--out-dir` | none | required | Where the files are written. Created (mode 0700) when it is missing. |
| `--file-stem` | none | the name | The files' name without its ending, so that a path does not depend on a host name: `--file-stem front` writes `front.pem`. |
| `--days` | none | `3650` for `init`, `1825` otherwise | How long the certificate is valid. Never beyond the authority's own last day. |
| `--replace` | none | off | Overwrite existing files. Without it an existing file is a refusal and nothing is written. `init` does not have it: an authority is never overwritten. |
| `--csr` | none | required | The signing request to sign. |
| `--out` | none | required | Where `sign-csr` writes the certificate. |
| `--allow-dns` (repeatable) | none | at least one | A DNS name the signed certificate is issued for. The first is its common name. |
| `--allow-ip` (repeatable) | none | none | An IP address the signed certificate is issued for. |

Every command prints the paths it wrote, one per line (`pin` and `expiry` print their value).
Exit status: 0 on success, 1 when the command was refused, 2 on a usage error.

* **Private keys.** A file holding a key (`ca.key`, `<stem>.key`, `<stem>.pem`) has mode 0600 from
  the moment it exists. No command prints a key, takes one as an argument or reads one from the
  environment. Keys are written unencrypted: protect the directory.
* **`<stem>.pem`** is the certificate followed by its key, the one file HAProxy's `crt` takes.
* **Nothing is half-written.** Each file is written under a temporary name and put in place in
  one step, and without `--replace` only if nothing is there.
* **One usage per certificate.** A server certificate carries the extended key usage `serverAuth`
  only, a client certificate `clientAuth` only, so a server's certificate cannot be presented as
  a client's. Neither can sign another certificate.
* **`sign-csr` decides what it signs.** The certificate is a server certificate for exactly the
  allowed names, whatever the request asks: its common name is the first `--allow-dns` name and
  no extension of the request is copied. (HAProxy accepts a certificate whose common name is the
  server's name even when its alternative names are not, so the common name is never taken from
  a request.) A request is refused when its subject or its
  alternative names ask for a name outside the allowed ones (or for any name that is neither a
  DNS name nor an IP address), when it asks to be a CA or to sign certificates, when its
  signature was not made with its own key, and when its key is not P-256, P-384, P-521 or RSA
  of at least 2048 bits.
* **Names** are DNS names made of letters, digits and `-`, in labels separated by `.`; no
  wildcard and no underscore. They are written in lower case.
* **Dates.** A certificate is valid from one day before it was made, so that a machine whose
  clock is behind does not refuse a certificate issued a moment ago.
* **Strict verifiers accept them.** The certificates carry what OpenSSL's strict X.509 mode
  wants (the default of Python 3.13's `ssl.create_default_context`): critical basic constraints
  and key usage, a subject key identifier, and on every issued certificate an authority key
  identifier and its names as subject alternative names.

### `leanpool-join`

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--script` | `LEANPOOL_JOIN_SCRIPT` | required | The join script to serve. It is served as it is and must hold no secret. |
| `--token-file` | `LEANPOOL_JOIN_TOKEN_FILE` | none | A file holding the window's token. |
| (no flag) | `LEANPOOL_JOIN_TOKEN` | none | The token itself. There is deliberately no flag for it. Give the file or the variable, not both; one of them is required. |
| `--tls-pem` | `LEANPOOL_JOIN_TLS_PEM` | required | The certificate and key to serve with, in one file: the pool's front door's, so that the pin a joining box checks is the front door's. |
| `--spool` | `LEANPOOL_JOIN_SPOOL` | required | The directory shared with whoever acts on requests. `requests/` is written, `responses/` is read. |
| `--api-key-file` | `LEANPOOL_JOIN_API_KEY_FILE` | none | A file holding the pool's API key, which a box receives with its certificate. |
| (no flag) | `LEANPOOL_JOIN_API_KEY` | none | The key itself. Give the file or the variable, not both. |
| `--pool-without-key` | `LEANPOOL_JOIN_POOL_WITHOUT_KEY` | off | The pool's Lean servers have no API key: a box is handed `null`. Without a key and without this, the service refuses to start. |
| `--image-directory` | `LEANPOOL_JOIN_IMAGE_DIRECTORY` | none | A directory holding the Lean server image this window ships: `manifest.json` and the one image file it names. Without it a joining box builds its own image. |
| `--host` | `LEANPOOL_JOIN_HOST` | `0.0.0.0` | The address to listen on. |
| `--port` | `LEANPOOL_JOIN_PORT` | `18110` | The port to listen on. |

Exit status: 0 after a requested stop, 1 when the service could not start (an unusable spool,
certificate or port), 2 on a usage error. What it serves and what it refuses is in
[Joining over the network](#joining-over-the-network-leanpool-join).

## Cache semantics

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

## Background work and the pool's size

Two small things a client can use, and neither is needed to use the pool.

**Priority.** When every worker is busy, checks wait in HAProxy's queue in arrival order, so a
client's share of a busy pool is its share of the requests in flight. A client whose work can
wait says so on each check:

```
X-Lean-Priority: background
```

A waiting background check is taken only when no normal check is waiting. The value is matched in
any letter case; any other value, and no header at all, is a normal check, so an old client and a
mistyped value behave as before. What it does not do:

* It never interrupts a check that is running, and a background check that has a worker keeps it.
  A client that starts while the pool is full of background checks waits for the first of them
  to finish.
* It does not shorten the wait limit. A background check that waits `timeout queue` gets the same
  503 as any check. Pause and ask again; do not record it as an answer. Under a client that
  keeps the pool full, background work makes no progress.
* The cache forwards the header on its second hop. A normal check never waits on a background
  check's flight for the same file: it sends its own request, and later callers join that one.
  The cost is one repeated check, and only when both kinds of client send the same file with the
  same timeout at the same moment.

**The pool's size.** Every answer that leaves the public port carries three response headers,
computed by HAProxy as the answer leaves:

| Header | What it counts |
|---|---|
| `X-Lean-Pool-Workers` | workers on the Lean servers that are up now |
| `X-Lean-Pool-Queued` | checks waiting in the queue for a worker, of both priorities |
| `X-Lean-Pool-Servers` | Lean servers that are up now |

`GET /health` carries the same numbers in its body, with the status codes it always had:

```json
{"status":"ok","workers":12,"queued":0,"servers":2}
```

So a client can size itself before its first check, and follow a server that joins or drops
without being restarted. Keep a little more in flight than there are workers (a fifth or a
quarter more), so that no worker waits for the client.

The numbers are **advisory**. Nothing in the pool reads them back: routing, the queue, each
server's limit and the cache are what they are without them. A client must carry on with its own
configured number when a header is missing, empty or not a whole number (a pool that runs an
older configuration sends none), and should never exceed a ceiling of its own, whatever the pool
says. `leanpool.signals` holds the names and two small readers (`capacity_from_headers`,
`capacity_from_health`) that return `None` for anything they cannot read. A server that its usage
agent has drained still counts while it is up: the number is the pool's size, not a promise of an
idle worker.

## The usage agent's weight formula

HAProxy connects to the agent every 5 seconds (`agent-check`), reads one line and closes. The
agent answers from its latest sample; a background sampler reads `/proc` every
`--sample-interval` seconds.

```
cpu share     = idle CPU time / all CPU time, between the two most recent readings of /proc/stat
                (time waiting for disk counts as idle)
memory share  = (MemAvailable - floor) / floor, limited to 0..1
                (1 when available memory is at least twice the floor, 0 at the floor)
load share    = 1 - (1-minute load average / cores), limited to 0..1     only with --use-load-average

percentage    = round(100 x the smallest share), limited to 1..100

reply         = "drain"           if MemAvailable < floor
                "ready up"        if there are not yet two CPU readings an interval apart
                "ready up N%"     otherwise
```

* **The CPU share is the whole box's idle share.** It counts every process on the box, so it
  includes the Lean server's own load: a box that is busy only because it is checking proofs
  reports a low percentage too. The agent cannot tell the pool's work from anyone else's.
  Weights are relative, so boxes that are equally busy with checks stay equally weighted; what
  the figure adds is that a box also busy with something else gets fewer new checks.
* The CPU share always comes from two readings. `/proc/stat` counts time since boot, so a single
  reading says nothing about now; until the second reading the agent reports no percentage and
  HAProxy leaves the weight as it is.
* The smallest share decides, because a check needs every resource at once.
* The percentage never drops below 1. Taking a server out of rotation is reserved for memory:
  below the floor the agent answers `drain`, and the box gets no new checks until memory
  recovers (the next normal reply starts with `ready`, which cancels the drain).
* If the agent cannot be reached, HAProxy keeps the server's last weight.

HAProxy sets the server's weight to `configured weight x N / 100` in whole numbers, and a weight
of 0 takes the server out of rotation. For that reason the generated configuration does not use
the worker count itself as the weight: it scales all weights by one common factor so that the
largest server gets as close to 256 (HAProxy's maximum) as possible. A 4-worker and an 8-worker
server get weights 128 and 256, not 4 and 8. The ratios between servers, which is all that
least-connections balancing reads, are the worker counts' ratios; the scaling only keeps a low
percentage from rounding down to zero.

## The generated HAProxy configuration

`leanpool-haproxy-config render` writes a complete `haproxy.cfg` in HAProxy 3.0 syntax:

* `global`: `tune.bufsize` and `maxconn` (see [Buffers and memory](#buffers-and-memory)).
* `resolvers pool_dns`: how server names are looked up while the proxy runs (see
  [Server names](#server-names)).
* `frontend lean_pool` on the public port. `GET /health` is answered by HAProxy itself from
  `nbsrv(checkers)`: 200 if at least one Lean server is up, 503 otherwise, so a health probe
  never queues behind proofs and does not depend on the cache. `default_backend cache`;
  `use_backend checkers` when the cache server is down, when the request carries the loop-guard
  header, and for every path other than `/api/check` (so the rest of Kimina's interface still
  works, uncached). `/status` is the cache's, and is answered 503 by HAProxy while the cache is
  down. The body of `/health` and three response headers on every answer carry the workers on
  servers that are up, the checks queued and the servers up: the first is summed per listed
  server (`srv_is_up(checkers/NAME)` times its worker count), the others are `queue(checkers)`
  and `nbsrv(checkers)`.
* `frontend checkers_door` on `127.0.0.1` only: sets the loop-guard header, uses `checkers`.
* `backend cache`: the cache server, health-checked every second and marked down on its first
  failed connection, with no connection cap; and the checkers door as its `backup` server (see
  [Losing the cache](#losing-the-cache)).
* `backend checkers`: `balance leastconn`; per server `check`, `maxconn <workers>`,
  `weight <workers x factor>` and `agent-check agent-port <port> agent-inter 5s`;
  `option httpchk GET /health`; `retries 2`,
  `retry-on conn-failure empty-response 500 502 503 504` and `option redispatch 1` (see
  [Failover](#failover-among-the-lean-servers)); and
  `http-request set-priority-class int(100)` for a check that carries
  `X-Lean-Priority: background`, which puts it behind every normal check in this backend's queue.
* `listen stats` on `127.0.0.1` only.

### Failover among the Lean servers

A check that failed **without Lean's answer** is sent again, to a different server when one has
a free worker (`option redispatch 1`):

* the connection could not be made (`conn-failure`), or was closed with no reply
  (`empty-response`): the server went away;
* the server answered 500, which is what Kimina answers when a worker crashed, or a gateway
  error (502, 503, 504).

`retries 2` allows at most two retries. A proof that crashes every worker it meets is therefore
stopped after three workers, and the caller then gets the 500.

**A Lean timeout is never retried.** Kimina reports it as HTTP 200 with the error in the body,
so to HAProxy it is a normal reply, and that is deliberate:

* it is Lean's answer for the time the client allowed, not a failure of the server;
* a second try would hold another worker for the full timeout, exactly when the pool is slowest;
* it would change measurements: a proof that timed out on one box and passed on a luckier one
  would make pass rates depend on how many servers the pool has.

For the same reasons HAProxy's own `response-timeout` (no reply within `timeout server`) is left
out of `retry-on`.

HAProxy can replay a request only if it had the whole of it, headers and body, in one buffer
when it first sent it. Two settings make that true for long proofs: the buffer is 256 KiB instead
of HAProxy's default 16 kB (`--max-request-bytes`), and `option http-buffer-request` makes
HAProxy wait for the complete request before it chooses a server. A request that does not fit
is still forwarded, and still retried when the connection could not be made (nothing had been
sent), but it is not replayed after a server received it.

### Buffers and memory

Larger buffers cost memory, so the generated `maxconn` is lowered with them. The worst case,
with every connection holding full buffers:

```
maxconn x buffers per connection x tune.bufsize
 1024   x          3             x   262144 bytes   =   768 MiB
```

Two buffers per connection is the figure in HAProxy's manual (request and response); the third
is the copy of the request kept for a replay. The manual's advice is to lower `maxconn` by the
factor `tune.bufsize` is raised, so change one with the other in mind; the generated file states
this arithmetic for the values it was rendered with.

A check that goes through the cache holds two of these connections (the client's and the
cache's), so the default allows about 500 checks in flight or waiting in HAProxy's queue.

### Server names

A server may be given by name or by IPv4 address.

* **At start**, a name is resolved by the system's resolver. If it does not resolve, that
  server starts as down and the proxy starts all the same (`init-addr last,libc,none`).
* **While running**, HAProxy asks the name servers in its `/etc/resolv.conf` for every server
  name again each 10 seconds (`resolvers pool_dns`, `parse-resolv-conf`,
  `timeout resolve 10s`), so a box whose address changed, such as one on DHCP, is followed, and
  a server that started unresolved comes up once its name resolves.
* **A failed lookup does not take a server out.** DNS says where a server is; only its health
  check says whether it is up. The server keeps its last address through a DNS outage for 24
  days (`hold nx`, `hold refused`, `hold timeout`, `hold other`), close to the longest period
  HAProxy accepts.
* IPv4 is preferred when a name has both kinds of address (`resolve-prefer ipv4`), because Kimina
  listens on IPv4 by default.
* A server given as an IPv4 address has no name to look up; none of this applies to it.

One trap: HAProxy's own DNS client only asks name servers. It does not read `/etc/hosts`, apply
search domains or use mDNS. A name that only the system's resolver knows resolves at start, is
never refreshed, and after 24 days of failed lookups HAProxy takes that server out. Give such a
server by IPv4 address, or use a name the DNS server answers as written.

### Losing the cache

Losing the cache is meant to cost no check:

* The first failed connection to the cache marks it down at once
  (`observe layer4 error-limit 1 on-error mark-down`) instead of waiting for its health check.
* The request that met the dead cache is sent again (`retry-on conn-failure empty-response`,
  `option redispatch`) to the `backup` server of the same backend, which is the checkers door:
  it gets the loop-guard header there and goes on to a Lean server. HAProxy pauses about one
  second before that retry.
* A request the cache had already received when it died is replayed the same way, if it fits in
  one buffer.
* From then on `use_backend checkers if !cache_up` sends checks straight to the Lean servers.
  `cache_up` is `srv_is_up(cache/cache)`, the cache server itself: `nbsrv(cache)` would count
  the backup and never report the cache as down.
* The cache is used again after two successful health checks, one second apart.

The price: a single failed connection to a cache that is alive also takes it out, for about two
seconds, during which checks are answered uncached.

### Timeouts

Timeouts are derived from the largest Lean timeout clients send, so the proxy never cuts a check
the Lean server would still have answered:

| Timeout | Value | With the defaults |
|---|---|---|
| `timeout server` (checkers) | server wait + 2 x Lean timeout + margin. Kimina allows the header import and the body the full Lean timeout each. | 210 s |
| `timeout queue` | 2 x Lean timeout + margin, unless `--queue-timeout` is given | 150 s |
| `timeout server` (cache) and `timeout client` | queue + checkers + margin | 390 s |

The cache's `--upstream-timeout` must be at least queue + checkers (360 s with the defaults);
the generated file states the number in its first lines.

### Statistics

HAProxy's statistics are on the loopback port only. From the cache container, which shares the
proxy's network namespace in the compose example:

```sh
docker compose -f compose.example.yaml exec cache \
    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18103/;csv').read().decode())"
```

### With TLS

Given the three TLS files ([TLS](#tls)), `render` writes the same configuration with these
differences, and nothing else changes:

* its first lines say what is encrypted. A configuration rendered without TLS starts with
  `# UNENCRYPTED:` instead;
* `global`: `ssl-default-bind-options ssl-min-ver TLSv1.3` and
  `ssl-default-server-options ssl-min-ver TLSv1.3`, so that a line a later edit adds is not
  below TLS 1.3 either. Every generated TLS line also says `ssl-min-ver TLSv1.3` itself;
* `frontend lean_pool`: `bind :18100 ssl crt <front door> ssl-min-ver TLSv1.3`;
* `backend checkers`: each server line gains
  `ssl verify required ca-file <authority> crt <client certificate>`, `sni str(<name>)` and
  `verifyhost <name>`, where `<name>` is the server's name in the list in lower case, and
  `check check-ssl check-sni <name>` so that the health check is the same mutual TLS;
* the agent check of each server becomes
  `agent-check agent-addr 127.0.0.1 agent-port <tunnel port>`;
* after every other section, `defaults agent_tunnels` (TCP mode, 10 s timeouts, only failures
  logged) and one `listen agent_tunnel_<name>` per server: bound to `127.0.0.1:<tunnel port>`,
  with one server, the box's agent port, reached with the same mutual TLS options and the same
  name check as the Lean server.

**Tunnel ports** are the first free ports counting up from `--agent-tunnel-port` (18300), in the
order of the server list, skipping the pool's own ports (public, checkers, statistics and the
cache's). The same list and options always give the same ports, and a server appended to the
list changes no other server's port. Removing a server moves the ports of the servers after it;
the listeners and the agent checks that use them are always rendered together.

If a box's agent cannot be reached, the tunnel closes the agent check's connection without a
reply. HAProxy ignores an empty reply, so the server keeps its last weight, as it does without
TLS.

With TLS these are refused, each because it would leave a hop unencrypted or an identity
unclear: a cache that is not on a loopback address; a server name that is not a DNS name; two
server names that differ only in letter case; a certificate path that is not a plain absolute
path; tunnels that do not fit below port 65535.

### A box's TLS front

`render-box` writes the configuration of the HAProxy that stands in front of one box's Lean
server and usage agent:

* `defaults`: `mode tcp`. The front carries bytes and never reads a request, so nothing about a
  check (its size, its headers, how long it takes) is limited or changed there. A Lean server
  that is gone shows to the pool proxy as a connection closed without a reply, which it retries
  on another server like a refused connection;
* `listen lean` and `listen agent`, each with
  `bind :<port> ssl crt <box certificate> ssl-min-ver TLSv1.3 ca-file <authority> verify required`:
  a caller without a certificate signed by the pool's authority is refused by the handshake;
* in both, `tcp-request session reject unless { ssl_c_s_dn(cn) -m str <proxy client name> }`
  before anything is forwarded;
* in both, one plain `server`: the Lean server and the agent, by `HOST:PORT`. Their names are
  looked up again while the front runs, as the pool proxy does for its servers, so a container
  that was restarted is found at its new address.

While Lean works on a check nothing is sent in either direction, so in TCP mode the front's
`timeout client` and `timeout server` must outlast the slowest check. They are the pool proxy's
own limit for a Lean server plus the margin once more (240 s with the defaults, against the
proxy's 210 s): the proxy always gives up first, and the front never cuts a check the proxy
would still have waited for. Render the front with the pool's `--lean-timeout`,
`--server-wait` and `--margin`. The agent's listener has 10 s timeouts.

## Admission (`leanpool-admit`)

A server on the wrong Lean or Mathlib version answers confidently and wrongly, and once it is
behind the balancer nothing shows which answers were its. So a server is tested alone, directly,
before it is added:

```sh
leanpool-admit --server http://lean-a.example:8000 --api-key-file api-key.txt --cases cases/
```

The cases directory holds Lean files of two kinds, and both are required:

* `verify/*.lean`: each must come back as a definitive Lean answer with no error-severity message
  and no `sorry`.
* `reject/*.lean`: each must come back with an error-severity message or a `sorry`.

Use files that depend on the pool's own Lean and Mathlib versions (proofs that are known to
check on the pinned versions, and near misses that are known not to), for example:

```
cases/verify/sum_comm.lean      import Mathlib
                                theorem sum_comm (a b : ℕ) : a + b = b + a := by ring

cases/reject/false_claim.lean   import Mathlib
                                theorem false_claim : (2 : ℕ) + 2 = 5 := by norm_num
```

A Lean timeout, a server error or any other reply that is not a definitive Lean answer is
"undecided", and undecided fails the case, whichever kind it is. A warning does not fail a
`verify` case, even one that contains the word "failed": a tactic that fails inside a combinator
that recovers warns while the proof is complete. Each case is sent once, as a plain Kimina
request with one snippet, and nothing is retried.

Before a file is sent, its leading `import` lines are moved above any leading comment. Kimina
takes a file's header to be the imports it starts with; a license comment before the imports
(which Lean itself accepts) would make it send the imports as part of the body, and Lean would
reject them. Imports inside comments and imports after the first command are not moved.

The command prints one JSON report (each case's expected and observed outcome, its seconds, the
counts, and checks per second at the given concurrency) and exits with

| Status | Meaning |
|---|---|
| 0 | every case behaved |
| 1 | at least one case misbehaved |
| 2 | bad arguments (an unusable cases directory, key file or TLS file included) |
| 3 | the server could not be reached, or the TLS handshake with it failed |

### Through a box's TLS front

With [TLS](#tls) a Lean server answers only through its box's front, and the front lets in
nothing but the pool proxy's client certificate. To test such a server alone, give the admission
test that certificate, the pool authority's certificate and the name the server will have in
the server list:

```sh
leanpool-admit --server https://192.0.2.10:8000 --api-key-file api-key.txt --cases cases/ \
    --tls-ca-file pki/ca/ca.crt --tls-client-pem pki/proxy/proxy-client.pem \
    --tls-server-name lean-a
```

The server is then held to the proof the proxy will ask of it: a certificate signed by the
pool's authority, for that name, whatever host or address `--server` dials. Only the given
authority is trusted, and nothing below TLS 1.3 is spoken.

* The three options go together. Some without the others, an `https://` server without them and
  an `http://` server with them are usage errors (status 2), refused before any request: a server
  that was meant to be reached over TLS is never spoken to in plain HTTP by an omission, and an
  `https://` URL is never trusted through the system's authorities.
* **The API key is not sent until the server has proved itself and accepted the certificate.**
  The first connection asks for `/health` without the key. A TLS client finishes its handshake
  before the server has judged the client's certificate, so only an answer to that request shows
  that the certificate was accepted. Checks, with the key, are sent after it.
* **A TLS failure fails admission at once, with status 3**, after one connection and at most ten
  seconds for each step of it. The report's `error` says which side refused which certificate:

  | What is wrong | The report says |
  |---|---|
  | the server's certificate is for another name | `the server's certificate is not for the name 'lean-a' given as --tls-server-name` |
  | another authority signed it | `the server's certificate was not signed by the authority in --tls-ca-file` |
  | it has expired | `the server's certificate has expired` |
  | the server refused our certificate with a TLS alert | `the server refused the client certificate in --tls-client-pem:` and why: `it is not a client certificate`, `it was not signed by the authority the server trusts`, `it has expired` |
  | the server accepted the handshake and closed (a box's front does this to a client certificate of the right authority that is not the proxy's) | `the server closed the connection after the TLS handshake without an answer` |
  | the port does not speak TLS 1.3 (a Lean server published without its front) | `is this the port of a TLS front?` |

The command reads the proxy's private key, so run it where that key already is: on the pool's
host.

## What is tested, and what is not verified

Two kinds of tests, neither of which needs Docker, the network or a Lean server. Lean servers
are in-process fakes that speak Kimina's interface.

**Tests that always run:**

* the generated configuration, line by line (that each directive described above is present,
  with its arguments), and every refusal of the generator and of the server-list editor,
  including that a refused or failed edit leaves the file byte-identical;
* the cache: an identical second check never reaches the upstream; what is and is not stored;
  bypass; single flight; request order; eviction order and the size cap; authentication; that
  the cache does not cap concurrency (150 checks in flight at once);
* the agent: the formula, the reply format, that the CPU share needs two readings, `drain`;
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
* the join service: each of its five requests, 404 for everything without the token, the
  one-box-per-window rule (from a second loopback address where the machine has one), every
  malformed request, the 16 KiB limit, what is written to the spool and when, and that neither
  the token nor the API key is ever logged. As a real process: it serves HTTPS only and refuses
  TLS 1.2; and, where `curl` is installed, `curl -fsSk --pinnedpubkey` fetches the script with
  the front door's pin, exits 90 having fetched nothing with any other pin, and carries a whole
  join through.

**Tests that run only where `haproxy` is installed** (`tests/test_haproxy_live.py`; skipped
otherwise). They start a real HAProxy on the generated configuration, in front of the real
cache and fake Lean servers on loopback, and show what HAProxy does with it:

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
  rotation, `drain` drains it, and the next normal reply restores it.

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

These have been run against HAProxy 3.0.29, the binary of the official `haproxy:3.0` image, and
`haproxy -c` accepts every generated file with no warning.

**What none of this establishes:**

* **Throughput has not been measured.** There are no performance numbers for the pool, the cache
  or the agent yet, and this README claims none.
* **HAProxy's memory use** with 256 KiB buffers has not been measured. The 768 MiB figure is
  arithmetic from the manual's two buffers per connection.
* **The 24-day hold was not waited out, and no test covers it.** Once, by hand, on HAProxy
  3.0.29: with the generated holds a server kept its address and kept answering through 25
  seconds without DNS, and with the holds shortened to 5 seconds the same server was taken out
  (as was one whose name only the system's resolver knew).
* The address-change test replaces one line of the generated configuration,
  `parse-resolv-conf`, with a test name server: HAProxy always reads `/etc/resolv.conf` for that
  directive, and a test cannot replace that file.
* The Dockerfile, the compose example and the CI workflow have not been run.
* **TLS has been run on one machine only**, every part on loopback. It has not been run in
  containers or between machines, and what it costs in CPU or throughput has not been measured.
* The fakes follow Kimina's interface as read from its source. The pool has not yet been run
  against real Kimina servers.

## Limitations

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
  alive.
* A Lean server answers 500 for more than a crashed worker (any error it could not handle), and
  each such check is tried on up to three workers before the caller sees the 500.
* A check is tried on at most three servers. A Lean server that fails every check at once but
  still answers `/health` stays in rotation, and least-connections balancing sends it new checks
  first because it is never busy. One or two such servers cost checks a retry or two; with
  three, some checks fail although a healthy server exists.
* A 429 from a Lean server (no free worker within its own wait) is returned to the client, not
  retried: HAProxy cannot retry on that status.
* A server name that only the system's resolver knows (`/etc/hosts`, a search domain, mDNS) is
  taken out by HAProxy after 24 days ([Server names](#server-names)). A mistyped name no longer
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

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

With `haproxy` (3.0) on the `PATH`, `uv run pytest` also runs the tests that need it; they take
about 70 seconds. Without it they are reported as skipped. A few tests compare with `openssl` or
drive `curl`, and are skipped where those are not installed.

`leanpool.agent` and `leanpool.haproxy` use the standard library only (a test imports them with
every third-party package made unimportable). `leanpool.cache`, `leanpool.admit` and
`leanpool.join` use [aiohttp](https://docs.aiohttp.org/); `leanpool.pki` uses
[cryptography](https://cryptography.io/).

## Credits and license

lean-pool exists to sit in front of the
[Kimina Lean Server](https://github.com/project-numina/kimina-lean-server) by Project Numina
(MIT license), which does the actual work of checking Lean. This project contains none of its
code; it speaks its HTTP interface.

lean-pool is released under the [MIT license](LICENSE).
