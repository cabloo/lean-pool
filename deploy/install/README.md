# Installers

Scripts that install a whole pool on Linux hosts with Docker: the Lean servers, the pool's front
door, and new boxes joining it. A deployment keeps its own values in a site directory and passes
it with `--site`. The other files in [`deploy/`](../README.md) are a manual walk-through of the same
pieces.

| File | What it is |
|---|---|
| [`install.sh`](install.sh) | Installs one Kimina Lean Server on this host: the image, the container, its firewall rules and the operator's door. With `--proxy NAME`, it runs the pool installer. |
| [`install-pool.sh`](install-pool.sh) | Installs the pool's front door (HAProxy and the cache) in front of instance NAME, which is TLS throughout. |
| [`kimina-ctl`](kimina-ctl) | The operator's commands for the Lean servers on a host, installed to `/usr/local/bin`. |
| [`lean-pool-ctl`](lean-pool-ctl) | The operator's commands for a pool (servers, admission, join windows), installed to `/usr/local/sbin`. |
| [`join-box.sh`](join-box.sh) | The body of the join script that `lean-pool-ctl NAME join-open` serves to a new box. |
| [`kimina-firewall`](kimina-firewall) | The firewall rules of one Lean server, re-applied at boot by a systemd unit. |
| [`pool-lib.sh`](pool-lib.sh) | What the scripts above share. |
| `docker-compose*.yml` | The compose files the installers copy into `/srv`. |
| [`site.example/`](site.example) | An example site: copy it and change its values. |

Each script describes what it does, in order, at its top, and `bash install.sh --help` lists every option.

## The site directory

The installers take their values from a site directory, never from the scripts:

```
site/
  site.env              SITE_PREFIX, SITE_LABEL, SITE_DOOR_KEY_MARKER, SITE_LAN_DOMAIN
  instances/
    default.env         instance 1, the instance with no name (optional)
    NAME.env            a named instance: install.sh --instance NAME (1-16 lower-case
                        letters and digits; not `default` or `main`)
  Dockerfile ...        the image recipes the instance files name, if any
```

[`site.example/site.env`](site.example/site.env) and
[`site.example/instances/upstream.env`](site.example/instances/upstream.env) document every
key. The installers read these files and never run them, and `install.sh` checks every value
for its shape before it installs anything.

An instance's workers, worker memory, CPUs and subnet only fill in settings that its
`kimina.env` does not have yet. After the first install, change them there (or with
`kimina-ctl set-repls N` and `set-cpus N`). Its port and image tag are written on every run.

An instance's image comes from one of two places:

- **Pulled.** `INSTANCE_IMAGE=projectnumina/kimina-lean-server:2.0.0` names an image to pull. A
  reference with a digest pins it exactly. The installer pulls it and tags it as
  `<SITE_PREFIX>/kimina:<INSTANCE_IMAGE_TAG>`. A later run does not pull it again while the
  reference and the tag are unchanged and the image is still under that tag.
- **Built.** `INSTANCE_DOCKERFILE` and `INSTANCE_BUILD_FILES` name a recipe in the site
  directory, and the installer builds it. For a named instance, a re-run builds again only when
  one of those files, the tag or `--build-jobs` changed, or the image under the tag is gone.
  Instance 1 calls `docker build` on every run, and Docker's cache makes an unchanged build
  quick.

An instance file names one or the other, never both.

## Installing a pool

On the pool's host, as root:

```
sudo bash install.sh --site ~/my-site --instance upstream
sudo bash install.sh --site ~/my-site --proxy upstream --admit-cases ../../examples/admission-cases
sudo lean-pool-ctl upstream join-open
```

1. The first command installs the Lean server.
2. The second installs the pool in front of it. A server joins only after it passes the admission cases ([admission](../../docs/admission.md)); the pool host's own server is listed without them. Every install of the pool needs `--admit-cases DIR` (an uninstall does not), and it replaces the pool's copy of the cases.
3. The third lets one new box join. It prints a `curl ... | sudo bash` command to run on that box. The box then downloads the image from the pool, or else builds it (or pulls it, for an instance that names an `INSTANCE_IMAGE`), and installs its server behind a TLS front that only the pool's proxy can pass.

Every step is safe to run again. A re-run keeps the pool's certificate authority, its key and its server list.

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
