"""The generated haproxy.cfg for servers of 4 and 8 workers."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from leanpool.haproxy import (
    LeanServer,
    PoolSettings,
    PoolSettingsError,
    derive_timeouts,
    render_haproxy_config,
    server_weights,
    worst_case_buffer_bytes,
)
from leanpool.haproxy.config import DNS_FAILURE_HOLD_DAYS

SERVERS = (
    LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4),
    LeanServer(name="lean-b", host="lean-b.example", port=8000, workers=8, agent_port=18250),
)


def section(config: str, title: str) -> list[str]:
    """The directives of one section, without comments."""
    blocks = [block.splitlines() for block in config.split("\n\n")]
    (block,) = [block for block in blocks if block[0] == title]
    return [line.strip() for line in block[1:] if not line.strip().startswith("#")]


@pytest.fixture
def config() -> str:
    return render_haproxy_config(SERVERS)


def test_the_public_frontend_answers_health_itself_from_the_checkers_that_are_up(
    config: str,
) -> None:
    frontend = section(config, "frontend lean_pool")
    assert "bind :18100" in frontend
    assert "acl checkers_up nbsrv(checkers) gt 0" in frontend
    health_rules = [line for line in frontend if " is_health" in line and "return" in line]
    assert len(health_rules) == 2
    assert health_rules[0].startswith("http-request return status 200 ")
    assert health_rules[0].endswith("if METH_GET is_health checkers_up")
    assert health_rules[1].startswith("http-request return status 503 ")
    assert health_rules[1].endswith("if METH_GET is_health")


def test_health_and_every_answer_carry_the_workers_up_the_queue_and_the_servers_up(
    config: str,
) -> None:
    """The pool's size: summed per listed server, once for /health's body and once per answer."""
    frontend = section(config, "frontend lean_pool")
    for rule, variable in (
        ("http-request", "txn.pool_workers"),
        ("http-after-response", "res.pool_workers"),
    ):
        total = [line for line in frontend if line.startswith(f"{rule} set-var({variable}) ")]
        assert total == [
            f"{rule} set-var({variable}) int(0)",
            f"{rule} set-var({variable}) srv_is_up(checkers/lean-a),mul(4),add({variable})",
            f"{rule} set-var({variable}) srv_is_up(checkers/lean-b),mul(8),add({variable})",
        ]
    numbers = (
        '"workers":%[var(txn.pool_workers)],'
        '"queued":%[queue(checkers)],"servers":%[nbsrv(checkers)]}'
    )
    health_rules = [line for line in frontend if " is_health" in line and "return" in line]
    assert all(' lf-string \'{"status":' in line and numbers in line for line in health_rules)
    # The sum is made before the reply that uses it.
    assert frontend.index("http-request set-var(txn.pool_workers) int(0)") < frontend.index(
        health_rules[0]
    )
    assert [line for line in frontend if line.startswith("http-after-response set-header ")] == [
        "http-after-response set-header X-Lean-Pool-Workers %[var(res.pool_workers)]",
        "http-after-response set-header X-Lean-Pool-Queued %[queue(checkers)]",
        "http-after-response set-header X-Lean-Pool-Servers %[nbsrv(checkers)]",
    ]


def test_nothing_in_the_pool_reads_its_own_numbers_back(config: str) -> None:
    """Advisory by construction: the variables and headers appear in no routing or queueing rule."""
    lines = [line.strip() for line in config.splitlines() if not line.strip().startswith("#")]
    for line in lines:
        if "pool_workers" in line or "X-Lean-Pool-" in line.replace("X-Lean-Pool-Hop", ""):
            assert line.startswith(
                ("http-request set-var(", "http-after-response set-", "http-request return status ")
            ), line


def test_a_background_check_is_taken_after_every_normal_check_that_is_waiting(config: str) -> None:
    """Priority: HAProxy's priority class, in the backend whose queue it is, for one value."""
    checkers = section(config, "backend checkers")
    assert (
        "http-request set-priority-class int(100) "
        "if { req.hdr(X-Lean-Priority) -i -m str background }"
    ) in checkers
    assert sum("set-priority-class" in line for line in config.splitlines()) == 1


def test_checks_go_to_the_cache_first_and_to_the_checkers_when_the_cache_is_down(
    config: str,
) -> None:
    frontend = section(config, "frontend lean_pool")
    assert frontend[-1] == "default_backend cache"
    assert "use_backend checkers if !cache_up" in frontend
    assert any(
        line.startswith("server cache 127.0.0.1:18102 check inter 1s fall 2 rise 2 ")
        for line in section(config, "backend cache")
    )


def test_status_is_the_caches_and_is_refused_while_the_cache_is_down(config: str) -> None:
    """Without this rule the cache's backup, a Lean server, would be asked for ``/status``."""
    frontend = section(config, "frontend lean_pool")
    refusal = (
        "http-request return status 503 content-type application/json string "
        """'{"status":"the cache is down"}' if is_cache_status !cache_up"""
    )
    assert frontend.index(refusal) < frontend.index("use_backend cache if is_cache_status")


def test_cache_down_means_the_cache_server_itself_not_its_backup(config: str) -> None:
    """``nbsrv(cache)`` counts a usable backup, so with one it would never report the cache down."""
    frontend = section(config, "frontend lean_pool")
    assert "acl cache_up srv_is_up(cache/cache)" in frontend
    assert not any("nbsrv(cache)" in line for line in frontend)


def test_the_cache_forwards_through_a_loopback_listener_that_marks_the_second_hop(
    config: str,
) -> None:
    door = section(config, "frontend checkers_door")
    assert door == [
        "bind 127.0.0.1:18101",
        "http-request set-header X-Lean-Pool-Hop checkers",
        "default_backend checkers",
    ]


def test_a_request_marked_as_second_hop_is_never_routed_to_the_cache(config: str) -> None:
    frontend = section(config, "frontend lean_pool")
    assert "acl is_second_hop req.hdr(X-Lean-Pool-Hop) -m found" in frontend
    guard = frontend.index("use_backend checkers if is_second_hop")
    assert guard < frontend.index("default_backend cache")


def test_only_checks_are_sent_to_the_cache(config: str) -> None:
    frontend = section(config, "frontend lean_pool")
    assert "acl is_check path /api/check /api/check/" in frontend
    assert "use_backend checkers if !is_check" in frontend


def test_checkers_are_balanced_by_least_connections_and_never_overfilled(config: str) -> None:
    checkers = section(config, "backend checkers")
    assert "balance leastconn" in checkers
    servers = [line for line in checkers if line.startswith("server ")]
    assert servers == [
        "server lean-a lean-a.example:8000 check maxconn 4 weight 128 "
        "agent-check agent-port 18200 agent-inter 5s",
        "server lean-b lean-b.example:8000 check maxconn 8 weight 256 "
        "agent-check agent-port 18250 agent-inter 5s",
    ]


def test_weights_are_proportional_to_workers() -> None:
    weights = server_weights(SERVERS)
    assert weights["lean-b"] / weights["lean-a"] == 8 / 4


@pytest.mark.parametrize("workers", [1, 4, 8, 16, 64, 256])
def test_a_report_of_one_percent_never_takes_the_largest_server_out_of_rotation(
    workers: int,
) -> None:
    """HAProxy sets weight = configured weight * percent / 100 in whole numbers; 0 is 'drained'."""
    server = LeanServer(name="lean", host="lean.example", port=8000, workers=workers)
    weight = server_weights([server])["lean"]
    assert weight <= 256
    assert weight * 1 // 100 >= 1


def test_each_checker_is_health_checked(config: str) -> None:
    assert "option httpchk GET /health" in section(config, "backend checkers")
    assert "option httpchk GET /health" in section(config, "backend cache")


def retried_events(config: str, backend: str) -> list[str]:
    (rule,) = [line for line in section(config, backend) if line.startswith("retry-on ")]
    return rule.split()[1:]


def test_failed_checks_are_retried_on_a_different_server(config: str) -> None:
    checkers = section(config, "backend checkers")
    assert "retry-on conn-failure empty-response 500 502 503 504" in checkers
    assert "option redispatch 1" in checkers


def test_a_crashed_worker_gets_another_server_but_only_twice(config: str) -> None:
    """Kimina answers 500 when a worker crashed; a proof that crashes every worker is bounded."""
    assert "500" in retried_events(config, "backend checkers")
    assert "retries 2" in section(config, "backend checkers")


def test_a_lean_timeout_is_never_retried(config: str) -> None:
    """A Lean timeout is an HTTP 200, and HAProxy's own timeout is ``response-timeout``."""
    for backend in ("backend checkers", "backend cache"):
        events = retried_events(config, backend)
        assert "200" not in events
        assert "response-timeout" not in events
        assert "all-retryable-errors" not in events


def test_the_cache_hop_never_retries_on_a_status(config: str) -> None:
    """A status retry on the cache hop would repeat a check the checkers already retried."""
    assert retried_events(config, "backend cache") == ["conn-failure", "empty-response"]
    assert not any("retry-on" in line for line in section(config, "defaults"))
    assert config.count("retry-on ") == 2


def test_buffers_hold_a_long_proof_so_it_can_be_replayed(config: str) -> None:
    global_section = section(config, "global")
    assert "tune.bufsize 262144" in global_section
    assert "maxconn 1024" in global_section


def test_requests_are_received_whole_before_a_server_is_chosen(config: str) -> None:
    """HAProxy keeps a copy for a replay only of a request it had fully received."""
    assert "option http-buffer-request" in section(config, "defaults")


def test_the_worst_case_buffer_memory_is_stated_with_its_arithmetic(config: str) -> None:
    assert worst_case_buffer_bytes(PoolSettings()) == 1024 * 3 * 262144 == 768 * 1024**2
    comment = " ".join(line.strip("# ") for line in config.splitlines() if line.startswith("    #"))
    assert "1024 connections x 3 buffers" in comment
    assert "x 262144 bytes = 768 MiB" in comment


def test_the_buffer_size_and_connection_limit_are_configurable() -> None:
    settings = PoolSettings(maximum_request_bytes=32768, maximum_connections=2048)
    config = render_haproxy_config(SERVERS, settings)
    assert "tune.bufsize 32768" in section(config, "global")
    assert "maxconn 2048" in section(config, "global")
    assert "# 2048 connections x 3 buffers" in config
    assert "x 32768 bytes = 192 MiB" in config


def test_server_names_are_looked_up_again_while_the_proxy_runs(config: str) -> None:
    resolvers = section(config, "resolvers pool_dns")
    assert resolvers == [
        "parse-resolv-conf",
        "timeout resolve 10s",
        "timeout retry 1s",
        "resolve_retries 3",
        "hold nx 24d",
        "hold refused 24d",
        "hold timeout 24d",
        "hold other 24d",
    ]


def test_a_dns_failure_is_held_for_less_than_haproxys_longest_period() -> None:
    assert DNS_FAILURE_HOLD_DAYS * 24 * 60 * 60 * 1000 <= 2147483647


@pytest.mark.parametrize("backend", ["backend checkers", "backend cache"])
def test_a_name_that_does_not_resolve_does_not_stop_the_proxy(config: str, backend: str) -> None:
    lines = section(config, backend)
    default_server = (
        "default-server init-addr last,libc,none resolvers pool_dns resolve-prefer ipv4"
    )
    assert default_server in lines
    first_server = min(index for index, line in enumerate(lines) if line.startswith("server "))
    # default-server applies only to the servers declared after it.
    assert lines.index(default_server) < first_server


def test_a_server_given_as_an_ipv4_address_is_declared_as_before() -> None:
    servers = [LeanServer(name="lean-a", host="192.0.2.10", port=8000, workers=4)]
    checkers = section(render_haproxy_config(servers), "backend checkers")
    assert checkers[-1] == (
        "server lean-a 192.0.2.10:8000 check maxconn 4 weight 256 "
        "agent-check agent-port 18200 agent-inter 5s"
    )


def test_the_first_failed_connection_marks_the_cache_down(config: str) -> None:
    (cache_server,) = [
        line for line in section(config, "backend cache") if line.startswith("server cache ")
    ]
    assert cache_server == (
        "server cache 127.0.0.1:18102 check inter 1s fall 2 rise 2 "
        "observe layer4 error-limit 1 on-error mark-down"
    )


def test_a_request_that_met_the_dead_cache_is_replayed_through_the_backup(config: str) -> None:
    cache = section(config, "backend cache")
    assert "retry-on conn-failure empty-response" in cache
    assert "option redispatch" in cache
    assert "retries 3" in cache
    assert cache[-1] == "server checkers_door 127.0.0.1:18101 backup"


def test_the_backup_path_enters_through_the_door_that_sets_the_loop_guard_header() -> None:
    settings = PoolSettings(checkers_port=9001, hop_header="X-Second-Hop")
    config = render_haproxy_config(SERVERS, settings)
    door = section(config, "frontend checkers_door")
    (backup,) = [line for line in section(config, "backend cache") if line.endswith(" backup")]
    door_address = door[0].removeprefix("bind ")
    assert backup == f"server checkers_door {door_address} backup"
    assert door[1:] == ["http-request set-header X-Second-Hop checkers", "default_backend checkers"]


def test_only_the_cache_server_is_backed_up(config: str) -> None:
    directives = [line.strip() for line in config.splitlines() if not line.strip().startswith("#")]
    assert [line for line in directives if line.endswith(" backup")] == [
        "server checkers_door 127.0.0.1:18101 backup"
    ]


def test_the_cache_server_has_no_connection_cap(config: str) -> None:
    assert not any("maxconn" in line for line in section(config, "backend cache"))


def test_timeouts_are_derived_from_the_lean_timeout(config: str) -> None:
    defaults = section(config, "defaults")
    # 60s Lean timeout: a check may take 60 (wait) + 2 * 60 (header, body) + 30 = 210s on a
    # server, wait 2 * 60 + 30 = 150s in the queue, and the cache hop covers both plus 30s.
    assert "timeout server 210s" in defaults
    assert "timeout queue 150s" in defaults
    assert "timeout client 390s" in defaults
    assert "timeout server 390s" in section(config, "backend cache")


@pytest.mark.parametrize("lean_timeout", [1, 30, 60, 300, 1800])
def test_no_timeout_can_cut_a_legitimate_slow_check(lean_timeout: int) -> None:
    settings = PoolSettings(lean_timeout_seconds=lean_timeout)
    timeouts = derive_timeouts(settings)
    slowest_check = settings.server_wait_seconds + 2 * lean_timeout
    assert timeouts.queue_seconds > lean_timeout
    assert timeouts.checker_seconds > slowest_check
    assert timeouts.cache_seconds > timeouts.queue_seconds + timeouts.checker_seconds


def test_the_configuration_states_how_long_the_cache_must_wait(config: str) -> None:
    assert "# The cache must wait at least 360s for the checkers" in config


def test_an_explicit_queue_timeout_is_used() -> None:
    config = render_haproxy_config(SERVERS, PoolSettings(queue_timeout_seconds=900))
    assert "timeout queue 900s" in section(config, "defaults")


def test_statistics_and_the_checkers_door_listen_on_loopback_only(config: str) -> None:
    assert section(config, "listen stats")[0] == "bind 127.0.0.1:18103"
    binds = [line.strip() for line in config.splitlines() if line.strip().startswith("bind ")]
    assert binds == ["bind :18100", "bind 127.0.0.1:18101", "bind 127.0.0.1:18103"]


def test_ports_and_the_hop_header_are_configurable() -> None:
    settings = PoolSettings(
        public_port=9000,
        checkers_port=9001,
        cache_address="cache.example:9002",
        stats_port=9003,
        hop_header="X-Second-Hop",
    )
    config = render_haproxy_config(SERVERS, settings)
    assert "bind :9000" in section(config, "frontend lean_pool")
    assert "bind 127.0.0.1:9001" in section(config, "frontend checkers_door")
    assert "bind 127.0.0.1:9003" in section(config, "listen stats")
    assert any(
        line.startswith("server cache cache.example:9002 ")
        for line in section(config, "backend cache")
    )
    assert "X-Lean-Pool-Hop" not in config
    assert "http-request set-header X-Second-Hop checkers" in config


def test_the_same_input_renders_the_same_configuration() -> None:
    assert render_haproxy_config(SERVERS) == render_haproxy_config(SERVERS)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"public_port": 18101}, "must differ"),
        ({"stats_port": 0}, "outside 1-65535"),
        ({"cache_address": "no-port"}, "HOST:PORT"),
        ({"cache_address": "bad host:1"}, "host"),
        ({"hop_header": "X Hop"}, "hop header"),
        ({"hop_header": "X-Hop\n    server evil 192.0.2.1:1"}, "hop header"),
        ({"lean_timeout_seconds": 0}, "Lean timeout"),
        ({"margin_seconds": 0}, "margin"),
        ({"server_wait_seconds": -1}, "server wait"),
        ({"queue_timeout_seconds": 60}, "above the Lean timeout"),
        ({"maximum_connections": 0}, "maximum connections"),
        ({"maximum_request_bytes": 16383}, "HAProxy's default buffer size, 16384"),
    ],
)
def test_unusable_settings_are_refused(change: dict[str, Any], reason: str) -> None:
    settings = dataclasses.replace(PoolSettings(), **change)
    with pytest.raises(PoolSettingsError, match=reason):
        render_haproxy_config(SERVERS, settings)


def test_an_empty_server_list_is_refused() -> None:
    with pytest.raises(PoolSettingsError, match="empty"):
        render_haproxy_config([])


def test_duplicate_servers_are_refused() -> None:
    same_name = LeanServer(name="lean-a", host="other.example", port=8000, workers=4)
    same_address = LeanServer(name="lean-c", host="lean-a.example", port=8000, workers=4)
    with pytest.raises(PoolSettingsError, match="'lean-a' is already in the list"):
        render_haproxy_config([*SERVERS, same_name])
    with pytest.raises(PoolSettingsError, match=r"lean-a\.example:8000 is already in the list"):
        render_haproxy_config([*SERVERS, same_address])


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="HAProxy is not installed")
@pytest.mark.parametrize(
    ("first_host", "second_host", "cache_address"),
    [
        ("192.0.2.10", "192.0.2.20", "127.0.0.1:18102"),
        # ".invalid" never resolves: HAProxy must accept the configuration all the same.
        ("lean-a.invalid", "192.0.2.20", "cache.invalid:18102"),
    ],
    ids=["addresses", "names-that-do-not-resolve"],
)
def test_haproxy_accepts_the_generated_configuration(
    tmp_path: Path, first_host: str, second_host: str, cache_address: str
) -> None:
    servers = [
        LeanServer(name="lean-a", host=first_host, port=8000, workers=4),
        LeanServer(name="lean-b", host=second_host, port=8000, workers=8),
    ]
    path = tmp_path / "haproxy.cfg"
    path.write_text(render_haproxy_config(servers, PoolSettings(cache_address=cache_address)))
    result = subprocess.run(
        ["haproxy", "-c", "-f", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
