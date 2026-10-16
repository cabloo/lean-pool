"""Which door a check came in by, as the proxy tells the cache.

A pool can have a second way in beside its front door: the screened door, for senders trusted
less than the pool's own (``docs/cache.md``). Its frontend sets one header on every request it
forwards, and the front door takes that header away from every request it receives. The cache
takes the screened key only from a request that carries the header, and the pool's own key only
from one that does not, so each key works at its own door alone. Only the proxy reaches the
cache, so the header is the proxy's word, never a sender's.
"""

DOOR_HEADER = "X-Lean-Pool-Door"
SCREENED_DOOR = "screened"
