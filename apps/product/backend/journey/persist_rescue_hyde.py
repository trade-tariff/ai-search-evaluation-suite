"""Persist the rescue-only-rerank and HyDE measurements as kg.eval_runs rows so
they appear in the experiment matrix. naive_vague curves are from the clean runs
(rescue br1wxiste, hyde bn60qn0tw, 2026-06-02). Idempotent (delete-then-insert).
Re-run measure_rerank.py / measure_hyde.py and update these if params change.
"""
import json
import os

import psycopg

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")

ROWS = [
    ("rescue_descvec",
     {"5": 0.443, "10": 0.500, "20": 0.571, "50": 0.743, "100": 0.800},
     "RRF top-80 kept; gpt-5-mini rescues ranks 81-200 into the top-100 (low effort). naive_vague only."),
    ("hyde_descvec",
     {"5": 0.429, "10": 0.500, "20": 0.557, "50": 0.729, "100": 0.814},
     "base desc_vec pool UNION HyDE hypothetical-doc retrieval (gpt-5-mini, minimal). naive_vague only."),
]

def main():
    conn = psycopg.connect(DSN)
    with conn.cursor() as cur:
        for label, curve, note in ROWS:
            cur.execute("DELETE FROM kg.eval_runs WHERE run_label = %s", (label,))
            cj = {"k_values": [5, 10, 20, 50, 100], "n_queries": 70,
                  "recall_at_k_exact": curve, "per_persona": {"naive_vague": curve}}
            cur.execute(
                """INSERT INTO kg.eval_runs
                   (run_label, config_json, finished_at, n_queries, recall_at_1,
                    recall_at_5, recall_at_10, mrr, curve_json, retrieval_limit, notes)
                   VALUES (%s, %s::jsonb, now(), %s, NULL, %s, %s, NULL, %s::jsonb, 500, %s)""",
                (label, json.dumps({"derived": True}), 70, curve["5"], curve["10"],
                 json.dumps(cj), note),
            )
    conn.commit()
    conn.close()
    print("inserted rescue_descvec + hyde_descvec")


if __name__ == "__main__":
    main()
