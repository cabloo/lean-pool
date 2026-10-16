"""The screen: what a check from a less trusted sender may hold.

A check is a Lean file, and Lean runs programs while it reads one: ``#eval`` of an ``IO`` action,
``run_tac``, a command an ``elab`` defines, an attribute that registers code. The cache reads every
file sent with its second key (the screened key) against the rules below before anything else, and
refuses the whole request if one file breaks them. The rules hold what a proof and its statement
never need:

* ``import`` only ``Mathlib``, ``Aesop`` and their modules;
* no ``#`` followed by a name but ``#print``, ``#check``, ``#reduce`` and ``#synth``, wherever it
  stands: a command is not told apart from notation, so ``#s`` (a finset's size) is refused too,
  and ``s.card`` passes;
* none of the words in ``REFUSED_WORDS``;
* attributes (``@[...]``, ``attribute [...]``) naming only those in ``ALLOWED_ATTRIBUTES``.

The whole text is read, comments and strings included, with ``«`` and ``»`` taken away first, and
each word as a whole word (``elaborate`` and ``my_macro_lemma`` pass). A word in a comment costs
its sender one refused check; telling Lean's comments apart here would be a second Lean parser to
get wrong.

**What it is not:** proof that a check runs no code. It is a list of words over a language whose
files can define syntax, and a determined sender may get round it. What it does is take the easy
ways away; a sandbox around each Lean worker has to hold what passes.
"""

from __future__ import annotations

import re

ALLOWED_IMPORTS = ("Mathlib", "Aesop")
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
_IMPORT = re.compile(r"(?<![\w'!?.])import\s+([\w'!?.]*)")
_HASH_COMMAND = re.compile(r"#([A-Za-z_][\w'!?]*)")
_ATTRIBUTE_OPEN = re.compile(r"@\[|(?<![\w'!?.])attribute\s*\[")
_ATTRIBUTE_TOKEN = re.compile(r"←|<-|↓|[\w'!?.]+|\S")
_GUILLEMETS = str.maketrans("", "", "«»")


def screen(code: str) -> str | None:
    """Return what in ``code`` the screen refuses, in words, or ``None`` when it passes."""
    text = code.translate(_GUILLEMETS)
    for finding in (
        _refused_import(text),
        _refused_hash_command(text),
        _refused_attribute(text),
        _refused_word(text),
    ):
        if finding is not None:
            return finding
    return None


def _refused_import(text: str) -> str | None:
    for match in _IMPORT.finditer(text):
        module = match.group(1)
        if not any(module == root or module.startswith(f"{root}.") for root in ALLOWED_IMPORTS):
            return f"`import {module}` (only {' and '.join(ALLOWED_IMPORTS)} may be imported)"
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
