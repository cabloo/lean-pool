# Installers

Scripts that install a whole pool on Linux hosts with Docker: the Lean servers, the pool's front
door, and new boxes joining it, TLS throughout. They run the compose files of
[`deploy/`](../README.md) exactly as they are there. A deployment keeps its own values in a site
directory and passes it with `--site`. [Deploying a pool](../../docs/deployment.md) walks through
them.

| File | What it is |
|---|---|
| [`install.sh`](install.sh) | Installs one Kimina Lean Server on this host: the image, the container, its firewall rules and the operator's door. With `--proxy NAME`, it runs the pool installer. |
| [`install-pool.sh`](install-pool.sh) | Installs the pool's front door (HAProxy and the cache) in front of instance NAME. |
| [`kimina-ctl`](kimina-ctl) | The operator's commands for the Lean servers on a host, installed to `/usr/local/bin`. |
| [`lean-pool-ctl`](lean-pool-ctl) | The operator's commands for a pool (servers, admission, join windows), installed to `/usr/local/sbin`. |
| [`join-box.sh`](join-box.sh) | The body of the join script that `lean-pool-ctl NAME join-open` serves to a new box. |
| [`join_helper.py`](join_helper.py) | Every decision of a join, as pure functions, for both sides of it. |
| [`kimina-firewall`](kimina-firewall) | The firewall rules of one Lean server, re-applied at boot by a systemd unit. |
| [`pool-lib.sh`](pool-lib.sh) | What the scripts above share, among it the move of a box an older version set up. |
| [`site.example/`](site.example/site.env) | An example site: copy it and change its values. Its two files document every key. |

Each script describes what it does, in order, at its top, and `bash install.sh --help` lists every
option. Every step is safe to run again.

- [The site directory](../../docs/deployment.md#a-site-directory)
- [Installing the pool's host](../../docs/deployment.md#the-pools-host) and
  [joining a box](../../docs/deployment.md#joining-a-box)
- [A box set up by an older installer](../../docs/deployment.md#a-box-set-up-by-an-older-installer):
  the first run of these installers moves it, and the steps back
- [Removing](../../docs/deployment.md#removing)

## The Docker Compose plugin

A host that has Docker without its compose plugin gets one at the end of the pre-flight: from the
package manager, else the release binary of Docker Compose **v5.6.0**, checked against its pinned
SHA-256 (`LEAN_POOL_COMPOSE_*` in [`pool-lib.sh`](pool-lib.sh)).

## Tests

[`tests/install/`](../../tests/install) runs these scripts on a fake machine: copies whose host
paths point into a fake root directory, and stand-ins for `docker`, `systemctl`, `iptables` and
the other tools they call.
[`tests/install/stand_ins.py`](../../tests/install/stand_ins.py) explains how. Running for real
needs root, Docker and systemd. The tests' own site is
[`tests/install/site/`](../../tests/install/site).
