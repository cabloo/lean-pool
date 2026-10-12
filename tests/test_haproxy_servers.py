"""The server list: parsing, strict validation, and the pure edit functions."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from leanpool.haproxy import (
    LeanServer,
    ServerListError,
    add_server,
    format_server_line,
    parse_server,
    parse_server_list,
    pause_server,
    remove_server,
    resume_server,
    servers_taking_checks,
    set_server_workers,
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


# --- pausing: a server that stays in the list and is sent nothing ---------------------------------


def test_the_word_paused_as_the_last_field_marks_a_server_with_or_without_an_agent_port() -> None:
    servers = parse_server_list("a a.example:8000 4 18200 paused\nb b.example:8000 8 paused\n")
    assert servers == (
        LeanServer("a", "a.example", 8000, 4, 18200, paused=True),
        LeanServer("b", "b.example", 8000, 8, 18200, paused=True),
    )


def test_a_paused_line_round_trips_through_its_written_form() -> None:
    server = LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4, paused=True)
    assert format_server_line(server) == "lean-a lean-a.example:8000 4 18200 paused"
    assert parse_server_list(format_server_line(server)) == (server,)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("lean-a lean.example:8000 4 18200 Paused", "got 5 fields"),  # the word, in lower case
        ("lean-a lean.example:8000 4 18200 paused paused", "got 6 fields"),
        ("lean-a lean.example:8000 4 paused 18200", "got 5 fields"),  # the last field only
        ("lean-a lean.example:8000 paused", "workers"),
    ],
)
def test_the_mark_is_one_word_in_one_place(line: str, reason: str) -> None:
    with pytest.raises(ServerListError, match=f"line 1: .*{reason}"):
        parse_server_list(f"{line}\n")


def test_a_server_may_be_called_paused() -> None:
    (server,) = parse_server_list("paused paused.example:8000 4\n")
    assert (server.name, server.paused) == ("paused", False)
    assert resume_server(
        pause_server("a a.example:8000 4\npaused p.example:8000 4\n", "paused"), "paused"
    ) == ("a a.example:8000 4\npaused p.example:8000 4\n")


def test_pausing_marks_one_line_and_leaves_every_other_character_as_it_was() -> None:
    edited = pause_server(SERVER_LIST, "lean-b")
    assert edited == SERVER_LIST.replace(
        "lean-b    192.0.2.20:8000        8        # agent port left out",
        "lean-b    192.0.2.20:8000        8 paused        # agent port left out",
    )
    first, second = parse_server_list(edited)
    assert (first.paused, second.paused) == (False, True)
    assert second == LeanServer("lean-b", "192.0.2.20", 8000, 8, 18200, paused=True)


def test_resuming_takes_the_mark_off_and_gives_the_file_it_was() -> None:
    for name in ("lean-a", "lean-b"):
        assert resume_server(pause_server(SERVER_LIST, name), name) == SERVER_LIST


def test_a_list_without_a_final_newline_or_with_other_line_endings_keeps_them() -> None:
    assert pause_server("a a.example:8000 4\nb b.example:8000 8", "b") == (
        "a a.example:8000 4\nb b.example:8000 8 paused"
    )
    windows = "a a.example:8000 4\r\nb b.example:8000 8\r\n"
    assert pause_server(windows, "a") == "a a.example:8000 4 paused\r\nb b.example:8000 8\r\n"
    assert resume_server(pause_server(windows, "a"), "a") == windows


def test_a_paused_server_runs_no_check_so_its_workers_are_not_the_pools() -> None:
    servers = parse_server_list(pause_server(SERVER_LIST, "lean-b"))
    assert [server.name for server in servers_taking_checks(servers)] == ["lean-a"]
    assert total_workers(servers) == 4


@pytest.mark.parametrize(
    ("edit", "name", "reason"),
    [
        (pause_server, "lean-z", "no server is named 'lean-z'"),
        (resume_server, "lean-z", "no server is named 'lean-z'"),
        (pause_server, "name", "no server is named 'name'"),  # a word of a comment
        (resume_server, "lean-a", "'lean-a' is not paused"),
    ],
)
def test_a_pause_or_resume_that_cannot_be_done_is_refused(
    edit: Callable[[str, str], str], name: str, reason: str
) -> None:
    with pytest.raises(ServerListError, match=reason):
        edit(SERVER_LIST, name)


def test_pausing_a_paused_server_is_refused() -> None:
    with pytest.raises(ServerListError, match="'lean-a' is already paused"):
        pause_server(pause_server(SERVER_LIST, "lean-a"), "lean-a")


def test_the_last_server_taking_checks_cannot_be_paused() -> None:
    one_left = pause_server(SERVER_LIST, "lean-a")
    with pytest.raises(ServerListError, match="would leave no server taking checks"):
        pause_server(one_left, "lean-b")
    with pytest.raises(ServerListError, match="would leave no server taking checks"):
        pause_server("only only.example:8000 4\n", "only")


def test_a_paused_servers_name_and_address_are_still_taken() -> None:
    paused = pause_server(SERVER_LIST, "lean-b")
    with pytest.raises(ServerListError, match="the name 'lean-b' is already in the list"):
        add_server(paused, parse_server("lean-b", "elsewhere.example:8000", "4"))
    with pytest.raises(ServerListError, match=r"192\.0\.2\.20:8000 is already in the list"):
        add_server(paused, parse_server("lean-c", "192.0.2.20:8000", "4"))


def test_a_paused_server_is_removed_like_any_other() -> None:
    assert remove_server(pause_server(SERVER_LIST, "lean-b"), "lean-b") == remove_server(
        SERVER_LIST, "lean-b"
    )


# --- the worker count a server is listed with -----------------------------------------------------


def test_a_new_worker_count_changes_one_field_and_leaves_every_other_character() -> None:
    edited = set_server_workers(SERVER_LIST, "lean-b", 6)
    assert edited == SERVER_LIST.replace(
        "lean-b    192.0.2.20:8000        8        # agent port left out",
        "lean-b    192.0.2.20:8000        6        # agent port left out",
    )
    first, second = parse_server_list(edited)
    assert (first.workers, second.workers) == (4, 6)
    assert total_workers(parse_server_list(edited)) == 10


def test_a_new_worker_count_keeps_the_agent_port_the_mark_and_the_line_ending() -> None:
    assert set_server_workers(SERVER_LIST, "lean-a", 13) == SERVER_LIST.replace(
        "lean-a    lean-a.example:8000    4        18200",
        "lean-a    lean-a.example:8000    13        18200",
    )
    paused = pause_server(SERVER_LIST, "lean-b")
    assert set_server_workers(paused, "lean-b", 6) == pause_server(
        set_server_workers(SERVER_LIST, "lean-b", 6), "lean-b"
    )
    windows = "  a a.example:8000 4 9\r\nb b.example:8000 8"
    assert set_server_workers(windows, "a", 12) == "  a a.example:8000 12 9\r\nb b.example:8000 8"
    assert set_server_workers(windows, "b", 2) == "  a a.example:8000 4 9\r\nb b.example:8000 2"


def test_a_server_whose_name_or_address_holds_its_count_keeps_them() -> None:
    assert set_server_workers("8 192.0.2.8:8 8 8\n", "8", 9) == "8 192.0.2.8:8 9 8\n"


@pytest.mark.parametrize(
    ("name", "workers", "reason"),
    [
        ("lean-z", 6, "no server is named 'lean-z'"),
        ("name", 6, "no server is named 'name'"),  # a word of a comment
        ("lean-b", 8, "'lean-b' is already listed with 8 workers"),
        ("lean-b", 0, "workers must be between 1 and 256"),
        ("lean-b", 257, "workers must be between 1 and 256"),
        ("lean-b", -1, "workers must be between 1 and 256"),
    ],
)
def test_a_worker_count_that_cannot_be_set_is_refused(name: str, workers: int, reason: str) -> None:
    with pytest.raises(ServerListError, match=reason):
        set_server_workers(SERVER_LIST, name, workers)


def test_a_malformed_list_is_not_edited() -> None:
    with pytest.raises(ServerListError, match="line 2"):
        set_server_workers("a a.example:8000 4\nb b.example:8000\n", "a", 6)
