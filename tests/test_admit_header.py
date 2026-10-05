"""Moving a file's leading imports above its leading comments."""

from __future__ import annotations

import pytest

from leanpool.admit import hoist_imports


def kimina_header(source: str) -> list[str]:
    """Kimina's own rule: the header is the leading lines that are blank or start with import."""
    header: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("import "):
            break
        if stripped:
            header.append(stripped)
    return header


LICENSED = """\
/-
Copyright (c) 2026 Someone. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
-/
import Mathlib
import Aesop

theorem two : 1 + 1 = 2 := by rfl
"""


def test_imports_after_a_license_comment_move_to_the_top() -> None:
    hoisted = hoist_imports(LICENSED)
    assert hoisted == (
        "import Mathlib\n"
        "import Aesop\n"
        "/-\n"
        "Copyright (c) 2026 Someone. All rights reserved.\n"
        "Released under Apache 2.0 license as described in the file LICENSE.\n"
        "-/\n"
        "\n"
        "theorem two : 1 + 1 = 2 := by rfl\n"
    )


def test_kimina_sees_no_header_before_and_the_whole_header_after() -> None:
    assert kimina_header(LICENSED) == []
    assert kimina_header(hoist_imports(LICENSED)) == ["import Mathlib", "import Aesop"]


def test_nothing_but_line_order_changes() -> None:
    assert sorted(hoist_imports(LICENSED).splitlines()) == sorted(LICENSED.splitlines())


@pytest.mark.parametrize(
    "source",
    [
        "import Mathlib\nimport Aesop\n\ntheorem two : 1 + 1 = 2 := by rfl\n",
        "\n\nimport Mathlib\n\nimport Aesop\ntheorem two : 1 + 1 = 2 := by rfl\n",
        "import Mathlib\n-- a comment after the imports\ntheorem two : 1 + 1 = 2 := by rfl\n",
        "theorem two : 1 + 1 = 2 := by rfl\n",
        "-- only a comment\n",
        "",
    ],
)
def test_a_file_whose_imports_already_come_first_is_unchanged(source: str) -> None:
    assert hoist_imports(source) is source


def test_imports_after_a_line_comment_move() -> None:
    source = "-- SPDX-License-Identifier: Apache-2.0\nimport Mathlib\ndef x := 1\n"
    assert hoist_imports(source) == (
        "import Mathlib\n-- SPDX-License-Identifier: Apache-2.0\ndef x := 1\n"
    )


def test_an_import_inside_a_block_comment_does_not_move() -> None:
    source = "/-\nimport Fake\n-/\nimport Real\ndef x := 1\n"
    assert hoist_imports(source) == "import Real\n/-\nimport Fake\n-/\ndef x := 1\n"


def test_an_import_inside_a_nested_block_comment_does_not_move() -> None:
    source = "/- outer /- inner -/\nimport StillInTheComment\n-/\nimport Real\ndef x := 1\n"
    assert hoist_imports(source) == (
        "import Real\n/- outer /- inner -/\nimport StillInTheComment\n-/\ndef x := 1\n"
    )


def test_an_import_inside_a_line_comment_does_not_move() -> None:
    source = "-- import Fake\nimport Real\ndef x := 1\n"
    assert hoist_imports(source) == "import Real\n-- import Fake\ndef x := 1\n"


def test_a_block_comment_opener_inside_a_line_comment_opens_nothing() -> None:
    source = "-- see /- below\nimport Real\ndef x := 1\n"
    assert hoist_imports(source) == "import Real\n-- see /- below\ndef x := 1\n"


@pytest.mark.parametrize(
    "source",
    [
        "/-! # Module documentation -/\nimport Late\ndef x := 1\n",
        "/-- A documentation comment. -/\nimport Late\ndef x := 1\n",
        "-- header\n/-!\nimport InsideTheDocumentation\n-/\nimport Late\n",
    ],
)
def test_documentation_is_a_command_to_lean_so_an_import_after_it_stays_misplaced(
    source: str,
) -> None:
    assert hoist_imports(source) is source


def test_an_import_after_the_first_command_does_not_move() -> None:
    source = "-- header\nimport Early\ndef x := 1\nimport Late\n"
    assert hoist_imports(source) == "import Early\n-- header\ndef x := 1\nimport Late\n"


def test_a_file_whose_only_import_is_misplaced_is_unchanged() -> None:
    source = "-- header\ndef x := 1\nimport Late\n"
    assert hoist_imports(source) is source


def test_imports_keep_their_order_and_their_trailing_comments() -> None:
    source = "-- header\nimport B -- second library\n-- between\nimport A\ndef x := 1\n"
    assert hoist_imports(source) == (
        "import B -- second library\nimport A\n-- header\n-- between\ndef x := 1\n"
    )


def test_a_last_line_without_a_newline_does_not_fuse_with_the_next() -> None:
    assert hoist_imports("-- header\nimport Mathlib") == "import Mathlib\n-- header\n"


def test_windows_line_endings_are_kept() -> None:
    source = "-- header\r\nimport Mathlib\r\ndef x := 1\r\n"
    assert hoist_imports(source) == "import Mathlib\r\n-- header\r\ndef x := 1\r\n"


def test_an_indented_import_moves_as_it_is() -> None:
    source = "-- header\n  import Mathlib\ndef x := 1\n"
    assert hoist_imports(source) == "  import Mathlib\n-- header\ndef x := 1\n"


@pytest.mark.parametrize(
    "source",
    [
        # The import shares its line with a comment that closes or opens around it: moving the
        # line would move part of the comment, so the line stays.
        "/- license -/ import Mathlib\ndef x := 1\n",
        "/- license\n-/ import Mathlib\ndef x := 1\n",
        "-- header\nimport Mathlib /- a comment that\ncontinues -/\ndef x := 1\n",
    ],
)
def test_an_import_sharing_its_line_with_part_of_a_comment_is_left_alone(source: str) -> None:
    assert hoist_imports(source) is source


def test_hoisting_twice_changes_nothing_more() -> None:
    once = hoist_imports(LICENSED)
    assert hoist_imports(once) is once
