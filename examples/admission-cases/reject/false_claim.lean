import Mathlib

/-- Must be rejected: the statement is false, so no tactic may close it. -/
theorem false_claim : (2 : ℕ) + 2 = 5 := by norm_num
