"""What the generated configuration says to a Prometheus scraper, and the pool's facts file.

Three paths at the front door, answered without the key: ``/metrics`` (HAProxy's exporter),
``/metrics/cache`` (the cache's counters) and ``/metrics/pool`` (a file written at render time
from the server list and the certificates). ``test_metrics_live.py`` shows what a real HAProxy
does with these rules; this file shows what is written.
"""

from __future__ import annotations

import base64
import dataclasses
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_haproxy_config import SERVERS, section
from tls_support import ThrowawayAuthority

from leanpool.haproxy import (
    LeanServer,
    PoolSettings,
    PoolSettingsError,
    maximum_pool_metrics_bytes,
    read_certificate_dates,
    render_haproxy_config,
    render_pool_metrics,
    write_pool_metrics,
)
from leanpool.haproxy.certificate_dates import CertificateDateError, not_after_seconds
from leanpool.haproxy.cli import main
from leanpool.metrics import Metric, Sample, render_metrics
from leanpool.pki import (
    Usage,
    create_authority,
    issue_certificate,
    new_private_key,
    read_certificate,
    subject_names,
)
from leanpool.pki.files import certificate_pem

WITH_FILE = PoolSettings(pool_metrics_file="pool.metrics")
EXPORTER_RULES = [
    "http-request set-query scope=global&scope=frontend&scope=backend&scope=server "
    "if METH_GET is_metrics",
    "http-request use-service prometheus-exporter if METH_GET is_metrics",
]
CACHE_DOWN_RULE = (
    'http-request return status 503 content-type text/plain string "the cache is down\\n" '
    "if METH_GET is_cache_metrics !cache_up"
)
FILE_RULE = (
    'http-request return status 200 content-type "text/plain; version=0.0.4" '
    "file pool.metrics if METH_GET is_pool_metrics"
)


@pytest.fixture
def config() -> str:
    return render_haproxy_config(SERVERS)


# --- the configuration ----------------------------------------------------------------------------


def test_metrics_is_haproxys_own_exporter_with_the_scrapers_query_replaced(config: str) -> None:
    frontend = section(config, "frontend lean_pool")
    assert "acl is_metrics path /metrics" in frontend
    # The query is fixed before the exporter reads it; the two rules stand together, in order.
    start = frontend.index(EXPORTER_RULES[0])
    assert frontend[start : start + 2] == EXPORTER_RULES
    assert sum("prometheus-exporter" in line for line in config.splitlines()) == 1
    # The exporter's report on name resolution names the resolver's address: it is not asked for.
    assert "resolvers" not in EXPORTER_RULES[0]
    assert "scope=*" not in config


def test_the_caches_counters_come_from_the_cache_server_alone(config: str) -> None:
    frontend = section(config, "frontend lean_pool")
    assert "acl is_cache_metrics path /metrics/cache" in frontend
    routed = "use_backend cache_metrics if METH_GET is_cache_metrics"
    # Answered 503 here while the cache server is down; otherwise the cache's own backend.
    assert frontend.index(CACHE_DOWN_RULE) < frontend.index(routed)
    # No backup, no retry, no health check of its own: the state is that of cache/cache.
    assert section(config, "backend cache_metrics") == [
        "retries 0",
        "default-server init-addr last,libc,none resolvers pool_dns resolve-prefer ipv4",
        "server cache 127.0.0.1:18102 track cache/cache",
    ]
    assert "backup" not in "\n".join(section(config, "backend cache_metrics"))


def test_the_metrics_backend_follows_the_cache_address() -> None:
    config = render_haproxy_config(SERVERS, PoolSettings(cache_address="cache.example:9002"))
    assert "server cache cache.example:9002 track cache/cache" in section(
        config, "backend cache_metrics"
    )


def test_a_scrape_is_routed_before_every_rule_that_was_there(config: str) -> None:
    """The rules that route checks are what they were, in their order, after the new one."""
    frontend = section(config, "frontend lean_pool")
    routing = [line for line in frontend if line.startswith(("use_backend", "default_backend"))]
    assert routing == [
        "use_backend cache_metrics if METH_GET is_cache_metrics",
        "use_backend cache if is_cache_status",
        "use_backend checkers if is_second_hop",
        "use_backend checkers if !is_check",
        "use_backend checkers if !cache_up",
        "default_backend cache",
    ]
    # HAProxy runs every http-request rule before any use_backend rule, wherever it is written,
    # and warns about one written after: none is.
    first_routing = frontend.index(routing[0])
    assert not [line for line in frontend[first_routing:] if line.startswith("http-request ")]


def test_every_metrics_rule_is_for_get_only() -> None:
    """``METH_GET`` is GET and HEAD, as for ``/health``: any other method is routed as before."""
    frontend = section(render_haproxy_config(SERVERS, WITH_FILE), "frontend lean_pool")
    rules = [line for line in frontend if "_metrics" in line and not line.startswith("acl ")]
    assert len(rules) == 5
    for rule in rules:
        assert " if METH_GET is_" in rule, rule
    # The three paths are exact: nothing beside or below them is taken.
    assert [line for line in frontend if line.startswith("acl ") and "_metrics" in line] == [
        "acl is_metrics path /metrics",
        "acl is_cache_metrics path /metrics/cache",
        "acl is_pool_metrics path /metrics/pool",
    ]


def test_without_a_file_there_is_no_rule_for_the_pools_facts(config: str) -> None:
    assert "/metrics/pool" not in config
    assert "is_pool_metrics" not in config
    assert "default-path" not in config
    assert " file " not in config


def test_with_a_file_haproxy_returns_it_from_beside_the_configuration() -> None:
    config = render_haproxy_config(SERVERS, WITH_FILE)
    frontend = section(config, "frontend lean_pool")
    assert "acl is_pool_metrics path /metrics/pool" in frontend
    assert FILE_RULE in frontend
    # A name with no directory, found beside the configuration wherever the two are installed.
    assert section(config, "global")[-1] == "default-path config"
    # Nothing else of the configuration depends on the file.
    without = render_haproxy_config(SERVERS)
    added = [line for line in config.splitlines() if line not in without.splitlines()]
    assert [line.strip() for line in added if not line.strip().startswith("#")] == [
        "default-path config",
        "acl is_pool_metrics path /metrics/pool",
        FILE_RULE,
    ]


@pytest.mark.parametrize(
    "name", ["", "dir/pool.metrics", "/etc/pool.metrics", "pool metrics", "..", "a" * 65, "x\ny"]
)
def test_a_file_name_that_could_not_stand_in_the_configuration_is_refused(name: str) -> None:
    with pytest.raises(PoolSettingsError, match="metrics file cannot be named"):
        render_haproxy_config(SERVERS, PoolSettings(pool_metrics_file=name))


def test_pausing_a_server_changes_no_metrics_rule(config: str) -> None:
    paused = render_haproxy_config([SERVERS[0], dataclasses.replace(SERVERS[1], paused=True)])
    for title in ("frontend lean_pool", "backend cache_metrics"):
        assert section(paused, title) == section(config, title)


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="HAProxy is not installed")
def test_haproxy_accepts_the_configuration_with_its_file_and_refuses_it_without(
    tmp_path: Path,
) -> None:
    """The file is read when the configuration is: it must be there first, beside it."""
    servers = [LeanServer(name="lean-a", host="192.0.2.10", port=8000, workers=4)]
    path = tmp_path / "haproxy.cfg"
    path.write_text(render_haproxy_config(servers, WITH_FILE))

    def judged() -> subprocess.CompletedProcess[str]:
        # From another directory: the file is found beside the configuration, not here.
        return subprocess.run(
            ["haproxy", "-c", "-f", str(path)], capture_output=True, text=True, check=False, cwd="/"
        )

    missing = judged()
    assert missing.returncode != 0
    assert "pool.metrics" in missing.stdout + missing.stderr
    write_pool_metrics(tmp_path / "pool.metrics", render_pool_metrics(servers), 8192)
    accepted = judged()
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "WARNING" not in accepted.stdout + accepted.stderr


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="HAProxy is not installed")
def test_haproxy_takes_a_file_as_large_as_the_renderer_allows_without_a_warning(
    tmp_path: Path,
) -> None:
    settings = PoolSettings(pool_metrics_file="pool.metrics", maximum_request_bytes=16384)
    limit = maximum_pool_metrics_bytes(settings)
    assert limit == 8192
    path = tmp_path / "haproxy.cfg"
    path.write_text(render_haproxy_config(SERVERS, settings))
    write_pool_metrics(tmp_path / "pool.metrics", "#" * (limit - 1) + "\n", limit)
    result = subprocess.run(
        ["haproxy", "-c", "-f", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stdout + result.stderr


# --- the text format ------------------------------------------------------------------------------


def test_the_text_format_escapes_what_a_label_or_a_help_line_cannot_hold() -> None:
    text = render_metrics(
        [
            Metric(
                "pool_thing",
                "gauge",
                "One line\nand a \\ in it.",
                [Sample(3, {"name": 'a"b\\c\nd', "kind": "plain"}), Sample(4)],
            ),
            Metric("pool_nothing", "counter", "Never written: it has no sample.", []),
        ]
    )
    assert text == (
        "# HELP pool_thing One line\\nand a \\\\ in it.\n"
        "# TYPE pool_thing gauge\n"
        'pool_thing{name="a\\"b\\\\c\\nd",kind="plain"} 3\n'
        "pool_thing 4\n"
    )


@pytest.mark.parametrize(
    "metric",
    [
        Metric("pool thing", "gauge", "A name with a space.", [Sample(1)]),
        Metric("pool_thing", "gauge", "A label with a dash.", [Sample(1, {"a-b": "c"})]),
    ],
)
def test_a_name_the_text_format_cannot_hold_is_refused(metric: Metric) -> None:
    with pytest.raises(ValueError, match="is not a"):
        render_metrics([metric])


# --- the pool's facts -----------------------------------------------------------------------------

POOL = [
    LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4),
    LeanServer(
        name="lean-b", host="192.0.2.20", port=8001, workers=8, agent_port=18250, paused=True
    ),
]
POOL_FACTS = """\
# HELP leanpool_server_listed_workers Workers the server list gives a Lean server.
# TYPE leanpool_server_listed_workers gauge
leanpool_server_listed_workers{server="lean-a"} 4
leanpool_server_listed_workers{server="lean-b"} 8
# HELP leanpool_server_paused Whether the server list marks a Lean server as paused (1) or not (0).
# TYPE leanpool_server_paused gauge
leanpool_server_paused{server="lean-a"} 0
leanpool_server_paused{server="lean-b"} 1
# HELP leanpool_certificate_not_after_seconds The end of a certificate's validity, in seconds \
since 1970.
# TYPE leanpool_certificate_not_after_seconds gauge
leanpool_certificate_not_after_seconds{certificate="ca.crt"} 2075839200
leanpool_certificate_not_after_seconds{certificate="front.crt"} 1918072800
leanpool_certificate_not_after_seconds{certificate="servers/lean-a.crt"} 1918072801
"""


def test_a_list_with_one_paused_server_and_three_certificates_is_the_fixture() -> None:
    dates = {"ca.crt": 2075839200, "front.crt": 1918072800, "servers/lean-a.crt": 1918072801}
    assert render_pool_metrics(POOL, dates) == POOL_FACTS


def test_without_certificates_the_facts_are_the_servers_alone() -> None:
    assert render_pool_metrics(POOL) == "".join(POOL_FACTS.splitlines(keepends=True)[:8])
    assert render_pool_metrics(POOL, {}) == render_pool_metrics(POOL)


def test_the_facts_hold_names_and_numbers_and_no_address() -> None:
    text = render_pool_metrics(POOL, {"ca.crt": 1})
    for hidden in ("example", "192.0.2.20", "8000", "8001", "18250", "18200"):
        assert hidden not in text


def test_a_text_haproxy_could_not_serve_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    path = tmp_path / "pool.metrics"
    text = render_pool_metrics(POOL)
    with pytest.raises(PoolSettingsError, match="more than the 100 HAProxy is given"):
        write_pool_metrics(path, text, 100)
    assert list(tmp_path.iterdir()) == []
    write_pool_metrics(path, text, len(text))
    assert path.read_text() == text
    assert path.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.iterdir()) == [path]  # written whole, with no file left beside it


def test_half_the_buffer_is_the_largest_file() -> None:
    assert maximum_pool_metrics_bytes(PoolSettings()) == 131072
    assert maximum_pool_metrics_bytes(PoolSettings(maximum_request_bytes=16384)) == 8192


# --- a certificate's last day, read with the standard library alone -------------------------------


@pytest.fixture
def authority(tmp_path: Path) -> ThrowawayAuthority:
    return ThrowawayAuthority.create(tmp_path / "pki")


def test_the_dates_are_the_ones_cryptography_reads(authority: ThrowawayAuthority) -> None:
    """The authority's (10 years), a server's and a client's (5), an expired one's."""
    authority.server("lean-a", "lean-a.lan", addresses=("127.0.0.1",))
    authority.client("lean-pool-proxy")
    authority.issue("old", Usage.SERVER, expired=True)
    for name in ("ca", "lean-a", "lean-pool-proxy", "old"):
        path = authority.directory / f"{name}.crt"
        expected = int(read_certificate(path).not_valid_after_utc.timestamp())
        assert not_after_seconds(path.read_text()) == expected
    # A file that holds a key beside the certificate is read the same way.
    combined = (authority.directory / "lean-a.pem").read_text()
    assert not_after_seconds(combined) == not_after_seconds(
        (authority.directory / "lean-a.crt").read_text()
    )


def test_a_date_from_2050_on_is_written_the_other_way_and_read_too() -> None:
    """X.509 writes a year with two digits up to 2049 and with four from 2050."""
    certificate = create_authority("long-lived", lifetime=timedelta(days=365 * 40)).certificate
    assert certificate.not_valid_after_utc > datetime(2050, 1, 1, tzinfo=UTC)
    pem = certificate_pem(certificate).decode()
    assert not_after_seconds(pem) == int(certificate.not_valid_after_utc.timestamp())


def test_a_date_before_2000_is_read_in_its_century(authority: ThrowawayAuthority) -> None:
    certificate = issue_certificate(
        authority.authority,
        new_private_key().public_key(),
        "from-the-nineties",
        subject_names(["from-the-nineties"], ()),
        Usage.SERVER,
        lifetime=timedelta(days=30),
        now=datetime(1996, 2, 29, 12, 0, 0, tzinfo=UTC),
    )
    expected = int(datetime(1996, 3, 30, 12, 0, 0, tzinfo=UTC).timestamp())
    assert not_after_seconds(certificate_pem(certificate).decode()) == expected


def der(pem: str) -> bytes:
    body = pem.split("-----BEGIN CERTIFICATE-----")[1].split("-----END CERTIFICATE-----")[0]
    return base64.b64decode("".join(body.split()))


def as_pem(encoded: bytes) -> str:
    text = base64.b64encode(encoded).decode()
    return f"-----BEGIN CERTIFICATE-----\n{text}\n-----END CERTIFICATE-----\n"


def test_what_is_not_a_certificate_is_refused_not_guessed_at(authority: ThrowawayAuthority) -> None:
    encoded = der(authority.certificate_path.read_text())
    key = (authority.directory / "ca.key").read_text()
    for text, reason in [
        ("a certificate", "holds no PEM certificate"),
        (key, "holds no PEM certificate"),
        ("-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n", "not base64"),
        (as_pem(b""), "not DER"),
        (as_pem(encoded[:40]), "not DER"),
        (as_pem(b"\x04" + encoded[1:]), "is not a certificate"),
        (as_pem(b"\x30\x03\x02\x01\x05"), "is not a certificate"),
        (as_pem(b"\x30\x85" + encoded[2:]), "not DER"),
    ]:
        with pytest.raises(CertificateDateError, match=reason):
            not_after_seconds(text)
    # Cut anywhere, it is refused or (once the dates are whole) read; it never fails otherwise.
    whole = not_after_seconds(as_pem(encoded))
    for length in range(len(encoded)):
        cut = encoded[:length]
        try:
            assert not_after_seconds(as_pem(cut)) == whole
        except CertificateDateError:
            continue


def test_a_date_that_is_no_date_is_refused(authority: ThrowawayAuthority) -> None:
    encoded = der(authority.certificate_path.read_text())
    stamp = read_certificate(authority.certificate_path).not_valid_after_utc.strftime(
        "%y%m%d%H%M%SZ"
    )
    assert encoded.count(stamp.encode()) == 1
    for wrong in (b"351341000000Z", b"35010100000Zz", b"3501010000 0Z"):
        assert len(wrong) == len(stamp)
        with pytest.raises(CertificateDateError, match="last day"):
            not_after_seconds(as_pem(encoded.replace(stamp.encode(), wrong)))


def test_every_certificate_under_a_directory_is_named_by_its_path_there(
    authority: ThrowawayAuthority, tmp_path: Path
) -> None:
    """The layout of a pool's public certificates: three of its own and one a server."""
    authority.server("lean-a")
    authority.client("lean-pool-proxy")
    certificates = tmp_path / "certs"
    (certificates / "servers").mkdir(parents=True)
    for source, name in [
        ("ca.crt", "ca.crt"),
        ("lean-pool-proxy.crt", "proxy-client.crt"),
        ("lean-a.crt", "servers/lean-a.crt"),
    ]:
        shutil.copy(authority.directory / source, certificates / name)
    (certificates / "servers" / "lean-b.crt").write_text("a certificate")
    (certificates / "notes.txt").write_text("not looked at")
    (certificates / "servers" / "lean-a.key").write_text("not looked at either")

    dates = read_certificate_dates(certificates)

    assert list(dates.not_after) == ["ca.crt", "proxy-client.crt", "servers/lean-a.crt"]
    for name, seconds in dates.not_after.items():
        assert seconds == int(read_certificate(certificates / name).not_valid_after_utc.timestamp())
    # A file that is no certificate has no date, and is named with the reason.
    assert dates.unreadable == {"servers/lean-b.crt": "it holds no PEM certificate"}


def test_a_certificates_directory_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PoolSettingsError, match="does not exist"):
        read_certificate_dates(tmp_path / "nowhere")


# --- leanpool-haproxy-config render --pool-metrics ------------------------------------------------

SERVER_LIST = "lean-a lean-a.example:8000 4\nlean-b 192.0.2.20:8001 8 18250 paused\n"


def render(*arguments: str) -> int:
    return main(["render", *arguments], {})


def test_render_writes_the_facts_and_a_configuration_that_names_them(
    tmp_path: Path, authority: ThrowawayAuthority, capsys: pytest.CaptureFixture[str]
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(SERVER_LIST)
    certificates = tmp_path / "certs"
    certificates.mkdir()
    shutil.copy(authority.certificate_path, certificates / "ca.crt")
    (certificates / "broken.crt").write_text("a certificate")
    facts = tmp_path / "out" / "pool.metrics"
    facts.parent.mkdir()

    status = render(
        "--servers", str(servers), "--pool-metrics", str(facts), "--certificates", str(certificates)
    )

    captured = capsys.readouterr()
    assert status == 0
    last_day = int(read_certificate(authority.certificate_path).not_valid_after_utc.timestamp())
    assert facts.read_text() == render_pool_metrics(POOL, {"ca.crt": last_day})
    assert FILE_RULE in section(captured.out, "frontend lean_pool")
    assert captured.err == (
        "warning: certificate broken.crt has no sample in /metrics/pool: "
        "it holds no PEM certificate\n"
    )
    assert [path.name for path in facts.parent.iterdir()] == ["pool.metrics"]


def test_render_without_the_option_writes_no_file_and_no_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(SERVER_LIST)
    assert render("--servers", str(servers)) == 0
    assert "/metrics/pool" not in capsys.readouterr().out
    assert [path.name for path in tmp_path.iterdir()] == ["servers"]


def test_the_facts_without_certificates_are_the_servers_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(SERVER_LIST)
    facts = tmp_path / "pool.metrics"
    assert render("--servers", str(servers), "--pool-metrics", str(facts)) == 0
    assert facts.read_text() == render_pool_metrics(POOL)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("server_list", "extra", "reason"),
    [
        ("lean-a lean-a.example:8000 4 paused\n", (), "every server in the list is paused"),
        ("not a server list\n", (), "refused"),
        (SERVER_LIST, ("--public-port", "18101"), "must differ"),
        (SERVER_LIST, ("--certificates", "{missing}"), "does not exist"),
        (
            SERVER_LIST,
            ("--max-request-bytes", "16384", "--certificates", "{many}"),
            "half its buffer",
        ),
    ],
)
def test_a_refused_render_prints_no_configuration_and_writes_no_facts(
    tmp_path: Path,
    authority: ThrowawayAuthority,
    capsys: pytest.CaptureFixture[str],
    server_list: str,
    extra: tuple[str, ...],
    reason: str,
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(server_list)
    many = tmp_path / "many"
    many.mkdir()
    for number in range(120):  # more certificates than half a 16 kB buffer has room for
        shutil.copy(
            authority.certificate_path, many / f"a-certificate-with-a-long-name-{number}.crt"
        )
    facts = tmp_path / "out" / "pool.metrics"
    facts.parent.mkdir()
    places = {"missing": str(tmp_path / "nowhere"), "many": str(many)}

    status = render(
        "--servers",
        str(servers),
        "--pool-metrics",
        str(facts),
        *(argument.format(**places) for argument in extra),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert reason in captured.err
    assert list(facts.parent.iterdir()) == []


def test_certificates_without_a_file_to_write_them_to_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(SERVER_LIST)
    assert render("--servers", str(servers), "--certificates", str(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--pool-metrics, which is missing" in captured.err


def test_a_file_that_cannot_be_written_is_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(SERVER_LIST)
    facts = tmp_path / "no-such-directory" / "pool.metrics"
    assert render("--servers", str(servers), "--pool-metrics", str(facts)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("refused: ")


def test_both_options_may_come_from_the_environment(
    tmp_path: Path, authority: ThrowawayAuthority, capsys: pytest.CaptureFixture[str]
) -> None:
    servers = tmp_path / "servers"
    servers.write_text(SERVER_LIST)
    certificates = tmp_path / "certs"
    certificates.mkdir()
    shutil.copy(authority.certificate_path, certificates / "ca.crt")
    facts = tmp_path / "facts.prom"
    environment = {
        "LEANPOOL_HAPROXY_SERVERS": str(servers),
        "LEANPOOL_HAPROXY_POOL_METRICS": str(facts),
        "LEANPOOL_HAPROXY_CERTIFICATES": str(certificates),
    }
    assert main(["render"], environment) == 0
    assert 'leanpool_certificate_not_after_seconds{certificate="ca.crt"}' in facts.read_text()
    # The configuration names the file by the name it was given.
    assert " file facts.prom if METH_GET is_pool_metrics" in capsys.readouterr().out
