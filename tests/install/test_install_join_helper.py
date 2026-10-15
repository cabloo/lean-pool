"""The decisions of a join, as pure functions.

`join_helper.py` holds everything of a join that decides something (the root-side `join-open` and the join
script on the new box only orchestrate around it), so these are real unit tests: no root, no Docker, no
network. The helper is loaded from its file, as the scripts run it.
"""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent / "site"
INSTALLERS = Path(__file__).resolve().parents[2] / "deploy" / "install"
HELPER = INSTALLERS / "join_helper.py"

spec = importlib.util.spec_from_file_location("join_helper", HELPER)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
sys.modules["join_helper"] = helper  # dataclasses look their module up by name
spec.loader.exec_module(helper)

CSR = "-----BEGIN CERTIFICATE REQUEST-----\nMIIBHTCBxAIBADA=\n-----END CERTIFICATE REQUEST-----\n"
CERTIFICATE = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----\n"
SERVERS = "# The Lean servers of pool demo\nworkstation 172.29.55.1:18001 8 18201\ndesktop desktop.lan:18001 6 18201\n"
# The pool's own names, as `certificate-names` and the settings give them (see reserved_names).
RESERVED = {"workstation", "workstation.lan", "lean-pool-proxy", "pool-gate", "localhost"}
TOKEN = "Zm9vYmFyYmF6cXV4MTIzNA"  # 22 characters, as join-open makes them
PIN = "sha256//aR+oLs7t3Drg8iCuBU78cmTEW0H6s6AFU4x873jIMAc="


def request(**changes):
    record = {
        "name": "laptop",
        "lean_port": 18001,
        "agent_port": 18201,
        "workers": 6,
        "csr": CSR,
        "address": "192.168.50.44",
    }
    record.update(changes)
    return record


def run_helper(*arguments, stdin=None):
    return subprocess.run(
        [sys.executable, str(HELPER), *map(str, arguments)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


# --- the patterns are the door's own ----------------------------------------------------------------------


def test_a_join_request_is_held_to_the_doors_own_patterns_for_add_backend():
    """Decision: `join-open` validates with the SAME patterns the door uses for add-backend. The three files
    must spell them identically, so a request the door would refuse can never join through a window."""
    door = (INSTALLERS / "kimina-ctl").read_text()
    control = (INSTALLERS / "lean-pool-ctl").read_text()
    assert f"server_name='{helper.SERVER_NAME_PATTERN}'" in door
    assert f"server_address='{helper.SERVER_ADDRESS_PATTERN}'" in door
    assert f"count='{helper.COUNT_PATTERN}'" in door
    assert f'valid_server_name() {{ [[ "$1" =~ {helper.SERVER_NAME_PATTERN} ]]; }}' in control
    assert f'valid_address()     {{ [[ "$1" =~ {helper.SERVER_ADDRESS_PATTERN} ]]; }}' in control
    assert f'valid_count()       {{ [[ "$1" =~ {helper.COUNT_PATTERN} ]]' in control
    assert f'valid_port()        {{ [[ "$1" =~ {helper.PORT_PATTERN} ]]' in control
    assert (
        f"LEAN_POOL_SERVER_NAME='{helper.SERVER_NAME_PATTERN}'"
        in (INSTALLERS / "pool-lib.sh").read_text()
    )
    # The join script checks its --name with the same pattern, before it asks the pool for anything.
    assert (
        f'[[ "$name" =~ {helper.SERVER_NAME_PATTERN} ]]' in (INSTALLERS / "join-box.sh").read_text()
    )
    # One label: no dot, no capital, no underscore, 1 to 40 characters.
    for name, good in (
        ("laptop", True),
        ("lap-top-2", True),
        ("a", True),
        ("x" * 40, True),
        ("x" * 41, False),
        ("lap.top", False),
        ("workstation.lan", False),
        ("Laptop", False),
        ("lap_top", False),
        ("-laptop", False),
        ("laptop-", False),
        ("", False),
    ):
        assert (re.fullmatch(helper.SERVER_NAME_PATTERN.strip("^$"), name) is not None) is good, (
            name
        )


# --- the signing request, on the pool's host ---------------------------------------------------------------


def test_a_well_formed_request_is_accepted_with_every_field():
    accepted = helper.validate_request(request(), SERVERS, RESERVED)
    assert (
        accepted.name,
        accepted.address,
        accepted.lean_port,
        accepted.agent_port,
        accepted.workers,
    ) == ("laptop", "192.168.50.44", 18001, 18201, 6)
    assert accepted.csr == CSR


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"name": "lap top"}, "ONE DNS label"),
        ({"name": "lap/top"}, "ONE DNS label"),
        ({"name": "x" * 41}, "ONE DNS label"),
        ({"name": "Laptop"}, "lower case"),  # a certificate's name is written in lower case
        ({"name": "lap_top"}, "ONE DNS label"),  # a certificate cannot carry '_'
        ({"name": "-laptop"}, "ONE DNS label"),
        ({"name": "lap.top"}, "no dot"),  # a dotted name is a host's name, never a box's
        ({"name": "workstation.lan"}, "no dot"),  # ... and this one is the pool's own front door
        ({"name": "laptop."}, "no dot"),
        ({"name": 7}, "ONE DNS label"),
        ({"lean_port": "18001"}, "whole number"),
        ({"lean_port": True}, "whole number"),
        ({"lean_port": 7}, "lean_port"),  # one digit: the door's pattern wants 2-5
        ({"lean_port": 70000}, "lean_port"),
        ({"agent_port": 18001}, "must differ"),
        ({"workers": 0}, "workers"),
        ({"workers": 257}, "workers"),
        ({"workers": 6.5}, "whole number"),
        ({"address": "laptop.lan"}, "IPv4"),
        ({"address": "fe80::1"}, "IPv4"),
        ({"address": "0.0.0.0"}, "IPv4"),
        ({"csr": "not a request"}, "PEM signing request"),
        ({"csr": CSR + CSR}, "PEM signing request"),
        ({"csr": CSR.replace("CERTIFICATE REQUEST", "PRIVATE KEY")}, "PEM signing request"),
        (
            {
                "csr": "-----BEGIN CERTIFICATE REQUEST-----\n"
                + "A" * 20000
                + "\n-----END CERTIFICATE REQUEST-----"
            },
            "PEM",
        ),
    ],
)
def test_a_malformed_field_is_refused_before_anything_is_signed(changes, reason):
    with pytest.raises(helper.Refused, match=reason):
        helper.validate_request(request(**changes), SERVERS, RESERVED)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        "laptop",
        {**request(), "extra": 1},
        {key: value for key, value in request().items() if key != "csr"},
    ],
)
def test_a_request_of_the_wrong_shape_is_refused(record):
    with pytest.raises(helper.Refused, match="exactly"):
        helper.validate_request(record, SERVERS, RESERVED)


def test_a_name_or_an_address_already_in_the_server_list_is_refused():
    with pytest.raises(helper.Refused, match="'desktop' is already in the pool"):
        helper.validate_request(request(name="desktop"), SERVERS, RESERVED)
    with pytest.raises(helper.Refused, match="already in the pool as 'workstation'"):
        helper.validate_request(request(address="172.29.55.1"), SERVERS, RESERVED)
    listed_in_capitals = SERVERS.replace("desktop ", "Desktop ")  # TLS compares names in lower case
    with pytest.raises(helper.Refused, match="already in the pool"):
        helper.validate_request(request(name="desktop"), listed_in_capitals, RESERVED)


# --- the pool's own names are never signed for a box ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("workstation", id="a DNS name on the front door's certificate"),
        pytest.param("lean-pool-proxy", id="the name on the proxy's client certificate"),
        pytest.param("pool-gate", id="the join host"),
        pytest.param("localhost", id="every machine's own name"),
    ],
)
def test_a_box_cannot_join_under_one_of_the_pools_own_names(name):
    """With these signed as server certificates, a box would pass for the pool's front door
    (or for its proxy's name) to every client that trusts the pool's authority. The server list is empty
    here, so nothing but the reservation refuses them."""
    with pytest.raises(helper.Refused, match="a name of the pool itself"):
        helper.validate_request(request(name=name), "", RESERVED)


@pytest.mark.parametrize(
    "reserved", [{"Workstation"}, {"WORKSTATION", "x"}, {"workStation.LAN", "Workstation"}]
)
def test_the_pools_names_are_compared_without_regard_to_letter_case(reserved):
    with pytest.raises(helper.Refused, match="a name of the pool itself"):
        helper.validate_request(request(name="workstation"), "", reserved)
    with pytest.raises(helper.Refused):  # and a request in capitals is refused as well
        helper.validate_request(request(name="Workstation"), "", reserved)


def test_localhost_is_reserved_whatever_the_pool_says_and_other_names_pass():
    with pytest.raises(helper.Refused, match="a name of the pool itself"):
        helper.validate_request(request(name="localhost"), "", {"workstation"})
    assert (
        helper.validate_request(request(name="workstation-2"), "", RESERVED).name == "workstation-2"
    )
    assert helper.validate_request(request(name="lan"), "", RESERVED).name == "lan"


def test_the_reserved_names_are_read_one_a_line_in_lower_case():
    text = "workstation\nWorkstation.LAN\n\n  Lean-Pool-Proxy  \n127.0.0.1\n"
    assert helper.reserved_names(text) == {
        "workstation",
        "workstation.lan",
        "lean-pool-proxy",
        "127.0.0.1",
        "localhost",
    }
    assert helper.reserved_names("") == {"localhost"}


def test_the_names_on_a_certificate_are_its_common_name_and_its_dns_names(tmp_path):
    """`certificate-names` is how `join-open` learns the pool's own names: from the certificates that are
    installed (the front door's and the proxy's), with the pool image's Python. A file with the key after
    the certificate, as HAProxy takes it, is read the same."""
    x509 = pytest.importorskip("cryptography.x509")
    import datetime
    import ipaddress

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "workstation")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("workstation"),
                    x509.DNSName("workstation.lan"),
                    x509.DNSName("pool.example"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    assert helper.certificate_names(pem) == [
        "workstation",
        "workstation",
        "workstation.lan",
        "pool.example",
    ]
    (tmp_path / "front.pem").write_bytes(pem)
    result = run_helper("certificate-names", tmp_path / "front.pem")
    assert result.returncode == 0 and result.stdout.split() == [
        "workstation",
        "workstation",
        "workstation.lan",
        "pool.example",
    ]
    assert "PRIVATE" not in result.stdout
    (tmp_path / "junk.pem").write_text("not a certificate\n")
    assert run_helper("certificate-names", tmp_path / "junk.pem").returncode == 1


# --- what reaches a terminal or a reply -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("plain words, kept: 100% ~ok~", "plain words, kept: 100% ~ok~"),
        ("line one\nline two\r\n\tthree", "line one line two three"),
        ("clear \x1b[2J\x1b[31mred\x07 bell", "clear [2J [31mred bell"),  # no escape, no bell
        ("caf\u00e9 \u202eevil \u0430dmin", "caf evil dmin"),  # no letter of another alphabet
        ("\x9b31m eight-bit escape", "31m eight-bit escape"),
        ("x" * 400, "x" * 297 + "..."),
    ],
)
def test_text_from_another_machine_is_shown_as_printable_ascii_only_and_capped(text, shown):
    """Such text went to the operator's terminal with only its line breaks removed."""
    assert helper.one_line(text) == shown
    assert (
        all(" " <= character <= "~" for character in helper.one_line(text))
        and len(helper.one_line(text)) <= helper.TEXT_LIMIT
    )


def test_what_is_written_into_a_reply_is_printable_ascii_too():
    hostile = "asks for \x1b]0;owned\x07 \u043e\u0440 names" + "y" * 900
    for reply in (
        helper.refusal_response(hostile)["reason"],
        helper.verdict_response(False, hostile)["detail"],
        helper.admission_detail(json.dumps({"error": hostile}), 3),
        helper.reason_of(json.dumps({"reason": hostile})),
        helper.read_verdict({"admitted": False, "detail": hostile})[1],
    ):
        assert all(" " <= character <= "~" for character in reply) and len(reply) <= 600, reply


def test_a_refusal_never_repeats_a_value_that_failed_its_pattern():
    """The reason is printed on the pool's host and sent back to the box: nothing unvalidated goes into it."""
    hostile = "x\x1b[31m$(reboot)"
    for changes in ({"name": hostile}, {"address": hostile}, {"csr": hostile}):
        with pytest.raises(helper.Refused) as refusal:
            helper.validate_request(request(**changes), SERVERS, RESERVED)
        assert "reboot" not in str(refusal.value) and "\x1b" not in str(refusal.value)


def test_the_ready_notice_must_come_from_the_address_that_asked():
    helper.validate_ready({"address": "192.168.50.44"}, "192.168.50.44")
    for record in ({"address": "192.168.50.45"}, {}, None, ["192.168.50.44"]):
        with pytest.raises(helper.Refused):
            helper.validate_ready(record, "192.168.50.44")


# --- which address the pool dials ---------------------------------------------------------------------------

CALLER = "192.168.50.44"


def test_the_lan_name_is_used_when_this_host_and_the_proxy_both_resolve_it_to_the_caller():
    both = {"laptop.lan": {CALLER}, "laptop": {CALLER}}
    assert (
        helper.choose_address("laptop", CALLER, both, both, "lan")[0] == "laptop.lan"
    )  # NAME.lan is tried first


def test_the_lan_name_is_the_sites_domain_and_a_malformed_domain_is_refused():
    both = {"laptop.home.arpa": {CALLER}, "laptop.lan": {CALLER}}
    assert helper.choose_address("laptop", CALLER, both, both, "home.arpa")[0] == "laptop.home.arpa"
    for domain in ("", "LAN", "lan;rm", ".lan", "lan/x"):
        with pytest.raises(helper.Refused):
            helper.choose_address("laptop", CALLER, both, both, domain)


def test_the_bare_name_is_used_when_only_it_resolves_to_the_caller():
    host, note = helper.choose_address(
        "laptop", CALLER, {"laptop": {CALLER}}, {"laptop": {CALLER, "10.0.0.9"}}, "lan"
    )
    assert host == "laptop" and "WARNING" not in note


@pytest.mark.parametrize(
    ("host_lookup", "proxy_lookup"),
    [
        ({}, {}),  # nobody knows the name
        (
            {"laptop.lan": {"192.168.50.99"}},
            {"laptop.lan": {"192.168.50.99"}},
        ),  # the name is another machine
        ({"laptop.lan": {CALLER}}, {}),  # only /etc/hosts knows it: HAProxy could never dial it
        ({}, {"laptop.lan": {CALLER}}),  # the admission test could not dial it from this host
    ],
)
def test_the_callers_address_is_used_with_a_warning_when_a_name_does_not_lead_to_it(
    host_lookup, proxy_lookup
):
    host, note = helper.choose_address("laptop", CALLER, host_lookup, proxy_lookup, "lan")
    assert host == CALLER
    assert note.startswith("WARNING") and "can change" in note


def test_lookup_output_is_read_back_as_it_was_printed():
    assert helper.parse_lookup("laptop.lan 192.168.50.44 10.0.0.2\nlaptop\n\n") == {
        "laptop.lan": {"192.168.50.44", "10.0.0.2"},
        "laptop": set(),
    }
    assert helper.lookup(["no-such-host.invalid"]) == {"no-such-host.invalid": set()}


# --- what goes into the spool -------------------------------------------------------------------------------


def test_the_responses_have_the_shape_the_join_service_reads():
    """`responses/certificate.json` is {certificate, ca} or {reason}; `verdict.json` is
    {admitted, detail}. (The service adds the API key to the certificate reply itself.)"""
    assert helper.certificate_response(CERTIFICATE, CERTIFICATE) == {
        "certificate": CERTIFICATE,
        "ca": CERTIFICATE,
    }
    assert helper.refusal_response("the name is taken") == {"reason": "the name is taken"}
    assert helper.verdict_response(True, "in") == {"admitted": True, "detail": "in"}
    assert helper.verdict_response(False, "line one\nline two\x1b[0m") == {
        "admitted": False,
        "detail": "line one line two [0m",
    }
    assert helper.refusal_response("")["reason"]  # the service wants a non-empty reason
    with pytest.raises(helper.Refused):
        helper.certificate_response(
            "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", CERTIFICATE
        )


def test_a_response_is_written_whole_and_readable_by_the_service(tmp_path):
    (tmp_path / "responses").mkdir()
    old_umask = os.umask(0o077)  # root's umask must not hide the file from the service
    try:
        helper.write_response(tmp_path, "verdict.json", helper.verdict_response(True, "in"))
    finally:
        os.umask(old_umask)
    written = tmp_path / "responses" / "verdict.json"
    assert json.loads(written.read_text()) == {"admitted": True, "detail": "in"}
    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert [path.name for path in (tmp_path / "responses").iterdir()] == [
        "verdict.json"
    ]  # no temporary left


def test_a_file_the_service_wrote_is_read_as_untrusted(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("root only")
    (tmp_path / "link.json").symlink_to(secret)
    with pytest.raises(helper.Refused):  # never through a link
        helper.read_untrusted_file(tmp_path / "link.json", 1024)
    (tmp_path / "big.json").write_text("x" * 2000)
    with pytest.raises(helper.Refused, match="larger"):
        helper.read_untrusted_file(tmp_path / "big.json", 1024)
    with pytest.raises(helper.Refused, match="not a regular file"):  # a directory is not a request
        helper.read_untrusted_file(tmp_path, 1024)
    os.mkfifo(tmp_path / "pipe.json")
    with pytest.raises(
        helper.Refused, match="not a regular file"
    ):  # and a pipe does not make root wait
        helper.read_untrusted_file(tmp_path / "pipe.json", 1024)
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(helper.Refused, match="not valid JSON"):
        helper.read_json_file(tmp_path / "bad.json", 1024)


# --- how admission ended --------------------------------------------------------------------------------------


def test_a_tls_failure_is_reported_in_the_admission_tests_own_words():
    """leanpool-admit exits 3 for "unreachable or the handshake failed" and says which in `error`."""
    error = "TLS with https://192.168.50.44:18001: the server's certificate is for another name than 'laptop'"
    report = json.dumps(
        {"server": "https://192.168.50.44:18001", "admitted": False, "error": error}
    )
    assert helper.admission_detail(report, 3) == error


def test_misbehaving_cases_are_named_and_a_pass_is_counted():
    cases = [
        {
            "case": f"verify/case{i}.lean",
            "expected": "accepted",
            "observed": "undecided",
            "behaved": False,
            "detail": "Lean timeout",
        }
        for i in range(5)
    ]
    report = {"admitted": False, "counts": {"cases": 12, "misbehaved": 5}, "cases": cases}
    detail = helper.admission_detail(json.dumps(report), 1)
    assert detail.startswith("5 of 12 admission cases misbehaved")
    assert (
        "verify/case0.lean expected accepted, observed undecided (Lean timeout)" in detail
        and "and 2 more" in detail
    )
    assert helper.admission_detail(
        json.dumps({"admitted": True, "counts": {"cases": 12, "misbehaved": 0}}), 0
    ) == ("all 12 admission cases behaved")
    assert (
        helper.admission_detail("usage: leanpool-admit", 2)
        == "the admission test gave no report (status 2)"
    )


# --- the join script ------------------------------------------------------------------------------------------

BODY = (INSTALLERS / "join-box.sh").read_text()
BUNDLE = "H4sIAAAAAAAAA+3BMQEAAADCoPVPbQwfoAAAAAAAAAAAAAAAAAAAAIC3AYbSVKsAKAAA\n"


def settings(**changes):
    values = {
        "url_base": "https://workstation.lan:18110",
        "token": TOKEN,
        "pin": PIN,
        "pool": "demo",
        "lean_timeout": "300",
        "server_wait": "120",
    }
    values.update(changes)
    return helper.ScriptSettings(**values)


def test_the_join_script_is_the_settings_the_body_and_the_bundle_and_ends_with_the_call_of_main(
    tmp_path,
):
    script = helper.join_script(settings(), BODY, BUNDLE)
    lines = script.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert lines[-1] == 'main "$@"'  # a download cut short defines functions and runs nothing
    assert f"JOIN_URL='https://workstation.lan:18110/j/{TOKEN}'" in lines
    assert f"JOIN_PIN='{PIN}'" in lines and "JOIN_POOL='demo'" in lines
    assert "JOIN_LEAN_TIMEOUT='300'" in lines and "JOIN_SERVER_WAIT='120'" in lines
    assert script.count(TOKEN) == 1  # the token is in the URL and nowhere else
    assert "PRIVATE KEY" not in script  # it holds no key
    path = tmp_path / "join.sh"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode == 0


def test_the_bundle_comes_back_out_of_the_script_byte_for_byte(tmp_path):
    script = helper.join_script(settings(), BODY, BUNDLE).replace('\nmain "$@"\n', "\nbundle\n")
    path = tmp_path / "join.sh"
    path.write_text(script)
    assert (
        subprocess.run(["bash", str(path)], capture_output=True, text=True, timeout=30).stdout
        == BUNDLE
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"token": "short"},
        {"token": TOKEN + "'; reboot; '"},
        {"token": "a" * 129},
        {"pin": "sha256//short"},
        {"pin": PIN + "' x='"},
        {"url_base": "http://workstation.lan:18110"},
        {"url_base": "https://workstation.lan"},
        {"url_base": "https://work station:18110"},
        {"url_base": "https://x:18110/j"},
        {"pool": "DEMO"},
        {"pool": "demo; id"},
        {"lean_timeout": "300s"},
        {"server_wait": ""},
    ],
)
def test_a_setting_that_could_break_out_of_its_quotes_is_refused(changes):
    with pytest.raises(helper.Refused) as refusal:
        helper.join_script(settings(**changes), BODY, BUNDLE)
    assert TOKEN not in str(refusal.value)  # a refusal never quotes the token


def test_a_body_without_main_or_a_bundle_that_is_not_base64_is_refused():
    with pytest.raises(helper.Refused):
        helper.join_script(settings(), "echo hello\n", BUNDLE)
    with pytest.raises(helper.Refused):
        helper.join_script(settings(), BODY, "LEANPOOL_BUNDLE_END\nrm -rf /\n")
    with pytest.raises(helper.Refused):
        helper.join_script(settings(), BODY, "")


# --- on the new box --------------------------------------------------------------------------------------------

GIB_IN_KIB = 1024 * 1024


@pytest.mark.parametrize(
    ("cores", "available_gib", "expected"),
    [
        (16, 28.6, 7),  # memory decides: 28.6 / 4
        (8, 64, 6),  # cores decide: 8 - 2
        (32, 256, 30),
        (4, 9, 2),  # both give 2
        (2, 64, 1),  # cores - 2 would be 0: at least 1
        (1, 64, 1),
        (16, 3, 1),  # under 4 GiB: at least 1
        (16, 0, 1),
    ],
)
def test_workers_default_to_the_smaller_of_cores_minus_two_and_available_memory_over_four_gib(
    cores, available_gib, expected
):
    workers, explanation = helper.default_workers(cores, int(available_gib * GIB_IN_KIB))
    assert workers == expected
    assert explanation.startswith(f"{expected} workers") and f"{cores} cores" in explanation


def test_the_csr_body_is_exactly_the_five_fields_the_service_takes():
    body = helper.csr_body("laptop", 18001, 18201, 6, CSR)
    assert list(body) == ["name", "lean_port", "agent_port", "workers", "csr"]
    assert body == {
        "name": "laptop",
        "lean_port": 18001,
        "agent_port": 18201,
        "workers": 6,
        "csr": CSR,
    }
    for name in ("Laptop", "lap_top", "", "x" * 41, "lap.top", "laptop.lan"):
        with pytest.raises(helper.Refused):
            helper.csr_body(name, 18001, 18201, 6, CSR)
    with pytest.raises(helper.Refused):
        helper.csr_body("laptop", 18001, 18201, 6, "garbage")


API_KEY = "0123456789abcdef0123456789abcdef"


def test_the_certificate_reply_is_split_into_certificate_authority_and_key():
    reply = {"certificate": CERTIFICATE, "ca": CERTIFICATE, "api_key": API_KEY}
    assert helper.split_certificate_reply(reply) == (CERTIFICATE, CERTIFICATE, API_KEY)


@pytest.mark.parametrize(
    "reply",
    [
        {"certificate": CERTIFICATE, "ca": CERTIFICATE},  # no key
        {"certificate": CERTIFICATE, "ca": CERTIFICATE, "api_key": None},  # a pool without a key
        {"certificate": CERTIFICATE, "ca": CERTIFICATE, "api_key": "short"},
        {"certificate": CERTIFICATE, "ca": CERTIFICATE, "api_key": API_KEY + "\nKIMINA_PORT=1"},
        {"certificate": "nope", "ca": CERTIFICATE, "api_key": API_KEY},
        {"certificate": CERTIFICATE, "ca": CSR, "api_key": API_KEY},
        {"reason": "refused"},
        None,
        [],
    ],
)
def test_a_reply_that_is_not_certificate_authority_and_key_is_refused_without_quoting_the_key(
    reply,
):
    with pytest.raises(helper.Refused) as refusal:
        helper.split_certificate_reply(reply)
    assert API_KEY not in str(refusal.value)


def test_the_verdict_and_a_refusals_reason_are_made_safe_to_print():
    assert helper.read_verdict({"admitted": True, "detail": "in the pool"}) == (True, "in the pool")
    assert helper.read_verdict({"admitted": False, "detail": "x\x1b[2J\ny"}) == (False, "x [2J y")
    for reply in ({"admitted": "yes", "detail": ""}, {"detail": "x"}, None):
        with pytest.raises(helper.Refused):
            helper.read_verdict(reply)
    assert helper.reason_of('{"reason": "the name is taken"}') == "the name is taken"
    assert helper.reason_of('{"detail": "Not Found"}') == "Not Found"
    assert helper.reason_of("<html>") == "no reason given"
    assert len(helper.reason_of(json.dumps({"reason": "x" * 5000}))) <= helper.TEXT_LIMIT


# --- the command line, as the scripts call it ---------------------------------------------------------------------


@pytest.fixture
def spool(tmp_path):
    (tmp_path / "spool" / "requests").mkdir(parents=True)
    (tmp_path / "spool" / "responses").mkdir()
    (tmp_path / "servers").write_text(SERVERS)
    (tmp_path / "reserved").write_text(
        "workstation\nworkstation.lan\nlean-pool-proxy\nWorkstation.lan\n"
    )
    return tmp_path


def test_request_prints_the_validated_fields_on_one_line_and_keeps_the_csr_root_only(spool):
    (spool / "spool" / "requests" / "csr.json").write_text(json.dumps(request()))
    result = run_helper(
        "request",
        "--spool",
        spool / "spool",
        "--servers",
        spool / "servers",
        "--reserved",
        spool / "reserved",
        "--csr-out",
        spool / "request.csr",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "laptop 192.168.50.44 18001 18201 6\n"
    assert (spool / "request.csr").read_text() == CSR
    assert stat.S_IMODE((spool / "request.csr").stat().st_mode) == 0o600


def test_a_refused_request_prints_its_reason_and_writes_no_csr(spool):
    (spool / "spool" / "requests" / "csr.json").write_text(json.dumps(request(name="desktop")))
    result = run_helper(
        "request",
        "--spool",
        spool / "spool",
        "--servers",
        spool / "servers",
        "--reserved",
        spool / "reserved",
        "--csr-out",
        spool / "request.csr",
    )
    assert result.returncode == 1 and "'desktop' is already in the pool" in result.stdout
    assert not (spool / "request.csr").exists()


@pytest.mark.parametrize("name", ["lean-pool-proxy", "localhost", "workstation.lan"])
def test_a_request_for_one_of_the_pools_names_is_refused_on_the_command_line_and_writes_no_csr(
    spool, name
):
    (spool / "servers").write_text("# an empty pool\n")
    (spool / "spool" / "requests" / "csr.json").write_text(json.dumps(request(name=name)))
    result = run_helper(
        "request",
        "--spool",
        spool / "spool",
        "--servers",
        spool / "servers",
        "--reserved",
        spool / "reserved",
        "--csr-out",
        spool / "request.csr",
    )
    assert result.returncode == 1 and (
        "a name of the pool itself" in result.stdout or "no dot" in result.stdout
    )
    assert not (spool / "request.csr").exists()


def test_without_the_pools_own_names_nothing_is_accepted(spool):
    """If the names on the pool's certificates could not be read, no name can be shown not to be one of them."""
    (spool / "reserved").write_text("\n")
    (spool / "spool" / "requests" / "csr.json").write_text(json.dumps(request()))
    result = run_helper(
        "request",
        "--spool",
        spool / "spool",
        "--servers",
        spool / "servers",
        "--reserved",
        spool / "reserved",
        "--csr-out",
        spool / "request.csr",
    )
    assert (
        result.returncode == 1
        and "could not be read" in result.stdout
        and not (spool / "request.csr").exists()
    )


def test_the_pool_sides_answers_reach_the_spool_through_the_command_line(spool):
    (spool / "box.crt").write_text(CERTIFICATE)
    assert (
        run_helper(
            "certificate-response",
            "--spool",
            spool / "spool",
            "--certificate",
            spool / "box.crt",
            "--ca",
            spool / "box.crt",
        ).returncode
        == 0
    )
    assert json.loads((spool / "spool" / "responses" / "certificate.json").read_text()) == {
        "certificate": CERTIFICATE,
        "ca": CERTIFICATE,
    }
    assert run_helper("refuse", "--spool", spool / "spool", "--reason", "taken").returncode == 0
    assert json.loads((spool / "spool" / "responses" / "certificate.json").read_text()) == {
        "reason": "taken"
    }
    assert (
        run_helper(
            "verdict", "--spool", spool / "spool", "--admitted", "no", "--detail", "wrong name"
        ).returncode
        == 0
    )
    verdict = spool / "spool" / "responses" / "verdict.json"
    assert json.loads(verdict.read_text()) == {"admitted": False, "detail": "wrong name"}
    told = run_helper("verdict-of", "--reply", verdict)
    assert told.returncode == helper.NOT_ADMITTED and told.stdout == "wrong name\n"
    assert (
        run_helper("verdict-of", "--reply", spool / "servers").returncode == 1
    )  # no readable verdict


def test_the_box_side_writes_the_key_into_a_root_only_file_and_prints_none_of_it(spool):
    reply = spool / "reply.json"
    reply.write_text(
        json.dumps({"certificate": CERTIFICATE, "ca": CERTIFICATE, "api_key": API_KEY})
    )
    (spool / "tls").mkdir()
    result = run_helper(
        "certificate",
        "--reply",
        reply,
        "--out-dir",
        spool / "tls",
        "--api-key-out",
        spool / "api-key",
    )
    assert result.returncode == 0, result.stderr
    assert API_KEY not in result.stdout + result.stderr
    assert (spool / "api-key").read_text().strip() == API_KEY
    assert stat.S_IMODE((spool / "api-key").stat().st_mode) == 0o600
    assert (spool / "tls" / "box.crt").read_text() == CERTIFICATE and (
        spool / "tls" / "ca.crt"
    ).read_text() == CERTIFICATE


def test_the_script_command_takes_the_token_from_a_file_and_writes_a_private_script(spool):
    """The token is a secret: it is read from a file, never passed as an argument (a command line is visible
    to every user of the machine)."""
    (spool / "token").write_text(TOKEN + "\n")
    (spool / "bundle.b64").write_text(BUNDLE)
    out = spool / "join.sh"
    result = run_helper(
        "script",
        "--url-base",
        "https://workstation.lan:18110",
        "--token-file",
        spool / "token",
        "--pin",
        PIN,
        "--pool",
        "demo",
        "--lean-timeout",
        "300",
        "--server-wait",
        "120",
        "--body",
        INSTALLERS / "join-box.sh",
        "--bundle",
        spool / "bundle.b64",
        "--out",
        out,
    )
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr
    assert (
        stat.S_IMODE(out.stat().st_mode) == 0o600
        and out.read_text().splitlines()[-1] == 'main "$@"'
    )
    assert "--token " not in HELPER.read_text() and re.search(
        r'command\(\s*"script".*?token_file=path', HELPER.read_text(), re.S
    )


def test_the_helper_runs_from_standard_input_as_join_open_runs_it_inside_the_proxys_namespace():
    """`join-open` pipes this file into the pool image's Python (`python - lookup NAME...`) to resolve a name
    as the proxy does. It must work without being a file on that side."""
    result = subprocess.run(
        [sys.executable, "-", "lookup", "localhost"],
        input=HELPER.read_text(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0 and result.stdout.startswith("localhost 127.0.0.1")


# --- the image a pool ships ------------------------------------------------------------------------------------------------

LEAN_TAG = "acme/kimina:lean-demo-1"
IMAGE_SHA = "5d" * 32


def image_record(**changes):
    record = {
        "tag": LEAN_TAG,
        "image_id": "sha256:" + "ab" * 32,
        "layers": "cd" * 32,
        "file": f"image-{IMAGE_SHA[:12]}.tar",
        "sha256": IMAGE_SHA,
        "bytes": 12_345_678_901,
        "machine": "x86_64",
        "format": "docker-save",
        "docker": "28.0.4",
    }
    record.update(changes)
    return record


def test_a_manifest_is_read_field_by_field_and_written_back_as_it_was():
    manifest = helper.validate_image_manifest(image_record())
    assert (
        manifest.record() == image_record()
        and tuple(manifest.record()) == helper.IMAGE_MANIFEST_FIELDS
    )
    assert (manifest.size, manifest.gigabytes) == (12_345_678_901, "12.3 GB")
    assert (
        helper.gib_rounded_up(manifest.size) == 12
        and helper.gib_rounded_up(1) == 1
        and helper.gib_rounded_up(2**30) == 1
    )
    assert (
        helper.validate_image_manifest(image_record(docker="27\x1b[31m.1")).docker == "27 [31m.1"
    )  # shown, so made safe


@pytest.mark.parametrize(
    "changes",
    [
        {"file": "../../etc/shadow"},
        {"file": "image-0123456789ab.tar"},  # not named after its own SHA-256
        {"file": "/srv/lean-pool-demo/api-key"},
        {"file": "image-5d5d5d5d5d5d.tar.gz"},
        {"sha256": "5D" * 32},
        {"sha256": "5d" * 31},
        {"layers": "cd" * 31},
        {"image_id": "ab" * 32},
        {"tag": "acme/kimina"},
        {"tag": "x:y; rm -rf /"},
        {"tag": ""},
        {"machine": "x86 64"},
        {"machine": ""},
        {"format": "oci-archive"},
        {"bytes": 0},
        {"bytes": -1},
        {"bytes": "12345"},
        {"bytes": True},
        {"bytes": 1.5},
        {"bytes": 201 * 1024**3},
    ],
)
def test_a_manifest_with_one_field_that_is_not_what_it_must_be_is_refused(changes):
    with pytest.raises(helper.Refused):
        helper.validate_image_manifest(image_record(**changes))


@pytest.mark.parametrize("record", [None, [], "manifest", 7, {}])
def test_what_is_not_a_manifest_is_refused(record):
    with pytest.raises(helper.Refused):
        helper.validate_image_manifest(record)


def test_a_box_takes_an_image_of_its_own_machine_type_and_tag_and_no_other():
    manifest = helper.validate_image_manifest(image_record())
    assert helper.image_plan(manifest, "x86_64", LEAN_TAG) is None
    assert (
        helper.image_plan(manifest, "aarch64", LEAN_TAG)
        == "the pool's image is for x86_64 machines and this box is aarch64"
    )
    other = "acme/kimina:lean-default-1"
    assert (
        helper.image_plan(manifest, "x86_64", other)
        == f"the pool's image is {LEAN_TAG} and this box's installer installs {other}"
    )


def test_the_plan_command_prints_what_the_join_script_reads_line_by_line(tmp_path):
    reply = tmp_path / "image.json"
    reply.write_text(json.dumps(image_record()))
    result = run_helper("image-plan", "--reply", reply, "--machine", "x86_64", "--tag", LEAN_TAG)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"image-{IMAGE_SHA[:12]}.tar",
        IMAGE_SHA,
        "12345678901",
        "cd" * 32,
        "12",
        "28.0.4",
        "12.3 GB",
    ]
    other = run_helper("image-plan", "--reply", reply, "--machine", "riscv64", "--tag", LEAN_TAG)
    assert other.returncode == helper.NOT_FOR_THIS_BOX == 4
    assert other.stdout == "the pool's image is for x86_64 machines and this box is riscv64\n"
    reply.write_text(json.dumps(image_record(file="../x")))
    refused = run_helper("image-plan", "--reply", reply, "--machine", "x86_64", "--tag", LEAN_TAG)
    assert (
        refused.returncode == 1
        and refused.stdout == ""
        and "'file' in the image manifest" in refused.stderr
    )
    reply.write_text("x" * (helper.MAXIMUM_MANIFEST_BYTES + 1))
    assert (
        run_helper(
            "image-plan", "--reply", reply, "--machine", "x86_64", "--tag", LEAN_TAG
        ).returncode
        == 1
    )
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "image.json")  # never read through a link
    assert (
        run_helper(
            "image-plan", "--reply", link, "--machine", "x86_64", "--tag", LEAN_TAG
        ).returncode
        == 1
    )


def test_the_pools_host_writes_a_manifest_only_of_what_a_box_would_accept_and_for_the_services_group(
    tmp_path,
):
    out = tmp_path / "manifest.json"
    arguments = [
        "image-manifest",
        "--tag",
        LEAN_TAG,
        "--image-id",
        "sha256:" + "ab" * 32,
        "--layers",
        "cd" * 32,
        "--file",
        f"image-{IMAGE_SHA[:12]}.tar",
        "--sha256",
        IMAGE_SHA,
        "--bytes",
        "12345678901",
        "--machine",
        "x86_64",
        "--docker",
        "28.0.4",
        "--out",
        out,
    ]
    result = run_helper(*arguments)
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(out.read_text()) == image_record() and stat.S_IMODE(out.stat().st_mode) == 0o640
    )
    out.unlink()
    arguments[arguments.index("--file") + 1] = "image.tar"
    assert run_helper(*arguments).returncode == 1 and not out.exists()


def test_an_export_is_shown_only_while_it_is_of_the_image_the_host_holds_and_its_file_is_whole(
    tmp_path,
):
    record = image_record(bytes=11)
    (tmp_path / "manifest.json").write_text(json.dumps(record))
    file = tmp_path / record["file"]
    file.write_bytes(b"hello world")

    def show(image_id):
        return run_helper(
            "image-export",
            "--manifest",
            tmp_path / "manifest.json",
            "--directory",
            tmp_path,
            "--image-id",
            image_id,
        )

    shown = show(record["image_id"])
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.splitlines() == [
        LEAN_TAG,
        record["file"],
        "11",
        "x86_64",
        "0.0 GB",
        "28.0.4",
    ]
    changed = show("sha256:" + "ef" * 32)
    assert changed.returncode == 1 and "the host's image changed" in changed.stderr
    assert show("none").returncode == 1  # the host holds no such image at all
    file.write_bytes(b"hello")
    cut = show(record["image_id"])
    assert (
        cut.returncode == 1 and "is not a file of the 11 bytes the manifest records" in cut.stderr
    )
    file.unlink()
    assert "is not there" in show(record["image_id"]).stderr
    file.symlink_to(tmp_path / "manifest.json")
    assert show(record["image_id"]).returncode == 1  # a link is not the exported file


def test_the_file_pattern_is_the_join_services_own():
    """The join service serves `image-<12 hex>.tar` and nothing else; the helper must name files the same way."""
    service = (INSTALLERS.parents[1] / "leanpool" / "join" / "image.py").read_text()
    assert '_FILE_PATTERN = re.compile(r"image-[0-9a-f]{12}\\.tar")' in service
    assert helper.IMAGE_FILE_PATTERN == r"^image-[0-9a-f]{12}\.tar$"
    assert (
        "MAXIMUM_MANIFEST_BYTES = 16 * 1024" in service
        and helper.MAXIMUM_MANIFEST_BYTES == 16 * 1024
    )
