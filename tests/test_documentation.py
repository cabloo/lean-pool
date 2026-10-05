"""The documentation is held to the code: what it names exists, and what it quotes is produced."""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from leanpool.admit.cli import main as admit_main
from leanpool.admit.runner import _HEALTH_TIMEOUT_SECONDS as HEALTH_TIMEOUT_SECONDS
from leanpool.admit.tls import (
    CLOSED_WITHOUT_AN_ANSWER,
    IS_THIS_A_FRONT,
    describe_client_certificate,
    describe_server_certificate,
)
from leanpool.agent.cli import main as agent_main
from leanpool.cache.cli import main as cache_main
from leanpool.haproxy import (
    BoxSettings,
    PoolSettings,
    TlsSettings,
    box_timeout_seconds,
    derive_timeouts,
    parse_server_list,
    render_box_config,
    render_haproxy_config,
    worst_case_buffer_bytes,
)
from leanpool.haproxy.cli import main as haproxy_main
from leanpool.join import MAXIMUM_REQUEST_BYTES
from leanpool.join.app import ROUTES as JOIN_ROUTES
from leanpool.join.cli import main as join_main
from leanpool.join.settings import MAXIMUM_TOKEN_LENGTH, MINIMUM_TOKEN_LENGTH
from leanpool.pki import AUTHORITY_LIFETIME, CERTIFICATE_LIFETIME
from leanpool.pki.cli import main as pki_main

PROJECT = Path(__file__).parent.parent
PAGES = [
    PROJECT / "README.md",
    PROJECT / "CONTRIBUTING.md",
    PROJECT / "SECURITY.md",
    PROJECT / "CHANGELOG.md",
    *sorted((PROJECT / "docs").glob("*.md")),
    PROJECT / "deploy" / "README.md",
    PROJECT / "examples" / "demo" / "README.md",
    PROJECT / "examples" / "admission-cases" / "README.md",
]
CONFIGURATION = (PROJECT / "docs" / "configuration.md").read_text(encoding="utf-8")
# Every page as one text: a fact these tests look for may be stated on any of them.
DOCUMENTATION = "\n".join(page.read_text(encoding="utf-8") for page in PAGES)

Command = Callable[[Sequence[str], Mapping[str, str]], int]
COMMANDS: list[tuple[Command, list[str]]] = [
    (cache_main, []),
    (agent_main, []),
    (admit_main, []),
    (haproxy_main, ["render"]),
    (haproxy_main, ["render-box"]),
    (haproxy_main, ["add"]),
    (haproxy_main, ["remove"]),
    (haproxy_main, ["list"]),
    (pki_main, ["init"]),
    (pki_main, ["issue-server"]),
    (pki_main, ["issue-client"]),
    (pki_main, ["csr"]),
    (pki_main, ["sign-csr"]),
    (join_main, []),
]


def help_text(command: Command, prefix: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exit_information:
        command([*prefix, "--help"], {})
    assert exit_information.value.code == 0
    return " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize(("command", "prefix"), COMMANDS)
def test_every_flag_and_variable_is_in_the_configuration_reference(
    command: Command, prefix: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    text = help_text(command, prefix, capsys)
    variables = set(re.findall(r"LEANPOOL_[A-Z_]+", text))
    flags = {flag for flag in re.findall(r"--[a-z][a-z-]+", text) if not flag.startswith("--no-")}
    flags.discard("--help")
    assert variables | flags, "the help text names no settings"
    missing = sorted(name for name in variables | flags if f"`{name}`" not in CONFIGURATION)
    assert missing == []


def test_the_documentation_and_the_compose_files_name_no_variable_that_does_not_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    known: set[str] = set()
    for command, prefix in COMMANDS:
        known |= set(re.findall(r"LEANPOOL_[A-Z_]+", help_text(command, prefix, capsys)))
    compose_files = [*PROJECT.glob("deploy/**/*.yaml"), *PROJECT.glob("examples/**/*.yaml")]
    written = DOCUMENTATION + "\n".join(path.read_text(encoding="utf-8") for path in compose_files)
    assert set(re.findall(r"LEANPOOL_[A-Z_]+", written)) <= known


def test_the_example_server_list_renders() -> None:
    servers = parse_server_list((PROJECT / "deploy" / "pool" / "servers.example").read_text())
    assert "server lean-a lean-a.example:8000 check maxconn 4 " in render_haproxy_config(servers)


def test_the_timeouts_the_documentation_quotes_are_the_generated_ones() -> None:
    config = render_haproxy_config(parse_server_list("lean-a 192.0.2.10:8000 4\n"))
    assert "timeout server 210s" in config
    assert "timeout queue 150s" in config
    assert "timeout client 390s" in config
    assert "at least 360s" in config
    for quoted in ("210 s", "150 s", "390 s", "360 s"):
        assert quoted in DOCUMENTATION


def test_the_failover_rules_the_documentation_quotes_are_the_generated_ones() -> None:
    config = render_haproxy_config(parse_server_list("lean-a 192.0.2.10:8000 4\n"))
    quoted_directives = [
        "retries 2",
        "retry-on conn-failure empty-response 500 502 503 504",
        "option redispatch 1",
        "option http-buffer-request",
        "retry-on conn-failure empty-response",
        "observe layer4 error-limit 1 on-error mark-down",
        "init-addr last,libc,none",
        "parse-resolv-conf",
        "timeout resolve 10s",
        "resolve-prefer ipv4",
        "use_backend checkers if !cache_up",
        "srv_is_up(cache/cache)",
    ]
    for directive in quoted_directives:
        assert directive in config
        assert f"`{directive}`" in DOCUMENTATION


def test_the_buffer_arithmetic_the_documentation_states_is_the_generated_one() -> None:
    settings = PoolSettings()
    assert (settings.maximum_connections, settings.maximum_request_bytes) == (1024, 262144)
    assert worst_case_buffer_bytes(settings) == 768 * 1024**2
    assert " 1024   x          3             x   262144 bytes   =   768 MiB" in DOCUMENTATION
    assert "= 768 MiB" in render_haproxy_config(parse_server_list("lean-a 192.0.2.10:8000 4\n"))


def quoted_in_the_documentation(directive: str, **placeholders: str) -> str:
    """Check a page quotes ``directive`` and return it with its placeholders filled in."""
    assert f"`{directive}`" in " ".join(DOCUMENTATION.split())
    for placeholder, value in placeholders.items():
        directive = directive.replace(f"<{placeholder.replace('_', ' ')}>", value)
    assert "<" not in directive
    return directive


def test_the_tls_directives_the_documentation_quotes_are_the_generated_ones() -> None:
    tls = TlsSettings(
        front_door_pem="/tls/front.pem", ca_file="/tls/ca.crt", client_pem="/tls/client.pem"
    )
    config = render_haproxy_config(
        parse_server_list("Lean-A 192.0.2.10:8000 4\n"), PoolSettings(tls=tls)
    )
    files = {
        "front_door": "/tls/front.pem",
        "authority": "/tls/ca.crt",
        "client_certificate": "/tls/client.pem",
    }
    quoted = [
        quoted_in_the_documentation("ssl-default-bind-options ssl-min-ver TLSv1.3"),
        quoted_in_the_documentation("ssl-default-server-options ssl-min-ver TLSv1.3"),
        quoted_in_the_documentation(
            "bind :18100 ssl crt <front door> ssl-min-ver TLSv1.3", **files
        ),
        quoted_in_the_documentation(
            "ssl verify required ca-file <authority> crt <client certificate>", **files
        ),
        quoted_in_the_documentation("sni str(<name>)", name="lean-a"),
        quoted_in_the_documentation("verifyhost <name>", name="lean-a"),
        quoted_in_the_documentation("check check-ssl check-sni <name>", name="lean-a"),
        quoted_in_the_documentation(
            "agent-check agent-addr 127.0.0.1 agent-port <tunnel port>", tunnel_port="18300"
        ),
        quoted_in_the_documentation("defaults agent_tunnels"),
        quoted_in_the_documentation("listen agent_tunnel_<name>", name="Lean-A"),
    ]
    for directive in quoted:
        assert directive in config
    assert "bind 127.0.0.1:18300" in config
    assert "`--agent-tunnel-port` (18300)" in DOCUMENTATION
    assert render_haproxy_config(parse_server_list("lean-a 192.0.2.10:8000 4\n")).startswith(
        quoted_in_the_documentation("# UNENCRYPTED:")
    )


def test_the_box_front_the_documentation_describes_is_the_generated_one() -> None:
    settings = BoxSettings(
        server_pem="/tls/box.pem",
        ca_file="/tls/ca.crt",
        proxy_client_name="lean-pool-proxy",
        lean_upstream="kimina:8000",
        agent_upstream="agent:18200",
    )
    config = render_box_config(settings)
    quoted = [
        quoted_in_the_documentation("mode tcp"),
        quoted_in_the_documentation(
            "bind :<port> ssl crt <box certificate> ssl-min-ver TLSv1.3 "
            "ca-file <authority> verify required",
            port="8000",
            box_certificate="/tls/box.pem",
            authority="/tls/ca.crt",
        ),
        quoted_in_the_documentation(
            "tcp-request session reject unless { ssl_c_s_dn(cn) -m str <proxy client name> }",
            proxy_client_name="lean-pool-proxy",
        ),
    ]
    for directive in quoted:
        assert directive in config
    assert (box_timeout_seconds(settings), derive_timeouts(PoolSettings()).checker_seconds) == (
        240,
        210,
    )
    assert "240 s with the defaults, against the proxy's 210 s" in " ".join(DOCUMENTATION.split())
    assert "timeout client 240s" in config
    assert "timeout server 10s" in config
    assert "The agent's listener has 10 s timeouts" in DOCUMENTATION


def test_the_admission_tls_failures_the_documentation_quotes_are_the_ones_reported() -> None:
    documentation = " ".join(DOCUMENTATION.split())

    def server_certificate(code: int) -> str:
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        error.verify_code, error.verify_message = code, "its own words"
        return describe_server_certificate(error, "lean-a")

    def client_certificate(reason: str) -> str:
        error = ssl.SSLError(1, reason)
        error.reason = reason
        return describe_client_certificate(error)

    refused = "the server refused the client certificate in --tls-client-pem:"
    quoted = {
        "the server's certificate is not for the name 'lean-a' given as --tls-server-name": (
            server_certificate(62)
        ),
        "the server's certificate was not signed by the authority in --tls-ca-file": (
            server_certificate(20)
        ),
        "the server's certificate has expired": server_certificate(10),
        refused: client_certificate("SSLV3_ALERT_UNSUPPORTED_CERTIFICATE"),
        "it is not a client certificate": client_certificate("SSLV3_ALERT_UNSUPPORTED_CERTIFICATE"),
        "it was not signed by the authority the server trusts": client_certificate(
            "TLSV1_ALERT_UNKNOWN_CA"
        ),
        "it has expired": client_certificate("SSLV3_ALERT_CERTIFICATE_EXPIRED"),
        "the server closed the connection after the TLS handshake without an answer": (
            CLOSED_WITHOUT_AN_ANSWER
        ),
        "is this the port of a TLS front?": IS_THIS_A_FRONT,
    }
    for words, reported in quoted.items():
        assert f"`{words}`" in documentation
        assert words in reported
    assert (
        "| 3 | the server could not be reached, or the TLS handshake with it failed |"
        in DOCUMENTATION
    )
    assert "at most ten seconds for each step" in documentation
    assert HEALTH_TIMEOUT_SECONDS == 10


def test_the_join_service_the_documentation_describes_is_the_one_served() -> None:
    documentation = " ".join(DOCUMENTATION.split())
    served = {(method, what or "") for method, what in JOIN_ROUTES}
    assert served == {
        ("GET", ""),
        ("POST", "csr"),
        ("GET", "certificate"),
        ("POST", "ready"),
        ("GET", "verdict"),
        ("GET", "image.json"),
        ("GET", "image"),
    }
    for method, what in served:
        path = f"/j/<token>/{what}".rstrip("/")
        assert f"| `{method} {path}`" in documentation
    assert MAXIMUM_REQUEST_BYTES == 16 * 1024
    assert "A request body is at most 16 KiB (413 above)" in documentation
    assert (MINIMUM_TOKEN_LENGTH, MAXIMUM_TOKEN_LENGTH) == (22, 128)
    assert "It is 22 to 128 letters, digits, `-` and `_`" in documentation
    assert "https://pool.example:18110/j/<token>" in documentation


def test_the_certificate_lifetimes_the_documentation_quotes_are_the_ones_issued() -> None:
    assert (AUTHORITY_LIFETIME.days, CERTIFICATE_LIFETIME.days) == (3650, 1825)
    assert "`3650` for `init`, `1825` otherwise" in DOCUMENTATION
    assert "valid for 10 years" in DOCUMENTATION


# --- the pages themselves: every link leads somewhere, and no page is left out -----------------

_FENCED_CODE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6} +(.*?) *$", re.MULTILINE)


def anchors_of(page: Path) -> set[str]:
    """The anchors GitHub gives a page's headings."""
    anchors: set[str] = set()
    for heading in _HEADING.findall(_FENCED_CODE.sub("", page.read_text(encoding="utf-8"))):
        kept = re.sub(r"[^\w\- ]", "", heading.lower())
        anchor = kept.replace(" ", "-")
        suffix = 0
        while (candidate := anchor if not suffix else f"{anchor}-{suffix}") in anchors:
            suffix += 1
        anchors.add(candidate)
    return anchors


def links_of(page: Path) -> list[str]:
    """The targets of a page's links, outside code."""
    text = _INLINE_CODE.sub("", _FENCED_CODE.sub("", page.read_text(encoding="utf-8")))
    return _LINK.findall(text)


def leads_somewhere(target: Path, anchor: str) -> bool:
    """Whether ``anchor`` (if any) is a heading of the page ``target``."""
    return not anchor or anchor in anchors_of(target)


@pytest.mark.parametrize("page", PAGES, ids=lambda page: str(page.relative_to(PROJECT)))
def test_every_link_between_the_pages_leads_somewhere(page: Path) -> None:
    broken = []
    for link in links_of(page):
        if link.startswith(("https://", "http://", "mailto:")):
            continue
        path, _, anchor = link.partition("#")
        target = (page.parent / path).resolve() if path else page
        inside_the_project = PROJECT in (target, *target.parents)
        if not (inside_the_project and target.exists() and leads_somewhere(target, anchor)):
            broken.append(link)
    assert broken == []


def test_every_page_of_the_documentation_is_listed_on_the_front_page_and_in_the_index() -> None:
    pages = {page.name for page in (PROJECT / "docs").glob("*.md")} - {"README.md"}
    front_page = set(links_of(PROJECT / "README.md"))
    index = set(links_of(PROJECT / "docs" / "README.md"))

    assert {f"docs/{name}" for name in pages} <= {link.partition("#")[0] for link in front_page}
    assert pages <= {link.partition("#")[0] for link in index}


def test_the_measurements_the_front_page_quotes_are_the_ones_on_record() -> None:
    front_page = " ".join((PROJECT / "README.md").read_text(encoding="utf-8").split())
    record = " ".join((PROJECT / "docs" / "verification.md").read_text(encoding="utf-8").split())
    hits, joined, misses = 403_933, 22_011, 556_669

    assert f"{hits + joined + misses:,} checks" in front_page
    assert round(100 * (hits + joined) / (hits + joined + misses)) == 43
    assert "**43% of checks never needed a Lean worker.**" in front_page
    for figure in (f"{hits:,}", f"{joined:,}", "781,151 results", "853 MB"):
        assert figure in front_page
        assert figure in record
    assert f"{misses:,}" in record
    for seconds in ("91.1", "0.1", "86.6"):
        assert f"{seconds} s" in front_page
        assert f"| {seconds} |" in record
    assert "13 ms in all to connect" in front_page
    assert "13 ms in all to connect" in record
