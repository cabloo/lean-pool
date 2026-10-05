"""Reading a generated ``haproxy.cfg`` back, section by section, for the tests."""

from __future__ import annotations


def sections(config: str) -> dict[str, list[str]]:
    """Every section's directives, without comments, by the section's title."""
    blocks = [block.splitlines() for block in config.split("\n\n")]
    return {
        block[0]: [line.strip() for line in block[1:] if not line.strip().startswith("#")]
        for block in blocks
        if not block[0].startswith("#")
    }


def directives(config: str, keyword: str) -> list[tuple[str, str]]:
    """Every ``bind`` or ``server`` line of a configuration, with the section it is in."""
    return [
        (title, line)
        for title, lines in sections(config).items()
        for line in lines
        if line.startswith(f"{keyword} ")
    ]
