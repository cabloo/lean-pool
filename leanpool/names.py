"""What counts as a DNS name here: one rule for the server list, the certificates and the proxy.

A name is a sequence of labels separated by dots. Each label is letters, digits and hyphens,
starts and ends with a letter or digit, and is at most 63 characters (RFC 1123); the whole name
is at most 253. There is no underscore, no wildcard and no trailing dot. Standard library only.
"""

from __future__ import annotations

import re

MAXIMUM_DNS_NAME_LENGTH = 253
_LABEL_PATTERN = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_NUMERIC_PATTERN = re.compile(r"[0-9.]+")


def is_dns_name(text: str) -> bool:
    """Whether ``text`` is a sequence of RFC 1123 labels of at most 253 characters."""
    return len(text) <= MAXIMUM_DNS_NAME_LENGTH and all(
        _LABEL_PATTERN.fullmatch(label) for label in text.split(".")
    )


def looks_numeric(text: str) -> bool:
    """Whether ``text`` is only digits and dots: an address (or a typo of one), never a name."""
    return _NUMERIC_PATTERN.fullmatch(text) is not None
