"""Composite 'lexical specificity' index (0-100) - one number per query summarising how
SPECIFIC / RARE the WORDING looks, blended from the pre-retrieval QPP signals.

IMPORTANT - what this is NOT: a recall predictor. Against the v2_composite retriever it
correlates only weakly with whether the gold code is retrieved (point-biserial r ~ 0.13,
~1.7% of variance), and it is fooled by out-of-vocabulary brand/consumer terms - 'branded'
inputs score high here yet retrieve no better, while terse 'vague' inputs score LOWEST yet
retrieve fine. The hybrid semantic pipeline is largely phrasing-robust, so surface
specificity does not track retrievability. For a real 'will this retrieve' signal use
POST-retrieval confidence (top RRF score, score gap, leg consensus, max query-composite
cosine), not this index. (codex convergence review, 2026-06.)

How it is built: each signal (query_len, avg_idf, max_idf, avg_ictf, llm_spec) is z-scored
across all gold queries and combined with FIXED orientation signs (+1 = more specific;
nn_density -1 = crowded neighbourhood; SCS excluded) - an equal-weight mean of oriented
z-scores, NOT correlation-weighted (an earlier docstring claimed correlation weighting;
the code never did it, and since every signal's r is ~0.1 it would not help anyway).
The blend is percentile-ranked 0-100, so '75' = more specific-looking wording than 75%
of the gold queries.

Stores kg.query_qpp.input_quality (column name kept for compatibility; displayed label is
'lexical specificity'). No LLM / embeddings - pure math on stored signals.
Env: IQ_RUN_LABEL (v2_plus_desc_vec) - reference run for the correlation report only; it
does NOT weight the blend.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

for p in [
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
RUN_LABEL = os.environ.get("IQ_RUN_LABEL", "v2_plus_desc_vec")
SIGNALS = ["query_len", "avg_idf", "max_idf", "avg_ictf", "scs", "nn_density", "llm_spec"]
PERSONA_ORDER = ["naive_vague", "naive_branded", "naive_specific",
                 "emu_generic", "emu_ordinary", "emu_specific", "original"]


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT q.gold_id, q.persona, q.query_len, q.avg_idf, q.max_idf, q.avg_ictf,
                      q.scs, q.nn_density, d.llm_spec
               FROM kg.query_qpp q
               LEFT JOIN kg.query_descriptiveness d ON d.gold_id=q.gold_id
               ORDER BY q.gold_id""")
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        print("no query_qpp rows", file=sys.stderr); sys.exit(1)
    n = len(rows)

    # signal matrix (impute missing with column mean) + hit vector
    X = np.full((n, len(SIGNALS)), np.nan)
    for i, r in enumerate(rows):
        for j, s in enumerate(SIGNALS):
            if r.get(s) is not None:
                X[i, j] = float(r[s])
    col_mean = np.nanmean(X, axis=0)
    inds = np.where(np.isnan(X))
    X[inds] = np.take(col_mean, inds[1])
    # z-score each signal
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd

    # Orient each signal so higher = more descriptive/specific input (config-independent).
    # nn_density inverted (dense neighbourhood = crowded/confusable); SCS excluded (it is
    # length-penalised and anti-correlates with descriptiveness, so it muddies the index).
    SIGN = {"query_len": 1, "avg_idf": 1, "max_idf": 1, "avg_ictf": 1,
            "scs": 0, "nn_density": -1, "llm_spec": 1}
    weights = np.array([SIGN[s] for s in SIGNALS], dtype=float)
    raw = (Z * weights).sum(axis=1) / np.abs(weights).sum()  # mean of oriented z-scores

    # percentile-rank -> 0..100 (clear, scale-free)
    order = raw.argsort().argsort()
    iq = np.round(100.0 * order / max(n - 1, 1), 1)

    with conn.cursor() as cur:
        cur.execute("ALTER TABLE kg.query_qpp ADD COLUMN IF NOT EXISTS input_quality numeric")
        for r, v in zip(rows, iq):
            cur.execute("UPDATE kg.query_qpp SET input_quality=%s WHERE gold_id=%s", (float(v), r["gold_id"]))
    conn.commit()

    print("Signal orientation (higher = more descriptive input; SCS excluded):")
    for s, w in zip(SIGNALS, weights):
        print(f"  {s:<12}{'+' if w > 0 else '-' if w < 0 else 'excluded'}")
    print("\nComposite INPUT QUALITY (0-100) per persona:")
    with conn.cursor() as cur:
        cur.execute("SELECT persona, round(avg(input_quality),0) iq FROM kg.query_qpp GROUP BY persona")
        per = {r["persona"]: r["iq"] for r in cur.fetchall()}
    for p in PERSONA_ORDER:
        if p in per:
            print(f"  {p:<16}{float(per[p]):>6.0f}")
    conn.close()


if __name__ == "__main__":
    main()
