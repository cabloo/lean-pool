# Deploying a pool

The [installers](../deploy/install/README.md) set up a whole pool on Linux machines with
Docker: a Lean server on every box, and on one of them the pool's front door, HAProxy and the
cache. The pool is [TLS](tls.md) throughout. Every box runs the compose files of
[`deploy/`](../deploy/README.md), exactly as they are there. To see a pool before you build
one, run the [demo](../examples/demo): it needs no Lean.

## The layout

```
                  the pool's host                                a box that joined (one of many)
       +--------------------------------------+            +------------------------------------------+
clients| HAProxy :18100 (HTTPS)               |   mutual   | TLS front :18001 --> Kimina          8000 |
  ---> |   cache (inside)                     | ---TLS---> |           :18201 --> usage agent    18200 |
       | its own Lean server, behind a front  |            +------------------------------------------+
       +--------------------------------------+
```

| Port | Where | What |
|---|---|---|
| 18100 | the pool's host | The one address clients use, over HTTPS. |
| 18110 | the pool's host, while a join window is open | The [join service](joining.md). |
| 18120 | the pool's host, on the docker0 bridge's address, where the pool opens it | The [screened door](#a-screened-door): checks alone, through the cache's screen, for machines you do not own. |
| the instance's port (18001 in the example site) | every box | The box's TLS front, to its Lean server. It lets in the pool proxy's certificate and nothing else. |
| that port + 200 | every box | The same front, to the box's usage agent. |
| 18101, 18102, 18103 | inside the proxy's network namespace | The proxy's door to the Lean servers, the cache, HAProxy's statistics. Loopback only; never published. |

The installers' firewall lets the LAN subnet and the Docker bridges reach the published ports
(the screened door: what arrives from a Docker bridge's interface alone), and nothing else. Nothing
in the pool needs a GPU.

## What a box needs

* **Linux, Docker and root.** The installers run with `sudo`. A box with Docker but without its
  compose plugin is given one: from the package manager, else Docker Compose's release binary,
  checked against its pinned SHA-256. Docker from the snap package is refused: it cannot mount
  `/tmp` or `/srv` into a container.
* **A Docker that can start a container with an init process**, which the Lean server's compose
  file asks for ([below](#the-containers-processes)). Docker's own packages bring its
  `docker-init`; a distribution's Docker may want its `tini` package beside it. The installer
  asks Docker with a container that does nothing before it changes a server.
* **iptables and systemd** for the firewall: a unit re-applies each server's rules at boot. A
  host without them (Docker inside WSL without systemd, Docker Desktop) is told so in the
  pre-flight. `--no-firewall` installs no rule: the ports are then reachable from every network
  the host is on, and on a box of a pool only the TLS front's certificate check protects them.
* **Free disk where Docker keeps its images:** what the instance file's `INSTANCE_MIN_FREE_GB`
  says, for a build. A box that joins and loads the pool's image needs room for that file and
  the image instead.
* **On the pool's host, a clone of this repository, a site directory** ([below](#a-site-directory))
  **and `python3`.** The installers run from the clone and build this project's image from it;
  `lean-pool-ctl` runs its join helper with the host's Python.
* **On a box that joins, `curl`.** The command it runs carries the installers and the site.
* **The same Lean and Mathlib everywhere.** Every box runs the image its instance file names, or
  builds it from the recipe the file names; a box that joins loads the pool's own image when the
  pool ships one for its machine type.

**Anyone who holds the pool's API key can run code on every Lean server box**, because checking
Lean code runs it. Read [SECURITY.md](../SECURITY.md) before the pool is reachable by anyone
but you.

## A site directory

What differs between sites comes from a site directory. Copy
[`deploy/install/site.example`](../deploy/install/site.example) and change it:

```
my-site/
  site.env              SITE_PREFIX, SITE_LABEL, SITE_DOOR_KEY_MARKER, SITE_LAN_DOMAIN
  instances/
    NAME.env            a Lean server instance: install.sh --instance NAME (1-16 lower-case
                        letters and digits; not `default` or `main`)
    default.env         instance 1, the instance with no name (optional)
  Dockerfile ...        the image recipes the instance files name, if any
```

The example's two files document every key. The installers read them and never run them, and
`install.sh` checks every value for its shape before it installs anything. A pool is named after
an instance: the pool's host runs that instance's Lean server, and so does every box that joins,
from the same file.

An instance's image comes from one of two places:

* **Pulled.** `INSTANCE_IMAGE=projectnumina/kimina-lean-server:2.0.0` names an image to pull; a
  reference with a digest pins it exactly. The installer pulls it and tags it as
  `<SITE_PREFIX>/kimina:<INSTANCE_IMAGE_TAG>`, and pulls it again only when the reference or
  the tag changed or the image is gone.
* **Built.** `INSTANCE_DOCKERFILE` and `INSTANCE_BUILD_FILES` name a recipe in the site
  directory, and the installer builds it. A named instance is built again only when one of those
  files, the tag or `--build-jobs` changed, or the image under the tag is gone.

An instance file names one or the other, never both. Its workers, worker memory, CPUs and subnet
only fill in what the instance's settings do not have yet. After the first install, change the
workers and CPUs with `kimina-ctl set-repls` and `set-cpus` ([below](#on-a-lean-server-box)).
Worker memory is `LEAN_SERVER_MAX_REPL_MEM` in the instance's `kimina.env`: change it there and
run the instance's install line again. The subnet is `LEAN_SERVER_SUBNET` there, but its network
is made with the instance: take the instance down first (`kimina-ctl [@NAME] down`).

The rest are the installers' own defaults, each an option where it may differ: the pool's ports
(18100, and 18110 while a join window is open), the time a check may take (300 s), and the
Lean server's settings (`deploy/README.md` lists [what they write](../deploy/README.md#settings)).
The pool's own Docker network is always 172.29.55.0/24: a host that already routes it cannot
run the pool.

## The pool's host

In the clone, as root:

```sh
cd deploy/install
sudo bash install.sh --site ~/my-site --instance upstream
sudo bash install.sh --site ~/my-site --proxy upstream --admit-cases ../../examples/admission-cases
```

The first command installs the Lean server: its image, its settings with a new API key, its
container, its firewall rules and `kimina-ctl`. It ends by checking one valid and one invalid
proof through the server. `bash install.sh --help` lists every option.

The second installs the pool in front of it. It makes the pool's certificate authority and the
certificates of the front door and the proxy, takes the Lean server's key as the pool's key,
starts a server list with this box's server in it, puts this box's server behind a TLS front,
and starts HAProxy and the cache. It ends by checking a valid and an invalid proof through the
pool, over HTTPS. The front door's certificate names the host (`HOST` and `HOST.<SITE_LAN_DOMAIN>`);
`--front-name NAME` adds a name clients use, once per name. A re-run without the option keeps
the names; one with it replaces them, so pass every extra name each time you pass any.

A server joins the pool only after it passes the admission cases alone
([admission](admission.md)). [`examples/admission-cases`](../examples/admission-cases) is a
start that any Lean with Mathlib passes. Add files that depend on *your* versions: a proof that
only checks on the Mathlib you pinned is what catches a box that has another. Every install of
the pool needs `--admit-cases DIR`, and it replaces the pool's copy of the cases.

Every step of both is safe to run again. A re-run keeps the API key, the authority, every
certificate, the server list, what the instance's `kimina.env` holds (workers, CPUs, CPU
priority, a TLS front), and the pool's [screened door](#a-screened-door) as it was, open or
closed, with its key and its copy's directory, and the directory of the pool's key's copy
([below](#a-copy-of-the-pools-key)). Other options are not remembered: run each line
again with the options it had. Left out, the pool's `--port` and `--lean-timeout` and the instance's `--port` go back to
their defaults, a run without `--operator-key` removes the operator's SSH door (a key that runs
`kimina-ctl` alone; `install.sh --help`), and one without `--no-agent` or `--no-firewall` adds
the usage agent or the firewall back.

What they leave on the host:

| Where | What |
|---|---|
| `/srv/kimina-NAME` | The instance: `kimina.env` (its settings and its key, readable by root only; `COMPOSE_FILE` names the files it runs), the compose files of `deploy/lean-server`, and its build files. Instance 1 is `/srv/kimina`. |
| `/srv/lean-pool-NAME` | The pool: `pool.env`, the server list, the certificates, the admission cases, the join bundle, and with the screened door its key (`screened-api-key`). |
| the screened key's copy | With `--screened-key-copy DIR`: `api-key` and `ca.crt` in DIR, owned by DIR's owner. An uninstall, even with `--purge`, leaves them. |
| the pool's key's copy | With `--pool-key-copy DIR`: the same two files, holding the pool's own key, in DIR. An uninstall, even with `--purge`, leaves them. |
| `/usr/local/bin/kimina-ctl` | Every operation on the Lean servers of this host. |
| `/usr/local/sbin/lean-pool-ctl` | Every operation on a pool. |

Each instance and each pool is one compose project: `lean-server-NAME` (instance 1:
`lean-server`) and `lean-pool-NAME`. Its env file names the project and the files it runs
([the settings](../deploy/README.md#settings)), so Compose's own names hold:
`lean-server-upstream-kimina-1`, `lean-pool-upstream-haproxy-1`.

## Joining a box

On the pool's host:

```sh
sudo lean-pool-ctl upstream join-open
```

That opens a join window for ONE box and prints the command to run on it, then waits for the box
(120 minutes; `--minutes N` sets another limit). On the new box, as a user who may `sudo`:

```sh
curl -fsSk --pinnedpubkey 'sha256//...' https://pool-host.lan:18110/j/<token> | sudo bash
```

Options go after `sudo bash -s --`: `--name NAME` (the box's name in the pool; its host name by
default), `--workers N`, `--cpus N`, `--no-firewall`. The box gets the image (from the pool, or
it builds it), makes its own key, has the pool sign its certificate, installs its Lean server
behind its TLS front with the pool's key, and tells the pool it is ready. The pool tests it alone
and adds it only if it passes. A failure at any step leaves the pool as it was.
[Joining over the network](joining.md) has the steps and the rules the join service keeps.

At its end the box prints the one command that removes it again.

## Check the pool

```sh
sudo lean-pool-ctl upstream status         # containers, the cache's counters, every server, certificates
sudo lean-pool-ctl upstream check          # one valid and one invalid proof through the pool
sudo lean-pool-ctl upstream ca > ca.crt    # the pool authority's certificate (public)
sudo kimina-ctl @upstream show-key         # the pool's API key
curl --cacert ca.crt https://pool-host.lan:18100/health
# {"status":"ok","workers":12,"queued":0,"servers":2}
```

A client needs the key and `ca.crt`, nothing else. [Using a pool](clients.md) is the clients'
side.

## Operating a pool

### Servers in the pool

```sh
sudo lean-pool-ctl upstream servers                     # the server list
sudo lean-pool-ctl upstream pause box-a                 # listed, and sent nothing
sudo lean-pool-ctl upstream resume box-a                # the admission test again, then checks again
sudo lean-pool-ctl upstream set-workers box-a 4         # after the box's own count was changed
sudo lean-pool-ctl upstream remove-backend box-a
sudo lean-pool-ctl upstream add-backend box-a box-a.lan:18001 4 18201
```

A box comes into the pool by [joining](#joining-a-box). `add-backend` puts back a server that
holds a certificate of this pool's authority for its name, such as one that was removed; it runs
the admission test first. Its last argument is the port of the box's usage agent: the
instance's port + 200, where the box's front serves it. Left out, it is 18200, which no box
behind a front serves, and the pool then has no word from the agent: no open slots, no drain
when the box runs low on memory. Every change validates the whole list, has HAProxy validate the new
configuration with the certificates in place, and only then reloads: a refused change leaves
the pool as it was. [A paused server](haproxy.md#a-paused-server) is sent no check and no
health check until it is resumed, and the pause survives a reload and a restart.

A reload is not a restart: HAProxy starts new workers on the new configuration while the old
ones finish the checks they hold, so a reload drops none. `lean-pool-ctl upstream reload`
reloads by hand, `restart` restarts the proxy and the cache, and `cache-stop` and `cache-start`
stop and start the cache alone: checks at the front door keep flowing while it is stopped,
uncached, and the screened door answers 503.

### On a Lean server box

```sh
sudo kimina-ctl @upstream status           # the server, its workers, its agent, its settings
sudo kimina-ctl @upstream check
sudo kimina-ctl @upstream set-repls 6      # the worker count; then set-workers on the pool's host
sudo kimina-ctl @upstream set-mem 28g      # the container's memory ceiling
sudo kimina-ctl @upstream logs 200
```

`kimina-ctl` without `@NAME` acts on instance 1. Its header lists every command, among them
[compiled search](compiled-search.md) on and off, [the read-only root](#a-read-only-root),
[the worker sandbox](#each-worker-in-a-sandbox), and `set-beside` for
[a box that also does other work](#a-box-that-also-does-other-work).

### The container's processes

`kimina-ctl status` prints how many processes the Lean server's container holds (`pids`), how
many of them have exited and been collected by nobody (`zombies`), and whether it has an init
process. Kimina closes a worker by killing its process group and waiting for its parent alone;
the orphaned REPL goes to the container's first process, so without an init each closed worker
leaves a zombie that holds a process slot. The container may hold 4096, and
at that limit **every check is answered HTTP 500 while the container still says it is healthy**.
The compose file gives the container Docker's init (`init: true`), which collects them:
`zombies` stays 0. A container made from an older compose file says `init process: NO` until
the installer makes it again.

### A read-only root

A checked file is a Lean file, and Lean runs programs while it reads one, so every check runs a
program inside the Lean server's container. With the container's root filesystem read-only, a
check can change neither Lean, Mathlib nor the REPL for the checks after it, nor fill the
host's disk. `/tmp` is then a 512 MB filesystem in memory, counted against the container's
memory ceiling, from which nothing can be run.

The installer turns it on only for a server it has seen keep working. Before it makes the
container again, it asks Docker what the running server has written
([`read-only-check.sh`](../deploy/lean-server/read-only-check.sh) says what is allowed); after,
it sends the read-only server a real check. Anything else written, or a check that fails, and
the server stays writable as before. The install still ends verified, and its last lines say
which. `kimina-ctl [@NAME] status` has a `read-only root:` line; `read-only off` keeps the
server writable through every later install, and `read-only on` turns it back on where a
reading passed.

It does not cover a program a check leaves running. With [the worker
sandbox](#each-worker-in-a-sandbox), that program can reach the next checks of its own worker
only; without it, those of the whole server, until the container is made again.

### Each worker in a sandbox

Kimina runs its workers in one container, as one user. The installer puts each worker in a
[sandbox of its own](worker-sandbox.md), where this box's kernel and Docker allow it: a check
then cannot reach into another worker's pipes, memory or environment, nor Kimina's, nor read
the server's key, and from Landlock 6 it cannot end another worker. The installer asks a
container of the box's own image first, and sends the server a real check after. Where that
check fails, it takes away one protection at a time:

1. The sandbox. If the check then passes, the workers start without it until the installer's
   next run.
2. If the check fails without the sandbox too, the sandbox was not the cause. Where the root is
   read-only, the sandbox comes back and the [read-only root](#a-read-only-root) is taken away
   instead; if the check then passes, the server runs sandboxed and writable.
3. If it fails that way too, the server is made with neither and checked once more: it runs
   with neither if that passes, and the install fails if not.

Where the root is not read-only, a server that fails with the sandbox and without it is left
without it, and the install fails. The last lines of the install say which protections the
server runs with; the next installer run tries both again. `kimina-ctl [@NAME] status` has a
`worker sandbox:` line, with how many of the running workers carry the sandbox's mark.
`sandbox off` keeps it off through every later install. `sandbox on` turns it back on where
this box's last probe passed; elsewhere it says why, exits 1, and clears the `off` setting, so
that the installer's next run asks the image again.

### A screened door

For machines whose owner you do not know, such as rented ones, the pool can open a second way in
that takes checks alone, through the cache's [screen](cache.md#the-screened-key), with a key of its
own ([the screened door](cache.md#the-screened-door)):

```sh
sudo bash install.sh --site ~/my-site --proxy upstream --admit-cases ../../examples/admission-cases \
  --screened-door --screened-key-copy /srv/hand-out/lean-pool
sudo lean-pool-ctl upstream screened-key new     # a new key; the old one is refused from then on
sudo lean-pool-ctl upstream screened-key copy    # the key and the authority written to the copy again
```

It is published on the docker0 bridge's address alone (port 18120) and firewalled to what arrives
from a docker bridge's interface: only the containers and the programs of the pool's host reach it,
and something there passes it on (an SSH tunnel to the machine, say). `--screened-key-copy DIR`
names the directory that program reads the key and the pool authority's certificate from; the
files are owned by that directory's owner. DIR's parent must exist, belong to that program's user,
and be reached through no link. The install's summary has a `screened` line, with where the door is
and where its key went; a copy that could not be written is a warning there, not a failed install.
`lean-pool-ctl upstream status` says the same, and never prints the key. The door stays open
through every re-run until `--no-screened-door`. Closing it does not retire the key: `screened-key
new` does, open or closed. The door takes as many checks as the pool's workers, counted at the
install (a server that joins later counts at the next install run).

### A copy of the pool's key

A program on the pool's host that starts work on other boxes, and hands those it trusts the
pool's own key, can be given a copy that follows the key:

```sh
sudo bash install.sh --site ~/my-site --proxy upstream --admit-cases ../../examples/admission-cases \
  --pool-key-copy /srv/hand-out/lean-pool-home
sudo lean-pool-ctl upstream pool-key copy        # the key and the authority written to the copy again
```

Every install run writes the pool's key (`api-key`, 0600) and the pool authority's certificate
(`ca.crt`, 0644) into DIR, as the [screened key's copy](cache.md#the-screened-door) is written:
owned by DIR's owner, its path walked with no link followed, each file made beside the old one
and moved over it. DIR is kept once given. It may not be the screened key's copy's directory,
lie inside it or hold it, since both copies hold files of the same names: the installer refuses
that before it changes anything. The summary has a `pool key` line; a copy that could not be
written is a warning there, not a failed install. Whoever can read DIR holds the pool's key,
which runs code on every Lean server of the pool ([SECURITY.md](../SECURITY.md)).

### Watching it

| What | How |
|---|---|
| Is the pool up, how big is it, is anything waiting? | `curl --cacert ca.crt https://pool-host.lan:18100/health` |
| What is the cache doing? | `sudo lean-pool-ctl upstream status`, or `/status` with the key ([the counters](cache.md)) |
| Which server answered which check, and how long each part took? | `sudo lean-pool-ctl upstream logs`: one line per request, with HAProxy's timers. |
| Each server's state, weight and queue | `status` again, from HAProxy's [statistics](haproxy.md#statistics). |
| All of it over time, in Prometheus | Scrape `/metrics`, `/metrics/cache` and `/metrics/pool` on the public port; no key is needed ([Metrics for Prometheus](haproxy.md#metrics-for-prometheus)). |
| How long do the certificates last? | `status` prints the days each one has left. |
| Which installer did each box last run? | `status` ends each server line with `version` and the first 12 digits its usage agent states, and `/health` ends with the pool host's own ([the installer's version](usage-agent.md#the-installers-version)). |

A server that drops out shows as `servers` and `workers` going down in `/health`. The checks it
held are sent to another server, and it is used again as soon as its health check passes.

### A box that also does other work

A weight alone cannot keep checks off a box that is busy with something else: under a client
that keeps the pool full, every worker that comes free is handed the next waiting check. An
agent that is given the Lean server's worker count and its control group's `cpu.stat` also
states how many checks fit beside the box's other work, and the box is sent no more.
`kimina-ctl @NAME set-beside N` sets that number, which must be measured for the box:
[open slots](usage-agent.md#open-slots-a-box-that-also-does-other-work) has the rule and how to
measure it. The installer starts every Lean server at the box's lowest CPU priority, so the box's
other work gets a contended CPU first (`--cpu-priority normal` for a box whose first job is Lean).

An installer's own checks are the exception. At the lowest weight, a Lean server just made on a
busy box would get almost no CPU, and its first check (a new worker importing Mathlib) would run
past its time limit. So from just before an installer makes a Lean server until its checks are
done (on a joining box, until the pool's admission test is done), every Lean server of the box
runs at the box's ordinary CPU weight, set with `systemctl set-property --runtime`. The lowest
comes back after, whether the installer succeeds, fails or is stopped with Ctrl-C. For as long as
that takes (an admission test can take up to an hour), the box's other work shares the CPU with
the Lean servers, and `kimina-ctl status` says so. An installer killed outright (`kill -9`, a
power cut) leaves the ordinary weight until the next install of a Lean server at the lowest
priority on the box, which puts the lowest back. A reboot also ends it.

### Sizing

* **A Lean server box** is sized by its workers. Each is a Lean process that holds Mathlib in
  memory and grows with the proofs it checks; Kimina limits each one's address space
  (`INSTANCE_WORKER_MEMORY`). Start with fewer workers than cores and watch the container's
  memory under load (`kimina-ctl status`); then about `0.8 × the memory ceiling ÷ the memory of
  a busy worker`, and no more than the threads you give it. The usage agent drains a box whose
  available memory falls below its floor (2 GiB by default). `kimina-ctl set-mem` changes the
  container's ceiling alone, and `status` says when the ceiling is above what the box has.
* **The pool's host** needs little for the pool itself. The cache's store is one SQLite file
  capped at 4 GiB by default; in the pool this project was written for, a stored result takes
  about 1.1 kB. HAProxy's buffers are sized for 256 KiB requests and can take up to 768 MiB with
  every connection in use ([buffers and memory](haproxy.md#buffers-and-memory)).
* **The pool's host is a single point of failure.** If it is down, the pool is down.

### Changing the Lean or Mathlib version

The cache's pin is the instance's image tag, so a new tag is a new pin, and results stored under
the old one stop matching and are evicted as the store fills. Do not run a pool whose servers
disagree: the pool cannot tell their answers apart, and the cache would store an old version's
answer under the new pin.

1. Take every box that joined out of the pool: `remove-backend`, then the command it printed at
   its join. A refresh keeps a box's image, so a box stays on the version it joined with.
2. Change the instance file's image and tag, and run the pool's host's two lines again. The pool
   now runs its own server alone, under the new pin.
3. Let each box join again: it gets the new image.

### Upgrading lean-pool

On the pool's host, `git pull` in the clone, then the same two lines as at the start, with the
options they had. Then open
a join window and run, on each box that joined, the window's command with `sudo bash -s --
--refresh`: the box takes the window's installers and keeps its image, its key, its certificate
and its place in the pool. A refresh asks the pool for nothing, so the window stays open and the
same command refreshes the next box. Once every box is refreshed, `lean-pool-ctl NAME status` shows one
version on every server line. [CHANGELOG.md](../CHANGELOG.md) says when a version changes an
interface.

### A box set up by an older installer

Installers older than the compose files of `deploy/` ran each instance as up to three compose
projects of their own files, named after the site's prefix, with their settings under other
names. The first run of today's installer on such a box moves the whole box before it changes
anything else:

* each instance and each pool becomes one project of `deploy/`'s files, under Compose's names;
* each setting is kept under its new name, and a setting the older files left to their own
  default is written out with that default, so nothing changes value;
* the older env file is kept beside the new one as `kimina.env.before-compose-names` (or
  `pool.env.before-compose-names`), readable by root only, and so are the older compose files and
  the front's configuration;
* the cache's results are copied into the new volume, and the older volume is removed once the
  pool has passed its check. A pool that was not running is moved without being started or
  checked: its older volume is kept, and the installer prints the command that removes it.

Each Lean server restarts once. The pool is down from the moment its older project stops until
the new one is up. A pool set up before TLS has no certificates to run `deploy/pool` with: the
move refuses it before it changes anything.

An instance or pool that fails its check is put back as it was and the run stops there, saying
so; what moved before it stays moved, and the next run moves the rest. The run installs the new
`kimina-ctl` and `lean-pool-ctl` before it moves anything, and they refuse an instance or pool
still under the older names, saying which line moves it: until a run moves it, use `docker`
itself to look at it. A box that joined moves on its refresh.

To go back to an older installer, take each new project down, `sudo kimina-ctl [@NAME] down`
for each instance and `sudo bash install.sh --site DIR --proxy NAME --uninstall` for each pool
(without `--purge`), and move every `*.before-compose-names` file back over the file of its name.
The older installer's next run brings the older projects back. The cache starts empty. A box
that joined has no older installer left after its refresh: put its files back the same way, then
refresh it from a join window of a pool's host that runs the older installer.

### Removing

```sh
sudo bash install.sh --site ~/my-site --proxy upstream --uninstall              # the pool
sudo bash install.sh --site ~/my-site --instance upstream --uninstall           # a Lean server
```

Each stops what it installed and turns its firewall units off. Where `ufw` is active, the
instance's own allow rule goes too, but those for the pool's ports and for a usage agent's port
stay (`sudo ufw status numbered`), and so do `lean-pool-ctl` and the pool firewall's script,
which other pools on the host share. `--purge` also removes the
directory: for an instance its images too, and for a pool the cache's results and the pool's
certificate authority, which voids every certificate it signed. Without `--purge` both are kept
and a later install takes them up again. A box that joined is removed with the command it printed
at its join; take it out of the server list with `remove-backend` on the pool's host.

## Troubleshooting

| What you see | Likely cause |
|---|---|
| A check fails with HTTP 500 or a JSON decode error | A worker died importing Mathlib, usually at its address-space limit: raise `LEAN_SERVER_MAX_REPL_MEM` ([above](#a-site-directory)). |
| Every check is answered HTTP 500 while the server says it is healthy | The container is at its process limit: `status` shows `pids` near 4096, most of them zombies, and `init process: NO`. Run the installer again ([the container's processes](#the-containers-processes)). |
| HTTP 429 from a Lean server | No worker came free within `LEAN_SERVER_MAX_WAIT`: more checks at once than it has workers. A client of the pool [sizes itself](clients.md#the-pools-size). |
| One server takes many times longer than the others, or drops in and out | Its box is busy with other work and is sent more than fits beside it: [a box that also does other work](#a-box-that-also-does-other-work). |
| The pool answers 503 at once, with servers up | Every server is drained: no box has room beside its other work, or its memory is short ([the pool's size](clients.md#the-pools-size)). It ends by itself. |
| A server answers from the LAN but not from a container on its host | The firewall lets in the LAN subnet and Docker's bridge networks (172.16.0.0/12) only. |
| The screened door answers 503 `the screened door is full` | Its senders have as many checks at the servers and waiting as the pool has workers ([the screened key](cache.md#the-screened-key)); the client retries. |

## Without the installers

The compose files of [`deploy/`](../deploy/README.md) run wherever Docker does. Each file's
header says what it expects: the settings, the rendered configuration, the certificates. The
[configuration reference](configuration.md) has every command and setting, and
[`deploy/README.md`](../deploy/README.md#settings) every variable the files read. This
documentation does not walk through it: the installers are the tested way.

## Getting the commands

| Where | How |
|---|---|
| In a clone of this repository | `uv run leanpool-pki ...` (and likewise for the others), from any directory of the clone. |
| Installed for your user | `uv tool install git+https://github.com/cabloo/lean-pool` or `pipx install git+https://github.com/cabloo/lean-pool` |
| In a Python environment | `pip install git+https://github.com/cabloo/lean-pool` (Python 3.11 or later) |
| From the image | `docker build -f deploy/Dockerfile -t lean-pool .` then `docker run --rm lean-pool leanpool-pki --help` |

A pool the installers set up needs none of these: they run the commands from the pool's own
image. The pages of this documentation write the commands bare (`leanpool-pki ...`); in a clone,
put `uv run` in front. The image runs the cache by default and holds all six commands.
`leanpool-agent` and `leanpool-haproxy-config` need nothing but the Python standard library.
