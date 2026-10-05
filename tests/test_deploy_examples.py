"""The compose examples in ``deploy/`` and the demo: what can be held to account without Docker.

Docker itself is not run here (the CI workflow validates and starts these files). These tests
keep the files consistent with the project: what they build exists, what they tell the reader
to run is a real command, and the ports that must stay inside a box are not published.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from leanpool.admit.cases import Expectation, load_cases
from leanpool.haproxy import PoolSettings, parse_server_list
from leanpool.haproxy.cli import main as haproxy_main

PROJECT = Path(__file__).parent.parent
POOL = PROJECT / "deploy" / "pool"
LEAN_SERVER = PROJECT / "deploy" / "lean-server"
COMPOSE_FILES = [
    POOL / "compose.yaml",
    POOL / "compose.tls.yaml",
    LEAN_SERVER / "compose.yaml",
    LEAN_SERVER / "compose.tls.yaml",
    PROJECT / "examples" / "demo" / "compose.yaml",
]


def services(path: Path) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = yaml.safe_load(path.read_text())["services"]
    return loaded


def published_ports(service: dict[str, Any]) -> set[int]:
    """The host ports a service publishes (``"18100:18100"``, ``"127.0.0.1:18100:18100"``)."""
    return {int(str(port).split(":")[-2]) for port in service.get("ports", [])}


def commands_in_the_header(path: Path, program: str) -> list[list[str]]:
    """The commands for ``program`` quoted in a file's leading comment, as argument lists."""
    comment = "\n".join(
        line.removeprefix("#") for line in path.read_text().splitlines() if line.startswith("#")
    )
    commands = []
    for candidate in comment.replace("\\\n", " ").splitlines():
        if candidate.split()[:1] == [program]:  # prose is not shell: only split a command
            words = shlex.split(candidate)
            commands.append(words[1 : words.index(">")] if ">" in words else words[1:])
    return commands


def test_every_compose_example_is_listed_here() -> None:
    found = {*PROJECT.glob("deploy/**/compose*.yaml"), *PROJECT.glob("examples/**/compose*.yaml")}

    assert found == set(COMPOSE_FILES)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda path: str(path.relative_to(PROJECT)))
def test_what_a_compose_file_builds_is_this_project(path: Path) -> None:
    for service in services(path).values():
        build = service.get("build")
        if build is not None:
            context = (path.parent / build["context"]).resolve()
            assert context == PROJECT
            assert (context / build["dockerfile"]).is_file()


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda path: str(path.relative_to(PROJECT)))
def test_no_compose_file_publishes_a_port_that_must_stay_inside_the_proxy(path: Path) -> None:
    settings = PoolSettings()
    inside = {
        settings.checkers_port,
        settings.stats_port,
        int(settings.cache_address.rpartition(":")[2]),
    }

    for service in services(path).values():
        assert not published_ports(service) & inside


@pytest.mark.parametrize("path", [POOL / "compose.yaml", PROJECT / "examples/demo/compose.yaml"])
def test_the_cache_shares_the_proxys_network_namespace(path: Path) -> None:
    pool = services(path)

    assert pool["cache"]["network_mode"] == "service:haproxy"
    assert published_ports(pool["haproxy"]) == {PoolSettings().public_port}
    assert "ports" not in pool["cache"]


def test_the_pool_refuses_to_start_without_a_pin_of_the_readers_own() -> None:
    pin = services(POOL / "compose.yaml")["cache"]["environment"]["LEANPOOL_CACHE_PIN"]

    assert pin.startswith("${LEANPOOL_CACHE_PIN:?")


def test_the_example_server_list_is_valid() -> None:
    servers = parse_server_list((POOL / "servers.example").read_text())

    assert [(server.name, server.workers) for server in servers] == [("lean-a", 4), ("lean-b", 8)]


def test_with_tls_a_box_publishes_only_its_front() -> None:
    plain, with_tls = (
        services(LEAN_SERVER / "compose.yaml"),
        services(LEAN_SERVER / "compose.tls.yaml"),
    )

    assert published_ports(plain["kimina"]) | published_ports(plain["agent"]) == {8000, 18200}
    assert published_ports(with_tls["front"]) == {8000, 18200}
    assert "ports" not in with_tls["kimina"]
    assert "ports" not in with_tls["agent"]
    assert with_tls["kimina"]["environment"] == plain["kimina"]["environment"]


def test_the_tls_file_of_the_pool_only_adds_the_certificates() -> None:
    assert services(POOL / "compose.tls.yaml") == {
        "haproxy": {"volumes": ["./pki/proxy:/etc/leanpool/tls:ro"]}
    }


def test_the_render_command_the_pools_tls_file_quotes_is_a_real_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    (command,) = commands_in_the_header(POOL / "compose.tls.yaml", "leanpool-haproxy-config")
    command[command.index("servers")] = str(POOL / "servers.example")

    assert haproxy_main(command, {}) == 0
    config = capsys.readouterr().out
    assert "bind :18100 ssl crt /etc/leanpool/tls/front.pem" in config
    assert "ca-file /etc/leanpool/tls/ca.crt crt /etc/leanpool/tls/proxy-client.pem" in config


def test_the_render_command_the_plain_pool_file_quotes_is_a_real_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    (command,) = commands_in_the_header(POOL / "compose.yaml", "leanpool-haproxy-config")
    command[command.index("servers")] = str(POOL / "servers.example")

    assert haproxy_main(command, {}) == 0
    assert capsys.readouterr().out.startswith("# UNENCRYPTED:")


def test_the_front_the_tls_box_file_tells_the_reader_to_render_matches_its_services(
    capsys: pytest.CaptureFixture[str],
) -> None:
    (command,) = commands_in_the_header(LEAN_SERVER / "compose.tls.yaml", "leanpool-haproxy-config")
    box = services(LEAN_SERVER / "compose.tls.yaml")

    assert haproxy_main(command, {}) == 0
    config = capsys.readouterr().out
    assert "server lean kimina:8000" in config
    assert "server agent agent:18200" in config
    assert {"kimina", "agent"} <= set(box)
    assert "./tls:/etc/leanpool/tls:ro" in box["front"]["volumes"]
    assert "crt /etc/leanpool/tls/box.pem" in config


def test_the_starter_admission_cases_are_a_usable_cases_directory() -> None:
    cases = load_cases(PROJECT / "examples" / "admission-cases")

    assert [(case.name, case.expectation) for case in cases] == [
        ("verify/sum_comm.lean", Expectation.VERIFY),
        ("reject/false_claim.lean", Expectation.REJECT),
        ("reject/uses_sorry.lean", Expectation.REJECT),
    ]
    assert all(case.code.startswith("import Mathlib\n") for case in cases)
