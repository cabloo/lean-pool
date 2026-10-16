"""The screen's rules: what a check sent with the screened key may hold."""

from __future__ import annotations

import pytest

from leanpool.screen import ALLOWED_ATTRIBUTES, REFUSED_WORDS, screen

HEADER = (
    "import Mathlib\nimport Aesop\n\nset_option maxHeartbeats 400000\n\n"
    "open BigOperators Real Nat Topology Rat\n\n"
)
PROOF = (
    HEADER + "theorem sq_sum (a b : Real) : 0 ≤ a ^ 2 + b ^ 2 := by\n"
    "  nlinarith [sq_nonneg a, sq_nonneg b]\n\n#print axioms sq_sum\n"
)


def test_a_proof_with_its_header_and_its_axiom_report_passes() -> None:
    assert screen(PROOF) is None


@pytest.mark.parametrize(
    "code",
    [
        "theorem t : 2 + 2 = 4 := by norm_num",
        "example : True := by\n  first | trivial | decide",
        "#check Nat.add_comm\n#reduce 2 + 2\n#synth Inhabited Nat\n#print axioms Nat.add_comm",
        "def f (n : Nat) : Nat := n + 1\nlemma f_pos (n : Nat) : 0 < f n := Nat.succ_pos n",
        "import Mathlib.Tactic\nimport Aesop.Frontend\ntheorem t : True := trivial",
        'notation "‖" x "‖" => abs x\ninstance : Inhabited Nat := ⟨0⟩',
        "theorem t (s : Finset Nat) : #[1, 2].size = 2 := rfl",
    ],
)
def test_what_proofs_and_statements_use_passes(code: str) -> None:
    assert screen(code) is None, screen(code)


@pytest.mark.parametrize("word", sorted(REFUSED_WORDS))
def test_every_refused_word_is_refused_in_the_text_a_comment_a_string_and_guillemets(
    word: str,
) -> None:
    for code in (
        f"theorem t : True := by {word}",
        f"theorem t : True := trivial -- {word}",
        f"/- {word} -/ theorem t : True := trivial",
        f'def s : String := "{word}"',
        f"def «{word}» : Nat := 0",
    ):
        assert screen(code) == f"the word `{word}`", code


@pytest.mark.parametrize(
    "code",
    [
        "theorem elaborate : True := trivial",
        "theorem my_macro_lemma : True := trivial",
        "def IOx : Nat := 0",
        "def nativeish : Nat := 0",
        "theorem t : True := by exact Lean.Elab.Term.elabTerm",
    ],
)
def test_a_refused_word_inside_a_longer_word_passes(code: str) -> None:
    assert screen(code) is None


def test_a_word_reached_through_a_dotted_name_is_refused() -> None:
    assert screen("def main : Unit := IO.println 1") == "the word `IO`"


@pytest.mark.parametrize(
    "command", ["eval", "eval!", "exit", "guard_msgs", "check_failure", "help", "s"]
)
def test_any_other_hash_command_is_refused(command: str) -> None:
    assert screen(f"theorem t : True := trivial\n#{command} 1") == f"the command `#{command}`"


@pytest.mark.parametrize("module", ["Lean", "Std", "Mathlibx", "Lean.Elab", "Init", "all"])
def test_an_import_of_anything_but_mathlib_and_aesop_is_refused(module: str) -> None:
    finding = screen(f"import Mathlib\nimport {module} Mathlib\ntheorem t : True := trivial")
    assert finding is not None
    assert finding.startswith(f"`import {module}`")


@pytest.mark.parametrize(
    "code",
    [
        "@[simp] theorem t : 1 + 1 = 2 := rfl",
        "@[simp ←] theorem t : 1 + 1 = 2 := rfl",
        "@[simp high, norm_cast] theorem t : 1 + 1 = 2 := rfl",
        "@[local simp] theorem t : 1 + 1 = 2 := rfl",
        "@[-simp] theorem t : 1 + 1 = 2 := rfl",
        "attribute [scoped simp] Nat.add_comm",
        "attribute [-simp] Nat.add_comm",
        "@[instance 100] def i : Inhabited Nat := ⟨0⟩",
        "@[reducible, inline] def f (n : Nat) : Nat := n",
        *(f"@[{name}] def f : Nat := 0" for name in sorted(ALLOWED_ATTRIBUTES)),
    ],
)
def test_the_listed_attributes_pass_alone_and_in_a_list(code: str) -> None:
    assert screen(code) is None, screen(code)


@pytest.mark.parametrize(
    ("code", "finding"),
    [
        ("@[tactic t] def f : Nat := 0", "the attribute `tactic`"),
        ("@[norm_num e] def f : Nat := 0", "the attribute `norm_num`"),
        ("@[simp, tactic t] def f : Nat := 0", "the attribute `tactic`"),
        ("attribute [command_elab c] f", "the attribute `command_elab`"),
        ("@[local delab app] def f : Nat := 0", "the attribute `delab`"),
        ("@[simp f] def f : Nat := 0", "`f` after the attribute `simp`"),
        ("@[] def f : Nat := 0", "the attribute list entry ``"),
        ("@[simp def f : Nat := 0", "an attribute list that is not closed"),
    ],
)
def test_any_other_attribute_or_argument_is_refused(code: str, finding: str) -> None:
    assert screen(code) == finding
