# Joining over the network (`leanpool-join`)

With [TLS](tls.md), a new Lean server box needs a certificate signed by the pool's authority
before the pool will speak to it. Carrying a signing request to the pool's host and a
certificate back by hand is fine for one box. `leanpool-join` does the carrying for a box that
can reach the pool's host. For one join window it serves a script and passes four messages
between the new box and a spool directory.

**It executes nothing.** Whoever watches the spool on the pool's host (an operator, or a script
of yours) decides: validates what the box sent, signs with `leanpool-pki sign-csr`, runs
`leanpool-admit` against the box alone, adds it to the server list. The service only moves the
files, so it can run as an unprivileged, read-only container that holds no authority key.

**What this project ships is the service, and it is one half of a join.** The other half is
yours: the script a new box runs, and whatever acts on the spool on the pool's host. Both are a
handful of this project's commands, shown [below](#one-join-step-by-step); the deployment this
project was written for wraps them in scripts of its own, with its own installer, firewall and
paths, which is why they are not here.

## A join window

A window is one random token and one box:

| Request | Answer |
|---|---|
| `GET /j/<token>` | 200 and the join script. |
| `POST /j/<token>/csr` with JSON `{name, lean_port, agent_port, workers, csr}` | 202. Written to `requests/csr.json` together with the caller's address. The first one binds the window to that address. |
| `GET /j/<token>/certificate` | 204 while there is no `responses/certificate.json`; 200 `{certificate, ca, api_key}` once that file holds `{certificate, ca}`; 409 `{reason}` if it holds `{reason}`. |
| `POST /j/<token>/ready` | 202. Written to `requests/ready.json` with the caller's address. |
| `GET /j/<token>/verdict` | 204 while there is no `responses/verdict.json`; 200 `{admitted, detail}` once there is. |
| `GET /j/<token>/image.json` | 200 and the manifest of the Lean server image this window ships, as the pool's host wrote it; 204 when it ships none. Binds nothing. |
| `GET /j/<token>/image` | 200 and the image file, or 206 and the part a `Range` asks for (a download that stopped goes on with `curl -C -`); 404 when the window ships none. Binds nothing. |

### Shipping the image

Building a Lean server's image takes a new box minutes to a quarter of an
hour, and the pool's host already has one. With `--image-directory` the service also serves that
image: the directory holds `manifest.json` and the image file it names, a plain
`image-<12 hex>.tar` (`docker save`) of exactly the size the manifest records in `bytes`.
Anything else in the directory is never served, and a manifest that names another kind of file,
a link, or a file of another size ships nothing. The service sends the file from disk in chunks
of 256 KiB and never reads it whole, so it stays within a small container whatever the image's
size. It decides nothing about the image: the box checks the file's SHA-256 against the manifest
before it loads it. The two requests bind nothing, so a box may fetch the image, fail, and come
back in the same window.

### The command a new box runs

It is one line. The box does not have the pool's authority yet, so it cannot check the
certificate's chain (`-k`). It checks the front door's public key instead, against a pin that
`leanpool-pki pin` prints on the pool's host. (This pin is a fingerprint of a key. It has
nothing to do with the cache's pin, which names Lean and Mathlib versions.)

```sh
curl -fsSk --pinnedpubkey 'sha256//...' https://pool.example:18110/j/<token> | sudo bash
```

curl compares the server's key with the pin before it sends anything, and exits 90 if they
differ. Pin every later request of the join script the same way: the script, the box's signing
request, its certificate and the pool's API key then all travel inside TLS to the machine that
holds the front door's key, and to no other.

### The rules it keeps

* **HTTPS only**, TLS 1.3 or later, with the front door's certificate. There is no plain mode.
* **Without the token, nothing.** A request whose path does not carry the token is answered 404,
  as is a request for anything but those above, so someone without the token cannot tell
  whether a window is open. The token is compared in constant time. It is 22 to 128 letters,
  digits, `-` and `_`: 128 random bits are 22 characters in URL-safe base64, 32 in hexadecimal.
* **One box per window.** Once a signing request has arrived, every request from another
  address is answered 409, whatever it asks. The address is the connection's own, never a
  header. The same signing request sent again is accepted; a different one is answered 409, so
  what the spool holds never changes under whoever is acting on it.
* **In order.** `ready` is answered 409 until a certificate has been issued, and the certificate
  and the verdict are handed out only to the box the window is bound to: an answer left in the
  spool is given to nobody before a signing request has arrived.
* **Small and checked.** A request body is at most 16 KiB (413 above). A signing request must be
  a JSON object with exactly the five fields; the name, the ports and the worker count obey the
  rules of the server list, the name must be one a certificate can carry, and `csr` must be one
  PEM signing request. Anything else is answered 422 and nothing is written. Whether the
  signing request is a good one is not judged here: `leanpool-pki sign-csr` does that, for the
  names it is told to allow.
* **The spool.** A request file appears complete or not at all, and is never changed once it
  exists. Write response files the same way, under a temporary name and then renamed; one that
  is not valid JSON yet is treated as not there, and one that is valid JSON of the wrong shape
  is answered 500. The service keeps no state of its own, so a restarted service carries on from
  what the spool holds. Give every window an empty spool and a new token.
* **Nothing secret is logged.** The token is part of every path, so the service has no access
  log, and what it logs names the caller's address and what happened: never a path, a body, the
  token or the API key.

## One join, step by step

`URL` is `https://pool.example:18110/j/<token>` and `PIN` is the front door's pin. Every
request of the box is `curl -fsSk --pinnedpubkey "$PIN"`.

| | On the pool's host | On the new box |
|---|---|---|
| 1 | Make a token and an empty spool directory, start `leanpool-join`, and print the front door's pin ([the commands](#opening-a-window)). | |
| 2 | | Run the one-line command. The script it fetches does the rest. |
| 3 | | Make the box's key and signing request, `leanpool-pki csr --out-dir tls --name NAME`, and `POST URL/csr` with `{name, lean_port, agent_port, workers, csr}`. The key never leaves the box. |
| 4 | `requests/csr.json` appears. Decide whether this box may join under this name. Sign: `leanpool-pki sign-csr --ca-dir pki/ca --csr ... --out ... --allow-dns NAME`. Write `responses/certificate.json` with `{certificate, ca}`, or `{reason}` to refuse. | |
| 5 | | Poll `GET URL/certificate` until it is no longer 204. It carries the certificate, the authority's certificate and the pool's API key. Join certificate and key into `box.pem`, start the Lean server, the usage agent and the [TLS front](tls.md#setting-it-up), then `POST URL/ready`. |
| 6 | `requests/ready.json` appears. Test the box alone, as the proxy will reach it: `leanpool-admit --server https://ADDRESS:PORT ... --tls-server-name NAME` ([admission](admission.md#through-a-boxs-tls-front)). If it passes: `leanpool-haproxy-config add`, `render`, validate, reload ([adding a server](deployment.md#adding-and-removing-a-server)). Write `responses/verdict.json` with `{admitted, detail}`. | |
| 7 | Stop `leanpool-join`. The window is over. | Poll `GET URL/verdict` and report it. |

A failure at any step leaves the pool as it was: nothing is added before step 6 has passed, and
a box that was refused holds a certificate that proves only its own name.

### Opening a window

Step 1, on the pool's host, in the directory that holds `pki/` and `api-key.txt`
(`deploy/pool`, if you followed [TLS](tls.md)), with your join script as `join.sh`:

```sh
mkdir -m 0700 spool
(umask 077 && openssl rand -hex 16 > token.txt)
leanpool-pki pin pki/proxy/front.crt
leanpool-join --script join.sh --token-file token.txt --tls-pem pki/proxy/front.pem \
    --spool spool --api-key-file api-key.txt
```

The second-to-last command prints the pin for the new box's `curl`; the last one serves the
window on port 18110 until it is stopped. The token is the last part of the window's address,
`https://pool.example:18110/j/<token>`.
