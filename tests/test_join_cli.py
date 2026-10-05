"""``leanpool-join`` settings: what is required, where secrets may come from, what is refused."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from tls_support import ThrowawayAuthority

from leanpool.join import TokenError, validate_token
from leanpool.join.cli import build_tls_context, parse_settings

TOKEN = "4f9d2c7a1b8e4d6f9a0c3e5b7d1f2a4c"
SCRIPT = "#!/bin/sh\necho joining\n"


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    """The files a window needs, and the options that name them."""
    (tmp_path / "join.sh").write_text(SCRIPT)
    (tmp_path / "token").write_text(f"{TOKEN}\n")
    (tmp_path / "key").write_text("pool-key\n")
    return {
        "--script": str(tmp_path / "join.sh"),
        "--token-file": str(tmp_path / "token"),
        "--tls-pem": str(tmp_path / "front.pem"),
        "--spool": str(tmp_path / "spool"),
        "--api-key-file": str(tmp_path / "key"),
    }


def options(files: dict[str, str], *without: str) -> list[str]:
    return [word for flag, value in files.items() if flag not in without for word in (flag, value)]


def usage_error(
    arguments: list[str], environment: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> str:
    with pytest.raises(SystemExit) as exit_information:
        parse_settings(arguments, environment)
    assert exit_information.value.code == 2
    return capsys.readouterr().err


def test_the_settings_of_a_window(files: dict[str, str], tmp_path: Path) -> None:
    settings = parse_settings(options(files), {})
    assert settings.token == TOKEN
    assert settings.script == SCRIPT.encode()
    assert settings.api_key == "pool-key"
    assert settings.spool == tmp_path / "spool"
    assert settings.tls_pem == tmp_path / "front.pem"
    assert (settings.host, settings.port) == ("0.0.0.0", 18110)


def test_every_setting_can_come_from_the_environment(files: dict[str, str]) -> None:
    environment = {
        "LEANPOOL_JOIN_SCRIPT": files["--script"],
        "LEANPOOL_JOIN_TOKEN_FILE": files["--token-file"],
        "LEANPOOL_JOIN_TLS_PEM": files["--tls-pem"],
        "LEANPOOL_JOIN_SPOOL": files["--spool"],
        "LEANPOOL_JOIN_API_KEY_FILE": files["--api-key-file"],
        "LEANPOOL_JOIN_HOST": "127.0.0.1",
        "LEANPOOL_JOIN_PORT": "19110",
    }
    settings = parse_settings([], environment)
    assert (settings.token, settings.api_key) == (TOKEN, "pool-key")
    assert (settings.host, settings.port) == ("127.0.0.1", 19110)
    # A flag wins over its variable.
    assert parse_settings(["--port", "19111"], environment).port == 19111


def test_the_token_and_the_key_may_be_given_in_the_environment_themselves(
    files: dict[str, str],
) -> None:
    environment = {"LEANPOOL_JOIN_TOKEN": TOKEN, "LEANPOOL_JOIN_API_KEY": "environment-key"}
    settings = parse_settings(options(files, "--token-file", "--api-key-file"), environment)
    assert (settings.token, settings.api_key) == (TOKEN, "environment-key")


def test_there_is_no_flag_that_takes_the_token_or_the_key_itself(
    files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    for flag in ("--token", "--api-key"):
        error = usage_error([*options(files), flag, "a-secret-on-the-command-line"], {}, capsys)
        assert "unrecognized arguments" in error


@pytest.mark.parametrize("missing", ["--script", "--tls-pem", "--spool"])
def test_the_script_the_certificate_and_the_spool_are_required(
    files: dict[str, str], capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    error = usage_error(options(files, missing), {}, capsys)
    assert f"{missing} (or LEANPOOL_JOIN_" in error
    assert "is required" in error


def test_a_window_needs_a_token(files: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    error = usage_error(options(files, "--token-file"), {}, capsys)
    assert "no token: set --token-file or LEANPOOL_JOIN_TOKEN" in error


def test_a_token_from_two_sources_is_refused(
    files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    error = usage_error(options(files), {"LEANPOOL_JOIN_TOKEN": TOKEN}, capsys)
    assert "not both" in error
    assert TOKEN not in error


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("", "the token is empty"),
        ("   \n", "the token is empty"),
        ("short-token", "22 to 128 characters"),
        ("a" * 21, "22 to 128 characters"),
        ("a" * 129, "22 to 128 characters"),
        ("4f9d2c7a1b8e4d6f9a0c3e5b7d1f/a4c", "letters, digits, '-' and '_' only"),
        ("4f9d2c7a1b8e4d6f 9a0c3e5b7d1f2a4c", "letters, digits, '-' and '_' only"),
        ("4f9d2c7a1b8e4d6f9a0c3e5b7d1f2a4c\nsecond-line-of-the-file", "letters, digits"),
        ("4f9d2c7a1b8e4d6f9a0c3e5b7d1f2a4ç", "letters, digits, '-' and '_' only"),
    ],
)
def test_a_malformed_token_is_refused_without_echoing_it(
    files: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    token: str,
    reason: str,
) -> None:
    with pytest.raises(TokenError, match=reason) as refusal:
        validate_token(token)
    assert token.strip() == "" or token.strip() not in str(refusal.value)
    (tmp_path / "token").write_text(token)
    error = usage_error(options(files), {}, capsys)
    assert reason in error
    assert token.strip() == "" or token.strip() not in error


@pytest.mark.parametrize("token", ["a" * 22, "A-b_9" * 6, "0123456789abcdef" * 2, "z" * 128])
def test_a_token_of_128_bits_or_more_is_accepted(token: str) -> None:
    assert validate_token(f"  {token}\n") == token


def test_an_unreadable_token_file_is_refused(
    files: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "token").unlink()
    assert "cannot read the token file" in usage_error(options(files), {}, capsys)


def test_handing_out_no_key_must_be_asked_for(
    files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    without_key = options(files, "--api-key-file")
    assert "no API key" in usage_error(without_key, {}, capsys)
    assert parse_settings([*without_key, "--pool-without-key"], {}).api_key is None
    environment = {"LEANPOOL_JOIN_POOL_WITHOUT_KEY": "1"}
    assert parse_settings(without_key, environment).api_key is None


def test_a_key_from_two_sources_or_a_malformed_one_is_refused_without_echoing_it(
    files: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = usage_error(options(files), {"LEANPOOL_JOIN_API_KEY": "another-key"}, capsys)
    assert "not both" in error
    assert "another-key" not in error
    (tmp_path / "key").write_text("two words\n")
    error = usage_error(options(files), {}, capsys)
    assert "printable ASCII without spaces" in error
    assert "two words" not in error


def test_a_missing_or_empty_script_is_refused(
    files: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "join.sh").write_text("\n")
    assert "is empty" in usage_error(options(files), {}, capsys)
    (tmp_path / "join.sh").unlink()
    assert "cannot read the join script" in usage_error(options(files), {}, capsys)


@pytest.mark.parametrize(
    "environment", [{"LEANPOOL_JOIN_PORT": "0"}, {"LEANPOOL_JOIN_POOL_WITHOUT_KEY": "maybe"}]
)
def test_an_unusable_variable_is_a_usage_error(
    files: dict[str, str], capsys: pytest.CaptureFixture[str], environment: dict[str, str]
) -> None:
    (variable,) = environment
    assert variable in usage_error(options(files), environment, capsys)


def test_the_service_speaks_tls_1_3_or_later_with_the_certificate_it_is_given(
    tmp_path: Path,
) -> None:
    front_door = ThrowawayAuthority.create(tmp_path).server("pool.example")
    context = build_tls_context(front_door)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_NONE  # a box proves itself with the token, not a key


def test_a_file_that_is_not_a_certificate_and_its_key_is_refused_without_quoting_it(
    tmp_path: Path,
) -> None:
    authority = ThrowawayAuthority.create(tmp_path)
    with pytest.raises(OSError, match="does not hold a certificate followed by its private key"):
        build_tls_context(authority.certificate_path)  # a certificate, but no key
    key_only = tmp_path / "ca.key"
    with pytest.raises(OSError, match="does not hold a certificate followed by") as refusal:
        build_tls_context(key_only)
    assert key_only.read_text().splitlines()[1] not in str(refusal.value)
    with pytest.raises(OSError, match="cannot read the certificate file"):
        build_tls_context(tmp_path / "missing.pem")
