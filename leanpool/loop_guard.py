"""The loop guard: the header that marks a check as already past the cache.

A check enters the proxy, goes to the cache, and on a miss comes back into the proxy to reach a
Lean server. If that second hop were ever routed to the cache again it would circle for ever.
Three places hold the guard, so no single misconfiguration can open the loop:

* the cache adds the header to every request it forwards;
* the proxy's loopback listener (where the cache forwards to) sets it too;
* the proxy's public frontend sends any request carrying it straight to the Lean servers, and
  the cache refuses one outright.
"""

DEFAULT_HOP_HEADER = "X-Lean-Pool-Hop"
HOP_HEADER_VALUE = "checkers"
