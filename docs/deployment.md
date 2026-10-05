# Deploying a pool

This guide takes you from nothing to a running pool, and then through operating it. It uses
plain HTTP, which is right for a first run on a network you trust; [TLS](tls.md) encrypts every
hop once the pool works. To see a pool before you build one, run the
[demo](../examples/demo): it needs no Lean.

## The layout

```
                         the pool box                          a Lean server box (one of many)
              +------------------------------+              +--------------------------------+
 clients ---> | HAProxy            :18100    | --- checks -> | Kimina Lean Server    :8000    |
              |   cache            (inside)  | --- "how      | leanpool-agent        :18200   |
              +------------------------------+     busy?" -> +--------------------------------+
```

| Port | Where | What |
|---|---|---|
| 18100 | the pool box | The one address clients use. |
| 8000 | each Lean server box | Kimina. Only the pool box needs to reach it. |
| 18200 | each Lean server box | The usage agent. Only the pool box needs to reach it. |
| 18101, 18102, 18103 | inside the proxy's network namespace | The proxy's door to the Lean servers, the cache, HAProxy's statistics. Loopback only; never published. |

The pool box can also be a Lean server box. Nothing in the pool needs a GPU.

You need Docker with compose on every box, and on the pool box a checkout of this repository
with [uv](https://docs.astral.sh/uv/), which runs the commands below without installing
anything (`uv run leanpool-...`). [Other ways to get the commands](#getting-the-commands) are
at the end.

## 1. Decide the key and the pin

**The API key** is one secret shared by the whole pool: clients send it, the cache checks it,
and every Lean server checks it again. Make one:

```sh
openssl rand -hex 32
```

**The pin** is a short string that names the Lean and Mathlib versions your servers run, such
as `lean4.27.0-mathlib4.27.0`. It is part of every cache key, so a result is only ever given
again to a pool with the same pin. Choose it once, and change it whenever the servers' image
changes.

## 2. Start each Lean server box

Each box runs a Kimina Lean Server and, beside it, the usage agent that tells the pool how
much headroom the box has. From a checkout on the box:

```sh
cd deploy/lean-server
printf 'LEAN_SERVER_API_KEY=%s\nLEAN_SERVER_MAX_REPLS=%s\n' 'the-pool-api-key' 8 > .env
docker compose up -d --build
```

`LEAN_SERVER_MAX_REPLS` is how many checks this box runs at once: its **workers**. Size it to
the box's cores and memory, and remember the number, because the pool's server list needs it.

Kimina is not part of lean-pool. [`compose.yaml`](../deploy/lean-server/compose.yaml) runs the
image Kimina's own compose file runs, and `KIMINA_IMAGE` in `.env` names another. What matters
to the pool is that every box runs **the same Lean and Mathlib**; building an image with your
versions is described in
[Kimina's README](https://github.com/project-numina/kimina-lean-server). A box that already
runs Kimina some other way only needs the agent added: `docker compose up -d --build agent`,
or `leanpool-agent` run directly (it uses the Python standard library only).

Check the box from the pool box:

```sh
curl -s http://lean-a.example:8000/health     # Kimina answers
cat < /dev/tcp/lean-a.example/18200           # (bash) the agent answers one line: ready up 87%
```

## 3. Test each Lean server before it joins

A server on the wrong Lean or Mathlib version answers confidently and wrongly, and once it is
behind the pool nothing shows which answers were its. So each server is tested alone first,
against files that must verify and files that must not. On the pool box:

```sh
cd deploy/pool
printf '%s\n' 'the-pool-api-key' > api-key.txt
uv run leanpool-admit --server http://lean-a.example:8000 --api-key-file api-key.txt \
    --cases ../../examples/admission-cases
```

[`examples/admission-cases`](../examples/admission-cases) is a starting point that any Lean
with Mathlib passes. Add files that depend on *your* versions: a proof that only checks on the
Mathlib you pinned is what catches a box that has another. [Admission](admission.md) has the
rules and the exit statuses.

## 4. Start the pool box

Still in `deploy/pool`: list the servers, render HAProxy's configuration from the list, and
start the proxy and the cache.

```sh
uv run leanpool-haproxy-config add --servers servers lean-a lean-a.example:8000 8
uv run leanpool-haproxy-config add --servers servers lean-b lean-b.example:8000 4
mkdir -p haproxy
uv run leanpool-haproxy-config render --servers servers > haproxy/haproxy.cfg

echo 'LEANPOOL_CACHE_PIN=lean4.27.0-mathlib4.27.0' > .env    # your pin
docker compose up -d --build
```

The cache reads `api-key.txt` as its own user, uid 10001: leave the file readable by that user
(mode 0644, or 0640 with group 10001).

Each `add` line is a name of your choosing, where the server listens, and its workers. The
[server list](configuration.md#leanpool-haproxy-config) is a small text file you can also edit
by hand. The rendered [`haproxy.cfg`](haproxy.md) is commented throughout and says in its first
lines what it was sized for; read it once.

Give a server by a name your DNS answers, or by IPv4 address. A name that only `/etc/hosts`
knows is resolved once and never followed ([why](haproxy.md#server-names)).

## 5. Check the pool

```sh
curl -s http://localhost:18100/health
# {"status":"ok","workers":12,"queued":0,"servers":2}

curl -s http://localhost:18100/api/check \
    -H "Authorization: Bearer $(cat api-key.txt)" -H 'Content-Type: application/json' \
    -d '{"snippets": [{"id": "attempt-1", "code": "theorem two : 1 + 1 = 2 := by rfl"}], "timeout": 60}'

curl -s http://localhost:18100/status -H "Authorization: Bearer $(cat api-key.txt)"
# {"hits": 0, "misses": 1, ... "stored_entries": 1, ...}
```

Send the check a second time: it comes back at once, with `"cached": true`. The pool is ready.
[Using a pool](clients.md) is the clients' side.

## Operating a pool

### Adding and removing a server

Test the new server, change the list, render to a new file, let HAProxy validate it, move it
into place, reload:

```sh
uv run leanpool-admit --server http://lean-c.example:8000 --api-key-file api-key.txt \
    --cases ../../examples/admission-cases
uv run leanpool-haproxy-config add --servers servers lean-c lean-c.example:8000 6
uv run leanpool-haproxy-config render --servers servers > haproxy/haproxy.cfg.new
docker run --rm --network none -v "$PWD/haproxy":/cfg:ro haproxy:3.0 \
    haproxy -c -f /cfg/haproxy.cfg.new
mv haproxy/haproxy.cfg.new haproxy/haproxy.cfg
docker compose exec haproxy sh -c 'kill -HUP 1'
```

`leanpool-haproxy-config remove --servers servers lean-c` takes one out the same way. `add`
and `remove` validate the whole list before they write, so a refused edit leaves the file as
it was.

### Reloading

`kill -HUP 1` inside the container makes HAProxy start new workers on the new configuration
while the old ones finish the checks they hold. Two details matter:

* **Send the signal from inside the container**, as above. `docker compose kill -s HUP` also
  reloads, but Docker counts a container it has signalled as stopped by hand, and
  `restart: unless-stopped` then no longer brings it back after a crash or a reboot.
* **The compose file mounts the directory `haproxy/`, not the file.** A file that is replaced
  by moving a new one over it is a new file, which a container that mounted the old one never
  sees.

### Watching it

| What | How |
|---|---|
| Is the pool up, how big is it, is anything waiting? | `curl -s http://localhost:18100/health` |
| What is the cache doing? | `curl -s http://localhost:18100/status -H "Authorization: Bearer ..."` ([the counters](cache.md)) |
| Which server answered which check, and how long each part took? | `docker compose logs -f haproxy`: one line per request, with HAProxy's timers. |
| Each server's state, weight and queue | HAProxy's [statistics](haproxy.md#statistics), on loopback inside the proxy. |
| How long do the certificates last? (TLS) | `leanpool-pki expiry CERTIFICATE` |

A server that drops out shows as `servers` and `workers` going down in `/health`. The checks it
held are sent to another server, and it is used again as soon as its health check passes.

### Changing the Lean or Mathlib version

Bring every Lean server to the new image, test each with cases for the new versions, then
change `LEANPOOL_CACHE_PIN` in `.env` and run `docker compose up -d`. Results stored under the
old pin stop matching and are evicted as the store fills. Do not run a pool whose servers
disagree: the pool cannot tell their answers apart.

### What is kept, and what is not

The cache's store is the compose volume `cache-data`; it survives `docker compose down` and is
deleted by `docker compose down -v`. Its counters start from zero whenever the cache starts.
HAProxy keeps nothing. Losing the store costs only time: every result in it can be computed
again.

### Before you open it to a network

This guide's pool is plain HTTP: checks and the API key cross the network readable, and the
generated `haproxy.cfg` says so in its first line. Keep ports 8000 and 18200 of the Lean server
boxes reachable from the pool box only, and move to [TLS](tls.md) before the pool is reachable
from anywhere you do not control.

## Getting the commands

| Where | How |
|---|---|
| In a checkout of this repository | `uv run leanpool-haproxy-config ...` (and likewise for the others), from any directory of the checkout. |
| Installed for your user | `uv tool install git+https://github.com/cabloo/lean-pool` or `pipx install git+https://github.com/cabloo/lean-pool` |
| In a Python environment | `pip install git+https://github.com/cabloo/lean-pool` (Python 3.11 or later) |
| From the image | `docker build -f deploy/Dockerfile -t lean-pool .` then `docker run --rm lean-pool leanpool-pki --help` |

The image runs the cache by default and holds all six commands. `leanpool-agent` and
`leanpool-haproxy-config` need nothing but the Python standard library.
