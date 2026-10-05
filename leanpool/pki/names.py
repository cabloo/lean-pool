"""The names a certificate may carry: DNS names and IP addresses, each validated strictly.

Names are the whole point of a certificate, so nothing here guesses: a DNS name is a sequence of
RFC 1123 labels (no wildcard, no underscore), compared in lower case, and an address is whatever
``ipaddress`` accepts. Everything else is refused with a message that says which rule it broke.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from cryptography import x509

from leanpool.names import is_dns_name, looks_numeric

Item = TypeVar("Item")
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# X.509 allows a common name at most this long (RFC 5280, ub-common-name).
MAXIMUM_COMMON_NAME_LENGTH = 64


class PkiError(ValueError):
    """A certificate operation was refused. The message never contains key material."""


@dataclass(frozen=True)
class SubjectNames:
    """The names one certificate is valid for: DNS names in lower case, then IP addresses."""

    dns: tuple[str, ...] = ()
    addresses: tuple[IpAddress, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.dns or self.addresses)

    def general_names(self) -> list[x509.GeneralName]:
        """The names as a certificate's subject alternative names."""
        return [
            *(x509.DNSName(name) for name in self.dns),
            *(x509.IPAddress(address) for address in self.addresses),
        ]

    def describe(self) -> str:
        """The names as one readable list, for messages."""
        return ", ".join([*self.dns, *(str(address) for address in self.addresses)]) or "none"


def parse_dns_name(text: str) -> str:
    """Validate a DNS name and return it in lower case.

    A name of only digits and dots is refused: it is an address (or a typo of one), and a
    certificate that carried it as a DNS name would match nothing.
    """
    if looks_numeric(text) or not is_dns_name(text):
        raise PkiError(
            f"{text!r} is not a DNS name: labels of letters, digits and '-', separated by '.'"
        )
    return text.lower()


def parse_common_name(text: str) -> str:
    """Validate the name a certificate is issued to: a DNS name of at most 64 characters."""
    name = parse_dns_name(text)
    if len(name) > MAXIMUM_COMMON_NAME_LENGTH:
        raise PkiError(
            f"the name {text!r} is longer than {MAXIMUM_COMMON_NAME_LENGTH} characters, the most "
            "a certificate's common name holds"
        )
    return name


def parse_ip_address(text: str) -> IpAddress:
    """Validate an IPv4 or IPv6 address."""
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        raise PkiError(f"{text!r} is not an IP address") from None


def subject_names(dns: Iterable[str] = (), addresses: Iterable[str] = ()) -> SubjectNames:
    """Build the names from text, validating each and dropping repeats (the first one stays)."""
    return SubjectNames(
        dns=_without_repeats([parse_dns_name(name) for name in dns]),
        addresses=_without_repeats([parse_ip_address(address) for address in addresses]),
    )


def _without_repeats(items: Sequence[Item]) -> tuple[Item, ...]:
    return tuple(dict.fromkeys(items))
