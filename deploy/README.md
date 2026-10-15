# Deployment files

| File | For |
|---|---|
| [`Dockerfile`](Dockerfile) | The one image of this project: it runs the cache by default, and the usage agent or the join service by name, and holds all six commands. Build it from the repository root: `docker build -f deploy/Dockerfile -t lean-pool .` |
| [`install/`](install/README.md) | The installers: Lean servers, the pool and joining boxes, from a site directory of values. [Deploying a pool](../docs/deployment.md) walks through them. |
| [`lean-server/compose.yaml`](lean-server/compose.yaml) | One Lean server: Kimina, hardened. |
| [`lean-server/compose.agent.yaml`](lean-server/compose.agent.yaml) | Added after it on a box in a pool: the usage agent. |
| [`lean-server/compose.tls.yaml`](lean-server/compose.tls.yaml) | Added after both for TLS: a front on both ports, which then publishes them instead. |
| [`lean-server/compiled-search/`](lean-server/compiled-search) | Makes and checks linarith's search step as compiled code on the box (`make.sh`). |
| [`lean-server/compose.compiled-search.yaml`](lean-server/compose.compiled-search.yaml) | Added last while that step is on: the workers start through it. |
| [`lean-server/read-only-check.sh`](lean-server/read-only-check.sh) | Says whether a running server would keep working with its root filesystem read-only. |
| [`pool/compose.yaml`](pool/compose.yaml) | The pool box: HAProxy and the cache. |
| [`pool/compose.tls.yaml`](pool/compose.tls.yaml) | Added after it for TLS: the proxy's certificates. |
| [`pool/compose.dns.yaml`](pool/compose.dns.yaml) | Added after them where the Lean servers are named on a LAN: a DNS server for the proxy. |
| [`pool/servers.example`](pool/servers.example) | The format of the server list. |

The installers run the compose files exactly as they are here: they copy the ones a box needs into its
directory under `/srv`, with an env file that names them (`COMPOSE_FILE`) and the project
(`COMPOSE_PROJECT_NAME`) and holds every setting below. Each compose file's header also says what it
expects when it is run without the installer. Docker reads every combination of them in this project's
CI; [what is verified](../docs/verification.md) says what else is.

## Settings

Each setting is a variable of the env file beside the compose files: `kimina.env` for a Lean server,
`pool.env` for the pool. A setting a program reads keeps that program's own name (the
[configuration reference](../docs/configuration.md) lists the `LEANPOOL_` ones); the others are named for
the box they belong to.

### What the compose files read

| Variable | Default | What it sets |
|---|---|---|
| `LEAN_SERVER_IMAGE` | `projectnumina/kimina-lean-server:2.0.0` | The Lean server's image: your Lean and Mathlib, the same on every box of a pool. |
| `LEAN_SERVER_API_KEY` | required | The pool's API key, which the server asks of every check. |
| `LEAN_SERVER_MAX_REPLS` | required | How many checks the box runs at once: its worker count. |
| `LEAN_SERVER_MAX_REPL_USES` | `200` | How many checks a worker runs before it is replaced. |
| `LEAN_SERVER_MAX_REPL_MEM` | `8G` | Each worker's address-space limit. |
| `LEAN_SERVER_MAX_WAIT` | `60` | Seconds a check waits for a free worker before the server answers 429. |
| `LEAN_SERVER_LOG_LEVEL` | `WARNING` | Kimina's log level. |
| `LEAN_SERVER_PUBLISHED_PORT` | `8000` | The host port of the Lean server (or of its TLS front). |
| `LEAN_SERVER_MEM_LIMIT` | `0` (none) | The server container's memory ceiling, with no swap beyond it. |
| `LEAN_SERVER_CPUS` | `0` (none) | The server container's CPU ceiling. |
| `LEAN_SERVER_CGROUP_PARENT` | empty | The control group the server is started in. |
| `LEAN_SERVER_ROOT_READ_ONLY` | `false` | Whether the server's root filesystem is read-only. |
| `LEAN_SERVER_SUBNET` | `172.29.53.0/24` | The project's network. |
| `LEAN_SERVER_AGENT_IMAGE` | `lean-pool` | The usage agent's image. |
| `LEAN_SERVER_AGENT_PORT` | `18200` | The host port of the usage agent (or of its side of the TLS front). |
| `LEANPOOL_AGENT_LEAN_CPU_STAT_PATH` | empty | The agent's own setting: the server's `cpu.stat`, under `/host-cgroup`. |
| `LEANPOOL_AGENT_CPU_RESERVE` | empty | The agent's own setting. |
| `LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK` | empty | The agent's own setting: the most checks while other work is on the box. |
| `LEANPOOL_AGENT_VERSION` | empty | The agent's own setting: the installer's version, which ends every reply. |
| `LEAN_SERVER_FRONT_IMAGE` | `haproxy:3.0` | The TLS front's image. |
| `LEAN_SERVER_FRONT_ADDRESS` | `172.29.53.250` | The TLS front's fixed address on the project's network. |
| `LEANPOOL_HAPROXY_IMAGE` | `haproxy:3.0` | The pool's proxy. |
| `LEANPOOL_HAPROXY_PUBLIC_PORT` | `18100` | The renderer's own setting: the one port clients know. |
| `LEANPOOL_IMAGE` | `lean-pool` | The cache's image. |
| `LEANPOOL_CACHE_PIN` | required | The cache's own setting: names the servers' Lean and Mathlib. |
| `LEANPOOL_CACHE_MAX_BYTES` | `4294967296` | The cache's own setting. |
| `LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS` | `1800` | The cache's own setting. |
| `LEANPOOL_CACHE_MAX_REQUEST_BYTES` | `16777216` | The cache's own setting. |
| `LEANPOOL_SUBNET` | `172.29.55.0/24` | The pool's network. |
| `LEANPOOL_DNS` | required by `compose.dns.yaml` | The proxy's DNS server. |

### What the installer's programs also keep

| Variable | File | What it records |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | both | The project: `lean-server`, `lean-server-NAME` or `lean-pool-NAME`. |
| `COMPOSE_FILE` | both | The compose files the box runs, in order. |
| `LEAN_SERVER_CPU_PRIORITY` | `kimina.env` | Whether the server runs at the box's lowest CPU priority. |
| `LEAN_SERVER_READ_ONLY` | `kimina.env` | `off` keeps the root filesystem writable whatever the check says. |
| `LEAN_SERVER_COMPILED_SEARCH` | `kimina.env` | `off` keeps the compiled search step off. |
| `LEAN_SERVER_NAME` | `kimina.env` | The box's name in its pool, and in its certificate. |
| `LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS` | both | The renderer's own setting: the slowest check, for the front and the proxy. |
| `LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS` | both | The renderer's own setting: the servers' `LEAN_SERVER_MAX_WAIT`. |
| `LEANPOOL_IMAGE_GID` | `pool.env` | The group the pool's image runs as, which may read its key. |
| `LEANPOOL_FRONT_NAMES` | `pool.env` | The names in the front door's certificate. |
| `LEANPOOL_JOIN_PUBLIC_HOST` | `pool.env` | The host name a new box is told to fetch its join script from. |
| `LEANPOOL_JOIN_PUBLIC_PORT` | `pool.env` | The port the join service is published on while a window is open. |
| `LEANPOOL_HAPROXY_INSTALLER_VERSION` | `pool.env` | The renderer's own setting: the installer's version, which the pool's `GET /health` carries. |
