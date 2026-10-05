"""An in-process DNS server for the live HAProxy tests: it answers from a table a test edits."""

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass, field

_HEADER_BYTES = 12
_ADDRESS_QUERY = 1
_NO_ERROR = 0x8580
_NAME_ERROR = 0x8583


@dataclass
class FakeDns(asyncio.DatagramProtocol):
    """Answers IPv4 address queries from ``addresses``; any other name does not exist."""

    addresses: dict[str, str] = field(default_factory=dict)
    queried_names: set[str] = field(default_factory=set)
    _transport: asyncio.DatagramTransport | None = None

    async def start(self) -> int:
        """Listen on a free UDP port of loopback and return it."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", 0)
        )
        self._transport = transport
        port: int = transport.get_extra_info("sockname")[1]
        return port

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        name, query_type, question = _read_question(data)
        self.queried_names.add(name)
        address = self.addresses.get(name)
        if address is None:
            reply = _header(data, _NAME_ERROR, answers=0) + question
        elif query_type == _ADDRESS_QUERY:
            reply = _header(data, _NO_ERROR, answers=1) + question + _address_record(address)
        else:
            reply = _header(data, _NO_ERROR, answers=0) + question
        assert self._transport is not None
        self._transport.sendto(reply, addr)


def _read_question(data: bytes) -> tuple[str, int, bytes]:
    """Return the queried name, the query type and the raw question section."""
    position = _HEADER_BYTES
    labels = []
    while data[position]:
        length = data[position]
        labels.append(data[position + 1 : position + 1 + length].decode("ascii"))
        position += 1 + length
    (query_type,) = struct.unpack("!H", data[position + 1 : position + 3])
    return ".".join(labels).lower(), query_type, data[_HEADER_BYTES : position + 5]


def _header(query: bytes, flags: int, answers: int) -> bytes:
    return query[:2] + struct.pack("!HHHHH", flags, 1, answers, 0, 0)


def _address_record(address: str) -> bytes:
    """An A record for the name in the question (a pointer to it), valid for five seconds."""
    name_pointer = b"\xc0\x0c"
    return name_pointer + struct.pack("!HHIH", 1, 1, 5, 4) + socket.inet_aton(address)
