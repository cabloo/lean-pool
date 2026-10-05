"""Settings: the flag wins, then the environment variable, then the built-in default."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from leanpool.api_key import ApiKeyError, read_api_key_file, validate_api_key
from leanpool.environment import (
    SettingsParser,
    environment_default,
    parse_port,
    parse_positive_integer,
    parse_positive_number,
    parse_switch,
    parse_whole_number,
)


def build_parser(environment: dict[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="example")
    settings = SettingsParser(parser, environment)
    settings.add("--port", "EXAMPLE_PORT", parse_port, 8000, "the port")
    settings.add("--name", "EXAMPLE_NAME", str, None, "a name")
    settings.add_switch("--verbose", "EXAMPLE_VERBOSE", False, "say more")
    return parser


def test_the_built_in_default_applies_when_nothing_is_set() -> None:
    options = build_parser({}).parse_args([])
    assert (options.port, options.name, options.verbose) == (8000, None, False)


def test_an_environment_variable_overrides_the_default() -> None:
    environment = {"EXAMPLE_PORT": "9000", "EXAMPLE_NAME": "pool", "EXAMPLE_VERBOSE": "yes"}
    options = build_parser(environment).parse_args([])
    assert (options.port, options.name, options.verbose) == (9000, "pool", True)


def test_a_flag_overrides_the_environment_variable() -> None:
    environment = {"EXAMPLE_PORT": "9000", "EXAMPLE_VERBOSE": "1"}
    options = build_parser(environment).parse_args(["--port", "9100", "--no-verbose"])
    assert (options.port, options.verbose) == (9100, False)


def test_an_empty_variable_counts_as_unset() -> None:
    assert build_parser({"EXAMPLE_PORT": ""}).parse_args([]).port == 8000


def test_an_unusable_variable_is_a_usage_error_naming_the_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_information:
        build_parser({"EXAMPLE_PORT": "70000"})
    assert exit_information.value.code == 2
    assert "EXAMPLE_PORT: port 70000 is outside 1-65535" in capsys.readouterr().err


def test_an_unusable_flag_is_a_usage_error_with_the_parsers_own_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_information:
        build_parser({}).parse_args(["--port", "http"])
    assert exit_information.value.code == 2
    assert "'http' is not a whole number" in capsys.readouterr().err


def test_the_help_text_names_each_variable_and_default() -> None:
    help_text = build_parser({}).format_help()
    assert "[env EXAMPLE_PORT; default 8000]" in help_text
    assert "[env EXAMPLE_NAME; required]" in help_text
    assert "[env EXAMPLE_VERBOSE; default off]" in help_text


def test_environment_default_returns_the_fallback_or_the_parsed_value() -> None:
    assert environment_default({}, "MISSING", parse_port, 1) == 1
    assert environment_default({"SET": "22"}, "SET", parse_port, 1) == 22
    with pytest.raises(ValueError, match="SET: 'x' is not a whole number"):
        environment_default({"SET": "x"}, "SET", parse_port, 1)


@pytest.mark.parametrize(
    "text",
    ["-1", "+1", "1.0", "1e3", " 1", "1 ", "", "01", "1_000", "\N{ARABIC-INDIC DIGIT ONE}"],
)
def test_whole_numbers_are_plain_decimal_digits_only(text: str) -> None:
    with pytest.raises(ValueError, match="not a whole number"):
        parse_whole_number(text)


def test_number_parsers_accept_what_they_should() -> None:
    assert parse_whole_number("0") == 0
    assert parse_positive_integer("12") == 12
    assert parse_port("65535") == 65535
    assert parse_positive_number("2.5") == 2.5


@pytest.mark.parametrize("text", ["0", "-1", "nan", "inf", "soon"])
def test_a_positive_number_must_be_finite_and_above_zero(text: str) -> None:
    with pytest.raises(ValueError, match="number"):
        parse_positive_number(text)


def test_out_of_range_numbers_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        parse_positive_integer("0")
    with pytest.raises(ValueError, match="outside 1-65535"):
        parse_port("0")


@pytest.mark.parametrize(
    ("text", "value"),
    [("1", True), ("TRUE", True), ("yes", True), ("on", True), ("0", False), ("No", False)],
)
def test_switch_words(text: str, value: bool) -> None:
    assert parse_switch(text) is value


def test_an_unknown_switch_word_is_refused() -> None:
    with pytest.raises(ValueError, match="neither on"):
        parse_switch("maybe")


def test_an_api_key_file_is_read_without_its_surrounding_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.write_text("  s3cret-key\n")
    assert read_api_key_file(path) == "s3cret-key"


@pytest.mark.parametrize("content", ["", "\n", "   \n"])
def test_an_empty_api_key_file_is_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "key"
    path.write_text(content)
    with pytest.raises(ApiKeyError, match="empty"):
        read_api_key_file(path)


@pytest.mark.parametrize("content", ["two keys", "first\nsecond", "tab\tkey", "clé", "bell\x07"])
def test_a_malformed_api_key_is_refused_without_echoing_it(content: str) -> None:
    with pytest.raises(ApiKeyError) as error_information:
        validate_api_key(content)
    assert content not in str(error_information.value)


def test_a_missing_api_key_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ApiKeyError, match="cannot read the API key file"):
        read_api_key_file(tmp_path / "missing")
