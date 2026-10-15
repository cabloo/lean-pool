# Deployment files

| File | For |
|---|---|
| [`Dockerfile`](Dockerfile) | The one image of this project: it runs the cache by default, and the usage agent or the join service by name, and holds all six commands. Build it from the repository root: `docker build -f deploy/Dockerfile -t lean-pool .` |
| [`pool/compose.yaml`](pool/compose.yaml) | The pool box: HAProxy and the cache. |
| [`pool/compose.tls.yaml`](pool/compose.tls.yaml) | Added to the file above for TLS: the proxy's certificates. |
| [`pool/servers.example`](pool/servers.example) | The format of the server list. |
| [`lean-server/compose.yaml`](lean-server/compose.yaml) | A Lean server box: Kimina and the usage agent, plain HTTP. |
| [`lean-server/compose.tls.yaml`](lean-server/compose.tls.yaml) | A Lean server box with TLS: Kimina and the agent behind a TLS front. |
| [`lean-server/compiled-search/`](lean-server/compiled-search) | Optional: makes and checks linarith's search step as compiled code on the box (`compiled-search/make.sh`). |
| [`lean-server/compose.compiled-search.yaml`](lean-server/compose.compiled-search.yaml) | Added after either Lean server file: the workers start through that step. |

[Deploying a pool](../docs/deployment.md) walks through them, [TLS](../docs/tls.md) through
the two TLS files, and [compiled search](../docs/compiled-search.md) through the last two.
They are adapted from the files of a running deployment and have not been run as they are
written here ([what is verified](../docs/verification.md)); Docker validates them in this
project's CI.

A deployment's own files (its key, its certificates, its server list, the configuration
rendered from it, and what `make.sh` makes) are created beside these files and are ignored
by git.
