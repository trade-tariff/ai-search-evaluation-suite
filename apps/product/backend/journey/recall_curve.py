"""Recall@K curve: at what K does retrieval find the gold code?

The current eval ran with limit=10. We need the deeper picture: if the gold
isn't in top-10, where is it? At rank 50? 200? Missing entirely?

This sets the production cap for `limit` in retrieve_candidates - the number
of candidates handed to the Q&A LLM. We want K such that recall@K is high
enough that the LLM-sees-it loop has a chance, but not so deep that we waste
context tokens on noise.

Output: recall@K for K in [5, 10, 20, 50, 100, 200, 500] across selected
configs (default: all_legs_on, all_legs_loo, all_legs_loo_no_curated).
Also breaks down by persona so we can see the curve for vague-vs-specific queries.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

for p in [Path(__file__).parent / ".env",
          Path(__file__).parent.parent / ".env",
          Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

from . import local_db
from .run_eval import _loo_exclusions

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MAX_K = int(os.environ.get("RECALL_MAX_K", "500"))
K_LIST = [5, 10, 20, 50, 100, 200, 500]

CONFIGS = {
    "all_legs_on": dict(
        use_curated=True,
        use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True,
    ),
    "all_legs_loo": dict(
        use_curated=True,
        use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True,
        loo=True,
    ),
    "all_legs_loo_no_curated": dict(
        use_curated=False,
        use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True,
        loo=True,
    ),
}


def find_rank(query: str, gold: str, config: dict, source_id: str | None) -> tuple[int | None, int | None, int | None]:
    """Returns (exact_rank, subheading_rank, heading_rank) 1-based, or None."""
    cfg = dict(config)
    loo = cfg.pop("loo", False)
    if loo:
        fact_excl, edge_excl = _loo_exclusions(source_id)
        cfg["exclude_fact_sources"] = fact_excl
        cfg["exclude_edge_ids"] = edge_excl
    try:
        cands = local_db.retrieve_candidates(query, limit=MAX_K, **cfg)
    except Exception as exc:
        print(f"  [error] {exc}")
        return None, None, None
    gold = (gold or "").replace(".", "")[:10]
    gold8 = gold[:8]
    gold4 = gold[:4]
    rk_e, rk_s, rk_h = None, None, None
    for idx, c in enumerate(cands, start=1):
        code = (c["commodity_code"] or "").replace(".", "")[:10]
        if rk_e is None and code == gold:
            rk_e = idx
        if rk_s is None and code[:8] == gold8:
            rk_s = idx
        if rk_h is None and code[:4] == gold4:
            rk_h = idx
        if rk_e and rk_s and rk_h:
            break
    return rk_e, rk_s, rk_h


def main():
    personas = (os.environ.get("PERSONAS")
                or "naive_vague,naive_branded,naive_specific").split(",")
    config_keys = (os.environ.get("RUN_CONFIGS")
                   or "all_legs_loo,all_legs_loo_no_curated,all_legs_on").split(",")

    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, query, expected_code, persona, source_id FROM kg.eval_gold WHERE persona = ANY(%s) ORDER BY id",
            (personas,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    print(f"Loaded {len(rows)} gold rows (personas: {personas})")
    print(f"Configs: {config_keys}  MAX_K: {MAX_K}")

    summary: dict[str, dict] = {}
    per_persona: dict[str, dict] = {}

    for cfg_key in config_keys:
        if cfg_key not in CONFIGS:
            print(f"unknown config: {cfg_key}", file=sys.stderr)
            continue
        cfg = CONFIGS[cfg_key]
        print(f"\n=== {cfg_key} ===")
        ranks_exact: list[int | None] = []
        ranks_sub: list[int | None] = []
        ranks_head: list[int | None] = []
        per_persona_ranks: dict[str, list[int | None]] = {}
        start = time.time()
        for i, row in enumerate(rows):
            rk_e, rk_s, rk_h = find_rank(row["query"], row["expected_code"], cfg, row["source_id"])
            ranks_exact.append(rk_e)
            ranks_sub.append(rk_s)
            ranks_head.append(rk_h)
            per_persona_ranks.setdefault(row["persona"], []).append(rk_e)
            if (i + 1) % 30 == 0:
                print(f"  {i + 1}/{len(rows)}")
        elapsed = time.time() - start
        n = len(rows)
        per_k = {k: sum(1 for r in ranks_exact if r is not None and r <= k) / n for k in K_LIST}
        per_k_sub = {k: sum(1 for r in ranks_sub if r is not None and r <= k) / n for k in K_LIST}
        per_k_head = {k: sum(1 for r in ranks_head if r is not None and r <= k) / n for k in K_LIST}
        misses = sum(1 for r in ranks_exact if r is None)
        summary[cfg_key] = {
            "exact": per_k,
            "subheading": per_k_sub,
            "heading": per_k_head,
            "misses": misses,
            "n": n,
            "elapsed_s": elapsed,
        }
        per_persona[cfg_key] = {}
        for persona, persona_ranks in per_persona_ranks.items():
            n_p = len(persona_ranks)
            per_persona[cfg_key][persona] = {
                k: sum(1 for r in persona_ranks if r is not None and r <= k) / n_p
                for k in K_LIST
            }
        print(f"  {n} rows in {elapsed:.1f}s.")
        for k in K_LIST:
            print(f"    recall@{k:>3}  exact={per_k[k]:.3f}  sub={per_k_sub[k]:.3f}  head={per_k_head[k]:.3f}")
        print(f"  hard misses beyond K={MAX_K}: {misses}/{n}")

    print("\n" + "=" * 70)
    print(f"RECALL@K CURVE  (n={len(rows)} naive-trader queries)")
    print("=" * 70)
    header = f"{'config':<28}" + "".join(f"@{k:<6}" for k in K_LIST)
    print(header)
    for cfg_key, s in summary.items():
        row = f"{cfg_key:<28}" + "".join(f"{s['exact'][k]*100:>5.1f}%" for k in K_LIST)
        print(row)

    print("\nPer-persona recall@K curves (exact match):")
    for cfg_key, by_p in per_persona.items():
        print(f"\n  {cfg_key}")
        print(f"  {'persona':<20}" + "".join(f"@{k:<6}" for k in K_LIST))
        for persona, k_vals in by_p.items():
            row = f"  {persona:<20}" + "".join(f"{k_vals[k]*100:>5.1f}%" for k in K_LIST)
            print(row)


if __name__ == "__main__":
    main()
