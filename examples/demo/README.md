# The demo

A whole pool on one machine in about a minute, with no Lean or Mathlib image to build: HAProxy
on the generated configuration, the cache, and two stand-in Lean servers with a usage agent
each. You need Docker with compose, and `bash`, `curl` and `awk` for the walkthrough.

From the repository root:

```sh
docker compose -f examples/demo/compose.yaml up --build -d
examples/demo/walkthrough.sh
docker compose -f examples/demo/compose.yaml down -v     # when you are done
```

The pool listens on `http://127.0.0.1:18100` and its API key is `demo-key`.

## What the walkthrough shows

| Step | What happens |
|---|---|
| 1 | `GET /health` says the pool's size: two servers, six workers. |
| 2 | A check. Nothing is stored yet, so a Lean server works on it for 2 seconds. |
| 3 | The same proof as another attempt comes back in milliseconds, under its own id, marked `"cached": true`. |
| 4 | A proof Lean rejects is a verdict too, and is stored like one. |
| 5 | A Lean timeout is no verdict: it is passed on as it is, and never stored. |
| 6 | A Lean server is stopped. The check that meets it is answered by the other one. |
| 7 | The cache is stopped. Checks keep flowing, straight to a Lean server. |
| 8 | Both come back. The cache kept its store, and the pool its size. |
| 9 | The cache's counters. |

The script prints what it sent and what came back, and stops with a non-zero status at the
first step that does not behave as described. It can be run again against the same pool: each
run sends code of its own.

## What is real here, and what is not

**Real:** [`haproxy.cfg`](haproxy.cfg), exactly as `leanpool-haproxy-config render` writes it
for [`servers`](servers) (a test fails if the two drift apart); the cache; the usage agents;
the image they run from.

**Not real:** the Lean servers. [`standin_lean_server.py`](standin_lean_server.py) speaks
Kimina's interface, waits as long as a check might take, and answers from markers in the code:

| The code contains | The stand-in answers |
|---|---|
| `sorry` | Lean's warning for a proof that assumes a `sorry` |
| `standin: error` | an error message, as for a wrong proof |
| `standin: timeout` | Kimina's reply to a Lean timeout: HTTP 200 with an `error` |
| `standin: crash` | HTTP 500, Kimina's reply when a worker crashed |
| anything else | accepted, with a message naming the stand-in that answered |

It judges nothing. It is also handy for trying a client of your own against a pool:

```sh
echo 'theorem two : 1 + 1 = 2 := by rfl' > two.lean
uv run python examples/pool_client.py --url http://127.0.0.1:18100 \
    --api-key-file <(echo demo-key) two.lean
```

For a pool of real Lean servers, see [Deploying a pool](../../docs/deployment.md).
