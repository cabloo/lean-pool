"""``leanpool-cache`` settings: the pin and the key are required, flags override variables."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanpool.cache import DEFAULT_EXHAUSTION_PATTERNS
from leanpool.cache.cli import parse_settings

KEY_ENVIRONMENT = {"LEANPOOL_CACHE_PIN": "lean-4.27.0", "LEANPOOL_CACHE_API_KEY": "pool-key"}


def usage_error(
    arguments: list[str], environment: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> str:
    with pytest.raises(SystemExit) as exit_information:
        parse_settings(arguments, environment)
    assert exit_information.value.code == 2
    return capsys.readouterr().err


def test_the_defaults() -> None:
    settings = parse_settings([], KEY_ENVIRONMENT)
    assert settings.pin == "lean-4.27.0"
    assert settings.api_key == "pool-key"
    assert (settings.host, settings.port) == ("127.0.0.1", 18102)
    assert settings.upstream_url == "http://127.0.0.1:18101"
    assert settings.upstream_timeout_seconds == 1800.0
    assert settings.maximum_bytes == 4 * 1024**3
    assert settings.database_path == Path("leanpool-cache.sqlite3")
    assert (
        settings.exhaustion_patterns
        == DEFAULT_EXHAUSTION_PATTERNS
        == (
            "out of memory",
            "stack overflow",
        )
    )
    assert settings.hop_header == "X-Lean-Pool-Hop"


def test_every_setting_can_come_from_the_environment() -> None:
    environment = {
        **KEY_ENVIRONMENT,
        "LEANPOOL_CACHE_HOST": "0.0.0.0",
        "LEANPOOL_CACHE_PORT": "9002",
        "LEANPOOL_CACHE_UPSTREAM_URL": "http://proxy.example:9001",
        "LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS": "600",
        "LEANPOOL_CACHE_DATABASE": "/var/lib/leanpool/cache.sqlite3",
        "LEANPOOL_CACHE_MAX_BYTES": "1000000",
        "LEANPOOL_CACHE_MAX_REQUEST_BYTES": "2000000",
        "LEANPOOL_CACHE_EXHAUSTION_PATTERNS": "out of memory, killed ,",
        "LEANPOOL_HOP_HEADER": "X-Second-Hop",
    }
    settings = parse_settings([], environment)
    assert (settings.host, settings.port) == ("0.0.0.0", 9002)
    assert settings.upstream_url == "http://proxy.example:9001"
    assert settings.upstream_timeout_seconds == 600.0
    assert settings.database_path == Path("/var/lib/leanpool/cache.sqlite3")
    assert (settings.maximum_bytes, settings.maximum_request_bytes) == (1_000_000, 2_000_000)
    assert settings.exhaustion_patterns == ("out of memory", "killed")
    assert settings.hop_header == "X-Second-Hop"


def test_flags_override_the_environment() -> None:
    environment = {
        **KEY_ENVIRONMENT,
        "LEANPOOL_CACHE_PORT": "9002",
        "LEANPOOL_CACHE_EXHAUSTION_PATTERNS": "killed",
    }
    arguments = [
        "--pin",
        "lean-4.28.0",
        "--port",
        "9102",
        "--exhaustion-pattern",
        "out of memory",
        "--exhaustion-pattern",
        "cannot allocate",
    ]
    settings = parse_settings(arguments, environment)
    assert settings.pin == "lean-4.28.0"
    assert settings.port == 9102
    assert settings.exhaustion_patterns == ("out of memory", "cannot allocate")


def test_the_pin_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    error = usage_error([], {"LEANPOOL_CACHE_API_KEY": "pool-key"}, capsys)
    assert "--pin (or LEANPOOL_CACHE_PIN) is required" in error


def test_running_without_a_key_must_be_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    error = usage_error(["--pin", "lean-4.27.0"], {}, capsys)
    assert "no API key" in error
    assert parse_settings(["--pin", "lean-4.27.0", "--allow-unauthenticated"], {}).api_key is None
    environment = {"LEANPOOL_CACHE_PIN": "p", "LEANPOOL_CACHE_ALLOW_UNAUTHENTICATED": "1"}
    assert parse_settings([], environment).api_key is None


def test_the_key_is_read_from_a_file(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("file-key\n")
    settings = parse_settings(["--pin", "p", "--api-key-file", str(key_file)], {})
    assert settings.api_key == "file-key"
    from_variable = parse_settings(
        [], {"LEANPOOL_CACHE_PIN": "p", "LEANPOOL_CACHE_API_KEY_FILE": str(key_file)}
    )
    assert from_variable.api_key == "file-key"


def test_an_empty_key_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("\n")
    assert "the API key is empty" in usage_error(
        ["--pin", "p", "--api-key-file", str(key_file)], {}, capsys
    )


def test_a_malformed_key_in_the_environment_is_refused_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {"LEANPOOL_CACHE_PIN": "p", "LEANPOOL_CACHE_API_KEY": "two words"}
    error = usage_error([], environment, capsys)
    assert "printable ASCII without spaces" in error
    assert "two words" not in error


def test_a_key_from_two_sources_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("file-key\n")
    error = usage_error(["--api-key-file", str(key_file)], KEY_ENVIRONMENT, capsys)
    assert "not both" in error


def test_there_is_no_flag_that_takes_the_key_itself(capsys: pytest.CaptureFixture[str]) -> None:
    error = usage_error(["--pin", "p", "--api-key", "pool-key"], {}, capsys)
    assert "unrecognized arguments" in error


@pytest.mark.parametrize(
    "environment",
    [
        {"LEANPOOL_CACHE_PORT": "0"},
        {"LEANPOOL_CACHE_MAX_BYTES": "4GB"},
        {"LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS": "-1"},
        {"LEANPOOL_CACHE_EXHAUSTION_PATTERNS": " , "},
        {"LEANPOOL_CACHE_ALLOW_UNAUTHENTICATED": "maybe"},
    ],
)
def test_an_unusable_variable_is_a_usage_error(
    environment: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    (variable,) = environment
    assert variable in usage_error([], {**KEY_ENVIRONMENT, **environment}, capsys)


def test_there_is_no_screened_key_by_default() -> None:
    assert parse_settings([], KEY_ENVIRONMENT).screened_api_key is None


def test_the_screened_key_is_read_from_a_file_or_the_environment(tmp_path: Path) -> None:
    key_file = tmp_path / "screened"
    key_file.write_text("screened-key\n")
    environment = {**KEY_ENVIRONMENT, "LEANPOOL_CACHE_SCREENED_IN_FLIGHT": "8"}
    from_flag = parse_settings(["--screened-api-key-file", str(key_file)], environment)
    assert from_flag.screened_api_key == "screened-key"
    from_file_variable = parse_settings(
        [], {**environment, "LEANPOOL_CACHE_SCREENED_API_KEY_FILE": str(key_file)}
    )
    assert from_file_variable.screened_api_key == "screened-key"
    from_variable = parse_settings(
        [], {**environment, "LEANPOOL_CACHE_SCREENED_API_KEY": "screened-key"}
    )
    assert from_variable.screened_api_key == "screened-key"
    assert (from_variable.screened_in_flight, from_variable.screened_max_timeout_seconds) == (
        8,
        300,
    )


def test_the_screened_doors_limits_are_read_from_flags_or_the_environment() -> None:
    environment = {**KEY_ENVIRONMENT, "LEANPOOL_CACHE_SCREENED_API_KEY": "screened-key"}
    from_flags = parse_settings(
        ["--screened-in-flight", "3", "--screened-max-timeout", "120"], environment
    )
    assert (from_flags.screened_in_flight, from_flags.screened_max_timeout_seconds) == (3, 120)
    assert from_flags.screened_max_request_bytes == 256 * 1024
    limited = {**environment, "LEANPOOL_CACHE_SCREENED_IN_FLIGHT": "3"}
    assert (
        parse_settings(["--screened-max-request-bytes", "4096"], limited)
    ).screened_max_request_bytes == 4096
    assert (
        parse_settings([], {**limited, "LEANPOOL_CACHE_SCREENED_MAX_REQUEST_BYTES": "8192"})
    ).screened_max_request_bytes == 8192
    from_variables = parse_settings(
        [],
        {
            **environment,
            "LEANPOOL_CACHE_SCREENED_IN_FLIGHT": "5",
            "LEANPOOL_CACHE_SCREENED_MAX_TIMEOUT_SECONDS": "60",
        },
    )
    assert (from_variables.screened_in_flight, from_variables.screened_max_timeout_seconds) == (
        5,
        60,
    )


def test_the_screened_imports_are_read_from_flags_or_the_environment() -> None:
    environment = {
        **KEY_ENVIRONMENT,
        "LEANPOOL_CACHE_SCREENED_API_KEY": "screened-key",
        "LEANPOOL_CACHE_SCREENED_IN_FLIGHT": "3",
    }
    assert parse_settings([], environment).screened_imports == ()
    flags = ["--screened-import", "FormalConjectures", "--screened-import", "Batteries"]
    assert parse_settings(flags, environment).screened_imports == ("FormalConjectures", "Batteries")
    from_variable = parse_settings(
        [], {**environment, "LEANPOOL_CACHE_SCREENED_IMPORTS": "FormalConjectures, Batteries"}
    )
    assert from_variable.screened_imports == ("FormalConjectures", "Batteries")
    assert (
        parse_settings([], {**environment, "LEANPOOL_CACHE_SCREENED_IMPORTS": ""}).screened_imports
        == ()
    )


def test_a_screened_import_that_is_not_a_root_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = usage_error(["--screened-import", "Formal.Conjectures"], KEY_ENVIRONMENT, capsys)
    assert "not the first part of a Lean module's name" in error


def test_a_screened_key_without_a_limit_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    environment = {**KEY_ENVIRONMENT, "LEANPOOL_CACHE_SCREENED_API_KEY": "screened-key"}
    error = usage_error([], environment, capsys)
    assert "a screened key needs --screened-in-flight" in error
    assert "screened-key" not in error.replace("a screened key", "")


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({**KEY_ENVIRONMENT, "LEANPOOL_CACHE_SCREENED_API_KEY": "pool-key"}, "must differ"),
        (
            {
                "LEANPOOL_CACHE_PIN": "p",
                "LEANPOOL_CACHE_ALLOW_UNAUTHENTICATED": "1",
                "LEANPOOL_CACHE_SCREENED_API_KEY": "screened-key",
            },
            "needs the pool's own key",
        ),
        ({**KEY_ENVIRONMENT, "LEANPOOL_CACHE_SCREENED_API_KEY": "two words"}, "printable ASCII"),
    ],
)
def test_a_screened_key_that_cannot_work_is_refused_without_echoing_it(
    environment: dict[str, str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    error = usage_error([], environment, capsys)
    assert message in error
    assert "screened-key" not in error
    assert "two words" not in error


def test_a_screened_key_from_two_sources_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "screened"
    key_file.write_text("screened-key\n")
    environment = {**KEY_ENVIRONMENT, "LEANPOOL_CACHE_SCREENED_API_KEY": "screened-key"}
    error = usage_error(["--screened-api-key-file", str(key_file)], environment, capsys)
    assert "not both" in error
