"""Retrieval-only source-family ablation over ATAR-labelled gold queries.

This runner answers a narrower question than the benchmark workbench:
which evidence sources move retrieval rank/recall the most?

It uses the existing ATAR-derived gold corpus, applies strict leave-one-out
exclusions for each target code, and never calls a judge. When vector configs
are enabled it caches each query embedding once in kg.query_embedding_cache so
each source configuration reuses the same embedding.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_env_candidates = [
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path.cwd() / ".env",
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
]
for _p in [p for p in _env_candidates if p is not None]:
    if _p.exists():
        load_dotenv(_p)
        break

import psycopg
from psycopg.rows import dict_row

from . import local_db
from .provider_guard import openai_allowed
from .run_eval import _matches_at, _norm_code, build_loo_map


DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
EMBEDDING_MODEL = os.environ.get("SOURCE_ABLATION_EMBEDDING_MODEL", "text-embedding-3-small")
DEFAULT_PERSONAS = ["naive_vague", "naive_branded", "naive_specific"]
K_VALUES = [1, 5, 10, 20, 50, 100, 200, 500]


def _base_config(*, vector: bool) -> dict[str, Any]:
    return {
        "use_curated": False,
        "use_vector": vector,
        "use_facts": False,
        "use_kg_context": False,
        "use_facts_vec": False,
        "use_kg_vec": False,
        "use_composite": True,
        "facts_cap": 0.5,
        "kg_cap": 0.5,
        "facts_vec_cap": 0.9,
        "kg_vec_cap": 0.9,
        "rrf_k": 60,
    }


def _with_facts(cfg: dict[str, Any], *, vector: bool, families: list[str] | None = None) -> dict[str, Any]:
    out = dict(cfg)
    out.update({"use_facts": True, "use_facts_vec": vector})
    if families:
        out["include_fact_families"] = families
    return out


def _with_edges(cfg: dict[str, Any], *, vector: bool, families: list[str] | None = None) -> dict[str, Any]:
    out = dict(cfg)
    out.update({"use_kg_context": True, "use_kg_vec": vector})
    if families:
        out["include_edge_families"] = families
    return out


def build_configs(*, vector: bool) -> dict[str, dict[str, Any]]:
    base = _base_config(vector=vector)

    all_evidence = dict(base)
    all_evidence.update({
        "use_curated": True,
        "use_facts": True,
        "use_kg_context": True,
        "use_facts_vec": vector,
        "use_kg_vec": vector,
    })

    configs: dict[str, dict[str, Any]] = {
        "base_text": base,
        "plus_ai_facets": _with_facts(base, vector=vector, families=["description_llm"]),
        "plus_search_references": _with_facts(
            {**base, "use_curated": True}, vector=vector, families=["search_references"],
        ),
        "plus_atar": _with_edges(
            _with_facts(base, vector=vector, families=["atar"]), vector=vector, families=["atar"],
        ),
        "plus_hsen": _with_edges(base, vector=vector, families=["hsen"]),
        "plus_chapter_notes": _with_edges(base, vector=vector, families=["chapter_notes"]),
        "plus_section_notes": _with_edges(base, vector=vector, families=["section_notes"]),
        "plus_girs": _with_edges(base, vector=vector, families=["girs"]),
        "plus_legal_notes": _with_edges(base, vector=vector, families=["chapter_notes", "section_notes", "girs"]),
        "plus_footnotes": _with_edges(base, vector=vector, families=["footnotes"]),
        "all_evidence": all_evidence,
        "drop_ai_facets": {**all_evidence, "exclude_fact_families": ["description_llm"]},
        "drop_search_references": {
            **all_evidence,
            "use_curated": False,
            "exclude_fact_families": ["search_references"],
        },
        "drop_atar": {
            **all_evidence,
            "exclude_fact_families": ["atar"],
            "exclude_edge_families": ["atar"],
        },
        "drop_hsen": {**all_evidence, "exclude_edge_families": ["hsen"]},
        "drop_chapter_notes": {**all_evidence, "exclude_edge_families": ["chapter_notes"]},
        "drop_section_notes": {**all_evidence, "exclude_edge_families": ["section_notes"]},
        "drop_legal_notes": {**all_evidence, "exclude_edge_families": ["chapter_notes", "section_notes", "girs"]},
        "drop_footnotes": {**all_evidence, "exclude_edge_families": ["footnotes"]},
    }
    return configs


def _ensure_embedding_cache(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kg.query_embedding_cache (
                cache_key text PRIMARY KEY,
                model text NOT NULL,
                query text NOT NULL,
                embedding_json jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _ensure_retrieval_indexes(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS commodity_facets_retrieval_fts_gin
            ON kg.commodity_facets
            USING gin (
                to_tsvector('english', facet_key || ' ' || facet_value || ' ' || COALESCE(evidence, ''))
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS kg_edges_retrieval_fts_gin
            ON kg.kg_edges
            USING gin (to_tsvector('english', title || ' ' || body))
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS commodity_facets_source_idx ON kg.commodity_facets (source)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS kg_edges_source_type_idx ON kg.kg_edges (source, type)"
        )
    conn.commit()


def _embedding_key(model: str, query: str) -> str:
    return hashlib.sha256(f"{model}\0{query}".encode("utf-8")).hexdigest()


def _load_cached_embeddings(conn, queries: list[str], model: str) -> dict[str, list[float]]:
    if not queries:
        return {}
    keys = [_embedding_key(model, q) for q in queries]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cache_key, embedding_json FROM kg.query_embedding_cache WHERE cache_key = ANY(%s)",
            (keys,),
        )
        by_key = {r["cache_key"]: [float(x) for x in r["embedding_json"]] for r in cur.fetchall()}
    return {q: by_key[_embedding_key(model, q)] for q in queries if _embedding_key(model, q) in by_key}


def _save_embeddings(conn, model: str, embeddings: dict[str, list[float]]) -> None:
    if not embeddings:
        return
    with conn.cursor() as cur:
        for query, embedding in embeddings.items():
            cur.execute(
                """
                INSERT INTO kg.query_embedding_cache (cache_key, model, query, embedding_json)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (cache_key) DO NOTHING
                """,
                (_embedding_key(model, query), model, query, json.dumps(embedding)),
            )
    conn.commit()


def _embed_missing(conn, queries: list[str], model: str, batch_size: int) -> dict[str, list[float]]:
    _ensure_embedding_cache(conn)
    cached = _load_cached_embeddings(conn, queries, model)
    missing = [q for q in queries if q not in cached]
    if not missing:
        print(f"embedding cache hit: {len(cached)}/{len(queries)}")
        return cached
    if not openai_allowed():
        raise RuntimeError(
            f"{len(missing)} query embeddings are missing and provider calls are not enabled"
        )

    from openai import OpenAI

    print(f"embedding cache miss: {len(missing)}/{len(queries)} unique queries")
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    created: dict[str, list[float]] = {}
    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        resp = client.embeddings.create(model=model, input=batch)
        for query, datum in zip(batch, resp.data):
            created[query] = [float(x) for x in datum.embedding]
        print(f"  embedded {min(i + len(batch), len(missing))}/{len(missing)}")
    _save_embeddings(conn, model, created)
    return {**cached, **created}


def _load_gold_rows(conn, personas: list[str], limit_codes: int) -> list[dict[str, Any]]:
    sql = """
        WITH eligible AS (
            SELECT expected_code
            FROM kg.eval_gold
            WHERE source_type = 'atar'
              AND active
              AND persona = ANY(%s)
            GROUP BY expected_code
            HAVING count(DISTINCT persona) = %s
            ORDER BY md5(expected_code)
            LIMIT %s
        ),
        ranked AS (
            SELECT id, query, expected_code, persona, source_id, source_type,
                   row_number() OVER (
                       PARTITION BY expected_code, persona
                       ORDER BY md5(coalesce(source_id, '') || ':' || id::text)
                   ) AS rn
            FROM kg.eval_gold
            WHERE expected_code IN (SELECT expected_code FROM eligible)
              AND persona = ANY(%s)
              AND source_type = 'atar'
              AND active
        )
        SELECT id, query, expected_code, persona, source_id, source_type
        FROM ranked
        WHERE rn = 1
        ORDER BY expected_code, persona
    """
    params = (personas, len(personas), limit_codes, personas)
    with conn.cursor() as cur:
        try:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(sql.replace("              AND active\n", "").replace("          AND active\n", ""), params)
            return [dict(r) for r in cur.fetchall()]


def _rank_metrics(candidates: list[dict[str, Any]], expected: str) -> tuple[int | None, int | None, int | None]:
    exact = subheading = heading = None
    for idx, candidate in enumerate(candidates, start=1):
        code = candidate.get("commodity_code") or ""
        is_exact, is_subheading, is_heading = _matches_at(code, expected)
        if is_exact and exact is None:
            exact = idx
        if is_subheading and subheading is None:
            subheading = idx
        if is_heading and heading is None:
            heading = idx
        if exact and subheading and heading:
            break
    return exact, subheading, heading


def _recall(ranks: list[int | None], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def _summarise(config: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ranks = [r["rank_exact"] for r in rows]
    present = [r for r in ranks if r is not None]
    out: dict[str, Any] = {
        "config": config,
        "n": len(rows),
        "mrr": sum((1.0 / r) for r in present) / len(ranks) if ranks else 0.0,
        "median_rank": statistics.median(present) if present else None,
        "hard_misses": sum(1 for r in ranks if r is None),
    }
    for k in K_VALUES:
        out[f"recall_at_{k}"] = _recall(ranks, k)
    for persona in sorted({r["persona"] for r in rows}):
        persona_ranks = [r["rank_exact"] for r in rows if r["persona"] == persona]
        out[f"{persona}_recall_at_100"] = _recall(persona_ranks, 100)
    return out


def _inventory(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT CASE
                     WHEN source LIKE 'atar:%' THEN 'atar'
                     WHEN source = 'search_reference' THEN 'search_references'
                     WHEN source = 'description_llm' THEN 'description_llm'
                     WHEN source = 'hand' THEN 'hand'
                     ELSE 'other'
                   END AS family,
                   count(*) AS n
            FROM kg.commodity_facets
            GROUP BY family
            ORDER BY n DESC
            """
        )
        fact_families = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT CASE
                     WHEN id LIKE 'atar\\_%' THEN 'atar'
                     WHEN id LIKE 'hsen:%' OR source LIKE 'hsen:%' THEN 'hsen'
                     WHEN source LIKE 'UK Tariff Chapter % Notes' THEN 'chapter_notes'
                     WHEN source LIKE 'UK Tariff Section % Notes' THEN 'section_notes'
                     WHEN type = 'classification_order' THEN 'girs'
                     WHEN type = 'footnote' OR source ILIKE '%footnote%' THEN 'footnotes'
                     ELSE 'other'
                   END AS family,
                   count(*) AS n
            FROM kg.kg_edges
            GROUP BY family
            ORDER BY n DESC
            """
        )
        edge_families = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM uk.measures")
        measure_rows = int(cur.fetchone()["n"])
    return {
        "fact_families": fact_families,
        "edge_families": edge_families,
        "measure_rows": measure_rows,
        "measure_note": "Measures are live tariff metadata but are not currently a source-labelled retrieval family.",
    }


def _write_outputs(output_dir: Path, label: str, payload: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{label}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    md_path = output_dir / f"{stem}.md"

    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    summary = payload["summary"]
    fieldnames = list(summary[0].keys()) if summary else []
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)

    lines = [
        "# Retrieval Source Ablation",
        "",
        f"- Run label: `{payload['run_label']}`",
        f"- Rows: `{payload['row_count']}` from `{payload['code_count']}` unique commodity codes",
        f"- Personas: `{', '.join(payload['personas'])}`",
        f"- Retrieval limit: `{payload['retrieval_limit']}`",
        f"- Vector enabled: `{payload['vector_enabled']}`",
        "",
        "## Inventory",
        "",
        "Fact families:",
        "",
        "| family | rows |",
        "|---|---:|",
    ]
    for row in payload["inventory"]["fact_families"]:
        lines.append(f"| {row['family']} | {row['n']} |")
    lines.extend(["", "Edge families:", "", "| family | rows |", "|---|---:|"])
    for row in payload["inventory"]["edge_families"]:
        lines.append(f"| {row['family']} | {row['n']} |")
    lines.extend([
        "",
        f"Measure rows in tariff DB: `{payload['inventory']['measure_rows']}`.",
        payload["inventory"]["measure_note"],
        "",
        "## Summary",
        "",
        "| config | r@20 | r@50 | r@100 | mrr | median rank | lift vs base r@100 | loss vs all r@100 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in summary:
        median = "" if row["median_rank"] is None else f"{float(row['median_rank']):.1f}"
        lines.append(
            f"| {row['config']} | {row['recall_at_20']:.3f} | {row['recall_at_50']:.3f} | "
            f"{row['recall_at_100']:.3f} | {row['mrr']:.3f} | {median} | "
            f"{row.get('lift_vs_base_at_100', 0.0):+.3f} | {row.get('loss_vs_all_at_100', 0.0):+.3f} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    if not personas:
        raise ValueError("at least one persona is required")
    vector = not args.no_vector
    configs = build_configs(vector=vector)
    if args.configs:
        requested = [c.strip() for c in args.configs.split(",") if c.strip()]
        configs = {k: configs[k] for k in requested if k in configs}
        missing = [k for k in requested if k not in configs]
        if missing:
            raise ValueError(f"unknown configs: {', '.join(missing)}")

    conn = psycopg.connect(DSN, row_factory=dict_row)
    try:
        if not args.skip_index_ensure:
            print("ensuring retrieval indexes")
            _ensure_retrieval_indexes(conn)
        rows = _load_gold_rows(conn, personas, args.limit_codes)
        if not rows:
            raise RuntimeError("no ATAR gold rows found for requested personas")
        code_count = len({_norm_code(r["expected_code"]) for r in rows})
        print(f"loaded {len(rows)} rows across {code_count} commodity codes")
        loo_map = build_loo_map(conn, rows)
        print(f"LOO map: {len(loo_map)} target codes")

        embeddings: dict[str, list[float]] = {}
        if vector:
            unique_queries = sorted({r["query"] for r in rows})
            embeddings = _embed_missing(conn, unique_queries, EMBEDDING_MODEL, args.embedding_batch_size)

        all_results: dict[str, list[dict[str, Any]]] = {}
        summary: list[dict[str, Any]] = []
        for config_name, config in configs.items():
            started = time.time()
            print(f"\n=== {config_name} ===")
            config_rows: list[dict[str, Any]] = []
            for idx, row in enumerate(rows, start=1):
                cfg = dict(config)
                fact_excl, edge_excl = loo_map.get(_norm_code(row.get("expected_code")), ([], []))
                cfg["exclude_fact_sources"] = fact_excl
                cfg["exclude_edge_ids"] = edge_excl
                if vector:
                    cfg["query_embedding"] = embeddings.get(row["query"])
                candidates = local_db.retrieve_candidates(
                    row["query"],
                    limit=args.retrieval_limit,
                    **cfg,
                )
                rank_exact, rank_subheading, rank_heading = _rank_metrics(candidates, row["expected_code"])
                config_rows.append({
                    "gold_id": row["id"],
                    "source_id": row["source_id"],
                    "persona": row["persona"],
                    "expected_code": row["expected_code"],
                    "rank_exact": rank_exact,
                    "rank_subheading": rank_subheading,
                    "rank_heading": rank_heading,
                    "top1": candidates[0]["commodity_code"] if candidates else None,
                    "sources_top1": candidates[0].get("sources", []) if candidates else [],
                })
                if idx % 50 == 0:
                    print(f"  {idx}/{len(rows)}")
            all_results[config_name] = config_rows
            row_summary = _summarise(config_name, config_rows)
            row_summary["elapsed_seconds"] = round(time.time() - started, 2)
            summary.append(row_summary)
            print(
                f"  r@100={row_summary['recall_at_100']:.3f} "
                f"mrr={row_summary['mrr']:.3f} misses={row_summary['hard_misses']}"
            )

        by_name = {r["config"]: r for r in summary}
        base_r100 = float(by_name.get("base_text", {}).get("recall_at_100") or 0.0)
        all_r100 = float(by_name.get("all_evidence", {}).get("recall_at_100") or 0.0)
        for row in summary:
            name = row["config"]
            r100 = float(row["recall_at_100"])
            row["lift_vs_base_at_100"] = round(r100 - base_r100, 6)
            row["loss_vs_all_at_100"] = round(all_r100 - r100, 6) if name.startswith("drop_") else 0.0

        payload = {
            "run_label": args.run_label,
            "started_at": started_at,
            "personas": personas,
            "row_count": len(rows),
            "code_count": code_count,
            "retrieval_limit": args.retrieval_limit,
            "vector_enabled": vector,
            "embedding_model": EMBEDDING_MODEL if vector else None,
            "configs": configs,
            "summary": summary,
            "results": all_results,
            "inventory": _inventory(conn),
        }
        payload["outputs"] = _write_outputs(Path(args.output_dir), args.run_label, payload)
        return payload
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run retrieval source-family ablations.")
    parser.add_argument("--run-label", default="source-ablation-100cc")
    parser.add_argument("--limit-codes", type=int, default=100)
    parser.add_argument("--personas", default=",".join(DEFAULT_PERSONAS))
    parser.add_argument("--retrieval-limit", type=int, default=500)
    parser.add_argument("--configs", default=None, help="Comma-separated subset of config names.")
    parser.add_argument("--no-vector", action="store_true", help="Disable vector legs and avoid embedding calls.")
    parser.add_argument("--embedding-batch-size", type=int, default=128)
    parser.add_argument("--skip-index-ensure", action="store_true")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parents[2] / "data" / "source_ablation"),
    )
    args = parser.parse_args()
    payload = run(args)
    print("\noutputs:")
    for kind, path in payload["outputs"].items():
        print(f"  {kind}: {path}")
    print("\nsummary:")
    for row in payload["summary"]:
        print(
            f"  {row['config']:<24} r@100={row['recall_at_100']:.3f} "
            f"lift={row['lift_vs_base_at_100']:+.3f} loss={row['loss_vs_all_at_100']:+.3f}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"source ablation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
