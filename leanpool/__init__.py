"""lean-pool: one endpoint for a pool of Kimina Lean servers.

Six parts, each usable alone:

* ``leanpool.cache``: the shared result cache HAProxy tries first.
* ``leanpool.agent``: the usage agent each Lean server box runs (standard library only).
* ``leanpool.haproxy``: the server list and the ``haproxy.cfg`` generators (standard library
  only).
* ``leanpool.admit``: the admission test a Lean server must pass before it joins a pool.
* ``leanpool.pki``: the pool's certificate authority and the certificates it signs.
* ``leanpool.join``: the service through which a new Lean server box asks to join.
"""

__version__ = "0.1.0"
