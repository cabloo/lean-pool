"""Settings come from environment variables, and command-line flags override them.

Every command in this project is configured the same way: the flag wins, then the environment
variable, then the built-in default. Containers set variables; a person at a shell passes flags.
Standard library only, because the usage agent and the HAProxy tooling must run on a bare Python.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable, Mapping
from typing import TypeVar

Value = TypeVar("Value")

MAXIMUM_PORT = 65535
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def parse_whole_number(text: str) -> int:
    """Read a non-negative whole number written in plain decimal digits.

    Signs, spaces, underscores and leading zeros are refused: a value that needs interpreting
    (is ``08`` eight, or a typo?) is more likely a mistake than an intention.
    """
    is_canonical = text.isascii() and text.isdigit() and (text == "0" or not text.startswith("0"))
    if not is_canonical:
        raise ValueError(f"{text!r} is not a whole number")
    return int(text)


def parse_positive_integer(text: str) -> int:
    """Read a whole number of at least 1."""
    value = parse_whole_number(text)
    if value < 1:
        raise ValueError("must be at least 1")
    return value


def parse_port(text: str) -> int:
    """Read a TCP port, 1 to 65535."""
    value = parse_whole_number(text)
    if not 1 <= value <= MAXIMUM_PORT:
        raise ValueError(f"port {value} is outside 1-{MAXIMUM_PORT}")
    return value


def parse_positive_number(text: str) -> float:
    """Read a finite number greater than zero, such as a duration in seconds."""
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{text!r} is not a positive number")
    return value


def parse_switch(text: str) -> bool:
    """Read an on/off value from an environment variable (1/0, true/false, yes/no, on/off)."""
    word = text.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"{text!r} is neither on (1, true, yes, on) nor off (0, false, no, off)")


def environment_default(
    environment: Mapping[str, str],
    variable: str,
    parse: Callable[[str], Value],
    fallback: Value,
) -> Value:
    """Return the variable's parsed value, or ``fallback`` when it is unset or empty.

    An empty variable counts as unset because that is what a compose file produces for a
    variable left blank. A value that does not parse raises ``ValueError`` naming the variable.
    """
    raw = environment.get(variable, "")
    if raw == "":
        return fallback
    try:
        return parse(raw)
    except ValueError as error:
        raise ValueError(f"{variable}: {error}") from error


class SettingsParser:
    """Adds options to an argument parser so each one defaults to an environment variable.

    The help text of every option names its variable and default, so ``--help`` is a complete
    configuration reference. A variable holding an unusable value ends the command with the
    usage error exit status (2), the same as an unusable flag.
    """

    def __init__(self, parser: argparse.ArgumentParser, environment: Mapping[str, str]) -> None:
        self._parser = parser
        self._environment = environment

    def add(
        self,
        flag: str,
        variable: str,
        parse: Callable[[str], Value],
        fallback: Value | None,
        description: str,
        *,
        optional: bool = False,
    ) -> None:
        """Add a value option. ``fallback`` None means the setting has no built-in default.

        Such a setting is described as required, unless ``optional`` says it may be left out.
        """
        self._parser.add_argument(
            flag,
            type=_argument_type(parse),
            default=self._default(variable, parse, fallback),
            metavar=flag.lstrip("-").upper().replace("-", "_"),
            help=_help_text(description, variable, _default_text(fallback, optional)),
        )

    def add_switch(self, flag: str, variable: str, fallback: bool, description: str) -> None:
        """Add an on/off option; ``--no-<flag>`` turns off what the environment turned on."""
        self._parser.add_argument(
            flag,
            action=argparse.BooleanOptionalAction,
            default=self._default(variable, parse_switch, fallback),
            help=_help_text(description, variable, _default_text("on" if fallback else "off")),
        )

    def _default(
        self, variable: str, parse: Callable[[str], Value], fallback: Value | None
    ) -> Value | None:
        try:
            return environment_default(self._environment, variable, parse, fallback)
        except ValueError as error:
            self._parser.error(str(error))


def _argument_type(parse: Callable[[str], Value]) -> Callable[[str], Value]:
    """Wrap a parser so argparse reports its own message, not "invalid <function name> value"."""

    def convert(text: str) -> Value:
        try:
            return parse(text)
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from error

    return convert


def _default_text(fallback: object, optional: bool = False) -> str:
    if fallback is not None:
        return f"default {fallback}"
    return "optional" if optional else "required"


def _help_text(description: str, variable: str, default: str) -> str:
    return f"{description} [env {variable}; {default}]".replace("%", "%%")
