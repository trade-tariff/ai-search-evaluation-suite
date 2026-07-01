"""Eval harness: replay gold queries through retrieval, measure recall + MRR
across multiple pipeline configurations.

Reads kg.eval_gold, runs each query against retrieve_candidates with the
selected config(s), and writes results to kg.eval_runs + kg.eval_run_results
so you can diff configs side-by-side.

Output: a per-config row in kg.eval_runs with:
  - recall@1, recall@5, recall@10        (exact 10-digit match)
  - recall@5_subheading                  (top-5 shares 8-digit prefix with gold)
  - recall@5_heading                     (top-5 shares 4-digit prefix with gold)
  - MRR (mean reciprocal rank)
And the row-level top-10 returned for every query so you can inspect failures.

CLI:
  python run_eval.py                    # runs the default configs vs. all gold rows
  RUN_CONFIGS=baseline,all python run_eval.py
  EVAL_PERSONAS=naive_vague,naive_branded python run_eval.py   # subset by persona
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_env_candidates = [
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path.cwd() / ".env",
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
]
for p in [candidate for candidate in _env_candidates if candidate is not None]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

from . import local_db

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")

# Pipeline configs to compare. Each maps to a dict of retrieve_candidates kwargs.
# Keep these short - the report blows up otherwise.
CONFIGS: dict[str, dict] = {
    "baseline_fts_only": dict(
        use_vector=False, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False,
    ),
    "fts_plus_description_vec": dict(
        use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False,
    ),
    "no_semantic_kg": dict(
        use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=False, use_kg_vec=False,
    ),
    "all_legs_on": dict(
        use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True,
    ),
    # The honest tests - per codex's "leave-one-ATAR-out" critique.
    # 'loo': True triggers per-row exclusion of the gold ATAR's own facts+edge.
    "all_legs_loo": dict(
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
    "no_curated_only": dict(
        # Strict: pure description+semantic, no curated, no exclusion.
        # Tells us if curated search_references are doing the heavy lifting.
        use_curated=False,
        use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True,
    ),

    # ---- Exp 2: leg triage (LOO + no curated baseline, each leg off in turn) ----
    "exp2_baseline_all_on": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
    ),
    "exp2_off_description_vec": dict(
        use_curated=False, use_vector=False, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
    ),
    "exp2_off_facts_fts": dict(
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
    ),
    "exp2_off_kg_fts": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=False,
        use_facts_vec=True, use_kg_vec=True, loo=True,
    ),
    "exp2_off_facts_vec": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=False, use_kg_vec=True, loo=True,
    ),
    "exp2_off_kg_vec": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=False, loo=True,
    ),

    # ---- Exp 2: RRF k sweep ----
    # (k=60 is exp2_baseline_all_on already)
    "exp2_rrf_k10": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, rrf_k=10,
    ),
    "exp2_rrf_k30": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, rrf_k=30,
    ),
    "exp2_rrf_k120": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, rrf_k=120,
    ),

    # ---- Exp 3: cap tuning (semantic caps, holding lexical caps at 0.5) ----
    "exp3_facts_vec_cap_03": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, facts_vec_cap=0.3,
    ),
    "exp3_facts_vec_cap_09": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, facts_vec_cap=0.9,
    ),
    "exp3_kg_vec_cap_03": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, kg_vec_cap=0.3,
    ),
    "exp3_kg_vec_cap_09": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, kg_vec_cap=0.9,
    ),
    "exp3_both_vec_caps_09": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
    ),
    # ---- Production v2 defaults (from Exp 2+3 verdict) ----
    # description_vec OFF + both semantic caps at 0.9, all other legs on.
    # This is what DEFAULT_CLASSIFY_CONFIG now reflects.
    "production_v2": dict(
        use_curated=False, use_vector=False, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
    ),

    # ---- Ablation ladder: isolate the AI / KG / facets contributions for the team matrix ----
    "ai_semantic": dict(  # semantic description embeddings only (no KG, no facets)
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True,
    ),
    "ai_kg": dict(  # AI semantic + Knowledge Graph only
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=True,
        use_facts_vec=False, use_kg_vec=True, loo=True, kg_vec_cap=0.9,
    ),
    "ai_facets": dict(  # AI semantic + Facets only
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=False,
        use_facts_vec=True, use_kg_vec=False, loo=True, facts_vec_cap=0.9,
    ),

    # ---- Production-faithful ladder: semantic base (NO KG/facets) +/- the two prod
    # features (AI-166 composite docs, AI-815 query rewrite). Gives a true "current
    # production" line and a clean with/without query-expansion comparison. ----
    "ai_semantic_composite": dict(  # semantic over AI-166 composite docs (no query rewrite)
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True,
    ),
    "ai_semantic_triage": dict(  # semantic + query rewrite ON (no composite)
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, triage=True,
    ),
    "ai_semantic_composite_triage": dict(  # = actual current production (semantic + composite + query rewrite)
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True, triage=True,
    ),
    "staging_ai": dict(  # OTT staging AI (admin_configuration.rb): keyword + vector-over-composite + RRF; triage/facts/KG OFF
        use_curated=True, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True,
    ),

    # ---- Targeting 95% recall@100 sweep ----
    # User bar: 95% recall@100 on naive_vague LOO. Currently at ~74-78%.
    # These three configs probe how far retrieval-only tweaks can push:
    #   - +desc_vec: trades top-K precision for tail coverage
    #   - +multi_query: rewrites vague queries into N variants, unions
    #   - +both:     stack the two

    "v2_plus_desc_vec": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
    ),
    "v2_plus_multi_query": dict(
        use_curated=False, use_vector=False, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
        multi_query=True,
    ),
    "v2_plus_both": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
        multi_query=True,
    ),

    # ---- Path A1: linear retrieval adapter on the description vector leg ----
    # Same as the two winners above, but the query vector is mapped through the
    # trained adapter (data/vec_adapter.npy) before the description-vector leg.
    # Adapter excludes eval-gold codes from training, so LOO stays honest.
    "v2_plus_descvec_adapter": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
        use_vec_adapter=True,
    ),
    "v2_plus_both_adapter": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True,
        facts_vec_cap=0.9, kg_vec_cap=0.9,
        multi_query=True, use_vec_adapter=True,
    ),

    # ---- Diagnostic: description-vector leg in ISOLATION, adapter on vs off.
    # Tells us whether the adapter helps the leg at all (distribution mismatch)
    # vs helps the leg but washes out in RRF fusion (leg dilution).
    "veconly_noadapter": dict(
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True,
    ),
    "veconly_adapter": dict(
        use_curated=False, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_vec_adapter=True,
    ),

    # ---- The two levers the POC was missing vs production (AI-166 composite docs
    # + AI-815 / AI-836 query expansion). Doc-side, query-side, and both. ----
    "v2_composite": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, facts_vec_cap=0.9, kg_vec_cap=0.9,
        use_composite=True,
    ),
    "v2_triage": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, facts_vec_cap=0.9, kg_vec_cap=0.9,
        triage=True,
    ),
    "v2_composite_triage": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
        use_facts_vec=True, use_kg_vec=True, loo=True, facts_vec_cap=0.9, kg_vec_cap=0.9,
        use_composite=True, triage=True,
    ),
    # ---- Facts-leg cap sweep (user req). Prod base = composite + triage (= grid_C1F0K0T1,
    # the 77.2% no-facts baseline). Add the facts legs (FTS + vector) at increasing caps to
    # test whether a low cap lets facts BOOST the gold without demoting it past rank-100
    # (i.e. recall@100 monotonic, facts a pure non-negative add). KG off to isolate facts.
    "facts_cap03": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=False,
        use_facts_vec=True, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        facts_cap=0.3, facts_vec_cap=0.3,
    ),
    "facts_cap05": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=False,
        use_facts_vec=True, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        facts_cap=0.5, facts_vec_cap=0.5,
    ),
    "facts_cap07": dict(
        use_curated=False, use_vector=True, use_facts=True, use_kg_context=False,
        use_facts_vec=True, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        facts_cap=0.7, facts_vec_cap=0.7,
    ),
    # ---- Rewrite model x prompt comparison (user req). FAITHFUL STAGING leg-set:
    # composite + curated(search_references) + vector + RRF + query rewrite, NO facts/kg.
    # No-rewrite floor for this leg-set = staging_ai (70.3). 2x2 over the rewrite:
    # model {gpt-5-mini (eval) | gpt-4.1-mini (staging)} x prompt {mine | staging}.
    # rw_g41_staging = faithful production rewrite. CAVEAT: staging rewrites only WHEN-NEEDED
    # (expand_search_when_needed_enabled=true); these rewrite EVERY query, so they upper-bound it.
    "rw_g5_mine": dict(
        use_curated=True, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        triage_model="gpt-5-mini", triage_prompt="mine",
    ),
    "rw_g41_mine": dict(
        use_curated=True, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        triage_model="gpt-4.1-mini-2025-04-14", triage_prompt="mine",
    ),
    "rw_g5_staging": dict(
        use_curated=True, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        triage_model="gpt-5-mini", triage_prompt="staging",
    ),
    "rw_g41_staging": dict(  # = faithful production rewrite (gpt-4.1-mini + staging prompt)
        use_curated=True, use_vector=True, use_facts=False, use_kg_context=False,
        use_facts_vec=False, use_kg_vec=False, loo=True, use_composite=True, triage=True,
        triage_model="gpt-4.1-mini-2025-04-14", triage_prompt="staging",
    ),
}


# ---- FULL FACTORIAL retrieval grid -------------------------------------
# 4 binary axes on the semantic-vector base (use_vector=True, loo=True, use_curated=True):
#   C = composite  (AI-166): swaps both description legs (FTS + vector) to composite text
#   F = facts      : use_facts (FTS) + use_facts_vec (vec) together, facts_vec_cap=0.9 when on
#   K = kg         : use_kg_context (FTS) + use_kg_vec (vec) together, kg_vec_cap=0.9 when on
#   T = triage     : LLM query rewrite (trader words -> tariff vocab) before retrieval
# = 2^4 = 16 combos, labelled grid_C{0/1}F{0/1}K{0/1}T{0/1}. The 8 T1 combos are LLM-slow
# (one rewrite call per query); the 8 T0 combos are fast (DB-only, ~20min/config).
# Generated rather than hand-typed so the factorial is exhaustive and typo-free.
# use_curated=False: the factorial measures the AI/semantic stack on its own merits,
# without the curated search_references leg doing hidden lifting.
def _grid_configs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in (0, 1):
        for f in (0, 1):
            for kgx in (0, 1):
                for t in (0, 1):
                    cfg = dict(
                        use_vector=True, use_curated=False, loo=True,
                        use_facts=bool(f), use_facts_vec=bool(f),
                        use_kg_context=bool(kgx), use_kg_vec=bool(kgx),
                        use_composite=bool(c),
                    )
                    if f:
                        cfg["facts_vec_cap"] = 0.9
                    if kgx:
                        cfg["kg_vec_cap"] = 0.9
                    if t:
                        cfg["triage"] = True
                    out[f"grid_C{c}F{f}K{kgx}T{t}"] = cfg
    return out


CONFIGS.update(_grid_configs())

# Match the candidate code against the gold code at three levels.
def _matches_at(returned: str, expected: str) -> tuple[bool, bool, bool]:
    r = (returned or "").replace(".", "")[:10]
    e = (expected or "").replace(".", "")[:10]
    return (r == e, r[:8] == e[:8], r[:4] == e[:4])


def _norm_code(code: str | None) -> str:
    """Normalise a gold expected_code to the 10-digit key used across the index:
    strip any non-digit, take the left 10. Matches commodity_code stored in
    kg.commodity_facets / kg.kg_edge_commodities (bare 10-digit, no dots)."""
    import re
    return re.sub(r"[^0-9]", "", code or "")[:10]


def build_loo_map(conn, rows: list[dict]) -> dict[str, tuple[list[str], list[str]]]:
    """Build a {normalised expected_code -> (fact_sources[], edge_ids[])} map ONCE
    per run (not per row).

    The old per-row exclusion only dropped the EXACT queried ATAR's own facts/edge.
    But 22 gold codes have MULTIPLE ATARs, so querying one left the sibling ATARs'
    facts/edges (same code, near-identical text) in the index = leakage. This keys
    on the gold's expected_code instead, so EVERY ATAR-derived fingerprint of that
    code is excluded.

    For each ATAR-derived gold code C we exclude:
      - fact_sources: all 'atar:%' sources in kg.commodity_facets for code C
      - edge_ids:     all 'atar_%' edge ids in kg.kg_edges linked to C via
                      kg.kg_edge_commodities (edge_id, commodity_code)
    plus a belt-and-braces union of the per-ATAR ids derived from every source_id
    in kg.eval_gold sharing expected_code C ('atar:NNN' facts, 'atar_NNN' edges).

    Composite (AI-166) docs are estate-wide, NOT ATAR-derived, so they are NOT
    excluded here - that leg stays clean by design.
    """
    # Distinct normalised codes from ATAR-derived gold rows only.
    codes: set[str] = set()
    code_to_source_ids: dict[str, set[str]] = {}
    for r in rows:
        if (r.get("source_type") or "") != "atar":
            continue
        c = _norm_code(r.get("expected_code"))
        if not c:
            continue
        codes.add(c)
        sid = r.get("source_id")
        if sid and sid.startswith("atar_"):
            code_to_source_ids.setdefault(c, set()).add(sid)

    if not codes:
        return {}

    code_list = sorted(codes)
    fact_by_code: dict[str, set[str]] = {c: set() for c in code_list}
    edge_by_code: dict[str, set[str]] = {c: set() for c in code_list}

    with conn.cursor() as cur:
        # All ATAR-derived fact sources for these codes. SQL returns the SAME
        # normalised 10-digit key it filters on, so bucketing can't drift.
        cur.execute(
            r"""
            SELECT left(regexp_replace(commodity_code, '[^0-9]', '', 'g'), 10) AS code, source
            FROM kg.commodity_facets
            WHERE source LIKE 'atar:%%'
              AND left(regexp_replace(commodity_code, '[^0-9]', '', 'g'), 10) = ANY(%s)
            """,
            (code_list,),
        )
        for code, src in cur.fetchall():
            fact_by_code.setdefault(code, set()).add(src)
        # All ATAR-derived edge ids linked to these codes via kg_edge_commodities.
        cur.execute(
            r"""
            SELECT left(regexp_replace(kec.commodity_code, '[^0-9]', '', 'g'), 10) AS code, kec.edge_id
            FROM kg.kg_edge_commodities kec
            WHERE kec.edge_id LIKE 'atar\_%%'
              AND left(regexp_replace(kec.commodity_code, '[^0-9]', '', 'g'), 10) = ANY(%s)
            """,
            (code_list,),
        )
        for code, eid in cur.fetchall():
            edge_by_code.setdefault(code, set()).add(eid)

    # Belt-and-braces: union the per-ATAR ids from every gold source_id on each code.
    for c, sids in code_to_source_ids.items():
        for sid in sids:
            atar_num = sid[len("atar_"):]
            fact_by_code.setdefault(c, set()).add(f"atar:{atar_num}")
            edge_by_code.setdefault(c, set()).add(sid)

    return {c: (sorted(fact_by_code.get(c, set())), sorted(edge_by_code.get(c, set()))) for c in code_list}


CURVE_KS = [5, 10, 20, 50, 60, 80, 100, 200, 500]
DEFAULT_LIMIT = int(os.environ.get("EVAL_LIMIT", "500"))


def _recall_at_k(ranks: list[int | None], k: int, n: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / n if n else 0.0


def run_config(conn, run_label: str, config: dict, rows: list[dict], limit: int | None = None,
               loo_map: dict[str, tuple[list[str], list[str]]] | None = None,
               personas: list[str] | None = None) -> int:
    """Run all gold rows through one config. Returns run_id.

    Captures the FULL recall@K curve (K in CURVE_KS) per config, not just
    K=5/10. The curve is the upper bound on what any downstream Q&A loop can
    achieve, so it's the primary diagnostic for retrieval experiments.
    """
    if limit is None:
        limit = DEFAULT_LIMIT
    print(f"\n=== {run_label} ===")
    print(f"  config: {config}  limit: {limit}")
    print(f"  rows: {len(rows)}")
    # Copy first so we never mutate the shared CONFIGS dict, and so the effective
    # flags survive into config_json (the old pop() dropped them).
    cfg = dict(config)
    loo_mode = bool(cfg.pop("loo", False))
    multi_query_mode = bool(cfg.pop("multi_query", False))
    triage_mode = bool(cfg.pop("triage", False))
    triage_model = cfg.pop("triage_model", None)
    triage_prompt = cfg.pop("triage_prompt", None)
    if loo_mode:
        print("  LOO mode ON - strict exclusion of ALL ATAR fingerprints of the gold code")
    if multi_query_mode:
        print("  multi_query ON - each query expanded to N variants + unioned")
    started = time.time()
    # Persist the FULL config plus the effective flags so the stored row is
    # self-describing (previously loo/triage/multi_query were pop()'d and vanished).
    config_json = {
        **cfg,
        "loo": loo_mode,
        "triage": triage_mode,
        "triage_model": triage_model,
        "triage_prompt": triage_prompt,
        "multi_query": multi_query_mode,
        "personas": personas,
        "retrieval_limit": limit,
        "active_gold_count": len(rows),
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kg.eval_runs (run_label, config_json, retrieval_limit) VALUES (%s, %s::jsonb, %s) RETURNING id",
            (run_label, json.dumps(config_json), limit),
        )
        run_id = cur.fetchone()["id"]
    conn.commit()

    rank_exact: list[int | None] = []
    rank_subheading: list[int | None] = []
    rank_heading: list[int | None] = []
    per_persona_ranks: dict[str, list[int | None]] = {}

    loo_map = loo_map or {}
    for i, row in enumerate(rows):
        per_row_kwargs = dict(cfg)
        if loo_mode:
            fact_excl, edge_excl = loo_map.get(_norm_code(row.get("expected_code")), ([], []))
            per_row_kwargs["exclude_fact_sources"] = fact_excl
            per_row_kwargs["exclude_edge_ids"] = edge_excl
        q = row["query"]
        if triage_mode:
            from . import triage
            q = triage.expand_query(q, model=triage_model, prompt_variant=(triage_prompt or "mine"))
        try:
            if multi_query_mode:
                from . import multi_query
                cands, _variants = multi_query.retrieve_candidates_multi(
                    q, limit=limit, **per_row_kwargs,
                )
            else:
                cands = local_db.retrieve_candidates(q, limit=limit, **per_row_kwargs)
        except Exception as exc:
            print(f"  [retrieval error] {row['id']}: {exc}")
            cands = []

        top_codes = [c["commodity_code"] for c in cands]
        # Store top 50 sources only (limit JSONB bloat - we only need them for
        # the failure inspector, not full curve)
        top_sources = {c["commodity_code"]: c.get("sources", []) for c in cands[:50]}
        expected = row["expected_code"]
        rk_exact, rk_sub, rk_head = None, None, None
        for idx, code in enumerate(top_codes, start=1):
            ex, sub, head = _matches_at(code, expected)
            if ex and rk_exact is None:
                rk_exact = idx
            if sub and rk_sub is None:
                rk_sub = idx
            if head and rk_head is None:
                rk_head = idx
            if rk_exact and rk_sub and rk_head:
                break
        rank_exact.append(rk_exact)
        rank_subheading.append(rk_sub)
        rank_heading.append(rk_head)
        per_persona_ranks.setdefault(row.get("persona", "?"), []).append(rk_exact)
        # Persist only the top-50 (the actually-useful inspection window) -
        # storing 500 top_codes per row blows up the DB for no real benefit.
        top_codes_persisted = top_codes[:50]
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.eval_run_results (run_id, gold_id, expected_code, top_codes, top_sources, rank_of_expected, rank_subheading, rank_heading)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (run_id, row["id"], expected, top_codes_persisted, json.dumps(top_sources), rk_exact, rk_sub, rk_head),
            )
        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  {i + 1}/{len(rows)}")
    conn.commit()

    n = len(rows)
    # Full curve
    curve_exact = {str(k): _recall_at_k(rank_exact, k, n) for k in CURVE_KS}
    curve_sub = {str(k): _recall_at_k(rank_subheading, k, n) for k in CURVE_KS}
    curve_head = {str(k): _recall_at_k(rank_heading, k, n) for k in CURVE_KS}
    per_persona_curve = {
        persona: {str(k): _recall_at_k(ranks, k, len(ranks)) for k in CURVE_KS}
        for persona, ranks in per_persona_ranks.items()
    }
    hard_misses = sum(1 for r in rank_exact if r is None)
    # Legacy point metrics (kept for backward compat with existing UI/columns)
    r_at_1 = _recall_at_k(rank_exact, 1, n)
    r_at_5 = curve_exact["5"]
    r_at_10 = curve_exact["10"]
    r_sub_5 = curve_sub["5"]
    r_head_5 = curve_head["5"]
    mrr = sum((1.0 / r) for r in rank_exact if r is not None) / n if n else 0.0

    curve_json = {
        "k_values": CURVE_KS,
        "max_k": limit,
        "n_queries": n,
        "hard_misses_beyond_max_k": hard_misses,
        "recall_at_k_exact": curve_exact,
        "recall_at_k_subheading": curve_sub,
        "recall_at_k_heading": curve_head,
        "per_persona": per_persona_curve,
    }

    elapsed = time.time() - started
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE kg.eval_runs
            SET finished_at = now(), n_queries = %s,
                recall_at_1 = %s, recall_at_5 = %s, recall_at_10 = %s,
                recall_at_5_subheading = %s, recall_at_5_heading = %s,
                mrr = %s, curve_json = %s::jsonb
            WHERE id = %s
            """,
            (n, r_at_1, r_at_5, r_at_10, r_sub_5, r_head_5, mrr, json.dumps(curve_json), run_id),
        )
    conn.commit()
    # Pretty-print the curve so the operator sees it during the run
    curve_line = "  recall@K curve: " + "  ".join(f"@{k}={curve_exact[str(k)]:.3f}" for k in CURVE_KS)
    print(curve_line)
    print(f"  MRR={mrr:.3f}  hard_misses_beyond_K{limit}={hard_misses}/{n}  ({elapsed:.1f}s)")
    return run_id


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)

    personas_env = os.environ.get("EVAL_PERSONAS")
    personas = personas_env.split(",") if personas_env else ["naive_vague", "naive_branded", "naive_specific"]
    configs_env = os.environ.get("RUN_CONFIGS")
    config_keys = configs_env.split(",") if configs_env else list(CONFIGS.keys())

    # Load gold rows for the requested personas. Filter to active rows (soft-dedup);
    # tolerate the column being absent for back-compat with pre-migration DBs.
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT id, query, expected_code, persona, source_id, source_type "
                "FROM kg.eval_gold WHERE active AND persona = ANY(%s) ORDER BY id",
                (personas,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(
                "SELECT id, query, expected_code, persona, source_id, source_type "
                "FROM kg.eval_gold WHERE persona = ANY(%s) ORDER BY id",
                (personas,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    # Optional per-persona sample cap - fast approximate runs of pathologically slow
    # configs (e.g. multi_query, which expands each query into N LLM variants).
    _sample_n = os.environ.get("EVAL_SAMPLE_PER_PERSONA")
    if _sample_n:
        from collections import defaultdict as _dd
        _n = int(_sample_n); _seen = _dd(int); _capped = []
        for _r in rows:
            if _seen[_r["persona"]] < _n:
                _capped.append(_r); _seen[_r["persona"]] += 1
        rows = _capped
        print(f"SAMPLED to {_n}/persona")
    print(f"Gold rows loaded: {len(rows)} (personas: {personas})")

    # Build the strict, code-keyed LOO exclusion map ONCE for this row set.
    loo_map = build_loo_map(conn, rows)
    print(f"LOO map built: {len(loo_map)} codes with ATAR fingerprints to exclude")

    run_ids = {}
    for key in config_keys:
        if key not in CONFIGS:
            print(f"unknown config: {key}", file=sys.stderr)
            continue
        rid = run_config(conn, key, CONFIGS[key], rows, loo_map=loo_map, personas=personas)
        run_ids[key] = rid

    # Final per-config summary + per-persona breakdown
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'config':<32} {'@1':>6} {'@5':>6} {'@10':>6} {'sub@5':>7} {'head@5':>7} {'MRR':>6}")
    for key, rid in run_ids.items():
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM kg.eval_runs WHERE id = %s", (rid,))
            r = cur.fetchone()
        print(f"{key:<32} {float(r['recall_at_1']):.3f}  {float(r['recall_at_5']):.3f}  {float(r['recall_at_10']):.3f}  "
              f"{float(r['recall_at_5_subheading']):.3f}   {float(r['recall_at_5_heading']):.3f}   {float(r['mrr']):.3f}")

    print("\nBy persona (recall@5 exact):")
    print(f"{'config':<32} {'naive_vague':>14} {'naive_branded':>14} {'naive_specific':>14}")
    for key, rid in run_ids.items():
        row = [f"{key:<32}"]
        for persona in ["naive_vague", "naive_branded", "naive_specific"]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT AVG(CASE WHEN rank_of_expected IS NOT NULL AND rank_of_expected <= 5 THEN 1.0 ELSE 0 END) AS r5
                    FROM kg.eval_run_results r
                    JOIN kg.eval_gold g ON g.id = r.gold_id
                    WHERE r.run_id = %s AND g.persona = %s
                    """,
                    (rid, persona),
                )
                r = cur.fetchone()
                v = float(r["r5"] or 0)
            row.append(f"{v:>14.3f}")
        print("  ".join(row))

    conn.close()


if __name__ == "__main__":
    main()
