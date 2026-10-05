# TLS

A pool set up as in [Deploying a pool](deployment.md) is plain HTTP. This page encrypts it.

Given three certificate files, `leanpool-haproxy-config render` writes a configuration in which
every hop that leaves a machine is encrypted, TLS 1.3 or later, and both ends of the hop between
the proxy and a Lean server prove who they are. The certificates come from the pool's own
certificate authority, made with `leanpool-pki`. Nothing outside the pool is involved.

```
 clients ---HTTPS---> HAProxy :18100 --> cache --> HAProxy 127.0.0.1:18101     (the proxy box;
 (trust the pool's                                        |                     loopback hops
  authority)                                         mutual TLS                 stay plain)
                                                          |
                    +-------------------------------------+---------- a Lean server box ---+
                    |                                     v                                |
                    |   TLS front (HAProxy)  :8000  --plain, inside the box-->  Kimina     |
                    |   TLS front (HAProxy)  :18200 --plain, inside the box-->  agent      |
                    +----------------------------------------------------------------------+
```

## What is encrypted

| Hop | How |
|---|---|
| a client to the proxy's public port | HTTPS. The proxy presents the front door's certificate and the client checks it against the pool authority's certificate. The API key travels inside. |
| the proxy to each Lean server, checks and health checks alike | Mutual TLS. The box presents its certificate, and the proxy checks that the pool's authority signed it and that it is for **the server's name in the server list**. The proxy presents its client certificate, and the box accepts no other. |
| the proxy to each usage agent | The same mutual TLS. HAProxy's agent check cannot speak TLS, so it asks a loopback listener of the proxy itself (one per server), and that listener carries the exchange to the box. |
| the proxy to the cache, the cache to the proxy's checkers door, statistics | Plain, on loopback, inside the proxy's own network namespace. With TLS `render` refuses a cache that is not on a loopback address. |
| a box's TLS front to the Lean server and the agent beside it | Plain. Keep it inside the box (an internal Docker network, or loopback), and do not publish the Lean server's own port any more. |

## Who holds which key

| Key | Where it is | What it is for |
|---|---|---|
| the authority's (`ca.key`) | on the pool's host, and nowhere else | Signing this pool's certificates. No container needs it. |
| the front door's | on the pool's host, readable by the proxy | Being the pool to its clients. |
| the proxy's client key | on the pool's host, readable by the proxy | Calling the Lean servers. |
| each box's | on that box, where it was made; it never leaves | Being that box, by name, to the proxy. |

The authority's certificate (`ca.crt`) is public: every client needs it, and so does every box.

* **A server's identity is its name, not its address.** A box's certificate is issued for the
  name the server has in the server list, and the proxy checks that name (`sni str(<name>)`,
  `verifyhost <name>`) whatever host name or address it dials. Addresses change (DHCP), and
  HAProxy's `verifyhost` does not match an IP address in a certificate.
* **A box's certificate cannot call another box.** It is a server certificate, and TLS refuses a
  server certificate presented by a client. A box's front also turns away every client
  certificate but the proxy's own, by name
  (`tcp-request session reject unless { ssl_c_s_dn(cn) -m str <proxy client name> }`).
* **There is no revocation list.** A box that leaves the pool keeps a certificate that proves
  only its own name, and the proxy no longer dials it. If a private key is lost to someone else,
  make a new authority and issue everything again.
* Certificates last 5 years and the authority 10; `leanpool-pki expiry` prints the days left.

## Setting it up

This starts from a pool that works in plain HTTP, set up as in
[Deploying a pool](deployment.md): `deploy/pool` on the pool box, `deploy/lean-server` on each
Lean server box, and [the commands](deployment.md#getting-the-commands) available on both. Here
the pool is reached as `pool.example` at 192.0.2.1 and has one Lean server, `lean-a`, at
192.0.2.10.

Do it in one sitting. From the moment a Lean server box moves behind its TLS front (step 4)
until the proxy has moved too (step 6), the proxy cannot reach that box. Checks go to the boxes
that have not moved yet, and are answered 503 once none is left.

### 1. On the pool box: the authority and the proxy's certificates

In `deploy/pool`. Give the front door every name and address clients use for the pool:

```sh
uv run leanpool-pki init --ca-dir pki/ca
uv run leanpool-pki issue-server --ca-dir pki/ca --out-dir pki/proxy --file-stem front \
    --name pool.example --ip 192.0.2.1
uv run leanpool-pki issue-client --ca-dir pki/ca --out-dir pki/proxy --file-stem proxy-client \
    --name lean-pool-proxy
cp pki/ca/ca.crt pki/proxy/ca.crt
```

`pki/proxy` now holds what the proxy needs: `front.pem`, `proxy-client.pem` and `ca.crt`. The
authority's key, `pki/ca/ca.key`, is needed again only to sign another certificate. No
container ever mounts it.

### 2. On the Lean server box: its key and a signing request

In `deploy/lean-server`. The name is the one the server has in the pool's server list:

```sh
uv run leanpool-pki csr --out-dir tls --name lean-a
```

That writes `tls/lean-a.key`, which never leaves the box, and `tls/lean-a.csr`, which is not
secret. Take the `.csr` file to the pool box, into `deploy/pool`.

### 3. On the pool box: sign the request

In `deploy/pool`, for exactly the name this box may have:

```sh
uv run leanpool-pki sign-csr --ca-dir pki/ca --csr lean-a.csr --out lean-a.crt \
    --allow-dns lean-a
```

Take `lean-a.crt` and a copy of `pki/ca/ca.crt` back to the box, into `deploy/lean-server/tls`.
Both are public.

### 4. On the Lean server box: the TLS front

In `deploy/lean-server`. Join the certificate and the key into the one file HAProxy takes, and
render the front's configuration:

```sh
(umask 077 && cat tls/lean-a.crt tls/lean-a.key > tls/box.pem)
mkdir -p front
uv run leanpool-haproxy-config render-box \
    --tls-server-pem /etc/leanpool/tls/box.pem --tls-ca-file /etc/leanpool/tls/ca.crt \
    --proxy-client-name lean-pool-proxy \
    --lean-upstream kimina:8000 --agent-upstream agent:18200 > front/haproxy.cfg
```

The front is the same HAProxy image as the proxy and runs as that image's own unprivileged user.
Let its group, and nobody else, read the key; then start the box again from
[`compose.tls.yaml`](../deploy/lean-server/compose.tls.yaml), in which the Lean server and the
agent publish nothing and only the front does:

```sh
gid=$(docker run --rm --network none --entrypoint id haproxy:3.0 -g)
sudo chgrp "$gid" tls tls/box.pem && chmod 0750 tls && chmod 0640 tls/box.pem
docker compose -f compose.tls.yaml up -d --build
```

### 5. On the pool box: test the box through its front

In `deploy/pool`. Before the proxy is moved, test the Lean server alone, as the proxy will reach
it ([Through a box's TLS front](admission.md#through-a-boxs-tls-front)):

```sh
uv run leanpool-admit --server https://192.0.2.10:8000 --api-key-file api-key.txt \
    --cases ../../examples/admission-cases \
    --tls-ca-file pki/ca/ca.crt --tls-client-pem pki/proxy/proxy-client.pem \
    --tls-server-name lean-a
```

Repeat steps 2 to 5 for every server in the list.

### 6. On the pool box: move the proxy

In `deploy/pool`. Render with the three certificate files, as the proxy's container sees them,
and let HAProxy validate the result with the certificates in place:

```sh
uv run leanpool-haproxy-config render --servers servers \
    --tls-front-door-pem /etc/leanpool/tls/front.pem \
    --tls-ca-file /etc/leanpool/tls/ca.crt \
    --tls-client-pem /etc/leanpool/tls/proxy-client.pem > haproxy/haproxy.cfg.new
```

```sh
gid=$(docker run --rm --network none --entrypoint id haproxy:3.0 -g)
sudo chgrp "$gid" pki/proxy pki/proxy/*.pem && chmod 0750 pki/proxy && chmod 0640 pki/proxy/*.pem
docker run --rm --network none -v "$PWD/haproxy":/cfg:ro \
    -v "$PWD/pki/proxy":/etc/leanpool/tls:ro haproxy:3.0 haproxy -c -f /cfg/haproxy.cfg.new
mv haproxy/haproxy.cfg.new haproxy/haproxy.cfg
docker compose -f compose.yaml -f compose.tls.yaml up -d
```

[`compose.tls.yaml`](../deploy/pool/compose.tls.yaml) adds one thing to the pool's compose
file: `pki/proxy`, mounted read-only where the configuration expects the certificates.

### 7. Clients

Clients keep their API key and get the authority's certificate, `pki/ca/ca.crt`, beside it:

```sh
curl --cacert ca.crt https://pool.example:18100/health
```

In Python, `ssl.create_default_context(cafile="ca.crt")` is all a client needs, with host name
checking on and under the strict X.509 rules that are the default from Python 3.13.

## Changing a pool that uses TLS

Everything in [Operating a pool](deployment.md#operating-a-pool) holds, with four differences:

* **Name the TLS compose files in every `docker compose` command**:
  `-f compose.yaml -f compose.tls.yaml` on the pool box, `-f compose.tls.yaml` on a Lean server
  box. A bare `docker compose up -d` there would start the plain arrangement again: the proxy
  without its certificates, or a box's Lean server on the port its TLS front holds. Setting
  `COMPOSE_FILE=compose.yaml:compose.tls.yaml` (on a box, `COMPOSE_FILE=compose.tls.yaml`) in the
  shell spares the typing.
* **Always render with the three `--tls-...` options**, or set `LEANPOOL_HAPROXY_TLS_FRONT_DOOR_PEM`,
  `LEANPOOL_HAPROXY_TLS_CA_FILE` and `LEANPOOL_HAPROXY_TLS_CLIENT_PEM` once in the shell you render
  from. A configuration rendered without them is plain HTTP, and says so in its first line.
* **Validate with the certificates mounted**, as in step 6: HAProxy reads them when it checks a
  configuration.
* **A new server needs steps 2 to 5 first**, and its name in the server list must be the name
  its certificate was signed for. [Joining over the network](joining.md) carries the signing
  request and the certificate for a box that can reach the pool box.

`leanpool-pki expiry CERTIFICATE` prints the days a certificate has left. To replace one:

* A box's certificate: sign the box's request again with `--replace` (the box keeps its key),
  join the new certificate and the key into `box.pem` as in step 4, and reload the front.
* The front door's or the proxy's own: issue it again with `--replace`. That makes a new key
  and writes new files, for you alone to read, so give HAProxy's group its access again as in
  step 6 before you reload the proxy. A new front door key also has a new pin: a
  [join window](joining.md) opened afterwards prints it.

Reload with `kill -HUP 1` inside the container, as for a new configuration.
