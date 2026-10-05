# Contributing

Bug reports, questions and patches are welcome. For anything larger than a fix, open an issue
first so that the design can be agreed before the work is done.

## Working on it

You need [uv](https://docs.astral.sh/uv/). Everything else is installed into the project's own
environment:

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

All four must pass; CI runs them on Python 3.11, 3.12 and 3.13.

With `haproxy` 3.0 on the `PATH`, `uv run pytest` also runs the tests that start a real HAProxy
on the generated configuration, and the demo's walkthrough. They take about a minute and a half
and are reported as skipped where HAProxy is not installed. A few tests compare with `openssl`
or drive `curl`, and are skipped where those are missing. Nothing in the suite needs Docker, the
network or a Lean server.

## What a change needs

* **A test that fails without it.** Tests are named for what they show
  (`test_a_lean_timeout_is_never_retried`), and Lean servers in tests are in-process fakes
  (`tests/fake_lean_server.py`).
* **The documentation in the same change.** The pages in `docs/` describe behaviour exactly, and
  `tests/test_documentation.py` holds them to the code: a new flag must be in the configuration
  reference, and a directive or message the pages quote must be the one the code produces.
* **No new dependency without a reason.** `leanpool.agent` and `leanpool.haproxy` use the
  standard library only, and a test imports them with every third-party package made
  unimportable. `leanpool.cache`, `leanpool.admit` and `leanpool.join` use
  [aiohttp](https://docs.aiohttp.org/); `leanpool.pki` uses
  [cryptography](https://cryptography.io/).

## The rules the code keeps

These are the properties a review looks for first:

* **The pool never changes what a client measures.** Nothing is stored, retried or reordered in
  a way that could turn one Lean verdict into another.
* **Every doubt is resolved towards the check being run again.** Not storing costs a recheck;
  storing a wrong answer is permanent.
* **A refused change changes nothing.** Editors validate the whole result before writing, and
  write through a temporary file.
* **No secret on a command line or in a log.** Keys and tokens come from files or the
  environment.
* **Whatever ends up inside `haproxy.cfg` is validated strictly first.**

## Layout

```
leanpool/     the package: cache, agent, haproxy, admit, pki, join
tests/        the test suite, and the fakes it uses
docs/         the documentation
deploy/       the Dockerfile and compose files for a real pool
examples/     the demo, an example client, starter admission cases
```
