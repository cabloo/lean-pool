"""The screen: what a check from a less trusted sender may hold.

A check is a Lean file, and Lean runs programs while it reads one: ``#eval`` of an ``IO`` action,
``run_tac``, a command an ``elab`` defines, an attribute that registers code. The cache reads every
file sent with its second key (the screened key) against the rules below before anything else, and
refuses the whole request if one file breaks them. The rules hold what a proof and its statement
never need:

* ``import`` only ``Mathlib``, ``Aesop`` and their modules, and those of the roots a pool adds
  (``allowed_imports``; the cache's ``--screened-import``), each admitting every module under it,
  and only as the word ``import``, whitespace, and a module's name ending at whitespace or at the
  end of the file, read as the file holds it: ASCII parts, each a letter or ``_`` and then letters,
  digits, ``_``, ``'``, ``!`` or ``?``, joined by single dots. Any other ``import``
  (``import/- c -/Lean.Elab``, ``import--c``, ``import Mathlib.«x.y»``, ``import Mathlib..x``) is
  refused;
* no ``#`` followed by a name but ``#print``, ``#check``, ``#reduce`` and ``#synth``, wherever it
  stands: a command is not told apart from notation, so ``#s`` (a finset's size) is refused too,
  and ``s.card`` passes;
* none of the words in ``REFUSED_WORDS``;
* attributes (``@[...]``, ``attribute [...]``) naming only those in ``ALLOWED_ATTRIBUTES``.

The whole text is read, comments and strings included, each word as a whole word (``elaborate``
and ``my_macro_lemma`` pass), and for every rule but the imports' with ``«`` and ``»`` taken away
first. A word in a comment costs its sender one refused check; telling Lean's comments apart here
would be a second Lean parser to get wrong.

**What it is not:** proof that a check runs no code. It is a list of words over a language whose
files can define syntax, and a determined sender may get round it. What it does is take the easy
ways away; a sandbox around each Lean worker has to hold what passes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

ALLOWED_IMPORTS = ("Mathlib", "Aesop")
# A root a pool may add: the first part of a Lean module's name (``FormalConjectures``), never a
# dotted name or a part of one, so a root admits that library's modules and no other's.
IMPORT_ROOT = re.compile(r"[A-Z][A-Za-z0-9_]{0,63}")
ALLOWED_HASH_COMMANDS = frozenset({"print", "check", "reduce", "synth"})

REFUSED_WORDS = frozenset(
    {
        # Commands and tactics that run a program, and the ways to define new ones.
        "run_cmd",
        "run_tac",
        "run_elab",
        "run_meta",
        "by_elab",
        "elab",
        "elab_rules",
        "macro",
        "macro_rules",
        "syntax",
        "declare_syntax_cat",
        "initialize",
        "builtin_initialize",
        "simproc",
        "dsimproc",
        "simproc_decl",
        "dsimproc_decl",
        # Code that escapes the kernel or Lean's own checks.
        "unsafe",
        "implemented_by",
        "extern",
        "native_decide",
        "native",
        "ofReduceBool",
        "reduceBool",
        # The monads that reach the world.
        "IO",
        "EIO",
        "BaseIO",
    }
)

ALLOWED_ATTRIBUTES = frozenset(
    {
        "simp",
        "norm_cast",
        "push_cast",
        "ext",
        "reducible",
        "semireducible",
        "irreducible",
        "instance",
        "inline",
        "specialize",
        "gcongr",
    }
)
# What may follow an allowed attribute's name: simp's direction and priority, norm_cast's labels.
_ALLOWED_ARGUMENTS = frozenset(
    {"←", "<-", "↓", "high", "mid", "low", "default", "elim", "move", "squash"}
)
_SCOPES = frozenset({"local", "scoped"})

# A word as Lean reads an identifier's part: letters (any script), digits, `_`, `'`, `!` and `?`.
# A dot separates the parts of a name, so `IO.println` holds the word `IO`.
_WORD = re.compile(r"[\w'!?]+")
# Every word ``import`` is judged, and only one form of it is read: whitespace, then a module's
# name ending at whitespace or at the end of the text. Any other is refused unread.
_IMPORT_WORD = re.compile(r"(?<![\w'!?.])import(?![\w'!?])")
# The name as Lean reads it only when no part is written in guillemets (a part ``«a.b»`` is one part
# to Lean and two to a reader that takes the guillemets away), so a name that has them is refused.
_MODULE_PART = r"[A-Za-z_][A-Za-z0-9_'!?]*"
_IMPORT = re.compile(rf"import\s+({_MODULE_PART}(?:\.{_MODULE_PART})*)(?=\s|\Z)")
_HASH_COMMAND = re.compile(r"#([A-Za-z_][\w'!?]*)")
_ATTRIBUTE_OPEN = re.compile(r"@\[|(?<![\w'!?.])attribute\s*\[")
_ATTRIBUTE_TOKEN = re.compile(r"←|<-|↓|[\w'!?.]+|\S")
_GUILLEMETS = str.maketrans("", "", "«»")


def screen(code: str, allowed_imports: Sequence[str] = ()) -> str | None:
    """Return what in ``code`` the screen refuses, in words, or ``None`` when it passes.

    ``allowed_imports`` names more roots whose modules a check may import, beside
    ``ALLOWED_IMPORTS``; each must match ``IMPORT_ROOT``.
    """
    for root in allowed_imports:
        if not IMPORT_ROOT.fullmatch(root):
            raise ValueError(f"{root!r} is not the root of a Lean module's name")
    text = code.translate(_GUILLEMETS)
    for finding in (
        _refused_import(code, (*ALLOWED_IMPORTS, *allowed_imports)),
        _refused_hash_command(text),
        _refused_attribute(text),
        _refused_word(text),
    ):
        if finding is not None:
            return finding
    return None


def _refused_import(text: str, roots: Sequence[str]) -> str | None:
    for word in _IMPORT_WORD.finditer(text):
        match = _IMPORT.match(text, word.start())
        if match is None:
            seen = text[word.start() : word.start() + 40].splitlines()[0]
            return f"`{seen}` (an import the screen cannot read: `import`, then a module's name)"
        module = match.group(1)
        if not any(module == root or module.startswith(f"{root}.") for root in roots):
            named = " and ".join([", ".join(roots[:-1]), roots[-1]])
            return f"`import {module}` (only {named} may be imported)"
    return None


def _refused_hash_command(text: str) -> str | None:
    for match in _HASH_COMMAND.finditer(text):
        if match.group(1) not in ALLOWED_HASH_COMMANDS:
            return f"the command `#{match.group(1)}`"
    return None


def _refused_word(text: str) -> str | None:
    for match in _WORD.finditer(text):
        if match.group(0) in REFUSED_WORDS:
            return f"the word `{match.group(0)}`"
    return None


def _refused_attribute(text: str) -> str | None:
    for match in _ATTRIBUTE_OPEN.finditer(text):
        body = _bracketed(text, match.end())
        if body is None:
            return "an attribute list that is not closed"
        for instance in _split_top_level(body):
            finding = _refused_attribute_instance(instance)
            if finding is not None:
                return finding
    return None


def _bracketed(text: str, start: int) -> str | None:
    """The text from ``start`` to the ``]`` that closes the ``[`` just before it."""
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return text[start:index]
    return None


def _split_top_level(body: str) -> list[str]:
    """Split an attribute list at the commas that are not inside brackets or parentheses."""
    parts, depth, start = [], 0, 0
    for index, character in enumerate(body):
        if character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return parts


def _refused_attribute_instance(instance: str) -> str | None:
    tokens = _ATTRIBUTE_TOKEN.findall(instance)
    if tokens and tokens[0] in _SCOPES:
        tokens = tokens[1:]
    if tokens and tokens[0] == "-":
        tokens = tokens[1:]
    if not tokens:
        return f"the attribute list entry `{instance.strip()}`"
    name, arguments = tokens[0], tokens[1:]
    if name not in ALLOWED_ATTRIBUTES:
        return f"the attribute `{name}`"
    for argument in arguments:
        if argument not in _ALLOWED_ARGUMENTS and not argument.isdigit():
            return f"`{argument}` after the attribute `{name}`"
    return None
