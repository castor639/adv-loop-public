import Mathlib

/-- Template root. Workspace formalizations live under `payload/scratch/` and are
built by pointing `lake env lean` at them; nothing here is a result. -/
theorem workspace_template_ok : (1 : Nat) + 1 = 2 := by norm_num
