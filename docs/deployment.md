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

Nothing in the pool needs a GPU. The pool box can also be a Lean server box
([one machine for both](#one-machine-for-both)).

## What you need

* **Linux, and Docker with the compose plugin, on every box.** The usage agent reads `/proc`.
* **A clone of this repository on every box**: the compose files build this project's image
  from it.
* **[uv](https://docs.astral.sh/uv/) on the pool box.** It runs this project's commands from the
  clone without installing anything (`uv run leanpool-...`).
  [Other ways to get the commands](#getting-the-commands) are at the end.
* **An image of the [Kimina Lean Server](https://github.com/project-numina/kimina-lean-server)
  with your Lean and Mathlib**, the same on every box. The compose file uses Kimina's published
  image unless you name another.

The compose files in [`deploy/`](../deploy) are adapted from the files of a running pool and are
validated by Docker in this project's CI. They have not been started as they are written here,
and that pool builds Kimina from source instead of using its published image
([what is verified](verification.md)). Read them before you run them; they are short.

**Anyone who holds the pool's API key can run code on every Lean server box**, because checking
Lean code runs it. Read [SECURITY.md](../SECURITY.md) before the pool is reachable by anyone
but you.

## 1. On the pool box: make the key, choose the pin

**The API key** is one secret shared by the whole pool: clients send it, the cache checks it,
and every Lean server checks it again. Make one, in the file the pool will read it from:

```sh
cd deploy/pool
openssl rand -hex 32 > api-key.txt
chmod 0644 api-key.txt
```

The cache reads that file as its own user, uid 10001, so it must stay readable by that user:
mode 0644, or 0640 with group 10001 if other people have accounts on the box.

**The cache's pin** is a short string that names the Lean and Mathlib your servers run. It is
part of every cache key, so a stored result is only ever given to a pool with the same pin.
The tag of the image your servers run is a good pin. Write it where the compose file reads it:

```sh
echo 'LEANPOOL_CACHE_PIN=kimina-lean-server-2.0.0' > .env
```

Change the pin whenever the servers' image changes. To ask a running server which Lean it has,
send it the check `#eval Lean.versionString`.

## 2. On each Lean server box: Kimina and the usage agent

Each box runs a Kimina Lean Server and, beside it, the usage agent that tells the pool how
much headroom the box has. In the clone on that box, with the key from step 1:

```sh
cd deploy/lean-server
printf 'LEAN_SERVER_API_KEY=%s\nLEAN_SERVER_MAX_REPLS=%s\n' 'PASTE-THE-KEY-HERE' 4 > .env
docker compose up -d --build
```

`LEAN_SERVER_MAX_REPLS` is how many checks this box runs at once: its **workers**, 4 here. Each
worker is one Lean process with Mathlib loaded, so size the number to the box's cores and
memory, and remember it: the pool's server list needs it.

Kimina is not part of lean-pool. [`compose.yaml`](../deploy/lean-server/compose.yaml) runs the
image Kimina's own compose file runs, and `KIMINA_IMAGE=...` in `.env` names another. What
matters to the pool is that every box runs **the same Lean and Mathlib**; building an image
with your versions is described in
[Kimina's README](https://github.com/project-numina/kimina-lean-server).

A box that already runs Kimina some other way needs only the agent beside it:

```sh
docker build -f deploy/Dockerfile -t lean-pool .          # in the clone's root
docker run -d --name leanpool-agent --restart unless-stopped -p 18200:18200 \
    lean-pool leanpool-agent
```

Then check the box from the pool box:

```sh
curl -s http://lean-a.example:8000/health     # Kimina answers
cat < /dev/tcp/lean-a.example/18200           # (bash) the agent answers one line: ready up 87%
```

## 3. On the pool box: test each Lean server before it joins

A server on the wrong Lean or Mathlib answers confidently and wrongly, and behind the pool its
answers look like everyone else's. So each server is tested alone first, against files that
must verify and files that must not. In `deploy/pool`:

```sh
uv run leanpool-admit --server http://lean-a.example:8000 --api-key-file api-key.txt \
    --cases ../../examples/admission-cases
```

[`examples/admission-cases`](../examples/admission-cases) is a starting point that any Lean
with Mathlib passes. Add files that depend on *your* versions: a proof that only checks on the
Mathlib you pinned is what catches a box that has another. [Admission](admission.md) has the
rules and the exit statuses.

## 4. On the pool box: the server list, the configuration, the pool

Still in `deploy/pool`. List the servers, render HAProxy's configuration from the list, and
start the proxy and the cache:

```sh
uv run leanpool-haproxy-config add --servers servers lean-a lean-a.example:8000 4
uv run leanpool-haproxy-config add --servers servers lean-b lean-b.example:8000 8
mkdir -p haproxy
uv run leanpool-haproxy-config render --servers servers > haproxy/haproxy.cfg
docker compose up -d --build
```

Each `add` line is a name of your choosing, where the server listens, and its workers. The
[server list](configuration.md#leanpool-haproxy-config) is a small text file you can also edit
by hand ([an example](../deploy/pool/servers.example)). The rendered
[`haproxy.cfg`](haproxy.md) is commented throughout and says in its first lines what it was
sized for; read it once.

Give a server by a name your DNS answers, or by IPv4 address. A name that only `/etc/hosts`
knows is resolved once and never followed ([why](haproxy.md#server-names)).

### One machine for both

When the pool box is also a Lean server box, **do not list that server as `localhost` or
`127.0.0.1`**. HAProxy runs in a container, and there those names mean the container itself:
the server would stay down while `curl localhost:8000` on the machine answers. List the
machine's own address on your network instead, such as `192.0.2.10:8000`.

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
into place, reload. In `deploy/pool`:

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
it was. A pool that uses TLS renders and validates
[with its certificates](tls.md#changing-a-pool-that-uses-tls).

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

### Sizing

* **A Lean server box** is sized by its workers. Each is a Lean process that holds Mathlib in
  memory and grows with the proofs it checks; Kimina limits each one's address space
  (`LEAN_SERVER_MAX_REPL_MEM`). Start with fewer workers than cores and watch
  the box's memory under load. The usage agent drains a box whose available memory falls below
  its floor (2 GiB by default).
* **The pool box** needs little. The cache's store is one SQLite file capped at 4 GiB by default
  (`LEANPOOL_CACHE_MAX_BYTES`); in the pool this project was written for, a stored result takes
  about 1.1 kB. HAProxy's buffers are sized for 256 KiB requests and can take up to 768 MiB with
  every connection in use ([buffers and memory](haproxy.md#buffers-and-memory)).
* **The pool box is a single point of failure.** If it is down, the pool is down. The Lean
  servers themselves still answer anyone who calls them directly.

### Changing the Lean or Mathlib version

Bring every Lean server to the new image, test each with cases for the new versions, then
change `LEANPOOL_CACHE_PIN` in `.env` and run `docker compose up -d`. Results stored under the
old pin stop matching and are evicted as the store fills. Do not run a pool whose servers
disagree: the pool cannot tell their answers apart.

### Upgrading lean-pool

On each box, `git pull` in the clone and `docker compose up -d --build`. On the pool box,
render the configuration again first, because a new version may generate a different one, and
validate it as [above](#adding-and-removing-a-server). [CHANGELOG.md](../CHANGELOG.md) says
when a version changes an interface.

### What is kept, and what is not

The cache's store is the compose volume `cache-data`; it survives `docker compose down` and is
deleted by `docker compose down -v`. Its counters start from zero whenever the cache starts.
HAProxy keeps nothing. Losing the store costs only time: every result in it can be computed
again.

### Before you open it to a network

This guide's pool is plain HTTP: checks and the API key cross the network readable, and the
generated `haproxy.cfg` says so in its first line. The compose files publish their ports on
every address of the machine. Keep ports 8000 and 18200 of the Lean server boxes reachable from
the pool box only, with your firewall, and move to [TLS](tls.md) before the pool is reachable
from anywhere you do not control.

## Getting the commands

| Where | How |
|---|---|
| In a clone of this repository | `uv run leanpool-haproxy-config ...` (and likewise for the others), from any directory of the clone. |
| Installed for your user | `uv tool install git+https://github.com/cabloo/lean-pool` or `pipx install git+https://github.com/cabloo/lean-pool` |
| In a Python environment | `pip install git+https://github.com/cabloo/lean-pool` (Python 3.11 or later) |
| From the image | `docker build -f deploy/Dockerfile -t lean-pool .` then `docker run --rm lean-pool leanpool-pki --help` |

The pages of this documentation write the commands bare (`leanpool-pki ...`) where it does not
matter how you got them; in a clone, put `uv run` in front. The image runs the cache by default
and holds all six commands. `leanpool-agent` and `leanpool-haproxy-config` need nothing but the
Python standard library.
