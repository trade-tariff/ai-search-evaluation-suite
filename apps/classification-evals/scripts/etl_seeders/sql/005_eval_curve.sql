-- Every retrieval experiment should produce a recall@K curve, not just point
-- metrics at K=5/10. Add a curve_json column to capture the full shape.
--
-- Schema:
-- {
--   "k_values": [5, 10, 20, 50, 100, 200, 500],
--   "recall_at_k_exact":      {"5": 0.30, "10": 0.39, ...},
--   "recall_at_k_subheading": {"5": 0.33, ...},
--   "recall_at_k_heading":    {"5": 0.46, ...},
--   "per_persona":            {"naive_vague": {"5": 0.27, "10": 0.41, ...}, ...},
--   "hard_misses_beyond_max_k": 17,
--   "max_k": 500
-- }

ALTER TABLE kg.eval_runs
    ADD COLUMN IF NOT EXISTS curve_json jsonb,
    ADD COLUMN IF NOT EXISTS retrieval_limit int;
