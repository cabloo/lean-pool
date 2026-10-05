# Configuration reference

Every command is configured the same way: a flag wins, then its environment variable, then the
default. `--help` on any command prints this reference.

## `leanpool-cache`

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

## `leanpool-agent`

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

## `leanpool-haproxy-config`

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
configuration of one Lean server box's TLS front (see [TLS](tls.md)) and takes no server list.
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
starts a comment ([example](../deploy/pool/servers.example)). Every field is validated strictly, because
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

## `leanpool-admit`

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

## `leanpool-pki`

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

## `leanpool-join`

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
[Joining over the network](joining.md).
