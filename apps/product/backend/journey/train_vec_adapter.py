"""Train a linear retrieval adapter (Path A1, numpy - no torch).

Learns W (d x d) that maps a query embedding TOWARD its commodity code's
self-text embedding, so trader vocabulary ("plant pot hanger") lands near
tariff vocabulary ("articles of stainless steel wire") in vector space.

Training pairs: uk.search_references.title (curated trader alias) -> the code's
uk.goods_nomenclature_self_texts.search_embedding. EXCLUDES every commodity
code that appears in kg.eval_gold (any naive persona) so the eval stays honest
- the adapter never sees a gold code in training.

Applied at inference to the QUERY vector ONLY (local_db._vector_leg via
adapter.apply_adapter); stored code embeddings are unchanged, so there is no
need to re-embed the 25k codes.

Objective: ridge-regularised least squares
    W = argmin_W  ||A W - C||^2 + lam ||W||^2
with A = alias embeddings (n x d), C = target code embeddings (n x d).
Closed form: W = (A^T A + lam I)^-1 (A^T C).  Instant on CPU.
At inference a query vector v maps to  v @ W  (then L2-normalised).

Cost: ~10k text-embedding-3-small calls (~$0.02), cached to disk so re-runs
are free.

Run:  .venv/bin/python train_vec_adapter.py
Env:  ADAPTER_RIDGE_FRAC (default 0.10)  ADAPTER_EMBED_BATCH (default 256)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

for p in [Path(__file__).parent / ".env",
          Path(__file__).parent.parent / ".env",
          Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
DATA = Path(__file__).parent / "data"
W_PATH = DATA / "vec_adapter.npy"
META_PATH = DATA / "vec_adapter_meta.json"
CACHE_EMB = DATA / "adapter_alias_emb.npy"
CACHE_TITLES = DATA / "adapter_alias_titles.json"

EMBED_MODEL = "text-embedding-3-small"
EMBED_BATCH = int(os.environ.get("ADAPTER_EMBED_BATCH", "256"))
RIDGE_FRAC = float(os.environ.get("ADAPTER_RIDGE_FRAC", "0.10"))
HELDOUT_FRAC = 0.10


def _norm_rows(M: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return M / n


def load_pairs(conn) -> tuple[list[str], list[str], dict[str, np.ndarray]]:
    """Returns (titles, codes_per_title, code_emb) where code_emb maps a code
    to its (unit-normalised) self-text embedding. Excludes eval-gold codes."""
    # 1. distinct code -> embedding (parse pgvector text as JSON; it is [a,b,...])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT st.goods_nomenclature_item_id AS code, st.search_embedding::text AS emb
            FROM uk.goods_nomenclature_self_texts st
            WHERE st.search_embedding IS NOT NULL
              AND st.goods_nomenclature_item_id IN (
                  SELECT DISTINCT goods_nomenclature_item_id FROM uk.search_references WHERE title IS NOT NULL
              )
              AND left(regexp_replace(st.goods_nomenclature_item_id, '[^0-9]', '', 'g'), 10) NOT IN (
                  SELECT left(regexp_replace(expected_code, '[^0-9]', '', 'g'), 10)
                  FROM kg.eval_gold WHERE persona LIKE 'naive%'
              )
            """
        )
        code_emb: dict[str, np.ndarray] = {}
        for r in cur.fetchall():
            code_emb[r["code"]] = np.asarray(json.loads(r["emb"]), dtype=np.float32)
    # 2. (title, code) pairs whose code survived the exclusion + has an embedding
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sr.title AS title, sr.goods_nomenclature_item_id AS code
            FROM uk.search_references sr
            WHERE sr.title IS NOT NULL AND length(trim(sr.title)) > 0
            ORDER BY sr.id
            """
        )
        titles: list[str] = []
        codes: list[str] = []
        for r in cur.fetchall():
            if r["code"] in code_emb:
                titles.append(r["title"].strip())
                codes.append(r["code"])
    return titles, codes, code_emb


def embed_titles(titles: list[str]) -> np.ndarray:
    """Embed alias titles via text-embedding-3-small, batched, with a disk
    cache keyed on the exact titles list (re-runs are free)."""
    if CACHE_EMB.exists() and CACHE_TITLES.exists():
        cached_titles = json.loads(CACHE_TITLES.read_text())
        if cached_titles == titles:
            print(f"  [cache hit] {len(titles)} alias embeddings")
            return np.load(CACHE_EMB)
        print("  [cache stale] titles changed, re-embedding")
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    out: list[list[float]] = []
    t0 = time.time()
    for i in range(0, len(titles), EMBED_BATCH):
        batch = titles[i:i + EMBED_BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        out.extend(d.embedding for d in resp.data)
        if (i // EMBED_BATCH) % 5 == 0:
            print(f"  embedded {min(i + EMBED_BATCH, len(titles))}/{len(titles)}  ({time.time() - t0:.0f}s)")
    arr = np.asarray(out, dtype=np.float32)
    np.save(CACHE_EMB, arr)
    CACHE_TITLES.write_text(json.dumps(titles))
    return arr


def solve_ridge(A: np.ndarray, C: np.ndarray, ridge_frac: float) -> np.ndarray:
    """W = (A^T A + lam I)^-1 (A^T C), lam scaled to the data."""
    d = A.shape[1]
    AtA = A.T @ A
    AtC = A.T @ C
    lam = ridge_frac * float(np.trace(AtA) / d)
    AtA[np.diag_indices_from(AtA)] += lam
    W = np.linalg.solve(AtA, AtC)
    return W.astype(np.float32), lam


def mean_cosine(A: np.ndarray, C: np.ndarray) -> float:
    """Mean row-wise cosine between two row-normalised matrices."""
    return float(np.mean(np.sum(_norm_rows(A) * _norm_rows(C), axis=1)))


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr)
        sys.exit(1)
    DATA.mkdir(exist_ok=True)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    print("Loading training pairs (excluding eval-gold codes)...")
    titles, codes, code_emb = load_pairs(conn)
    conn.close()
    print(f"  {len(titles)} pairs across {len(set(codes))} codes")

    print("Embedding alias titles...")
    A_all = embed_titles(titles)
    C_all = np.stack([code_emb[c] for c in codes]).astype(np.float32)
    # Align both spaces on the unit sphere - we only care about direction.
    A_all = _norm_rows(A_all)
    C_all = _norm_rows(C_all)

    # Held-out split BY CODE (not alias row) so a code's aliases don't span both
    # sides - an honest "did W generalise" check (codex review).
    rng = np.random.default_rng(42)
    uniq = sorted(set(codes))
    perm = rng.permutation(len(uniq))
    n_hold_codes = max(1, int(len(uniq) * HELDOUT_FRAC))
    hold_codes = {uniq[i] for i in perm[:n_hold_codes]}
    hold = np.array([i for i, c in enumerate(codes) if c in hold_codes], dtype=int)
    train = np.array([i for i, c in enumerate(codes) if c not in hold_codes], dtype=int)
    A_tr, C_tr = A_all[train], C_all[train]
    A_ho, C_ho = A_all[hold], C_all[hold]

    print(f"Solving ridge on {len(train)} pairs (held out {len(hold)})...")
    W, lam = solve_ridge(A_tr, C_tr, RIDGE_FRAC)

    base_cos = mean_cosine(A_ho, C_ho)
    adpt_cos = mean_cosine(A_ho @ W, C_ho)
    print(f"  lambda={lam:.4f}")
    print(f"  held-out mean cosine(alias, code):  baseline={base_cos:.4f}  adapted={adpt_cos:.4f}  "
          f"(delta {adpt_cos - base_cos:+.4f})")

    # Refit on ALL data for the shipped artifact.
    W_full, _ = solve_ridge(A_all, C_all, RIDGE_FRAC)
    np.save(W_PATH, W_full)
    META_PATH.write_text(json.dumps({
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "embed_model": EMBED_MODEL,
        "n_pairs": len(titles),
        "n_codes": len(set(codes)),
        "ridge_frac": RIDGE_FRAC,
        "lambda": lam,
        "heldout_baseline_cosine": base_cos,
        "heldout_adapted_cosine": adpt_cos,
        "dim": int(A_all.shape[1]),
        "note": "Linear retrieval adapter. Apply to QUERY vector only (v @ W, then L2-normalise). "
                "Excludes naive-persona eval-gold codes from training.",
    }, indent=2))
    print(f"Saved W to {W_PATH}  (shape {W_full.shape})")
    print(f"Saved meta to {META_PATH}")


if __name__ == "__main__":
    main()
