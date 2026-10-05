"""Run the server list and configuration tool with ``python -m leanpool.haproxy``."""

import sys

from leanpool.haproxy.cli import main

sys.exit(main())
