# TLS

A pool the [installers](deployment.md) set up is TLS throughout. Every hop that leaves a machine
is encrypted, TLS 1.3 or later, and both ends of the hop between the proxy and a Lean server
prove who they are. The certificates come from the pool's own certificate authority, made with
`leanpool-pki` on the pool's host. Nothing outside the pool is involved.

```
 clients ---HTTPS---> HAProxy :18100 --> cache --> HAProxy 127.0.0.1:18101     (the proxy box;
 (trust the pool's                                        |                     loopback hops
  authority)                                         mutual TLS                 stay plain)
                                                          |
                    +-------------------------------------+---------- a Lean server box ---+
                    |                                     v                                |
                    |   TLS front (HAProxy)  :18001 --plain, inside the box-->  Kimina     |
                    |   TLS front (HAProxy)  :18201 --plain, inside the box-->  agent      |
                    +----------------------------------------------------------------------+
```

## What is encrypted

| Hop | How |
|---|---|
| a client to the proxy's public port | HTTPS. The proxy presents the front door's certificate and the client checks it against the pool authority's certificate. The API key travels inside. |
| the proxy to each Lean server, checks and health checks alike | Mutual TLS. The box presents its certificate, and the proxy checks that the pool's authority signed it and that it is for **the server's name in the server list**. The proxy presents its client certificate, and the box accepts no other. |
| the proxy to each usage agent | The same mutual TLS. HAProxy's agent check cannot speak TLS, so it asks a loopback listener of the proxy itself (one per server), and that listener carries the exchange to the box. |
| the proxy to the cache, the cache to the proxy's checkers door, statistics | Plain, on loopback, inside the proxy's own network namespace. With TLS `render` refuses a cache that is not on a loopback address. |
| a box's TLS front to the Lean server and the agent beside it | Plain, on the box's own Docker network. The Lean server and the agent publish no port of their own: the front alone does. |

## Who holds which key

| Key | Where it is | What it is for |
|---|---|---|
| the authority's (`ca.key`) | on the pool's host, and nowhere else | Signing this pool's certificates. No container that keeps running mounts it: only the short-lived ones that issue a certificate or sign a box's request. |
| the front door's | on the pool's host, readable by the proxy, and by the join service while a join window is open (it serves the window with the same key) | Being the pool to its clients. |
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

## How the installers set it up

`install.sh --proxy NAME` makes, on the pool's host, in `/srv/lean-pool-NAME`:

| Directory | Who may read it | What it holds |
|---|---|---|
| `pki/` | root | The authority (`pki/ca`, made once; no container that runs mounts it), and what it issued, keys included: the front door's certificate, the proxy's client certificate (`lean-pool-proxy`), and the pool's host's own server's. |
| `tls/` | root and HAProxy's group | What the proxy reads, mounted read-only: `front.pem` and `proxy-client.pem` (each a certificate and its key), `ca.crt`. |
| `certs/` | anyone | Public certificates only: `ca.crt`, which `lean-pool-ctl NAME ca` prints, and the ones `status` reads for the days left. |

The front door's certificate names the host, `HOST.<SITE_LAN_DOMAIN>`, loopback, the Docker
bridge, and every `--front-name`. A re-run keeps every certificate it finds and issues one again
only when its names changed or one of its files is missing.

A box that [joins](joining.md) makes its own key, which never leaves it, and sends a signing
request for its name through the join window. The pool signs it for exactly that name
(`leanpool-pki sign-csr --allow-dns NAME`) and hands back the certificate, the authority's
certificate and the pool's API key. The box's installer then puts its Lean server and usage agent
behind a front rendered with `leanpool-haproxy-config render-box`, which lets in the proxy's
client certificate and nothing else. The pool's host's own server stands behind the same front.

## Clients

Clients keep their API key and get the authority's certificate beside it, from
`sudo lean-pool-ctl NAME ca`:

```sh
curl --cacert ca.crt https://pool-host.lan:18100/health
```

In Python, `ssl.create_default_context(cafile="ca.crt")` is all a client needs, with host name
checking on and under the strict X.509 rules that are the default from Python 3.13.

## Certificates over time

`sudo lean-pool-ctl NAME status` prints the days each certificate has left. To replace one:

* **A box's that joined:** take it out of the server list (`lean-pool-ctl NAME remove-backend
  SERVER`), remove it with the command it printed at its join, and let it join again. A join
  refuses a name or an address that is already in the list.
* **The pool's host's own server's:** remove `box.crt`, `box.key` and `box.pem` from `pki/box`
  and run the pool's install line again, which issues what is missing.
* **The front door's or the proxy's own:** remove its three files (`.crt`, `.key`, `.pem`) from
  `pki/proxy` and run the pool's install line again. A new front door key has a new pin: a
  [join window](joining.md) opened afterwards prints it.
* **The authority's:** `install.sh --site DIR --proxy NAME --uninstall --purge` removes it with
  everything it signed, and a new install makes a new one; every box then joins again.

Without the installers, the commands behind all of this are `leanpool-pki` (the authority, the
certificates, the expiry) and the `--tls-...` options of `leanpool-haproxy-config`, which are in
the [configuration reference](configuration.md). The pool's
[`compose.tls.yaml`](../deploy/pool/compose.tls.yaml) and the box's
[`compose.tls.yaml`](../deploy/lean-server/compose.tls.yaml) say what each expects.
