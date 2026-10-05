"""The cache's pure rules: the key, what may be stored, and how a request is read."""

from __future__ import annotations

from typing import Any

import pytest
from fake_lean_server import lean_answer, lean_timeout, repl_error

from leanpool.cache import DEFAULT_EXHAUSTION_PATTERNS, check_key, is_storable
from leanpool.cache.request import InvalidRequestError, parse_check_request

CODE = "theorem two : 1 + 1 = 2 := by rfl"


def test_the_key_is_a_sha256_that_is_stable_across_runs() -> None:
    """The value is pinned: a change to how keys are derived silently empties every store."""
    key = check_key("pin", CODE, 60)
    assert len(key) == 32
    assert key.hex() == "a589c2fa8e49c7c06ec37674c0399e3bd79a6fd66e2fe65b42b38fd07e1c1a6b"


def test_a_different_pin_code_or_timeout_is_a_different_key() -> None:
    key = check_key("pin", CODE, 60)
    assert check_key("other pin", CODE, 60) != key
    assert check_key("pin", CODE + " ", 60) != key
    assert check_key("pin", CODE, 61) != key
    assert check_key("pin", CODE, None) != key


def test_no_pin_and_code_can_imitate_another_pin_and_code() -> None:
    assert check_key("a", "b,c", 1) != check_key("a,b", "c", 1)
    assert check_key('a"', "b", 1) != check_key("a", '"b', 1)


def test_code_that_is_not_valid_unicode_still_has_a_key() -> None:
    assert len(check_key("pin", "lone surrogate \ud800", 60)) == 32


def test_a_definitive_lean_answer_is_storable_whether_it_proved_or_failed() -> None:
    assert is_storable(lean_answer(), DEFAULT_EXHAUSTION_PATTERNS)
    failed = lean_answer(("error", "unsolved goals\n⊢ False"), ("warning", "unused variable"))
    assert is_storable(failed, DEFAULT_EXHAUSTION_PATTERNS)
    assert is_storable(lean_answer(sorries=1), DEFAULT_EXHAUSTION_PATTERNS)


def test_a_lean_error_about_heartbeats_or_recursion_depth_is_deterministic_and_storable() -> None:
    heartbeats = lean_answer(("error", "(deterministic) timeout at `whnf`, maximum heartbeats"))
    recursion = lean_answer(("error", "maximum recursion depth has been reached"))
    assert is_storable(heartbeats, DEFAULT_EXHAUSTION_PATTERNS)
    assert is_storable(recursion, DEFAULT_EXHAUSTION_PATTERNS)


@pytest.mark.parametrize(
    "result",
    [
        lean_timeout(),
        {"time": 60, "error": "Lean REPL header command timed out in 60 seconds"},
        {"time": 0.1, "error": "server_error: the worker died"},
        {"time": 0.1},
        {"time": 0.1, "response": None},
        {"time": 0.1, "response": "not an object"},
        repl_error(),
        {"time": 0.1, "response": {"messages": "not a list"}},
        {"time": 0.1, "response": {"messages": ["not an object"]}},
        {"time": 0.1, "error": "", "response": {"messages": []}},
    ],
)
def test_anything_but_a_definitive_lean_answer_is_never_storable(result: dict[str, Any]) -> None:
    assert not is_storable(result, DEFAULT_EXHAUSTION_PATTERNS)


@pytest.mark.parametrize(
    "text",
    [
        "INTERNAL PANIC: out of memory",
        "Out Of Memory",
        "Stack overflow detected. Aborting.",
        "error: stack overflow",
    ],
)
@pytest.mark.parametrize("severity", ["error", "warning", "info"])
def test_a_message_reporting_resource_exhaustion_is_never_storable(
    text: str, severity: str
) -> None:
    assert not is_storable(lean_answer((severity, text)), DEFAULT_EXHAUSTION_PATTERNS)


def test_the_exhaustion_patterns_are_configurable() -> None:
    killed = lean_answer(("error", "process was Killed by the supervisor"))
    assert is_storable(killed, DEFAULT_EXHAUSTION_PATTERNS)
    assert not is_storable(killed, ["killed by"])
    assert is_storable(lean_answer(("error", "out of memory")), [])


def test_a_request_is_read_with_its_snippets_in_order() -> None:
    request = parse_check_request(
        {
            "snippets": [{"id": "b", "code": "second"}, {"id": "a", "code": "first"}],
            "timeout": 60,
            "debug": True,
            "reuse": False,
            "future_option": {"kept": True},
        }
    )
    assert [(snippet.identifier, snippet.code) for snippet in request.snippets] == [
        ("b", "second"),
        ("a", "first"),
    ]
    assert (request.timeout, request.debug, request.bypass_cache) == (60, True, False)
    assert request.forwarded_fields == {
        "timeout": 60,
        "debug": True,
        "reuse": False,
        "future_option": {"kept": True},
    }


def test_defaults_are_the_servers_timeout_no_debug_and_caching() -> None:
    request = parse_check_request({"snippets": [{"id": "a", "code": "x"}]})
    assert (request.timeout, request.debug, request.bypass_cache) == (None, False, False)


def test_no_cache_bypasses_and_is_not_forwarded() -> None:
    request = parse_check_request({"snippets": [{"id": "a", "code": "x"}], "no_cache": True})
    assert request.bypass_cache
    assert "no_cache" not in request.forwarded_fields


def test_an_infotree_request_bypasses_because_the_key_does_not_cover_it() -> None:
    body = {"snippets": [{"id": "a", "code": "x"}], "infotree": "full"}
    assert parse_check_request(body).bypass_cache
    assert not parse_check_request({**body, "infotree": None}).bypass_cache


def test_a_whole_float_timeout_is_the_same_timeout() -> None:
    request = parse_check_request({"snippets": [{"id": "a", "code": "x"}], "timeout": 60.0})
    assert request.timeout == 60
    assert isinstance(request.timeout, int)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([], "JSON object"),
        ("snippets", "JSON object"),
        ({}, "non-empty list"),
        ({"snippets": []}, "non-empty list"),
        ({"snippets": "x"}, "non-empty list"),
        ({"snippets": ["x"]}, "object with `id` and `code`"),
        ({"snippets": [{"id": "a"}]}, "string `id` and a string `code`"),
        ({"snippets": [{"id": 1, "code": "x"}]}, "string `id` and a string `code`"),
        ({"snippets": [{"id": "a", "code": "x"}, {"id": "a", "code": "y"}]}, "unique ids"),
        ({"snippets": [{"id": "a", "code": "x"}], "timeout": -1}, "timeout"),
        ({"snippets": [{"id": "a", "code": "x"}], "timeout": 1.5}, "timeout"),
        ({"snippets": [{"id": "a", "code": "x"}], "timeout": "60"}, "timeout"),
        ({"snippets": [{"id": "a", "code": "x"}], "timeout": True}, "timeout"),
        ({"snippets": [{"id": "a", "code": "x"}], "timeout": float("inf")}, "timeout"),
        ({"snippets": [{"id": "a", "code": "x"}], "no_cache": "yes"}, "no_cache"),
        ({"snippets": [{"id": "a", "code": "x"}], "debug": 1}, "debug"),
    ],
)
def test_a_malformed_request_is_refused(body: object, reason: str) -> None:
    with pytest.raises(InvalidRequestError, match=reason):
        parse_check_request(body)
