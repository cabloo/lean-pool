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

On the pool's host, make the authority, the front door's certificate (with every name and
address clients use for the pool) and the proxy's client certificate:

```sh
leanpool-pki init --ca-dir pki/ca
leanpool-pki issue-server --ca-dir pki/ca --out-dir pki/proxy --file-stem front \
    --name pool.example --ip 192.0.2.1
leanpool-pki issue-client --ca-dir pki/ca --out-dir pki/proxy --file-stem proxy-client \
    --name lean-pool-proxy
cp pki/ca/ca.crt pki/proxy/
```

`pki/proxy` now holds what the proxy needs: `front.pem`, `proxy-client.pem` and `ca.crt`. The
authority's key, `pki/ca/ca.key`, is needed again only to sign another certificate; no
container ever mounts it.

Mount `pki/proxy` read-only in the proxy's container, say at `/etc/leanpool/tls`, readable by
the user HAProxy runs as, and render with the paths as that container sees them. In
`deploy/pool`, [`compose.tls.yaml`](../deploy/pool/compose.tls.yaml) adds exactly that mount and
says how to set the files' group; the rendered file goes to `haproxy/haproxy.cfg` there, and the
pool is started with both compose files.

```sh
leanpool-haproxy-config render --servers servers \
    --tls-front-door-pem /etc/leanpool/tls/front.pem \
    --tls-ca-file /etc/leanpool/tls/ca.crt \
    --tls-client-pem /etc/leanpool/tls/proxy-client.pem > haproxy.cfg
```

On each Lean server box, make the box's key and a signing request for the name the server will
have in the server list (`lean-a` here). The request goes to the pool's host; the key stays:

```sh
leanpool-pki csr --out-dir tls --name lean-a             # on the box
leanpool-pki sign-csr --ca-dir pki/ca --csr lean-a.csr --out lean-a.crt \
    --allow-dns lean-a                                   # on the pool's host
cat lean-a.crt tls/lean-a.key > tls/box.pem              # on the box again, with umask 077
```

Then put the TLS front where the Lean server and the agent used to be published. It is the same
HAProxy image as the proxy, given `box.pem` and a copy of the authority's `ca.crt`; the Lean
server and the agent sit behind it on a network that does not leave the box.
[`deploy/lean-server/compose.tls.yaml`](../deploy/lean-server/compose.tls.yaml) is that
arrangement; it reads `tls/box.pem`, `tls/ca.crt` and the front's configuration as
`front/haproxy.cfg`:

```sh
leanpool-haproxy-config render-box \
    --tls-server-pem /etc/leanpool/tls/box.pem --tls-ca-file /etc/leanpool/tls/ca.crt \
    --proxy-client-name lean-pool-proxy \
    --lean-upstream kimina:8000 --agent-upstream agent:18200 > box-haproxy.cfg
```

Before the box is added to the server list, test its Lean server alone through the front, from
the pool's host, as the proxy will reach it (see
[Through a box's TLS front](admission.md#through-a-boxs-tls-front)):

```sh
leanpool-admit --server https://192.0.2.10:8000 --api-key-file api-key.txt --cases cases/ \
    --tls-ca-file pki/ca/ca.crt --tls-client-pem pki/proxy/proxy-client.pem \
    --tls-server-name lean-a
```

Clients keep their API key and get the authority's certificate beside it:

```sh
curl --cacert ca.crt https://pool.example:18100/health
```

In Python, `ssl.create_default_context(cafile="ca.crt")` is all a client needs, with host name
checking on and under the strict X.509 rules that are the default from Python 3.13.
