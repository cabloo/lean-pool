"""``leanpool-haproxy-config``: add, remove, list and render, and what a refusal leaves behind."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from leanpool.haproxy.cli import main
from leanpool.haproxy.files import write_file_atomically

ORIGINAL = """\
# The pool's Lean servers.
lean-a    lean-a.example:8000    4    18200
"""


@pytest.fixture
def servers_file(tmp_path: Path) -> Path:
    path = tmp_path / "servers"
    path.write_text(ORIGINAL)
    return path


def run(*arguments: str, environment: dict[str, str] | None = None) -> int:
    return main(list(arguments), environment or {})


def test_add_creates_the_list_and_appends_to_it(tmp_path: Path) -> None:
    path = tmp_path / "servers"
    assert run("add", "--servers", str(path), "lean-a", "lean-a.example:8000", "4") == 0
    assert (
        run("add", "--servers", str(path), "lean-b", "192.0.2.20:8000", "8", "--agent-port", "9")
        == 0
    )
    assert path.read_text() == "lean-a lean-a.example:8000 4 18200\nlean-b 192.0.2.20:8000 8 9\n"


def test_add_keeps_comments_and_existing_lines(servers_file: Path) -> None:
    assert run("add", "--servers", str(servers_file), "lean-b", "lean-b.example:8000", "8") == 0
    assert servers_file.read_text() == ORIGINAL + "lean-b lean-b.example:8000 8 18200\n"


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (("lean-a", "elsewhere.example:8000", "4"), "the name 'lean-a' is already in the list"),
        (("lean-b", "lean-a.example:8000", "4"), "lean-a.example:8000 is already in the list"),
        (("lean b", "lean-b.example:8000", "4"), "name"),
        (("lean-b", "lean_b.example:8000", "4"), "host"),
        (("lean-b", "lean-b.example:0", "4"), "port"),
        (("lean-b", "lean-b.example:8000", "0"), "workers"),
        (("lean-b", "lean-b.example:8000", "many"), "workers"),
        (("lean-b", "lean-b.example:8000", "4", "--agent-port", "99999"), "agent port"),
    ],
)
def test_a_refused_add_leaves_the_file_byte_identical(
    servers_file: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    reason: str,
) -> None:
    before = servers_file.read_bytes()
    assert run("add", "--servers", str(servers_file), *arguments) == 1
    assert servers_file.read_bytes() == before
    assert reason in capsys.readouterr().err
    assert os.listdir(servers_file.parent) == [servers_file.name]


def test_add_refuses_to_edit_a_list_it_cannot_parse(tmp_path: Path) -> None:
    path = tmp_path / "servers"
    path.write_text("not a server list\n")
    assert run("add", "--servers", str(path), "lean-a", "lean-a.example:8000", "4") == 1
    assert path.read_text() == "not a server list\n"


def test_remove_deletes_the_server(servers_file: Path) -> None:
    assert run("remove", "--servers", str(servers_file), "lean-a") == 0
    assert servers_file.read_text() == "# The pool's Lean servers.\n"


def test_a_refused_remove_leaves_the_file_byte_identical(servers_file: Path) -> None:
    before = servers_file.read_bytes()
    assert run("remove", "--servers", str(servers_file), "lean-z") == 1
    assert servers_file.read_bytes() == before


def test_remove_does_not_create_a_missing_list(tmp_path: Path) -> None:
    path = tmp_path / "servers"
    assert run("remove", "--servers", str(path), "lean-a") == 1
    assert not path.exists()


def test_an_edit_keeps_the_files_permissions(servers_file: Path) -> None:
    servers_file.chmod(0o640)
    assert run("add", "--servers", str(servers_file), "lean-b", "lean-b.example:8000", "8") == 0
    assert stat.S_IMODE(servers_file.stat().st_mode) == 0o640


def test_a_write_that_fails_part_way_leaves_the_original_and_no_temporary_file(
    servers_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_source: object, _target: object) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="the disk went away"):
        write_file_atomically(servers_file, "replacement\n")
    assert servers_file.read_text() == ORIGINAL
    assert os.listdir(servers_file.parent) == [servers_file.name]


def test_the_cli_reports_a_failed_write_as_a_refusal(
    servers_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_source: object, _target: object) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(os, "replace", fail)
    assert run("add", "--servers", str(servers_file), "lean-b", "lean-b.example:8000", "8") == 1
    assert servers_file.read_text() == ORIGINAL


def test_list_prints_the_validated_servers_and_the_pools_workers(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run("add", "--servers", str(servers_file), "lean-b", "lean-b.example:8000", "8")
    assert run("list", "--servers", str(servers_file)) == 0
    assert capsys.readouterr().out == (
        "lean-a lean-a.example:8000 4 18200\n"
        "lean-b lean-b.example:8000 8 18200\n"
        "# 2 servers, 12 workers\n"
    )


def test_render_prints_the_configuration(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(servers_file)) == 0
    config = capsys.readouterr().out
    assert "server lean-a lean-a.example:8000 check maxconn 4 weight 256 " in config
    assert "bind :18100" in config


def test_render_refuses_a_malformed_list_and_prints_no_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "servers"
    path.write_text("lean-a lean-a.example:8000 4\nlean-a lean-b.example:8000 4\n")
    assert run("render", "--servers", str(path)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "line 2" in captured.err


def test_render_refuses_a_missing_or_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(tmp_path / "missing")) == 1
    assert "does not exist" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.write_text("# no servers yet\n")
    assert run("render", "--servers", str(empty)) == 1
    assert "empty" in capsys.readouterr().err


def test_render_settings_come_from_the_environment_and_flags_override_them(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = {
        "LEANPOOL_HAPROXY_SERVERS": str(servers_file),
        "LEANPOOL_HAPROXY_PUBLIC_PORT": "9000",
        "LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS": "300",
        "LEANPOOL_HOP_HEADER": "X-Second-Hop",
    }
    assert run("render", environment=environment) == 0
    config = capsys.readouterr().out
    assert "bind :9000" in config
    assert "timeout queue 630s" in config
    assert "X-Second-Hop" in config

    assert run("render", "--public-port", "9100", environment=environment) == 0
    assert "bind :9100" in capsys.readouterr().out


def test_render_refuses_a_queue_timeout_not_above_the_lean_timeout(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(servers_file), "--queue-timeout", "60") == 1
    assert "above the Lean timeout" in capsys.readouterr().err


def test_render_sizes_buffers_for_the_largest_replayable_request(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(servers_file)) == 0
    default_config = capsys.readouterr().out
    assert "tune.bufsize 262144" in default_config
    assert "maxconn 1024" in default_config

    environment = {
        "LEANPOOL_HAPROXY_SERVERS": str(servers_file),
        "LEANPOOL_HAPROXY_MAX_REQUEST_BYTES": "65536",
        "LEANPOOL_HAPROXY_MAXIMUM_CONNECTIONS": "2048",
    }
    assert run("render", environment=environment) == 0
    config = capsys.readouterr().out
    assert "tune.bufsize 65536" in config
    assert "maxconn 2048" in config
    assert "x 65536 bytes = 384 MiB" in config

    assert run("render", "--max-request-bytes", "131072", environment=environment) == 0
    assert "tune.bufsize 131072" in capsys.readouterr().out


def test_render_refuses_a_buffer_smaller_than_haproxys_default(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(servers_file), "--max-request-bytes", "8192") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HAProxy's default buffer size, 16384" in captured.err


TLS_FILES = {
    "--tls-front-door-pem": "/etc/leanpool/tls/front.pem",
    "--tls-ca-file": "/etc/leanpool/tls/ca.crt",
    "--tls-client-pem": "/etc/leanpool/tls/proxy-client.pem",
}
TLS_OPTIONS = [word for option in TLS_FILES.items() for word in option]


def test_render_without_tls_says_first_that_the_pool_is_unencrypted(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(servers_file)) == 0
    config = capsys.readouterr().out
    assert config.startswith("# UNENCRYPTED: ")
    assert "ssl" not in config.replace("UNENCRYPTED", "")


def test_render_with_the_three_tls_files_encrypts_the_pool(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("render", "--servers", str(servers_file), *TLS_OPTIONS) == 0
    config = capsys.readouterr().out
    assert "UNENCRYPTED" not in config
    assert "bind :18100 ssl crt /etc/leanpool/tls/front.pem ssl-min-ver TLSv1.3" in config
    assert (
        "server lean-a lean-a.example:8000 ssl verify required ca-file /etc/leanpool/tls/ca.crt "
        "crt /etc/leanpool/tls/proxy-client.pem ssl-min-ver TLSv1.3 "
        "sni str(lean-a) verifyhost lean-a check check-ssl check-sni lean-a "
        "maxconn 4 weight 256 agent-check agent-addr 127.0.0.1 agent-port 18300 agent-inter 5s"
    ) in config
    assert "listen agent_tunnel_lean-a\n    bind 127.0.0.1:18300\n" in config


@pytest.mark.parametrize(
    "given",
    [
        ["--tls-front-door-pem"],
        ["--tls-ca-file"],
        ["--tls-client-pem"],
        ["--tls-front-door-pem", "--tls-ca-file"],
        ["--tls-front-door-pem", "--tls-client-pem"],
        ["--tls-ca-file", "--tls-client-pem"],
    ],
)
def test_render_never_falls_back_to_plain_when_a_tls_file_is_missing(
    servers_file: Path, capsys: pytest.CaptureFixture[str], given: list[str]
) -> None:
    """A pool that was meant to be encrypted must not be rendered plain by an omission."""
    options = [word for flag in given for word in (flag, TLS_FILES[flag])]
    assert run("render", "--servers", str(servers_file), *options) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    missing = ", ".join(flag for flag in TLS_FILES if flag not in given)
    assert f"missing: {missing}. Nothing was rendered" in captured.err


def test_render_tls_settings_come_from_the_environment_and_flags_override_them(
    servers_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = {
        "LEANPOOL_HAPROXY_SERVERS": str(servers_file),
        "LEANPOOL_HAPROXY_TLS_FRONT_DOOR_PEM": "/run/tls/front.pem",
        "LEANPOOL_HAPROXY_TLS_CA_FILE": "/run/tls/ca.crt",
        "LEANPOOL_HAPROXY_TLS_CLIENT_PEM": "/run/tls/client.pem",
        "LEANPOOL_HAPROXY_AGENT_TUNNEL_PORT": "19000",
    }
    assert run("render", environment=environment) == 0
    config = capsys.readouterr().out
    assert "bind :18100 ssl crt /run/tls/front.pem " in config
    assert " ca-file /run/tls/ca.crt crt /run/tls/client.pem " in config
    assert "agent-check agent-addr 127.0.0.1 agent-port 19000 " in config

    assert run("render", "--agent-tunnel-port", "19500", environment=environment) == 0
    assert "bind 127.0.0.1:19500" in capsys.readouterr().out

    # Two of the three files from the environment are refused like two of three flags.
    del environment["LEANPOOL_HAPROXY_TLS_CA_FILE"]
    assert run("render", environment=environment) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (["--tls-ca-file", "ca.crt"], "must be an absolute path"),
        (["--cache-address", "cache.example:18102"], "the cache must be on a loopback address"),
        (["--agent-tunnel-port", "65535"], "do not fit"),
    ],
)
def test_render_refuses_tls_settings_that_would_leave_something_unsafe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: list[str], reason: str
) -> None:
    path = tmp_path / "servers"
    path.write_text("lean-a lean-a.example:8000 4\nlean-b lean-b.example:8000 8\n")
    assert run("render", "--servers", str(path), *TLS_OPTIONS, *change) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert reason in captured.err


def test_render_with_tls_refuses_a_server_name_no_certificate_can_carry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "servers"
    path.write_text("lean_a lean-a.example:8000 4\n")
    assert run("render", "--servers", str(path)) == 0
    capsys.readouterr()
    assert run("render", "--servers", str(path), *TLS_OPTIONS) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the server name 'lean_a' is checked against the server's certificate" in captured.err


BOX_OPTIONS = {
    "--tls-server-pem": "/etc/leanpool/tls/box.pem",
    "--tls-ca-file": "/etc/leanpool/tls/ca.crt",
    "--proxy-client-name": "lean-pool-proxy",
    "--lean-upstream": "kimina:8000",
    "--agent-upstream": "agent:18200",
}


def box_arguments(**changes: str | None) -> list[str]:
    """The required options of ``render-box``, with some changed or (None) left out."""
    options = {**BOX_OPTIONS, **{f"--{name.replace('_', '-')}": v for name, v in changes.items()}}
    return [word for flag, value in options.items() if value is not None for word in (flag, value)]


def test_render_box_prints_a_boxs_tls_front_and_needs_no_server_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run("render-box", *box_arguments()) == 0
    config = capsys.readouterr().out
    bind_options = (
        "ssl crt /etc/leanpool/tls/box.pem ssl-min-ver TLSv1.3 "
        "ca-file /etc/leanpool/tls/ca.crt verify required"
    )
    assert f"listen lean\n    bind :8000 {bind_options}\n" in config
    assert f"listen agent\n    bind :18200 {bind_options}\n" in config
    assert config.count("{ ssl_c_s_dn(cn) -m str lean-pool-proxy }") == 2
    assert "    server lean kimina:8000\n" in config
    assert "    server agent agent:18200\n" in config
    assert "timeout server 240s" in config


def test_render_box_takes_ports_and_durations(capsys: pytest.CaptureFixture[str]) -> None:
    options = ["--lean-port", "18001", "--agent-port", "18201", "--lean-timeout", "300"]
    options += ["--server-wait", "10", "--margin", "5", "--maximum-connections", "64"]
    assert run("render-box", *box_arguments(), *options) == 0
    config = capsys.readouterr().out
    assert "    bind :18001 ssl " in config
    assert "    bind :18201 ssl " in config
    assert "timeout server 620s" in config  # 10 + 2 x 300 + 5, and 5 more than the pool
    assert "maxconn 64" in config


def test_render_box_settings_come_from_the_environment_and_flags_override_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {
        "LEANPOOL_HAPROXY_BOX_TLS_SERVER_PEM": "/run/tls/box.pem",
        "LEANPOOL_HAPROXY_TLS_CA_FILE": "/run/tls/ca.crt",
        "LEANPOOL_HAPROXY_BOX_PROXY_CLIENT_NAME": "the-proxy",
        "LEANPOOL_HAPROXY_BOX_LEAN_UPSTREAM": "lean:9000",
        "LEANPOOL_HAPROXY_BOX_AGENT_UPSTREAM": "usage:9001",
        "LEANPOOL_HAPROXY_BOX_LEAN_PORT": "18001",
        "LEANPOOL_HAPROXY_BOX_AGENT_PORT": "18201",
        "LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS": "300",
        "LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS": "10",
        "LEANPOOL_HAPROXY_MARGIN_SECONDS": "5",
        "LEANPOOL_HAPROXY_MAXIMUM_CONNECTIONS": "64",
    }
    assert run("render-box", environment=environment) == 0
    config = capsys.readouterr().out
    assert "bind :18001 ssl crt /run/tls/box.pem ssl-min-ver TLSv1.3 ca-file /run/tls/ca.crt" in (
        config
    )
    assert "bind :18201 ssl " in config
    assert "{ ssl_c_s_dn(cn) -m str the-proxy }" in config
    assert "server lean lean:9000" in config
    assert "server agent usage:9001" in config
    assert "timeout client 620s" in config
    assert "maxconn 64" in config

    assert run("render-box", "--lean-port", "18002", environment=environment) == 0
    assert "bind :18002 ssl " in capsys.readouterr().out


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"tls_server_pem": "box.pem"}, "must be an absolute path"),
        ({"proxy_client_name": "lean pool proxy"}, "the proxy's client name"),
        ({"lean_upstream": "kimina"}, "HOST:PORT"),
        ({"agent_upstream": "agent_1:18200"}, "host"),
        ({"lean_port": "18200"}, "must differ"),
    ],
)
def test_render_box_refuses_unusable_settings_and_prints_no_configuration(
    capsys: pytest.CaptureFixture[str], changes: dict[str, str], reason: str
) -> None:
    assert run("render-box", *box_arguments(**changes)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert reason in captured.err


@pytest.mark.parametrize("left_out", sorted(BOX_OPTIONS))
def test_render_box_needs_every_one_of_its_settings(
    capsys: pytest.CaptureFixture[str], left_out: str
) -> None:
    arguments = box_arguments(**{left_out.removeprefix("--").replace("-", "_"): None})
    with pytest.raises(SystemExit) as exit_information:
        main(["render-box", *arguments], {})
    assert exit_information.value.code == 2
    assert f"{left_out} (or LEANPOOL_HAPROXY_" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["list"], {}),  # no server list given
        (["render", "--servers", "servers", "--public-port", "0"], {}),
        (["render", "--servers", "servers"], {"LEANPOOL_HAPROXY_STATS_PORT": "many"}),
        (["render", "--servers", "servers", "--agent-tunnel-port", "0"], {}),
        (["render", *TLS_OPTIONS], {}),  # TLS files, but no server list
        (["add", "--servers", "servers", "lean-a"], {}),  # missing fields
        (["render-box", *box_arguments(), "--lean-port", "0"], {}),
        (["render-box", *box_arguments(), "--servers", "servers"], {}),  # not a box's option
        (["render-box", *box_arguments()], {"LEANPOOL_HAPROXY_BOX_AGENT_PORT": "many"}),
        (["frobnicate"], {}),
        ([], {}),
    ],
)
def test_usage_errors_exit_with_status_two(
    arguments: list[str], environment: dict[str, str]
) -> None:
    with pytest.raises(SystemExit) as exit_information:
        main(arguments, environment)
    assert exit_information.value.code == 2
