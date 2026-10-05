import Mathlib

/-- Passes on any Lean with Mathlib: `ring` proves commutativity of addition. -/
theorem sum_comm (a b : ℕ) : a + b = b + a := by ring
