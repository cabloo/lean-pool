"""The compose files in ``deploy/`` and the demo's: what can be held to account without Docker.

Docker itself is not run here (the CI workflow has it read every combination of these files, and starts the
demo). These tests read the files as Compose does (`tests/compose_files.py`) and keep them consistent with the
project: what they build exists, what they tell the reader to run is a real command, the ports that must stay
inside a box are not published, and every setting they read is in the table of `deploy/README.md`.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest

from compose_files import read, resolve
from leanpool.admit.cases import Expectation, load_cases
from leanpool.haproxy import PoolSettings, parse_server_list
from leanpool.haproxy.cli import main as haproxy_main

PROJECT = Path(__file__).parent.parent
POOL = PROJECT / "deploy" / "pool"
LEAN_SERVER = PROJECT / "deploy" / "lean-server"
DEMO = PROJECT / "examples" / "demo"
COMPOSE_FILES = [
    POOL / "compose.yaml",
    POOL / "compose.tls.yaml",
    POOL / "compose.dns.yaml",
    LEAN_SERVER / "compose.yaml",
    LEAN_SERVER / "compose.agent.yaml",
    LEAN_SERVER / "compose.tls.yaml",
    LEAN_SERVER / "compose.compiled-search.yaml",
    DEMO / "compose.yaml",
]
# The settings a project cannot start without, as a reader's .env would give them.
REQUIRED = {
    "LEAN_SERVER_API_KEY": "key",
    "LEAN_SERVER_MAX_REPLS": "4",
    "LEANPOOL_CACHE_PIN": "pin",
    "LEANPOOL_DNS": "192.0.2.53",
}
LEAN_SERVER_PROJECTS = {
    "plain": ["compose.yaml"],
    "with the agent": ["compose.yaml", "compose.agent.yaml"],
    "with TLS": ["compose.yaml", "compose.agent.yaml", "compose.tls.yaml"],
    "with TLS and the compiled step": [
        "compose.yaml",
        "compose.agent.yaml",
        "compose.tls.yaml",
        "compose.compiled-search.yaml",
    ],
}
POOL_PROJECTS = {
    "plain": ["compose.yaml"],
    "with TLS": ["compose.yaml", "compose.tls.yaml"],
    "with TLS and a DNS server": ["compose.yaml", "compose.tls.yaml", "compose.dns.yaml"],
}
PROJECTS = [
    *((LEAN_SERVER, name, files) for name, files in LEAN_SERVER_PROJECTS.items()),
    *((POOL, name, files) for name, files in POOL_PROJECTS.items()),
    (DEMO, "the demo", ["compose.yaml"]),
]
PROJECT_IDS = [f"{directory.name}, {name}" for directory, name, _ in PROJECTS]


def project(directory: Path, files: list[str], **settings: str) -> dict[str, Any]:
    return resolve([directory / name for name in files], {**REQUIRED, **settings})


def services(directory: Path, files: list[str], **settings: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = project(directory, files, **settings)["services"]
    return found


def published_ports(service: dict[str, Any]) -> set[int]:
    """The host ports a service publishes (``"18100:18100"``, ``"0.0.0.0:18100:18100"``)."""
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


def settings_table(heading: str) -> list[str]:
    """The variables in the first column of the table under ``heading`` in deploy/README.md."""
    text = (PROJECT / "deploy" / "README.md").read_text()
    section = text.split(f"### {heading}\n", 1)[1].split("\n#", 1)[0]
    return re.findall(r"^\| `([A-Z_]+)` \|", section, flags=re.MULTILINE)


def test_every_compose_file_is_listed_here() -> None:
    found = {*PROJECT.glob("deploy/**/compose*.yaml"), *PROJECT.glob("examples/**/compose*.yaml")}

    assert found == set(COMPOSE_FILES)


@pytest.mark.parametrize(("directory", "name", "files"), PROJECTS, ids=PROJECT_IDS)
def test_every_project_resolves_and_builds_only_this_project(
    directory: Path, name: str, files: list[str]
) -> None:
    for service in services(directory, files).values():
        build = service.get("build")
        if build is not None:
            context = (directory / build["context"]).resolve()
            assert context == PROJECT
            assert (context / build["dockerfile"]).is_file()


@pytest.mark.parametrize(("directory", "name", "files"), PROJECTS, ids=PROJECT_IDS)
def test_no_project_publishes_a_port_that_must_stay_inside_the_proxy(
    directory: Path, name: str, files: list[str]
) -> None:
    settings = PoolSettings()
    inside = {
        settings.checkers_port,
        settings.stats_port,
        int(settings.cache_address.rpartition(":")[2]),
    }

    for service in services(directory, files).values():
        assert not published_ports(service) & inside


@pytest.mark.parametrize("directory", [POOL, DEMO], ids=["deploy/pool", "the demo"])
def test_the_cache_shares_the_proxys_network_namespace(directory: Path) -> None:
    pool = services(directory, ["compose.yaml"])

    assert pool["cache"]["network_mode"] == "service:haproxy"
    assert published_ports(pool["haproxy"]) == {PoolSettings().public_port}
    assert "ports" not in pool["cache"]


def test_the_pool_refuses_to_start_without_a_pin_of_the_readers_own() -> None:
    pin = read(POOL / "compose.yaml")["services"]["cache"]["environment"]["LEANPOOL_CACHE_PIN"]

    assert pin.startswith("${LEANPOOL_CACHE_PIN:?")


def test_the_pools_key_is_a_secret_the_cache_reads_from_its_file() -> None:
    pool = project(POOL, ["compose.yaml"])

    assert pool["secrets"] == {"pool_api_key": {"file": "./api-key"}}
    assert pool["services"]["cache"]["secrets"] == ["pool_api_key"]
    assert (
        pool["services"]["cache"]["environment"]["LEANPOOL_CACHE_API_KEY_FILE"]
        == "/run/secrets/pool_api_key"
    )


def test_the_example_server_list_is_valid() -> None:
    servers = parse_server_list((POOL / "servers.example").read_text())

    assert [(server.name, server.workers) for server in servers] == [("lean-a", 4), ("lean-b", 8)]


def test_with_tls_a_box_publishes_only_its_front() -> None:
    plain = services(LEAN_SERVER, LEAN_SERVER_PROJECTS["with the agent"])
    with_tls = services(LEAN_SERVER, LEAN_SERVER_PROJECTS["with TLS"])

    assert published_ports(plain["kimina"]) | published_ports(plain["agent"]) == {8000, 18200}
    assert published_ports(with_tls["front"]) == {8000, 18200}
    assert "ports" not in with_tls["kimina"]
    assert "ports" not in with_tls["agent"]
    assert with_tls["kimina"]["environment"] == plain["kimina"]["environment"]


def test_the_published_ports_are_settings_and_the_ports_inside_are_fixed() -> None:
    box = services(
        LEAN_SERVER,
        LEAN_SERVER_PROJECTS["with TLS"],
        LEAN_SERVER_PUBLISHED_PORT="18001",
        LEAN_SERVER_AGENT_PORT="18201",
    )

    assert box["front"]["ports"] == ["0.0.0.0:18001:8000", "0.0.0.0:18201:18200"]


def test_the_compiled_step_changes_only_how_a_worker_starts() -> None:
    without = services(LEAN_SERVER, LEAN_SERVER_PROJECTS["with TLS"])
    with_it = services(LEAN_SERVER, LEAN_SERVER_PROJECTS["with TLS and the compiled step"])

    added = {
        key: value
        for key, value in with_it["kimina"]["environment"].items()
        if without["kimina"]["environment"].get(key) != value
    }
    assert added == {"LEAN_SERVER_REPL_PATH": "/opt/lean-native/repl-native"}
    (mount,) = with_it["kimina"]["volumes"]
    assert mount["target"] == "/opt/lean-native"
    assert mount["read_only"] is True
    rest = {key: value for key, value in with_it["kimina"].items() if key not in ("environment", "volumes")}
    assert rest == {key: value for key, value in without["kimina"].items() if key != "environment"}


def test_the_tls_file_of_the_pool_only_adds_the_certificates() -> None:
    assert read(POOL / "compose.tls.yaml")["services"] == {
        "haproxy": {"volumes": ["./tls:/etc/leanpool/tls:ro"]}
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
    box = services(LEAN_SERVER, LEAN_SERVER_PROJECTS["with TLS"])

    assert haproxy_main(command, {}) == 0
    config = capsys.readouterr().out
    assert "server lean kimina:8000" in config
    assert "server agent agent:18200" in config
    assert {"kimina", "agent", "front"} <= set(box)
    assert "./tls:/etc/leanpool/tls:ro" in box["front"]["volumes"]
    assert "crt /etc/leanpool/tls/box.pem" in config


def test_every_setting_the_compose_files_read_is_in_the_table_and_nothing_else() -> None:
    read_here: set[str] = set()
    for path in COMPOSE_FILES:
        if path.parent != DEMO:  # the demo's file is its own
            read_here |= set(re.findall(r"\$\{([A-Z_]+)", path.read_text()))

    assert sorted(read_here) == sorted(settings_table("What the compose files read"))


def test_a_setting_is_in_one_table_only() -> None:
    kept = settings_table("What the installer's programs also keep")

    assert set(settings_table("What the compose files read")) & set(kept) == set()
    assert len(kept) == len(set(kept))


def test_the_starter_admission_cases_are_a_usable_cases_directory() -> None:
    cases = load_cases(PROJECT / "examples" / "admission-cases")

    assert [(case.name, case.expectation) for case in cases] == [
        ("verify/sum_comm.lean", Expectation.VERIFY),
        ("reject/false_claim.lean", Expectation.REJECT),
        ("reject/uses_sorry.lean", Expectation.REJECT),
    ]
    assert all(case.code.startswith("import Mathlib\n") for case in cases)
