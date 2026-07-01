"""Query Performance Prediction (QPP) specificity predictors - the literature set.

Replaces the ad-hoc LLM "spec" score with the standard pre-retrieval predictors
(He & Ounis; Carmel & Yom-Tov survey) plus a neural-embedding specificity
(Arabzadeh et al., ECIR 2020). All cheap, no LLM on the lexical set.

Per kg.eval_gold query, computes -> kg.query_qpp:
  query_len   : # query lexemes (Postgres 'english' stems)
  avg_idf     : (1/|Q|) Sum log(N/df_t)        rare terms => specific
  max_idf     : max log(N/df_t)                single rarest term
  avg_ictf    : (1/|Q|) Sum log(|C|/cf_t)      inverse collection term freq
  scs         : log(1/|Q|) + avg_ictf          Simplified Clarity Score
  nn_density  : mean pairwise cosine among the query's top-k corpus neighbours
                (dense neighbourhood => specific; scattered => generic)

Collection = uk.goods_nomenclature_self_texts (the text the vector/FTS legs use).
df_t / cf_t come from a single ts_stat pass; query lexemes are stemmed by the
same 'english' config so they match the corpus stats.

Then correlates each predictor with recall@100 (point-biserial = Pearson vs the
binary gold-in-top-100 outcome) for a chosen run, and ranks them - so we can
show which predictor actually tracks retrieval, and that the cheap ones beat the
LLM score (r=0.11).

Env: QPP_RUN_LABEL(v2_plus_desc_vec) QPP_NN_K(20) QPP_EMBED_BATCH(256) QPP_FORCE(0)
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

for p in [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
RUN_LABEL = os.environ.get("QPP_RUN_LABEL", "v2_plus_desc_vec")
NN_K = int(os.environ.get("QPP_NN_K", "20"))
EMBED_BATCH = int(os.environ.get("QPP_EMBED_BATCH", "256"))
FORCE = os.environ.get("QPP_FORCE", "0") == "1"
PERSONA_ORDER = ["naive_vague", "naive_branded", "naive_specific",
                 "emu_generic", "emu_ordinary", "emu_specific", "original"]
PREDICTORS = ["query_len", "avg_idf", "max_idf", "avg_ictf", "scs", "nn_density", "llm_spec"]


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS kg.query_qpp (
                 gold_id bigint PRIMARY KEY, persona text, source_id text,
                 query_len int, avg_idf numeric, max_idf numeric,
                 avg_ictf numeric, scs numeric, nn_density numeric)""")
    conn.commit()


def load_corpus_stats(conn):
    """df_t (ndoc) + cf_t (nentry) per lexeme over the self-text collection, via ts_stat."""
    print("  building corpus df/cf via ts_stat (one pass over self_texts)...", flush=True)
    t0 = time.time()
    # Honest holdout: exclude eval-gold codes from the corpus stats (codex review).
    excl = ("AND goods_nomenclature_item_id NOT IN "
            "(SELECT left(regexp_replace(expected_code,'[^0-9]','','g'),10) FROM kg.eval_gold)")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) n FROM uk.goods_nomenclature_self_texts "
                    "WHERE self_text IS NOT NULL " + excl)
        N = cur.fetchone()["n"]
        cur.execute(
            "SELECT word, ndoc, nentry FROM ts_stat("
            "$$SELECT to_tsvector('english', self_text) FROM uk.goods_nomenclature_self_texts "
            "WHERE self_text IS NOT NULL " + excl + "$$)")
        df, cf = {}, {}
        for r in cur.fetchall():
            df[r["word"]] = r["ndoc"]
            cf[r["word"]] = r["nentry"]
    total_tokens = sum(cf.values()) or 1
    print(f"    N={N} docs, {len(df)} lexemes, |C|={total_tokens} tokens ({time.time()-t0:.0f}s)", flush=True)
    return N, total_tokens, df, cf


def query_lexemes(cur, query):
    """Query lexemes under the SAME 'english' stemmer as the corpus, so they match."""
    cur.execute("SELECT strip(to_tsvector('english', %s))::text AS tsv", (query,))
    tsv = cur.fetchone()["tsv"] or ""
    return [tok.strip("'") for tok in tsv.split() if tok.strip("'")]


def lexical_predictors(lexemes, N, total_tokens, df, cf):
    if not lexemes:
        return {"query_len": 0, "avg_idf": 0.0, "max_idf": 0.0, "avg_ictf": 0.0, "scs": 0.0}
    idfs = [math.log(N / max(df.get(t, 0.5), 0.5)) for t in lexemes]
    ictfs = [math.log(total_tokens / max(cf.get(t, 0.5), 0.5)) for t in lexemes]
    avg_ictf = sum(ictfs) / len(ictfs)
    return {
        "query_len": len(lexemes),
        "avg_idf": sum(idfs) / len(idfs),
        "max_idf": max(idfs),
        "avg_ictf": avg_ictf,
        "scs": math.log(1.0 / len(lexemes)) + avg_ictf,
    }


def embed_queries(queries):
    """text-embedding-3-small for every query (cheap; different rate bucket from gpt-5-mini)."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60.0, max_retries=4)
    out = []
    for i in range(0, len(queries), EMBED_BATCH):
        resp = client.embeddings.create(model="text-embedding-3-small", input=queries[i:i + EMBED_BATCH])
        out.extend(d.embedding for d in resp.data)
    return [np.asarray(e, dtype=np.float32) for e in out]


def nn_density(cur, qvec):
    """Mean pairwise cosine among the query's top-k nearest self-text neighbours."""
    s = "[" + ",".join(f"{x:.6f}" for x in qvec.tolist()) + "]"
    cur.execute(
        "SELECT search_embedding::text e FROM uk.goods_nomenclature_self_texts "
        "WHERE search_embedding IS NOT NULL "
        "AND goods_nomenclature_item_id NOT IN "
        "(SELECT left(regexp_replace(expected_code,'[^0-9]','','g'),10) FROM kg.eval_gold) "
        "ORDER BY search_embedding <=> %s::vector LIMIT %s",
        (s, NN_K))
    import json as _j
    vs = [np.asarray(_j.loads(r["e"]), dtype=np.float32) for r in cur.fetchall()]
    if len(vs) < 2:
        return None
    M = np.stack(vs)
    M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    sims = M @ M.T
    iu = np.triu_indices(len(vs), k=1)
    return float(sims[iu].mean())


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr); sys.exit(1)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, persona, source_id, query FROM kg.eval_gold ORDER BY id")
        gold = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT gold_id FROM kg.query_qpp")
        done = {r["gold_id"] for r in cur.fetchall()}
    todo = gold if FORCE else [g for g in gold if g["id"] not in done]
    print(f"{len(gold)} gold queries; computing QPP for {len(todo)}")

    if todo:
        N, total_tokens, df, cf = load_corpus_stats(conn)
        print("  embedding queries for neural density...", flush=True)
        qvecs = embed_queries([g["query"] for g in todo])
        print("  computing predictors...", flush=True)
        with conn.cursor() as cur:
            for i, (g, qv) in enumerate(zip(todo, qvecs)):
                lex = query_lexemes(cur, g["query"])
                lp = lexical_predictors(lex, N, total_tokens, df, cf)
                try:
                    nnd = nn_density(cur, qv)
                except Exception as exc:
                    print(f"    [nn_density err {g['id']}: {exc!r}]", flush=True)
                    nnd = None
                cur.execute(
                    """INSERT INTO kg.query_qpp (gold_id, persona, source_id, query_len, avg_idf,
                       max_idf, avg_ictf, scs, nn_density) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (gold_id) DO UPDATE SET query_len=EXCLUDED.query_len,
                       avg_idf=EXCLUDED.avg_idf, max_idf=EXCLUDED.max_idf, avg_ictf=EXCLUDED.avg_ictf,
                       scs=EXCLUDED.scs, nn_density=EXCLUDED.nn_density""",
                    (g["id"], g["persona"], g["source_id"], lp["query_len"], lp["avg_idf"],
                     lp["max_idf"], lp["avg_ictf"], lp["scs"], nnd))
                if (i + 1) % 50 == 0:
                    conn.commit(); print(f"    {i+1}/{len(todo)}", flush=True)
        conn.commit()
        print("  persisted to kg.query_qpp")

    # ---- per-persona means ----
    with conn.cursor() as cur:
        cur.execute("SELECT persona, count(*) n, round(avg(query_len),1) ql, round(avg(avg_idf),2) idf, "
                    "round(avg(max_idf),2) midf, round(avg(scs),2) scs, round(avg(nn_density),3) nnd "
                    "FROM kg.query_qpp GROUP BY persona")
        per = {r["persona"]: r for r in cur.fetchall()}
    print("\nPer-persona QPP means:")
    print(f"  {'persona':<16}{'len':>6}{'avgIDF':>8}{'maxIDF':>8}{'SCS':>8}{'nnDens':>8}")
    for p in PERSONA_ORDER:
        if p in per:
            r = per[p]
            print(f"  {p:<16}{float(r['ql']):>6.1f}{float(r['idf']):>8.2f}{float(r['midf']):>8.2f}"
                  f"{float(r['scs']):>8.2f}{float(r['nnd'] or 0):>8.3f}")

    # ---- correlation of each predictor with recall@100 ----
    with conn.cursor() as cur:
        cur.execute(
            """SELECT q.query_len, q.avg_idf, q.max_idf, q.avg_ictf, q.scs, q.nn_density,
                      d.llm_spec,
                      (rr.rank_of_expected IS NOT NULL AND rr.rank_of_expected<=100)::int hit
               FROM kg.query_qpp q
               JOIN kg.eval_run_results rr ON rr.gold_id=q.gold_id
               JOIN kg.eval_runs er ON er.id=rr.run_id
               LEFT JOIN kg.query_descriptiveness d ON d.gold_id=q.gold_id
               WHERE er.id=(SELECT max(id) FROM kg.eval_runs
                            WHERE run_label=%s AND finished_at IS NOT NULL AND curve_json IS NOT NULL)""",
            (RUN_LABEL,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if rows:
        hit = np.array([r["hit"] for r in rows], dtype=float)
        print(f"\nPredictor power vs recall@100 (run '{RUN_LABEL}', n={len(rows)}):")
        print(f"  {'predictor':<12}{'point-biserial r':>18}")
        scored = []
        for pred in PREDICTORS:
            vals = np.array([float(r[pred]) if r[pred] is not None else np.nan for r in rows])
            mask = ~np.isnan(vals)
            if mask.sum() > 2 and np.std(vals[mask]) > 0:
                r = float(np.corrcoef(vals[mask], hit[mask])[0, 1])
            else:
                r = float("nan")
            scored.append((pred, r))
        for pred, r in sorted(scored, key=lambda x: -(abs(x[1]) if x[1] == x[1] else -1)):
            print(f"  {pred:<12}{r:>18.3f}")
    else:
        print(f"\n(no eval_run_results for '{RUN_LABEL}' yet)")


if __name__ == "__main__":
    main()
