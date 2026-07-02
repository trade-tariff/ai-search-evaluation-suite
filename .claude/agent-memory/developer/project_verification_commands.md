---
name: project-verification-commands
description: How to verify edits in ai-search-evaluation-suite without npm (local tsc + py_compile)
metadata:
  type: project
---

Verify edits in this repo without running npm.
**Why:** npm is off-limits per task constraints; but `apps/product/frontend/node_modules/.bin/tsc` exists and works.
**How to apply:**
- Frontend: `cd apps/product/frontend && node_modules/.bin/tsc --noEmit -p tsconfig.json`
- Python: `python3 -m py_compile <files>` from repo root
- Note: `classify_matrix_view.py` exists in BOTH `apps/product/backend/classification_core/` and `apps/product/backend/journey/` - near-duplicate copies; string fixes usually need applying to both.
