# The worker sandbox

An optional step on each Lean server box: every worker in a sandbox of its own, so that the code a
check runs cannot reach the server's other checks or its key.

A check is a Lean file, and Lean runs programs while it reads one (`#eval` of an `IO` action,
`run_tac`, a command an `elab` defines). Kimina runs many workers in one container, as one user,
and gives each one check after another. Without the sandbox, a program a check leaves running
can open another worker's pipes through `/proc` and change the answers it gives, end other
workers, or read the server's API key from its environment.

## What it refuses

As a worker starts, it puts itself into a [Landlock](https://docs.kernel.org/userspace-api/landlock.html)
domain of its own: the kernel's own sandbox for unprivileged processes (Linux 5.13 and later).
Everything the worker starts inherits it. The kernel then refuses that worker:

| | from Landlock version |
|---|---|
| every process outside its own domain through `/proc` and `ptrace`: another worker's pipes, memory and environment, and Kimina's own | 1 |
| creating, changing or removing files anywhere but `/tmp`, and writing any device but `/dev/null`. Reading is not limited | 1 |
| TCP, both ways: a check cannot call the server's own API on its loopback | 4 (Linux 6.7) |
| signals and abstract Unix sockets to processes outside its domain: a check cannot end another worker | 6 (Linux 6.12) |

The worker's program also starts **without the server's key**: `LEAN_SERVER_API_KEY`, and any
variable whose name holds `KEY`, `TOKEN`, `SECRET` or `PASSWORD`, are taken out of its environment,
and `LEANPOOL_WORKER_SANDBOX=landlock-N` is put in. The sandbox stops it reading Kimina's.

It costs one start of the image's Python each time a worker starts (a worker answers up to
`LEAN_SERVER_MAX_REPL_USES` checks), and the kernel's check on each path a worker creates or
changes. Nothing in Kimina changes.

## What it does not hold

* **A check against its own worker.** A worker answers many checks in turn, so a check can still
  change the answers that same worker gives the checks after it, until the worker is recycled.
  Only a new process for each check closes that, at the price of a worker's start for every
  check (seconds, with Mathlib's import).
* **Shared ceilings.** A worker can still slow the others: the container's CPU, memory and
  process ceilings are shared, as before.
* **The kernel itself.** A container and Landlock are both parts of the host's kernel. A flaw in
  it is not covered; a second kernel in user space (gVisor) would be.

## Turning it on

A box the [installers](deployment.md) set up has it already, where it works: the installer asks a
container of the box's own image whether this kernel and this Docker can make the sandbox, adds
it only where they can, and then sends the server a real check. Where the check fails, the
sandbox is taken away again, the server is made again without it and checked again, and the
installer says why; where it fails without the sandbox too, the sandbox was not the cause and
comes back ([one protection at a time](deployment.md#each-worker-in-a-sandbox)). `sudo kimina-ctl [@NAME] sandbox off` turns it off and `on` turns it back on;
each makes the Lean server again and nothing else. `kimina-ctl status` says what the running
server's workers are started as, and how many of them carry the sandbox's mark:

```
--- worker sandbox: on (Landlock 6: /proc, files, TCP, signals); workers sandboxed: 12 of 12
```

On a box that runs the compose files of [`deploy/lean-server`](../deploy/lean-server) without the
installers, in the clone:

```sh
cd deploy/lean-server
worker-sandbox/make.sh
docker compose -f compose.yaml -f compose.worker-sandbox.yaml up -d
```

`make.sh` takes a few seconds and ends with one line, `WORKER SANDBOX: on - Landlock 6: /proc,
files, TCP, signals`, or `WORKER SANDBOX: OFF - ` and the reason. Add
[`compose.worker-sandbox.yaml`](../deploy/lean-server/compose.worker-sandbox.yaml) after every
other overlay, and only where it said `on`: **a worker that cannot be sandboxed does not start**,
so a server started with the file where the sandbox cannot be made answers nothing. Run `make.sh`
again whenever the image, the kernel or Docker changes.

## How it is put together

[`deploy/lean-server/worker-sandbox/`](../deploy/lean-server/worker-sandbox):

* `make.sh` runs one container of the box's image, made as the server is (no network, no
  capabilities, no new privileges, a read-only root, `/tmp` in memory). There it reads the path of
  the image's REPL and runs `sandbox.py --probe`, which makes the sandbox in that container and
  says which version it got. Where that passed, it puts the files below in the box's `sandbox/`
  directory, each replaced whole, and writes `sandbox/state`.
* `lean-worker` is what Kimina starts each worker as (the overlay sets `LEAN_SERVER_REPL_PATH` to
  it). It starts the program the worker would have been without the sandbox, inside it: the
  [compiled search step](compiled-search.md)'s launcher where that overlay is on, else the image's
  REPL.
* `sandbox.py` makes the domain (standard library only, on the image's own Python), takes the
  secrets out of the environment and starts the program. A worker that cannot be sandboxed is not
  started: it exits 126 and says why on its error output, which Kimina keeps.

The tests (`tests/test_worker_sandbox.py`) run on the machine's own kernel and show each wall
twice: the act succeeds without the sandbox, and is refused under it.
