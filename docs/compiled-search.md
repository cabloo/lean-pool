# Compiled search for linarith

An optional step on each Lean server box. It does not involve the pool: it makes the Lean server
itself faster on the proofs that take it longest, and changes no verdict.

Many proofs that look short take seconds to check. In the deployment this project was written
for, most of that time was one step of `nlinarith` and `linarith`: the search for the numbers
that close the goal, a simplex method over exact fractions. Mathlib ships that code for Lean's
interpreter, not as machine code. The files in
[`deploy/lean-server/compiled-search/`](../deploy/lean-server/compiled-search) compile that one
step on the box, from the C files the box's own Kimina image already holds, and have every worker
load it. Measured there, the step ran 8 to 10 times faster, 71 checks a run had sent took 217 s
of Lean time instead of 608 s, and no verdict changed.

## Turning it on

On the Lean server box, in the clone, once the box runs (step 2 of
[deploying a pool](deployment.md#2-on-each-lean-server-box-kimina-and-the-usage-agent)):

```sh
cd deploy/lean-server
compiled-search/make.sh
docker compose -f compose.yaml -f compose.compiled-search.yaml up -d
```

`make.sh` takes a few minutes and ends with one line:

```
COMPILED SEARCH STEP: on - linarith's search took 9.3 s as the image runs it and 0.9 s compiled (10.5 times less); the same answers; a program a tactic starts does not inherit the library
```

or `COMPILED SEARCH STEP: OFF - ` and the reason. Either way the box can then be started with
[`compose.compiled-search.yaml`](../deploy/lean-server/compose.compiled-search.yaml): when there
is no library, the workers start as they did before. (If `make.sh` could not run the image at
all, its line says so and that the box must not be started with the file.) On a box started with
other overlays as well (the usage agent, TLS), add this one last. The second command restarts the
Lean server, so a pool's failover sends the checks it held elsewhere
([failover](haproxy.md#failover-among-the-lean-servers)).

`make.sh` uses the image given with `--image` (or as its argument), else the one named by
`LEAN_SERVER_IMAGE` in `.env`, else compose.yaml's default. Run it again whenever the box's image
changes. Until it has, the launcher finds that the library was made from other files and starts
the plain REPL. A run for the same image and the same `compiled-search/` files makes nothing
again, and says `COMPILED SEARCH STEP: on - already made from this image and checked`. `--box`
names another directory to fill than `deploy/lean-server`, and `--name` the build's container:
the [installer](../deploy/install/README.md) uses both.

To turn it off, start the box without the second file: `docker compose -f compose.yaml up -d`.

## What `make.sh` does

1. It starts one container of the box's Kimina image, with no network, no capabilities and no
   way to gain privileges, and the files of `compiled-search/` mounted read-only. The image must
   already be on the box: nothing is downloaded, and the image is not changed.
2. In it, `build.sh` compiles seven modules (listed in
   [`modules`](../deploy/lean-server/compiled-search/modules)) with the Lean toolchain's own
   compiler into one library of under a megabyte, from the C files Mathlib's build cache left
   beside its `.olean` files.
3. Still in it, `check.py` starts a worker the way Kimina does, once as the image runs it and
   once with the library, and passes only when:
   * the library is in the second worker's memory and not in the first's;
   * three proofs get the same answer from both: one `nlinarith` closes, one it cannot close (the
     same error, word for word), and one whose tactic starts another program (`bv_decide`);
   * a program started from inside Lean does not inherit the library;
   * the search, timed by Lean's own profiler, is at least 3 times faster.
4. Only on a pass is the library copied out into `deploy/lean-server/native/`, which the second
   compose file mounts read-only. `native/state` keeps the line, and `native.log` the whole
   output. `native.built` records what the library was made from: one hash over the files of
   `compiled-search/`, and the image's ID.

## What keeps a worker from running the wrong thing

| What could go wrong | What stops it |
|---|---|
| The image is updated and the library made from the old one is still in `native/` | Each time a worker starts, the launcher (`repl-native`) takes one hash over the source files in the container again. If it is not the hash the library was built from, the worker starts the image's REPL alone. |
| The library is missing something and fails in the middle of a proof | It is linked so that everything it refers to is resolved the moment it loads. A gap shows when the worker starts, and in the check before that. |
| The library makes Lean skip a module's own start-up code | The build stops unless the library announces exactly the seven modules it compiled and no other. |
| A tactic starts another program, and that program is handed the library too | `bv_decide` runs a separate solver, which would refuse to start with a library only Lean can load. The library takes itself out of what child programs inherit as soon as it loads, and the check proves it. |
| The build or the check fails | Nothing is installed but the launcher, which then starts the plain REPL. |
| `make.sh` never ran | The second compose file will not start the box: its directory must exist. |

## What it cannot change, and what it does

**It cannot let a wrong proof pass.** Lean does not trust the search. The search proposes
numbers, `linarith` builds a proof from them, and Lean's kernel checks that proof as it always
does. A fault in the compiled step could make a correct proof fail; it could not make a false one
verify. The check compares three proofs; it does not prove the two programs the same.

**It does change which checks run out of time.** A proof that needed more than the client's time
limit may now finish. So a run after the switch solves slightly more at the same limit than a run
before it, for this reason alone: compare solve rates across the switch only with that said.

The cache needs no change. A verdict is the same with the step on or off, so the
[cache's pin](cache.md) still names what it did, and a stored verdict is still the one Lean gives.
A timeout was never stored.

## Seeing that it is on

`cat native/state` says what the last `make.sh` found. Whether the running workers have the
library loaded, from inside the Lean server's container:

```sh
docker compose exec kimina sh -c 'grep -l linarith-search /proc/[0-9]*/maps | wc -l'
```

prints the number of workers started so far that have it.

## Measured

In the deployment this project was written for, on Kimina built from source with Lean 4.27,
each figure observed once:

* **Where the time was.** One prover-training run sent 30,210 different checks, which took 28.4
  hours of Lean time; 94% of it was in checks that call `nlinarith`. In 46 slow checks traced with
  Lean's profiler, 78% of the Lean time was this one search.
* **Each box's own check**, on 12 October 2026, as `make.sh` printed it on four machines:

  | Machine | Search as the image runs it | Compiled | |
  |---|---|---|---|
  | 12 workers | 9.3 s | 0.9 s | 10.5 times less |
  | 6 workers | 10.5 s | 1.1 s | 9.8 times less |
  | 9 workers | 5.9 s | 0.7 s | 8.6 times less |
  | 3 workers | 4.7 s | 0.6 s | 8.0 times less |

* **The same checks both ways**, the same day, on the 12-worker server alone: 86 checks a run
  had sent, sent again past the cache with the step on and then off. 71 finished both ways, and
  no verdict differed, between the two or from the one stored. Their Lean time was 608 s off
  and 217 s on. 10 checks that ran out of the 60-second limit with the step off finished with it
  on, 5 of them verified; none went the other way.
* **Turning it on during a run.** The three other machines were switched over while a client
  sent 350 to 650 checks a minute. Every answer was a success (2xx): the restarts cut about 24 checks in
  flight, and the pool sent each of them to another server.

## Limits

* It has been run with one Lean and Mathlib: Lean v4.27.0 and the Mathlib of that toolchain, on
  x86-64 Linux. A Mathlib that renamed or moved one of the seven modules makes `build.sh` stop
  and name it; a Lean that finds compiled code differently makes the check fail. Either way the
  box goes on as before, with the step off.
* The image must hold Mathlib's C files under `.lake/packages/*/.lake/build/ir/`. Mathlib's build
  cache puts them there with the `.olean` files, and the deployment's images had them; an image
  built another way may not, and `make.sh` then says so. Kimina's published image has not been
  tried.
* Only the search of `linarith` and `nlinarith` is compiled. Every other tactic runs as before.
* The image must set `LEAN_SERVER_REPL_PATH` to an absolute path and `LEAN_SERVER_PROJECT_DIR`, as
  Kimina's own image does.
* The clone must be on a file system that lets programs run (not mounted `noexec`): the
  container starts its workers from `native/`.
* **The measurements above came from an earlier form of the installer.** It ran the same
  `build.sh` and `check.py`, and a launcher with a fixed REPL path, in the same kind of container,
  and started the servers. The [installer](../deploy/install/README.md) now runs `make.sh` itself.
  Here `build.sh` and `check.py` were run as written against a real Lean outside Docker, and the
  tests hold `make.sh` to what it does with a stand-in for Docker. Docker validates the compose
  files in CI.
