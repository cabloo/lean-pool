"""Moving a Lean file's imports to its first lines, where Kimina looks for them.

Kimina takes a file's header to be the ``import`` lines (and blank lines) it *starts* with, and
runs the rest as the body in an environment where that header is already loaded. Lean itself
accepts comments before the imports, and library files usually open with a license comment. For
such a file Kimina finds no header, sends the imports as part of the body, and Lean rejects them
("invalid 'import' command, it must be used in the beginning of the file"). A correct proof then
comes back as an error for a reason that has nothing to do with the server under test.
"""

from __future__ import annotations

import re

_IMPORT_KEYWORD = "import "
_BLOCK_COMMENT_OPEN = "/-"
_BLOCK_COMMENT_CLOSE = "-/"
_DOCUMENTATION_OPENERS = ("/--", "/-!")
_LINE_COMMENT = "--"
_MARKER_LENGTH = 2
_AFTER_EACH_NEWLINE = re.compile(r"(?<=\n)")


def hoist_imports(source: str) -> str:
    """Return ``source`` with its leading ``import`` lines moved above any leading comment.

    Only the file's leading region is considered: everything before the first command, where
    Lean allows imports at all. Within it:

    * an ``import`` line moves to the top, in its original order and with its line ending;
    * comments (``--`` and ``/- ... -/``) and blank lines stay where they are;
    * text inside a comment is never an import, whatever it says (block comments nest);
    * an ``import`` after the first command stays put, so Lean still reports the misplacement.
      A documentation comment (``/-- ... -/`` or ``/-! ... -/``) counts as a command, as it
      does for Lean.

    A file with no comment before its imports is returned unchanged. A line is moved only if it
    is an import from its first character and leaves no block comment open (moving half of a
    comment would change what the rest of the file means); any other line stays where it is.
    """
    lines = _AFTER_EACH_NEWLINE.split(source)
    import_indexes = _leading_import_indexes(lines)
    if not _has_comment_before_an_import(lines, import_indexes):
        return source
    moved = set(import_indexes)
    imports = [_with_line_ending(lines[index]) for index in import_indexes]
    rest = [line for index, line in enumerate(lines) if index not in moved]
    return "".join(imports + rest)


def _leading_import_indexes(lines: list[str]) -> list[int]:
    """Return the indexes of the movable import lines in the file's leading region."""
    indexes: list[int] = []
    depth = 0
    for index, line in enumerate(lines):
        depth_before = depth
        code, depth = _code_outside_comments(line, depth)
        content = code.strip()
        if not content:
            continue
        if not content.startswith(_IMPORT_KEYWORD):
            break
        starts_as_import = line.lstrip().startswith(_IMPORT_KEYWORD)
        if depth_before == 0 and depth == 0 and starts_as_import:
            indexes.append(index)
    return indexes


def _has_comment_before_an_import(lines: list[str], import_indexes: list[int]) -> bool:
    """Whether anything but imports and blank lines precedes the last leading import."""
    if not import_indexes:
        return False
    moved = set(import_indexes)
    return any(index not in moved and lines[index].strip() for index in range(import_indexes[-1]))


def _code_outside_comments(line: str, depth: int) -> tuple[str, int]:
    """Return the part of ``line`` that is not inside a comment, and the comment depth after it.

    ``depth`` is how many block comments (``/- ... -/``, which nest) are open at the start of
    the line. A line comment (``--``) outside any block comment hides the rest of the line.

    A documentation comment (``/-- ... -/``) or module documentation (``/-! ... -/``) is code,
    as it is to Lean: it belongs to a command, so it ends the region where imports are allowed.
    """
    code: list[str] = []
    position = 0
    while position < len(line):
        marker = line[position : position + _MARKER_LENGTH]
        if depth == 0 and line.startswith(_DOCUMENTATION_OPENERS, position):
            code.append(line[position:])
            break
        if marker == _BLOCK_COMMENT_OPEN:
            depth += 1
            position += _MARKER_LENGTH
        elif depth > 0 and marker == _BLOCK_COMMENT_CLOSE:
            depth -= 1
            position += _MARKER_LENGTH
        elif depth == 0 and marker == _LINE_COMMENT:
            break
        else:
            if depth == 0:
                code.append(line[position])
            position += 1
    return "".join(code), depth


def _with_line_ending(line: str) -> str:
    """Return the line ending in a newline, so a last line moved to the top does not fuse."""
    return line if line.endswith("\n") else line + "\n"
