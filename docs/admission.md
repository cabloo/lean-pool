# Admission (`leanpool-admit`)

A server on the wrong Lean or Mathlib version answers confidently and wrongly, and once it is
behind the balancer nothing shows which answers were its. So a server is tested alone, directly,
before it is added:

```sh
leanpool-admit --server http://lean-a.example:8000 --api-key-file api-key.txt --cases cases/
```

The cases directory holds Lean files of two kinds, and both are required:

* `verify/*.lean`: each must come back as a definitive Lean answer with no error-severity message
  and no `sorry`.
* `reject/*.lean`: each must come back with an error-severity message or a `sorry`.

[`examples/admission-cases`](../examples/admission-cases) is a starting set that any Lean with
Mathlib passes (it was checked on Lean 4.27). Add files that depend on the pool's own Lean and
Mathlib versions: proofs that are known to check on the pinned versions, and near misses that
are known not to. A case looks like this:

```
cases/verify/sum_comm.lean      import Mathlib
                                theorem sum_comm (a b : ℕ) : a + b = b + a := by ring

cases/reject/false_claim.lean   import Mathlib
                                theorem false_claim : (2 : ℕ) + 2 = 5 := by norm_num
```

A Lean timeout, a server error or any other reply that is not a definitive Lean answer is
"undecided", and undecided fails the case, whichever kind it is. A warning does not fail a
`verify` case, even one that contains the word "failed": a tactic that fails inside a combinator
that recovers warns while the proof is complete. Each case is sent once, as a plain Kimina
request with one snippet, and nothing is retried.

Before a file is sent, its leading `import` lines are moved above any leading comment. Kimina
takes a file's header to be the imports it starts with; a license comment before the imports
(which Lean itself accepts) would make it send the imports as part of the body, and Lean would
reject them. Imports inside comments and imports after the first command are not moved.

The command prints one JSON report (each case's expected and observed outcome, its seconds, the
counts, and checks per second at the given concurrency) and exits with

| Status | Meaning |
|---|---|
| 0 | every case behaved |
| 1 | at least one case misbehaved |
| 2 | bad arguments (an unusable cases directory, key file or TLS file included) |
| 3 | the server could not be reached, or the TLS handshake with it failed |

## Through a box's TLS front

With [TLS](tls.md) a Lean server answers only through its box's front, and the front lets in
nothing but the pool proxy's client certificate. To test such a server alone, give the admission
test that certificate, the pool authority's certificate and the name the server will have in
the server list:

```sh
leanpool-admit --server https://192.0.2.10:8000 --api-key-file api-key.txt --cases cases/ \
    --tls-ca-file pki/ca/ca.crt --tls-client-pem pki/proxy/proxy-client.pem \
    --tls-server-name lean-a
```

The server is then held to the proof the proxy will ask of it: a certificate signed by the
pool's authority, for that name, whatever host or address `--server` dials. Only the given
authority is trusted, and nothing below TLS 1.3 is spoken.

* The three options go together. Some without the others, an `https://` server without them and
  an `http://` server with them are usage errors (status 2), refused before any request: a server
  that was meant to be reached over TLS is never spoken to in plain HTTP by an omission, and an
  `https://` URL is never trusted through the system's authorities.
* **The API key is not sent until the server has proved itself and accepted the certificate.**
  The first connection asks for `/health` without the key. A TLS client finishes its handshake
  before the server has judged the client's certificate, so only an answer to that request shows
  that the certificate was accepted. Checks, with the key, are sent after it.
* **A TLS failure fails admission at once, with status 3**, after one connection and at most ten
  seconds for each step of it. The report's `error` says which side refused which certificate:

  | What is wrong | The report says |
  |---|---|
  | the server's certificate is for another name | `the server's certificate is not for the name 'lean-a' given as --tls-server-name` |
  | another authority signed it | `the server's certificate was not signed by the authority in --tls-ca-file` |
  | it has expired | `the server's certificate has expired` |
  | the server refused our certificate with a TLS alert | `the server refused the client certificate in --tls-client-pem:` and why: `it is not a client certificate`, `it was not signed by the authority the server trusts`, `it has expired` |
  | the server accepted the handshake and closed (a box's front does this to a client certificate of the right authority that is not the proxy's) | `the server closed the connection after the TLS handshake without an answer` |
  | the port does not speak TLS 1.3 (a Lean server published without its front) | `is this the port of a TLS front?` |

The command reads the proxy's private key, so run it where that key already is: on the pool's
host.
