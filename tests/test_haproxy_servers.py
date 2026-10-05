"""The server list: parsing, strict validation, and the pure edit functions."""

from __future__ import annotations

import pytest

from leanpool.haproxy import (
    LeanServer,
    ServerListError,
    add_server,
    format_server_line,
    parse_server,
    parse_server_list,
    remove_server,
    total_workers,
)

SERVER_LIST = """\
# The pool's Lean servers.
# name    host:port              workers  agent-port
lean-a    lean-a.example:8000    4        18200

lean-b    192.0.2.20:8000        8        # agent port left out
"""


def test_a_list_is_read_with_comments_blank_lines_and_a_default_agent_port() -> None:
    servers = parse_server_list(SERVER_LIST)
    assert servers == (
        LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4, agent_port=18200),
        LeanServer(name="lean-b", host="192.0.2.20", port=8000, workers=8, agent_port=18200),
    )
    assert total_workers(servers) == 12


def test_an_empty_list_has_no_servers() -> None:
    assert parse_server_list("") == ()
    assert parse_server_list("# nothing yet\n\n") == ()


def test_a_line_round_trips_through_its_written_form() -> None:
    server = LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4, agent_port=9)
    assert format_server_line(server) == "lean-a lean-a.example:8000 4 9"
    assert parse_server_list(format_server_line(server)) == (server,)


def test_host_names_are_compared_without_regard_to_case() -> None:
    assert parse_server("lean-a", "Lean-A.Example:8000", "4").host == "lean-a.example"
    with pytest.raises(ServerListError, match="already in the list"):
        parse_server_list("a LEAN.example:8000 4\nb lean.example:8000 4\n")


@pytest.mark.parametrize(
    "name",
    ["", "-lean", ".lean", "lean a", "lean#1", "lean/a", "léan", "a" * 64, "lean\nserver"],
)
def test_a_malformed_name_is_refused(name: str) -> None:
    with pytest.raises(ServerListError, match="name"):
        parse_server(name, "lean.example:8000", "4")


@pytest.mark.parametrize(
    "address",
    [
        "lean.example",  # no port
        ":8000",  # no host
        "lean_a.example:8000",  # underscore is not allowed in a host name
        "-lean.example:8000",
        "lean..example:8000",
        "lean.example.:8000",
        "lean example:8000",
        "256.1.1.1:8000",
        "1.2.3:8000",
        "0.0.0.0:8000",
        "[::1]:8000",
        "http://lean.example:8000",
        "a" * 254 + ":8000",
    ],
)
def test_a_malformed_host_is_refused(address: str) -> None:
    with pytest.raises(ServerListError, match=r"host|address"):
        parse_server("lean", address, "4")


@pytest.mark.parametrize("port", ["0", "65536", "-1", "80a", "", "08000", "8 000", "+80", "8e3"])
def test_a_malformed_port_is_refused(port: str) -> None:
    with pytest.raises(ServerListError, match="port"):
        parse_server("lean", f"lean.example:{port}", "4")
    with pytest.raises(ServerListError, match="port"):
        parse_server("lean", "lean.example:8000", "4", port)


@pytest.mark.parametrize("workers", ["0", "257", "-4", "four", "4.0", "", "04", "1_0"])
def test_a_malformed_worker_count_is_refused(workers: str) -> None:
    with pytest.raises(ServerListError, match="workers"):
        parse_server("lean", "lean.example:8000", workers)


def test_a_server_built_directly_is_validated_too() -> None:
    with pytest.raises(ServerListError, match="workers"):
        LeanServer(name="lean", host="lean.example", port=8000, workers=0)
    with pytest.raises(ServerListError, match="name"):
        LeanServer(name="lean a", host="lean.example", port=8000, workers=4)
    with pytest.raises(ServerListError, match="host"):
        LeanServer(name="lean", host="lean example", port=8000, workers=4)
    with pytest.raises(ServerListError, match="agent port"):
        LeanServer(name="lean", host="lean.example", port=8000, workers=4, agent_port=70000)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("lean-a lean.example:8000", "got 2 fields"),
        ("lean-a lean.example:8000 4 18200 extra", "got 5 fields"),
        ("lean-a lean.example 4", "HOST:PORT"),
    ],
)
def test_a_malformed_line_is_refused_with_its_line_number(line: str, reason: str) -> None:
    with pytest.raises(ServerListError, match=f"line 3: .*{reason}"):
        parse_server_list(f"# header\n\n{line}\n")


def test_a_duplicate_name_or_address_in_a_list_is_refused() -> None:
    with pytest.raises(ServerListError, match="line 2: the name 'lean-a' is already in the list"):
        parse_server_list("lean-a a.example:8000 4\nlean-a b.example:8000 4\n")
    with pytest.raises(
        ServerListError, match=r"line 2: the address a\.example:8000 is already in the list"
    ):
        parse_server_list("lean-a a.example:8000 4\nlean-b a.example:8000 4\n")


def test_two_servers_on_one_box_may_share_its_agent() -> None:
    servers = parse_server_list("one box.example:8000 4 18200\ntwo box.example:8001 4 18200\n")
    assert [server.agent_port for server in servers] == [18200, 18200]


def test_adding_a_server_appends_one_line_and_keeps_the_rest_untouched() -> None:
    new_server = parse_server("lean-c", "lean-c.example:8000", "16", "18300")
    edited = add_server(SERVER_LIST, new_server)
    assert edited == SERVER_LIST + "lean-c lean-c.example:8000 16 18300\n"


def test_adding_to_a_list_without_a_final_newline_starts_a_new_line() -> None:
    edited = add_server("lean-a a.example:8000 4", parse_server("lean-b", "b.example:8000", "8"))
    assert edited == "lean-a a.example:8000 4\nlean-b b.example:8000 8 18200\n"


def test_adding_the_first_server_to_an_empty_list() -> None:
    assert add_server("", parse_server("lean-a", "a.example:8000", "4")) == (
        "lean-a a.example:8000 4 18200\n"
    )


def test_adding_a_duplicate_name_is_refused() -> None:
    with pytest.raises(ServerListError, match="the name 'lean-a' is already in the list"):
        add_server(SERVER_LIST, parse_server("lean-a", "elsewhere.example:8000", "4"))


def test_adding_a_duplicate_address_is_refused() -> None:
    with pytest.raises(
        ServerListError, match=r"192\.0\.2\.20:8000 is already in the list as 'lean-b'"
    ):
        add_server(SERVER_LIST, parse_server("lean-c", "192.0.2.20:8000", "4"))


def test_adding_to_a_malformed_list_is_refused() -> None:
    with pytest.raises(ServerListError, match="line 1"):
        add_server("this is not a server list\n", parse_server("lean-a", "a.example:8000", "4"))


def test_removing_a_server_deletes_only_its_line() -> None:
    edited = remove_server(SERVER_LIST, "lean-a")
    assert edited == SERVER_LIST.replace("lean-a    lean-a.example:8000    4        18200\n", "")
    assert [server.name for server in parse_server_list(edited)] == ["lean-b"]


def test_removing_an_unknown_server_is_refused() -> None:
    with pytest.raises(ServerListError, match="no server is named 'lean-z'"):
        remove_server(SERVER_LIST, "lean-z")


def test_a_name_mentioned_only_in_a_comment_is_not_a_server() -> None:
    with pytest.raises(ServerListError, match="no server is named 'name'"):
        remove_server(SERVER_LIST, "name")
