"""``leanpool-pki``: the files each command writes, their modes, and what a refusal leaves."""

from __future__ import annotations

import os
import ssl
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from tls_support import handshake, strict_client_context, tls_server

from leanpool.pki import PkiError, read_certificate, read_private_key, read_request, write_file
from leanpool.pki.cli import main

Captured = pytest.CaptureFixture[str]


def run(*arguments: object, environment: dict[str, str] | None = None) -> int:
    return main([str(argument) for argument in arguments], environment or {})


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def contents(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def alternative_names(certificate: x509.Certificate) -> list[str]:
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [str(name.value) for name in names]


def usages(certificate: x509.Certificate) -> list[x509.ObjectIdentifier]:
    return list(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)


def common_name(certificate: x509.Certificate) -> str | bytes:
    (attribute,) = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attribute.value


@pytest.fixture
def authority(tmp_path: Path, capsys: Captured) -> Path:
    directory = tmp_path / "ca"
    assert run("init", "--ca-dir", directory) == 0
    capsys.readouterr()
    return directory


@pytest.fixture(autouse=True)
def open_umask() -> Iterator[None]:
    """A umask that hides nothing: a file's mode is then what the code set, not what luck did."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def test_init_makes_an_authority_whose_key_only_its_owner_reads(
    tmp_path: Path, capsys: Captured
) -> None:
    directory = tmp_path / "pool" / "ca"
    assert run("init", "--ca-dir", directory) == 0
    assert capsys.readouterr().out == f"{directory / 'ca.crt'}\n{directory / 'ca.key'}\n"
    assert sorted(os.listdir(directory)) == ["ca.crt", "ca.key"]
    assert mode(directory / "ca.key") == 0o600
    assert mode(directory / "ca.crt") == 0o644
    assert mode(directory) == 0o700
    certificate = read_certificate(directory / "ca.crt")
    assert common_name(certificate) == "lean-pool CA"
    assert certificate.public_key() == read_private_key(directory / "ca.key").public_key()


def test_init_takes_a_name_and_a_lifetime(tmp_path: Path, capsys: Captured) -> None:
    assert run("init", "--ca-dir", tmp_path, "--name", "example pool CA", "--days", "30") == 0
    assert common_name(read_certificate(tmp_path / "ca.crt")) == "example pool CA"
    capsys.readouterr()
    assert run("expiry", tmp_path / "ca.crt") == 0
    assert capsys.readouterr().out == "29\n"


@pytest.mark.parametrize("existing", ["ca.crt", "ca.key"])
def test_init_never_overwrites_an_authority(
    authority: Path, capsys: Captured, existing: str
) -> None:
    other = "ca.key" if existing == "ca.crt" else "ca.crt"
    (authority / other).unlink()
    before = contents(authority)
    assert run("init", "--ca-dir", authority) == 1
    assert contents(authority) == before
    assert "already exists; nothing was written" in capsys.readouterr().err


def test_init_has_no_way_to_replace_an_authority(authority: Path) -> None:
    with pytest.raises(SystemExit) as exit_information:
        run("init", "--ca-dir", authority, "--replace")
    assert exit_information.value.code == 2


def test_no_other_command_overwrites_the_authority_even_when_told_to_replace(
    authority: Path, tmp_path: Path, capsys: Captured
) -> None:
    """A certificate issued into the authority's directory under the authority's file name."""
    assert run("csr", "--out-dir", tmp_path / "box", "--name", "lean-a") == 0
    capsys.readouterr()
    before = contents(authority)
    # The same directory, spelled another way.
    same_directory = authority.parent / "." / authority.name
    issue = ["--ca-dir", authority, "--out-dir", same_directory, "--file-stem", "ca", "--replace"]
    sign = ["sign-csr", "--ca-dir", authority, "--csr", tmp_path / "box" / "lean-a.csr"]
    attempts = [
        ["issue-server", *issue, "--name", "pool.example"],
        ["issue-client", *issue, "--name", "lean-pool-proxy"],
        [*sign, "--out", same_directory / "ca.crt", "--allow-dns", "lean-a", "--replace"],
        [*sign, "--out", same_directory / "ca.key", "--allow-dns", "lean-a", "--replace"],
    ]
    for attempt in attempts:
        assert run(*attempt) == 1
        assert "is the authority's own file; it is never overwritten" in capsys.readouterr().err
        assert contents(authority) == before


def test_issue_server_writes_a_certificate_a_key_and_both_in_one_file(
    authority: Path, tmp_path: Path, capsys: Captured
) -> None:
    out = tmp_path / "front"
    arguments = ["--name", "Pool.Example", "--dns", "pool", "--ip", "192.0.2.7"]
    assert run("issue-server", "--ca-dir", authority, "--out-dir", out, *arguments) == 0
    names = ["pool.example.crt", "pool.example.key", "pool.example.pem"]
    assert capsys.readouterr().out == "".join(f"{out / name}\n" for name in names)
    assert sorted(os.listdir(out)) == names
    assert [mode(out / name) for name in names] == [0o644, 0o600, 0o600]
    certificate = read_certificate(out / "pool.example.crt")
    assert alternative_names(certificate) == ["pool.example", "pool", "192.0.2.7"]
    assert common_name(certificate) == "pool.example"
    assert usages(certificate) == [ExtendedKeyUsageOID.SERVER_AUTH]
    certificate.verify_directly_issued_by(read_certificate(authority / "ca.crt"))
    # The combined file is the certificate followed by the key: what HAProxy's `crt` takes.
    combined = (out / "pool.example.pem").read_bytes()
    assert (
        combined
        == (out / "pool.example.crt").read_bytes() + (out / "pool.example.key").read_bytes()
    )
    assert read_certificate(out / "pool.example.pem") == certificate
    assert read_private_key(out / "pool.example.pem").public_key() == certificate.public_key()


def test_issue_client_writes_a_client_certificate(authority: Path, tmp_path: Path) -> None:
    assert (
        run(
            "issue-client",
            "--ca-dir",
            authority,
            "--out-dir",
            tmp_path,
            "--name",
            "lean-pool-proxy",
        )
        == 0
    )
    certificate = read_certificate(tmp_path / "lean-pool-proxy.crt")
    assert usages(certificate) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert common_name(certificate) == "lean-pool-proxy"
    assert alternative_names(certificate) == ["lean-pool-proxy"]
    assert mode(tmp_path / "lean-pool-proxy.key") == 0o600
    assert mode(tmp_path / "lean-pool-proxy.pem") == 0o600


def test_a_client_certificate_takes_no_further_names(authority: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_information:
        run(
            "issue-client",
            "--ca-dir",
            authority,
            "--out-dir",
            tmp_path,
            "--name",
            "a",
            "--dns",
            "b",
        )
    assert exit_information.value.code == 2


def test_the_file_stem_names_the_files_and_not_the_certificate(
    authority: Path, tmp_path: Path
) -> None:
    arguments = ["--name", "pool.example", "--file-stem", "front"]
    assert run("issue-server", "--ca-dir", authority, "--out-dir", tmp_path, *arguments) == 0
    assert sorted(os.listdir(tmp_path)) == ["ca", "front.crt", "front.key", "front.pem"]
    assert common_name(read_certificate(tmp_path / "front.crt")) == "pool.example"


@pytest.mark.parametrize("stem", ["../front", "front/door", ".front", "", "front door"])
def test_a_file_stem_cannot_leave_the_directory(
    authority: Path, tmp_path: Path, capsys: Captured, stem: str
) -> None:
    out = tmp_path / "out"
    arguments = ["--name", "pool.example", "--file-stem", stem]
    assert run("issue-server", "--ca-dir", authority, "--out-dir", out, *arguments) == 1
    assert "file stem" in capsys.readouterr().err
    assert not out.exists()
    assert sorted(os.listdir(tmp_path)) == ["ca"]


@pytest.mark.parametrize("existing", ["front.crt", "front.key", "front.pem"])
def test_issuing_never_overwrites_unless_told_to(
    authority: Path, tmp_path: Path, capsys: Captured, existing: str
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / existing).write_text("someone's file\n")
    issue = ["issue-server", "--ca-dir", authority, "--out-dir", out, "--name", "pool.example"]
    issue += ["--file-stem", "front"]
    assert run(*issue) == 1
    assert contents(out) == {existing: b"someone's file\n"}
    assert "already exists; nothing was written" in capsys.readouterr().err

    assert run(*issue, "--replace") == 0
    first_key = (out / "front.key").read_bytes()
    assert run(*issue, "--replace") == 0
    assert (out / "front.key").read_bytes() != first_key  # a new key each time
    assert [mode(out / name) for name in ("front.crt", "front.key", "front.pem")] == [
        0o644,
        0o600,
        0o600,
    ]
    assert sorted(os.listdir(out)) == ["front.crt", "front.key", "front.pem"]


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (["--name", "pool_example"], "is not a DNS name"),
        (["--name", "*.example"], "is not a DNS name"),
        (["--name", "192.0.2.7"], "is not a DNS name"),
        (["--name", "a" * 30 + "." + "b" * 40], "longer than 64 characters"),
        (["--name", "pool.example", "--dns", "bad name"], "is not a DNS name"),
        (["--name", "pool.example", "--ip", "pool"], "is not an IP address"),
    ],
)
def test_a_malformed_name_is_refused_and_nothing_is_written(
    authority: Path, tmp_path: Path, capsys: Captured, arguments: list[str], reason: str
) -> None:
    out = tmp_path / "out"
    assert run("issue-server", "--ca-dir", authority, "--out-dir", out, *arguments) == 1
    assert reason in capsys.readouterr().err
    assert not out.exists()
    assert run("csr", "--out-dir", out, *arguments) == 1
    assert not out.exists()


def test_issuing_without_an_authority_is_refused(tmp_path: Path, capsys: Captured) -> None:
    out = tmp_path / "out"
    issue = ["--out-dir", out, "--name", "pool.example"]
    assert run("issue-server", "--ca-dir", tmp_path / "missing", *issue) == 1
    assert "cannot read the certificate" in capsys.readouterr().err
    assert not out.exists()


def test_an_authority_with_a_damaged_key_is_refused_without_showing_the_key(
    authority: Path, tmp_path: Path, capsys: Captured
) -> None:
    damaged = (authority / "ca.key").read_text().replace("PRIVATE KEY", "PRIVATE KEX")
    (authority / "ca.key").write_text(damaged)
    out = tmp_path / "out"
    assert (
        run("issue-server", "--ca-dir", authority, "--out-dir", out, "--name", "pool.example") == 1
    )
    captured = capsys.readouterr()
    assert "does not hold an unencrypted PEM private key" in captured.err
    assert damaged.splitlines()[1] not in captured.err
    assert not out.exists()


def test_csr_makes_a_key_that_stays_and_a_request_to_send(tmp_path: Path, capsys: Captured) -> None:
    out = tmp_path / "box"
    assert run("csr", "--out-dir", out, "--name", "Lean-A", "--ip", "192.0.2.7") == 0
    assert capsys.readouterr().out == f"{out / 'lean-a.csr'}\n{out / 'lean-a.key'}\n"
    assert sorted(os.listdir(out)) == ["lean-a.csr", "lean-a.key"]
    assert mode(out / "lean-a.key") == 0o600
    assert mode(out / "lean-a.csr") == 0o644
    request = read_request(out / "lean-a.csr")
    assert request.is_signature_valid
    assert request.public_key() == read_private_key(out / "lean-a.key").public_key()
    names = request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(name.value) for name in names] == ["lean-a", "192.0.2.7"]


def test_csr_never_overwrites_a_key_unless_told_to(tmp_path: Path, capsys: Captured) -> None:
    assert run("csr", "--out-dir", tmp_path, "--name", "lean-a") == 0
    before = contents(tmp_path)
    assert run("csr", "--out-dir", tmp_path, "--name", "lean-a") == 1
    assert contents(tmp_path) == before
    assert "already exists" in capsys.readouterr().err
    assert run("csr", "--out-dir", tmp_path, "--name", "lean-a", "--replace") == 0
    assert contents(tmp_path)["lean-a.key"] != before["lean-a.key"]


@pytest.fixture
def box_request(tmp_path: Path, capsys: Captured) -> Path:
    assert run("csr", "--out-dir", tmp_path / "box", "--name", "lean-a") == 0
    capsys.readouterr()
    return tmp_path / "box" / "lean-a.csr"


def test_sign_csr_issues_a_server_certificate_for_the_allowed_names(
    authority: Path, box_request: Path, tmp_path: Path, capsys: Captured
) -> None:
    out = tmp_path / "lean-a.crt"
    allow = ["--allow-dns", "lean-a", "--allow-ip", "192.0.2.7"]
    assert run("sign-csr", "--ca-dir", authority, "--csr", box_request, "--out", out, *allow) == 0
    assert capsys.readouterr().out == f"{out}\n"
    assert mode(out) == 0o644
    certificate = read_certificate(out)
    assert alternative_names(certificate) == ["lean-a", "192.0.2.7"]
    assert common_name(certificate) == "lean-a"
    assert usages(certificate) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert certificate.public_key() == read_request(box_request).public_key()
    certificate.verify_directly_issued_by(read_certificate(authority / "ca.crt"))


@pytest.mark.parametrize(
    ("allow", "reason"),
    [
        (["--allow-dns", "lean-b"], "asks for lean-a, which is outside the allowed names (lean-b)"),
        (["--allow-ip", "192.0.2.7"], "at least one DNS name must be allowed"),
        ([], "at least one DNS name must be allowed"),
        (["--allow-dns", "lean_a"], "is not a DNS name"),
        (["--allow-dns", "lean-a", "--allow-ip", "lean-a"], "is not an IP address"),
    ],
)
def test_sign_csr_refuses_and_writes_nothing(
    authority: Path,
    box_request: Path,
    tmp_path: Path,
    capsys: Captured,
    allow: list[str],
    reason: str,
) -> None:
    out = tmp_path / "signed" / "lean-a.crt"
    out.parent.mkdir()
    assert run("sign-csr", "--ca-dir", authority, "--csr", box_request, "--out", out, *allow) == 1
    assert reason in capsys.readouterr().err
    assert os.listdir(out.parent) == []


def test_sign_csr_refuses_a_file_that_is_not_a_request(
    authority: Path, tmp_path: Path, capsys: Captured
) -> None:
    out = tmp_path / "out.crt"
    sign = ["sign-csr", "--ca-dir", authority, "--out", out, "--allow-dns", "lean-a"]
    assert run(*sign, "--csr", authority / "ca.crt") == 1
    assert "does not hold a PEM signing request" in capsys.readouterr().err
    assert run(*sign, "--csr", tmp_path / "missing.csr") == 1
    assert "cannot read the signing request" in capsys.readouterr().err
    assert not out.exists()


def test_sign_csr_never_overwrites_a_certificate_unless_told_to(
    authority: Path, box_request: Path, tmp_path: Path, capsys: Captured
) -> None:
    out = tmp_path / "lean-a.crt"
    out.write_text("someone's file\n")
    sign = ["sign-csr", "--ca-dir", authority, "--csr", box_request, "--out", out]
    sign += ["--allow-dns", "lean-a"]
    assert run(*sign) == 1
    assert out.read_text() == "someone's file\n"
    assert "already exists" in capsys.readouterr().err
    assert run(*sign, "--replace") == 0
    assert common_name(read_certificate(out)) == "lean-a"


def test_pin_prints_the_value_curl_takes(authority: Path, tmp_path: Path, capsys: Captured) -> None:
    assert run("issue-server", "--ca-dir", authority, "--out-dir", tmp_path, "--name", "pool") == 0
    capsys.readouterr()
    assert run("pin", tmp_path / "pool.crt") == 0
    pin = capsys.readouterr().out
    assert pin.startswith("sha256//")
    assert pin.endswith("=\n")
    assert len(pin) == len("sha256//") + 44 + 1
    # The combined file names the same key.
    assert run("pin", tmp_path / "pool.pem") == 0
    assert capsys.readouterr().out == pin
    assert run("pin", authority / "ca.crt") == 0
    assert capsys.readouterr().out != pin


def test_expiry_prints_the_days_left(authority: Path, tmp_path: Path, capsys: Captured) -> None:
    issue = ["issue-server", "--ca-dir", authority, "--out-dir", tmp_path, "--name", "pool"]
    assert run(*issue) == 0
    capsys.readouterr()
    assert run("expiry", tmp_path / "pool.crt") == 0
    assert capsys.readouterr().out == "1824\n"  # 1825 days, less the moment since it was made
    assert run("expiry", authority / "ca.crt") == 0
    assert capsys.readouterr().out == "3649\n"
    assert run(*issue, "--days", "7", "--replace") == 0
    capsys.readouterr()
    assert run("expiry", tmp_path / "pool.crt") == 0
    assert capsys.readouterr().out == "6\n"


def test_pin_and_expiry_refuse_a_file_that_is_not_a_certificate(
    authority: Path, tmp_path: Path, capsys: Captured
) -> None:
    for command in ("pin", "expiry"):
        assert run(command, authority / "ca.key") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "does not hold a PEM certificate" in captured.err
        assert run(command, tmp_path / "missing.crt") == 1
        assert "cannot read the certificate" in capsys.readouterr().err


def test_no_command_ever_prints_a_private_key(
    authority: Path, tmp_path: Path, capsys: Captured
) -> None:
    out = tmp_path / "out"
    assert run("issue-server", "--ca-dir", authority, "--out-dir", out, "--name", "pool") == 0
    assert run("issue-client", "--ca-dir", authority, "--out-dir", out, "--name", "proxy") == 0
    assert run("csr", "--out-dir", out, "--name", "lean-a") == 0
    sign = ["sign-csr", "--ca-dir", authority, "--csr", out / "lean-a.csr"]
    assert run(*sign, "--out", out / "lean-a.crt", "--allow-dns", "lean-a") == 0
    assert run("pin", out / "pool.pem") == 0
    assert run("expiry", out / "pool.pem") == 0
    assert run("issue-server", "--ca-dir", authority, "--out-dir", out, "--name", "pool") == 1
    captured = capsys.readouterr()
    printed = captured.out + captured.err
    assert "PRIVATE KEY" not in printed
    key_files = [authority / "ca.key", out / "pool.key", out / "proxy.key", out / "lean-a.key"]
    for key_file in key_files:
        for line in key_file.read_text().splitlines()[1:-1]:
            assert line not in printed


def test_the_authority_directory_can_come_from_the_environment(
    authority: Path, tmp_path: Path
) -> None:
    environment = {"LEANPOOL_PKI_CA_DIR": str(authority)}
    issue = ["issue-server", "--out-dir", tmp_path / "out", "--name", "pool"]
    assert run(*issue, environment=environment) == 0
    # A flag wins over the variable.
    assert run(*issue, "--replace", "--ca-dir", tmp_path / "missing", environment=environment) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["frobnicate"],
        ["init"],  # no authority directory
        ["issue-server", "--out-dir", "out", "--name", "pool"],  # no authority directory
        ["issue-server", "--ca-dir", "ca", "--name", "pool"],  # no output directory
        ["issue-server", "--ca-dir", "ca", "--out-dir", "out"],  # no name
        ["issue-server", "--ca-dir", "ca", "--out-dir", "out", "--name", "pool", "--days", "0"],
        ["init", "--ca-dir", "ca", "--days", "ten"],
        ["csr", "--name", "lean-a"],
        ["sign-csr", "--ca-dir", "ca", "--out", "out.crt", "--allow-dns", "lean-a"],  # no request
        ["sign-csr", "--ca-dir", "ca", "--csr", "request.csr", "--allow-dns", "lean-a"],  # no --out
        ["pin"],
        ["expiry"],
    ],
)
def test_usage_errors_exit_with_status_two(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_information:
        main(arguments, {})
    assert exit_information.value.code == 2


def test_a_private_file_is_private_before_it_has_any_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is set on the empty temporary file, so a key is never readable by others."""
    modes_at_write: list[int] = []
    real_fsync = os.fsync

    def record(descriptor: int) -> None:
        modes_at_write.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record)
    write_file(tmp_path / "key", b"secret", 0o600)
    assert modes_at_write == [0o600]
    assert mode(tmp_path / "key") == 0o600


def test_a_write_that_fails_part_way_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_source: object, _target: object) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="the disk went away"):
        write_file(tmp_path / "key", b"secret", 0o600)
    assert os.listdir(tmp_path) == []

    (tmp_path / "existing").write_bytes(b"old")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="the disk went away"):
        write_file(tmp_path / "existing", b"new", 0o600, replace=True)
    assert contents(tmp_path) == {"existing": b"old"}


def test_a_file_is_put_in_place_only_if_nothing_is_there(tmp_path: Path) -> None:
    path = tmp_path / "ca.key"
    write_file(path, b"first", 0o600)
    with pytest.raises(PkiError, match="already exists; it was not overwritten"):
        write_file(path, b"second", 0o600)
    assert contents(tmp_path) == {"ca.key": b"first"}
    write_file(path, b"third", 0o644, replace=True)
    assert contents(tmp_path) == {"ca.key": b"third"}
    assert mode(path) == 0o644


def test_a_failed_write_is_reported_as_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Captured
) -> None:
    def fail(_source: object, _target: object) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(os, "link", fail)
    assert run("init", "--ca-dir", tmp_path / "ca") == 1
    assert "refused: the disk went away" in capsys.readouterr().err
    assert os.listdir(tmp_path / "ca") == []


def test_certificates_made_by_the_command_satisfy_a_strict_python_client(tmp_path: Path) -> None:
    """The pool's Lean client trusts the pool with ``ssl.create_default_context(cafile=...)``,
    host-name checking on, and (from Python 3.13) strict X.509 rules. The front door is reached
    by name and by a fixed address, so its certificate carries both.
    """
    assert run("init", "--ca-dir", tmp_path / "ca") == 0
    assert run("init", "--ca-dir", tmp_path / "other-ca") == 0
    names = ["--name", "pool.example", "--dns", "pool", "--ip", "127.0.0.1"]
    for authority_name, out in (("ca", "front"), ("other-ca", "impostor")):
        issue = ["issue-server", "--ca-dir", tmp_path / authority_name, "--out-dir", tmp_path / out]
        assert run(*issue, "--file-stem", "front", *names) == 0
    context = strict_client_context(tmp_path / "ca" / "ca.crt")
    assert context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED

    with tls_server(tmp_path / "front" / "front.pem") as (port, _failures):
        assert handshake(port, context, "pool.example") == b"ok"  # the name
        assert handshake(port, context, "127.0.0.1") == b"ok"  # the address
        with pytest.raises(ssl.SSLCertVerificationError, match="Hostname mismatch"):
            handshake(port, context, "elsewhere.example")

    with tls_server(tmp_path / "impostor" / "front.pem") as (port, _failures):
        for name in ("pool.example", "127.0.0.1"):
            with pytest.raises(ssl.SSLCertVerificationError, match="certificate verify failed"):
                handshake(port, context, name)


def test_a_signed_request_satisfies_a_strict_python_client(tmp_path: Path) -> None:
    assert run("init", "--ca-dir", tmp_path / "ca") == 0
    assert run("csr", "--out-dir", tmp_path, "--name", "lean-a") == 0
    sign = ["sign-csr", "--ca-dir", tmp_path / "ca", "--csr", tmp_path / "lean-a.csr"]
    assert run(*sign, "--out", tmp_path / "lean-a.crt", "--allow-dns", "lean-a") == 0
    combined = tmp_path / "lean-a.pem"
    combined.write_bytes(
        (tmp_path / "lean-a.crt").read_bytes() + (tmp_path / "lean-a.key").read_bytes()
    )
    with tls_server(combined) as (port, _failures):
        assert handshake(port, strict_client_context(tmp_path / "ca" / "ca.crt"), "lean-a") == b"ok"
