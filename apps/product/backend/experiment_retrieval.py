from __future__ import annotations

import csv
import html
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
SCHEMA = os.environ.get("TARIFF_DB_SCHEMA", "uk")
KG_SCHEMA = os.environ.get("TARIFF_DB_KG_SCHEMA", "kg")
MATRIX_CSV = Path(__file__).parent.parent / "data" / "matrix" / "retrieval_matrix.csv"

TOP_RUN_LABEL = "all_legs_on_gpt54mini_scope_qna_plus_facts"
DEFAULT_LIMIT = 500
DISPLAY_LIMIT = 25
MATRIX_ROWS_WITH_IMPLICIT_REFERENCES = {
    "all_legs_on",
    "all_legs_loo",
    "baseline_fts_only",
    "fts_plus_description_vec",
    "no_semantic_kg",
}


def _conn():
    return psycopg.connect(DSN, row_factory=dict_row)


@lru_cache(maxsize=32)
def _kg_has_column(table: str, column: str) -> bool:
    """Runtime-tolerant probe for optional KG evidence-label migrations."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = %s
                LIMIT 1
                """,
                (KG_SCHEMA, table, column),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _kg_use_scope_filter(alias: str, table: str, scope: str) -> str:
    if not _kg_has_column(table, "use_scopes"):
        return ""
    return f"AND '{scope}' = ANY({alias}.use_scopes)"


from commodity_codes import flat_code as _flat_code  # canonical, shared


def _pct(value: str) -> float:
    try:
        return round(float(value) * 100, 1)
    except Exception:
        return 0.0


def _normalise_matrix_config(run_label: str, cfg: dict[str, Any]) -> dict[str, Any]:
    normalised = dict(cfg)
    if "use_curated" not in normalised and run_label in MATRIX_ROWS_WITH_IMPLICIT_REFERENCES:
        normalised["use_curated"] = True
    return normalised


PERSONA_ORDER = [
    "naive_vague",
    "naive_branded",
    "naive_specific",
    "emu_generic",
    "emu_ordinary",
    "emu_specific",
    "original",
]
PERSONA_SHORT = {
    "naive_vague": "naive vague",
    "naive_branded": "naive branded",
    "naive_specific": "naive specific",
    "emu_generic": "expert generic",
    "emu_ordinary": "expert ordinary",
    "emu_specific": "expert specific",
    "original": "ATAR original",
}
SPECIFICITY_SIGNALS = ["query_len", "avg_idf", "max_idf", "avg_ictf", "nn_density", "llm_spec"]
SPECIFICITY_SIGN = {
    "query_len": 1.0,
    "avg_idf": 1.0,
    "max_idf": 1.0,
    "avg_ictf": 1.0,
    "nn_density": -1.0,
    "llm_spec": 1.0,
}


def _active_eval_gold_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"WHERE {prefix}active" if _kg_has_column("eval_gold", "active") else ""


def _ensure_qpp_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {KG_SCHEMA}.query_qpp (
              gold_id bigint PRIMARY KEY,
              persona text,
              source_id text,
              query_len integer,
              avg_idf numeric,
              max_idf numeric,
              avg_ictf numeric,
              scs numeric,
              nn_density numeric,
              input_quality numeric
            )
            """
        )
        cur.execute(f"ALTER TABLE {KG_SCHEMA}.query_qpp ADD COLUMN IF NOT EXISTS input_quality numeric")
        cur.execute(f"ALTER TABLE {KG_SCHEMA}.query_qpp ADD COLUMN IF NOT EXISTS nn_density numeric")
    conn.commit()


def _query_lexemes(cur, query: str) -> list[str]:
    cur.execute("SELECT strip(to_tsvector('english', %s))::text AS tsv", (query,))
    tsv = str((cur.fetchone() or {}).get("tsv") or "")
    out: list[str] = []
    for part in tsv.split():
        token = part.split(":", 1)[0].strip("'")
        if token:
            out.append(token)
    return out


@lru_cache(maxsize=1)
def _corpus_stats() -> tuple[int, int, dict[str, int], dict[str, int]]:
    active = "WHERE active" if _kg_has_column("eval_gold", "active") else ""
    exclude_sql = (
        f"AND goods_nomenclature_item_id NOT IN ("
        f"SELECT left(regexp_replace(expected_code,'[^0-9]','','g'),10) FROM {KG_SCHEMA}.eval_gold {active})"
    )
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM {SCHEMA}.goods_nomenclature_self_texts "
            f"WHERE self_text IS NOT NULL {exclude_sql}"
        )
        n_docs = int((cur.fetchone() or {}).get("n") or 1)
        cur.execute(
            "SELECT word, ndoc, nentry FROM ts_stat("
            f"$$SELECT to_tsvector('english', self_text) FROM {SCHEMA}.goods_nomenclature_self_texts "
            f"WHERE self_text IS NOT NULL {exclude_sql}$$)"
        )
        df: dict[str, int] = {}
        cf: dict[str, int] = {}
        for r in cur.fetchall():
            df[str(r["word"])] = int(r["ndoc"] or 0)
            cf[str(r["word"])] = int(r["nentry"] or 0)
    return n_docs, sum(cf.values()) or 1, df, cf


def _lexical_predictors(query: str) -> dict[str, float | int]:
    n_docs, total_tokens, df, cf = _corpus_stats()
    with _conn() as c, c.cursor() as cur:
        lexemes = _query_lexemes(cur, query)
    if not lexemes:
        return {"query_len": 0, "avg_idf": 0.0, "max_idf": 0.0, "avg_ictf": 0.0, "scs": 0.0}
    idfs = [math.log(n_docs / max(df.get(t, 0.5), 0.5)) for t in lexemes]
    ictfs = [math.log(total_tokens / max(cf.get(t, 0.5), 0.5)) for t in lexemes]
    avg_ictf = sum(ictfs) / len(ictfs)
    return {
        "query_len": len(lexemes),
        "avg_idf": sum(idfs) / len(idfs),
        "max_idf": max(idfs),
        "avg_ictf": avg_ictf,
        "scs": math.log(1.0 / len(lexemes)) + avg_ictf,
    }


def _specificity_raw_scores(rows: list[dict[str, Any]]) -> list[float]:
    usable: list[str] = []
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    for key in SPECIFICITY_SIGNALS:
        values = [float(r[key]) for r in rows if r.get(key) is not None]
        if not values:
            continue
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / max(len(values), 1)
        std = math.sqrt(variance) or 1.0
        if std <= 0:
            continue
        means[key] = mean
        stds[key] = std
        usable.append(key)
    if not usable:
        return [0.0 for _ in rows]
    out: list[float] = []
    for row in rows:
        total = 0.0
        weight = 0.0
        for key in usable:
            value = row.get(key)
            v = float(value) if value is not None else means[key]
            total += SPECIFICITY_SIGN[key] * ((v - means[key]) / stds[key])
            weight += abs(SPECIFICITY_SIGN[key])
        out.append(total / max(weight, 1.0))
    return out


def _refresh_input_quality(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT q.gold_id, q.query_len, q.avg_idf, q.max_idf, q.avg_ictf, q.scs,
                   q.nn_density, d.llm_spec
            FROM {KG_SCHEMA}.query_qpp q
            LEFT JOIN {KG_SCHEMA}.query_descriptiveness d ON d.gold_id = q.gold_id
            ORDER BY q.gold_id
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return 0
    raw = _specificity_raw_scores(rows)
    order = sorted(range(len(raw)), key=lambda idx: raw[idx])
    percentiles = [0.0] * len(raw)
    denom = max(len(raw) - 1, 1)
    for rank, idx in enumerate(order):
        percentiles[idx] = round(100.0 * rank / denom, 1)
    with conn.cursor() as cur:
        for row, value in zip(rows, percentiles):
            cur.execute(
                f"UPDATE {KG_SCHEMA}.query_qpp SET input_quality = %s WHERE gold_id = %s",
                (value, row["gold_id"]),
            )
    conn.commit()
    return len(rows)


def ensure_qpp_backfill(force: bool = False) -> dict[str, Any]:
    """Populate kg.query_qpp with non-LLM lexical predictors when missing."""
    with _conn() as conn:
        _ensure_qpp_table(conn)
        active = _active_eval_gold_clause()
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM {KG_SCHEMA}.eval_gold {active}")
            gold_count = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(f"SELECT count(*) AS n FROM {KG_SCHEMA}.query_qpp")
            qpp_count = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(f"SELECT count(*) AS n FROM {KG_SCHEMA}.query_qpp WHERE input_quality IS NOT NULL")
            iq_count = int((cur.fetchone() or {}).get("n") or 0)
        if force or qpp_count < gold_count or iq_count < max(1, qpp_count):
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, persona, source_id, query
                    FROM {KG_SCHEMA}.eval_gold
                    {active}
                    ORDER BY id
                    """
                )
                gold = [dict(r) for r in cur.fetchall()]
                cur.execute(f"SELECT gold_id FROM {KG_SCHEMA}.query_qpp")
                done = {int(r["gold_id"]) for r in cur.fetchall()}
                todo = gold if force else [g for g in gold if int(g["id"]) not in done]
                for g in todo:
                    lp = _lexical_predictors(str(g.get("query") or ""))
                    cur.execute(
                        f"""
                        INSERT INTO {KG_SCHEMA}.query_qpp
                          (gold_id, persona, source_id, query_len, avg_idf, max_idf, avg_ictf, scs, nn_density)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (gold_id) DO UPDATE
                          SET persona = EXCLUDED.persona,
                              source_id = EXCLUDED.source_id,
                              query_len = EXCLUDED.query_len,
                              avg_idf = EXCLUDED.avg_idf,
                              max_idf = EXCLUDED.max_idf,
                              avg_ictf = EXCLUDED.avg_ictf,
                              scs = EXCLUDED.scs
                        """,
                        (
                            g["id"], g.get("persona"), g.get("source_id"),
                            lp["query_len"], lp["avg_idf"], lp["max_idf"], lp["avg_ictf"], lp["scs"], None,
                        ),
                    )
            conn.commit()
            refreshed = _refresh_input_quality(conn)
            return {
                "available": True,
                "backfilled": len(todo),
                "scored": refreshed,
                "mode": "lexical_only_non_llm",
                "note": "QPP backfill used query length and corpus term-rarity signals; no provider calls.",
            }
        return {
            "available": True,
            "backfilled": 0,
            "scored": iq_count,
            "mode": "already_populated",
            "note": "Existing kg.query_qpp rows were reused.",
        }


def query_lexical_specificity(query: str) -> dict[str, Any]:
    try:
        backfill = ensure_qpp_backfill()
        lp = _lexical_predictors(query)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT q.query_len, q.avg_idf, q.max_idf, q.avg_ictf, q.scs,
                       q.nn_density, d.llm_spec
                FROM {KG_SCHEMA}.query_qpp q
                LEFT JOIN {KG_SCHEMA}.query_descriptiveness d ON d.gold_id = q.gold_id
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {"available": False, "label": "Lexical specificity", "score": None, "note": "No QPP reference rows available."}
        target = dict(lp)
        target["nn_density"] = None
        target["llm_spec"] = None
        raw = _specificity_raw_scores(rows + [target])
        score = round(100.0 * sum(1 for value in raw[:-1] if value <= raw[-1]) / max(len(raw) - 1, 1), 1)
        return {
            "available": True,
            "label": "Lexical specificity",
            "score": score,
            "basis": "pre_retrieval_qpp",
            "mode": backfill.get("mode"),
            "note": "Measures how specific/rare the wording looks versus eval inputs; not a recall predictor.",
            "signals": lp,
        }
    except Exception as exc:
        return {
            "available": False,
            "label": "Lexical specificity",
            "score": None,
            "note": f"Could not compute lexical specificity: {type(exc).__name__}: {str(exc)[:180]}",
        }


def _difficulty_band(score: float | None) -> tuple[str, str]:
    if score is None:
        return "unavailable", "neutral"
    if score >= 70:
        return "hard", "bad"
    if score >= 45:
        return "mixed", "warning"
    if score >= 25:
        return "moderate", "neutral"
    return "easy", "good"


@lru_cache(maxsize=1)
def _chapter_to_section() -> dict[str, str]:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                SELECT LEFT(gn.goods_nomenclature_item_id, 2) AS chapter_digits,
                       s.numeral AS section_numeral
                FROM {SCHEMA}.chapters_sections cs
                JOIN {SCHEMA}.sections s ON s.id = cs.section_id
                JOIN {SCHEMA}.goods_nomenclatures gn ON gn.goods_nomenclature_sid = cs.goods_nomenclature_sid
                """
            )
            return {str(r["chapter_digits"]): str(r["section_numeral"] or "") for r in cur.fetchall()}
    except Exception:
        return {}


def _candidate_complexity_rows(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[int, int]]:
    flats = [_flat_code(str(row.get("commodity_code") or row.get("code") or "")) for row in candidates]
    flats = [code for code in flats if code]
    meta: dict[str, dict[str, Any]] = {}
    if flats:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT ON (gn.goods_nomenclature_item_id)
                           gn.goods_nomenclature_item_id AS code,
                           gn.goods_nomenclature_sid AS sid,
                           gnt.number_indents,
                           st.generation_type
                    FROM {SCHEMA}.goods_nomenclatures gn
                    LEFT JOIN {SCHEMA}.goods_nomenclature_tree_nodes gnt
                      ON gnt.goods_nomenclature_sid = gn.goods_nomenclature_sid
                    LEFT JOIN {SCHEMA}.goods_nomenclature_self_texts st
                      ON st.goods_nomenclature_sid = gn.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = ANY(%s)
                    ORDER BY gn.goods_nomenclature_item_id, gn.validity_start_date DESC NULLS LAST
                    """,
                    (flats,),
                )
                meta = {str(r["code"]): dict(r) for r in cur.fetchall()}
        except Exception:
            meta = {}
    rows: list[dict[str, Any]] = []
    indent_by_sid: dict[int, int] = {}
    for idx, row in enumerate(candidates, start=1):
        code = _flat_code(str(row.get("commodity_code") or row.get("code") or ""))
        if not code:
            continue
        m = meta.get(code) or {}
        sid = int(m.get("sid") or idx)
        indent = int(m.get("number_indents") or 0)
        indent_by_sid[sid] = indent
        try:
            score_f = float(row.get("score") if row.get("score") is not None else max(0.000001, 1.0 / (idx + 60)))
        except Exception:
            score_f = max(0.000001, 1.0 / (idx + 60))
        item = {
            "goods_nomenclature_item_id": code,
            "goods_nomenclature_sid": sid,
            "score": score_f,
            "generation_type": m.get("generation_type"),
        }
        if row.get("cosine_score") is not None:
            item["cosine_score"] = row.get("cosine_score")
        rows.append(item)
    return rows, indent_by_sid


def _difficulty_explanation(metrics: dict[str, Any]) -> str:
    if not metrics or int(metrics.get("n_results") or 0) <= 0:
        return "No retrieved candidates, so the query is maximally difficult for this stack."
    parts: list[str] = []
    if int(metrics.get("n_section") or 0) > 1:
        parts.append(f"spans {int(metrics.get('n_section') or 0)} sections")
    elif int(metrics.get("n_chapter") or 0) > 1:
        parts.append(f"spans {int(metrics.get('n_chapter') or 0)} chapters")
    elif int(metrics.get("n_heading") or 0) > 1:
        parts.append(f"spans {int(metrics.get('n_heading') or 0)} headings")
    else:
        parts.append("stays in one narrow tariff area")
    if int(metrics.get("worst_case_questions") or 0) > 0:
        parts.append(f"about {int(metrics.get('worst_case_questions') or 0)} worst-case Q&A turns")
    if float(metrics.get("score_flatness") or 0) >= 0.8:
        parts.append("scores are flat")
    if float(metrics.get("vagueness") or 0) >= 0.2:
        parts.append("retrieval signal is weak")
    return "; ".join(parts) + "."


def query_difficulty_from_candidates(query: str, candidates: list[dict[str, Any]], k: int | None = None) -> dict[str, Any]:
    limit = max(1, min(int(k or len(candidates) or 1), len(candidates) or 1))
    subset = list(candidates or [])[:limit]
    if not subset:
        score = 100.0
        metrics = {"term": query, "k": limit, "n_results": 0, "composite": 1.0, "vagueness": 1.0}
    else:
        try:
            from intercept_kpis import compute
            rows, indent_by_sid = _candidate_complexity_rows(subset)
            kpis = compute(
                term=query,
                results=rows,
                k=limit,
                chapter_to_section=_chapter_to_section(),
                indent_depth_by_sid=indent_by_sid,
            )
            metrics = kpis.as_row()
            score = round(max(0.0, min(1.0, float(metrics.get("composite") or 0.0))) * 100.0, 1)
        except Exception as exc:
            return {
                "available": False,
                "label": "Query difficulty",
                "score": None,
                "band": "unavailable",
                "tone": "neutral",
                "note": f"Could not compute query difficulty: {type(exc).__name__}: {str(exc)[:180]}",
            }
    band, tone = _difficulty_band(score)
    return {
        "available": True,
        "label": "Query difficulty",
        "score": score,
        "band": band,
        "tone": tone,
        "basis": "post_retrieval_shortlist_complexity",
        "note": "Higher means the shortlist is broader/flatter and likely needs more disambiguation; not a recall prediction.",
        "explanation": _difficulty_explanation(metrics),
        "metrics": metrics,
    }


def _iq_cell(value: Any) -> str:
    if value is None:
        return "<td style='text-align:center;color:#6b7280'>-</td>"
    try:
        v = float(value)
    except Exception:
        return f"<td style='text-align:center;color:#cbd5e1'>{html.escape(str(value))}</td>"
    if v >= 70:
        bg, fg = "#064e3b", "#6ee7b7"
    elif v >= 55:
        bg, fg = "#065f46", "#a7f3d0"
    elif v >= 45:
        bg, fg = "#3f6212", "#d9f99d"
    elif v >= 35:
        bg, fg = "#854d0e", "#fde68a"
    else:
        bg, fg = "#7c2d12", "#fed7aa"
    return f"<td style='background:{bg};color:{fg};text-align:center;font-variant-numeric:tabular-nums'>{v:.0f}</td>"


def matrix_input_quality_html() -> str:
    summary = ensure_qpp_backfill()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT persona, round(avg(input_quality), 0) AS iq, count(*) AS n
            FROM {KG_SCHEMA}.query_qpp
            GROUP BY persona
            """
        )
        rows = {str(r["persona"]): dict(r) for r in cur.fetchall()}
    headers = "".join(f"<th style='text-align:center'>{html.escape(PERSONA_SHORT.get(p, p))}</th>" for p in PERSONA_ORDER)
    body = "".join(_iq_cell((rows.get(p) or {}).get("iq")) for p in PERSONA_ORDER)
    note = html.escape(str(summary.get("note") or ""))
    return (
        "<section id='matrix-input-quality' style='margin-top:28px'>"
        "<h2 style='font-size:16px;margin:18px 0 6px;color:#e5e7eb'>Input phrasing quality</h2>"
        "<div style='color:#9ca3af;font-size:13px;margin-bottom:10px;max-width:1040px;line-height:1.5'>"
        "Lexical specificity, 0-100: query length plus corpus term rarity, with optional existing LLM specificity if populated. "
        "This measures wording specificity, not recall. "
        f"{note}</div>"
        "<table><thead><tr><th style='text-align:left'>Lexical specificity</th>" + headers + "</tr></thead>"
        "<tbody><tr><td style='color:#cbd5e1'>0-100 by persona</td>" + body + "</tr></tbody></table></section>"
    )




def _load_matrix_rows() -> list[dict[str, Any]]:
    with MATRIX_CSV.open(newline="") as f:
        rows = list(csv.DictReader(f))
    out: list[dict[str, Any]] = []
    for row in rows:
        run_label = row.get("run_label", "")
        try:
            cfg = json.loads(row.get("config_json") or "{}")
        except json.JSONDecodeError:
            cfg = {}
        cfg = _normalise_matrix_config(run_label, cfg)
        rank_raw = row.get("rank_by_code_macro_at_100") or ""
        rank = int(rank_raw) if rank_raw.isdigit() else None
        title, description, caveats = describe_experiment(run_label, cfg)
        out.append(
            {
                "rank": rank,
                "run_label": run_label,
                "run_id": row.get("run_id"),
                "title": title,
                "description": description,
                "caveats": caveats,
                "headline_recall_pct": _pct(row.get("code_macro_recall_at_100", "0")),
                "ott_baseline": str(row.get("ott_baseline", "")).lower() == "true",
                "runnable": is_runnable_config(cfg),
                "config": cfg,
            }
        )
    return out


def experiment_catalog() -> list[dict[str, Any]]:
    return _load_matrix_rows()


def top_experiment_info() -> dict[str, Any]:
    rows = _load_matrix_rows()
    for row in rows:
        if row["run_label"] == TOP_RUN_LABEL:
            return row
    return rows[0]


def select_experiment(run_label: str | None = None) -> dict[str, Any]:
    catalog = _load_matrix_rows()
    selected_label = run_label or TOP_RUN_LABEL
    for row in catalog:
        if row["run_label"] == selected_label:
            return row
    raise ValueError(f"Unknown experiment: {run_label}")


def experiment_requires_provider(run_label: str | None = None) -> bool:
    cfg = select_experiment(run_label)["config"]
    return bool(cfg.get("use_vector") or cfg.get("use_facts_vec") or cfg.get("use_kg_vec"))


def is_runnable_config(cfg: dict[str, Any]) -> bool:
    # Query rewrite / triage depends on the eval-time rewrite harness, which is
    # intentionally not included in this shareable app.
    return not bool(cfg.get("triage"))


def _enabled_parts(cfg: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if cfg.get("use_composite"):
        parts.append("AI-enriched code text")
    if cfg.get("use_vector"):
        parts.append("semantic vector")
    if cfg.get("use_facts") or cfg.get("use_facts_vec"):
        parts.append("facets")
    if cfg.get("use_kg_context") or cfg.get("use_kg_vec"):
        parts.append("KG")
    if cfg.get("use_curated"):
        parts.append("Search References")
    if cfg.get("triage"):
        parts.append("rewrite")
    return parts or ["keyword FTS"]


def describe_experiment(run_label: str, cfg: dict[str, Any]) -> tuple[str, str, list[str]]:
    known_titles = {
        "baseline_fts_only": "Classic keyword baseline",
        "staging_ai": "Production-style AI staging baseline",
        "rw_g41_staging": "Staging rewrite upper-bound baseline",
        "no_curated_only": "Top overall: semantic + KG + facets, no Search References",
        "all_legs_on": "Semantic + KG + facets + Search References",
        "all_legs_on_gpt54mini_scope_qna_plus_facts": "All retrieval legs + GPT-5.4-mini scope_qna_plus facts",
        "ai_semantic_composite_triage": "AI-enriched semantic search with query rewrite",
        "rw_g5_staging": "Staging rewrite with GPT-5-class rewrite model",
        "rw_g5_mine": "Eval rewrite with GPT-5-class rewrite model",
    }

    title = known_titles.get(run_label)
    if not title:
        if run_label.startswith("facts_cap"):
            cap = cfg.get("facts_cap", "?")
            title = f"AI-enriched rewrite + facets capped at {cap}"
        elif run_label.startswith("grid_"):
            title = "Grid: " + " + ".join(_enabled_parts(cfg))
        elif run_label.startswith("exp2_off_"):
            removed = run_label.removeprefix("exp2_off_").replace("_", " ")
            title = f"Ablation: {removed} removed"
        elif run_label.startswith("exp2_rrf_"):
            title = f"RRF fusion sweep: {run_label.removeprefix('exp2_rrf_')}"
        elif run_label.startswith("exp3_"):
            title = f"Secondary-leg cap sweep: {run_label.removeprefix('exp3_').replace('_', ' ')}"
        else:
            title = run_label.replace("_", " ")

    signals: list[str] = []
    if cfg.get("use_composite"):
        signals.append("AI-enriched code text")
    else:
        signals.append("base commodity self-text")
    if cfg.get("use_vector"):
        signals.append("semantic vector search")
    else:
        signals.append("no description-vector leg")
    if cfg.get("use_curated", False):
        signals.append("Search References matches")
    elif "use_curated" in cfg:
        signals.append("Search References disabled")
    else:
        signals.append("Search References not flagged in the matrix config")
    if cfg.get("use_facts"):
        signals.append("structured facet keyword matches")
    if cfg.get("use_facts_vec"):
        signals.append("structured facet semantic matches")
    if cfg.get("use_kg_context"):
        signals.append("KG rule/note keyword matches")
    if cfg.get("use_kg_vec"):
        signals.append("KG rule/note semantic matches")
    if cfg.get("triage"):
        signals.append("query rewrite / triage before retrieval")

    extras: list[str] = []
    if cfg.get("fact_author_model"):
        extras.append(
            "LLM fact authoring: "
            f"{cfg.get('fact_author_model')} / {cfg.get('fact_prompt_version', 'unknown prompt')}"
        )
    if cfg.get("fact_rows_active"):
        extras.append(
            f"active LLM facts: {cfg.get('fact_rows_active'):,} across {cfg.get('fact_codes_active', '?')} CCs"
        )
    if cfg.get("loo") is not None:
        extras.append(f"leave-one-out: {'on' if cfg.get('loo') else 'off'}")
    if cfg.get("multi_query") is not None:
        extras.append(f"multi-query: {'on' if cfg.get('multi_query') else 'off'}")
    if cfg.get("active_gold_count"):
        extras.append(f"evaluation inputs: {cfg.get('active_gold_count')}")

    description = "Uses " + "; ".join(signals + extras) + "."
    caveats: list[str] = []
    if cfg.get("triage"):
        caveats.append("This row is described in the catalog but is not runnable in the local trial form because the rewrite harness is not bundled.")
    if cfg.get("retrieval_limit"):
        caveats.append(f"The matrix headline is recall@100; this run was evaluated with retrieval_limit={cfg['retrieval_limit']}.")
    if cfg.get("fact_author_model"):
        caveats.append(
            "New fact-model permutation: normalized KG/fact labels include "
            f"{cfg.get('fact_author_model')} facts from prompt {cfg.get('fact_prompt_version', 'unknown')}."
        )
    return title, description, caveats


def _embed_query(text: str, api_key: str | None) -> list[float]:
    if not api_key:
        raise RuntimeError("OpenAI API key required for the top experiment because it uses semantic vector legs.")
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.embeddings.create(model="text-embedding-3-small", input=text)
    return response.data[0].embedding


def _fts_leg(query: str, limit: int) -> list[dict[str, Any]]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT st.goods_nomenclature_item_id AS commodity_code,
                   st.self_text AS description,
                   ts_rank_cd(to_tsvector('english', st.self_text), q.tsq) AS score
            FROM {SCHEMA}.goods_nomenclature_self_texts st, q
            WHERE to_tsvector('english', st.self_text) @@ q.tsq
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "fts",
            }
            for r in cur.fetchall()
        ]


def _description_substring_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (gn.goods_nomenclature_item_id)
                   gn.goods_nomenclature_item_id AS commodity_code,
                   gnd.description AS description,
                   similarity(gnd.description, %s) AS score
            FROM {SCHEMA}.goods_nomenclatures gn
            JOIN {SCHEMA}.goods_nomenclature_descriptions gnd
              ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
            WHERE gn.validity_end_date IS NULL
              AND gnd.description ILIKE %s
            ORDER BY gn.goods_nomenclature_item_id, similarity(gnd.description, %s) DESC
            LIMIT %s
            """,
            (query, f"%{query}%", query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "substring",
            }
            for r in cur.fetchall()
        ]


def _curated_leg(query: str, limit: int) -> list[dict[str, Any]]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH direct AS (
              SELECT sr.goods_nomenclature_item_id AS commodity_code,
                     COALESCE(
                       (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                        WHERE gnd.goods_nomenclature_sid = sr.goods_nomenclature_sid LIMIT 1),
                       sr.title
                     ) AS description,
                     similarity(sr.title, %s) AS score
              FROM {SCHEMA}.search_references sr
              WHERE sr.title %% %s OR sr.title ILIKE %s
              ORDER BY similarity(sr.title, %s) DESC
              LIMIT %s
            ),
            siblings AS (
              SELECT gn.goods_nomenclature_item_id AS commodity_code,
                     gnd.description AS description,
                     0.05 AS score
              FROM {SCHEMA}.goods_nomenclatures gn
              JOIN {SCHEMA}.goods_nomenclature_descriptions gnd
                ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
              WHERE gn.validity_end_date IS NULL
                AND LEFT(gn.goods_nomenclature_item_id, 4) IN (
                  SELECT DISTINCT LEFT(commodity_code, 4) FROM direct
                )
                AND gn.goods_nomenclature_item_id NOT IN (SELECT commodity_code FROM direct)
              LIMIT %s
            )
            SELECT * FROM direct
            UNION ALL
            SELECT * FROM siblings
            """,
            (query, query, f"%{query}%", query, limit, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "reference",
            }
            for r in cur.fetchall()
            if r["commodity_code"]
        ]


def _facts_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT cf.commodity_code,
                   max(
                     ts_rank_cd(
                       to_tsvector('english', cf.facet_key || ' ' || cf.facet_value || ' ' || COALESCE(cf.evidence, '')),
                       q.tsq
                     )
                     * COALESCE(
                       CASE cf.authority_tier
                         WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0 WHEN 4 THEN 1.0
                         WHEN 5 THEN 0.85 WHEN 6 THEN 0.70 WHEN 7 THEN 0.70
                         ELSE 0.50
                       END, 0.70
                     )
                   ) AS score,
                   min(cf.authority_tier) AS best_tier,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = cf.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM {KG_SCHEMA}.commodity_facets cf, q
            WHERE to_tsvector('english', cf.facet_key || ' ' || cf.facet_value || ' ' || COALESCE(cf.evidence, '')) @@ q.tsq
              {_kg_use_scope_filter("cf", "commodity_facets", "retrieval")}
            GROUP BY cf.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "facts",
                "best_tier": r.get("best_tier"),
            }
            for r in cur.fetchall()
        ]


def _kg_context_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT kec.commodity_code,
                   max(
                     ts_rank_cd(to_tsvector('english', e.title || ' ' || e.body), q.tsq)
                     * COALESCE(
                       CASE e.authority_tier
                         WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0
                         ELSE 0.80
                       END, 0.80
                     )
                   ) AS score,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = kec.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM {KG_SCHEMA}.kg_edges e
            JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = e.id
            CROSS JOIN q
            WHERE to_tsvector('english', e.title || ' ' || e.body) @@ q.tsq
              {_kg_use_scope_filter("e", "kg_edges", "retrieval")}
            GROUP BY kec.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "kg_context",
            }
            for r in cur.fetchall()
        ]


def _vector_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT st.goods_nomenclature_item_id AS commodity_code,
                   st.self_text AS description,
                   1 - (st.search_embedding <=> %s::vector) AS score
            FROM {SCHEMA}.goods_nomenclature_self_texts st
            WHERE st.search_embedding IS NOT NULL
            ORDER BY st.search_embedding <=> %s::vector
            LIMIT %s
            """,
            (literal, literal, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "vector",
            }
            for r in cur.fetchall()
        ]


def _composite_fts_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT goods_nomenclature_item_id AS commodity_code,
                   composite_text AS description,
                   ts_rank_cd(to_tsvector('english', composite_text), q.tsq) AS score
            FROM {KG_SCHEMA}.composite_search_text, q
            WHERE to_tsvector('english', composite_text) @@ q.tsq
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "fts_composite",
            }
            for r in cur.fetchall()
        ]


def _composite_vector_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT goods_nomenclature_item_id AS commodity_code,
                   composite_text AS description,
                   1 - (composite_embedding <=> %s::vector) AS score
            FROM {KG_SCHEMA}.composite_search_text
            WHERE composite_embedding IS NOT NULL
            ORDER BY composite_embedding <=> %s::vector
            LIMIT %s
            """,
            (literal, literal, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "vector_composite",
            }
            for r in cur.fetchall()
        ]


def _facts_vec_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    fact_pool = limit * 4
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH top_facts AS (
                SELECT cf.commodity_code,
                       cf.authority_tier,
                       1 - (cf.embedding <=> %s::vector) AS cosine
                FROM {KG_SCHEMA}.commodity_facets cf
                WHERE cf.embedding IS NOT NULL
                  {_kg_use_scope_filter("cf", "commodity_facets", "retrieval")}
                ORDER BY cf.embedding <=> %s::vector
                LIMIT %s
            )
            SELECT commodity_code,
                   max(
                     cosine *
                     CASE authority_tier
                       WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0 WHEN 4 THEN 1.0
                       WHEN 5 THEN 0.85 WHEN 6 THEN 0.70 WHEN 7 THEN 0.70
                       ELSE 0.50
                     END
                   ) AS score,
                   min(authority_tier) AS best_tier,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM top_facts
            GROUP BY commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (literal, literal, fact_pool, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "facts_vec",
                "best_tier": r.get("best_tier"),
            }
            for r in cur.fetchall()
        ]


def _kg_vec_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    edge_pool = limit * 4
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH top_edges AS (
                SELECT e.id,
                       e.authority_tier,
                       1 - (e.embedding <=> %s::vector) AS cosine
                FROM {KG_SCHEMA}.kg_edges e
                WHERE e.embedding IS NOT NULL
                  {_kg_use_scope_filter("e", "kg_edges", "retrieval")}
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            )
            SELECT kec.commodity_code,
                   max(
                     te.cosine *
                     CASE te.authority_tier
                       WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0
                       ELSE 0.80
                     END
                   ) AS score,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = kec.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM top_edges te
            JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = te.id
            GROUP BY kec.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (literal, literal, edge_pool, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "kg_vec",
            }
            for r in cur.fetchall()
        ]


def _rrf_fuse(legs: list[tuple[str, list[dict[str, Any]], float]], limit: int, k: int = 60) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for _name, leg, cap in legs:
        for rank, row in enumerate(leg, start=1):
            code = row.get("commodity_code")
            if not code:
                continue
            if code not in fused:
                fused[code] = {
                    "commodity_code": code,
                    "description": row.get("description") or "",
                    "score": 0.0,
                    "sources": [],
                }
            fused[code]["score"] += cap * 1.0 / (rank + k)
            source = row.get("source")
            if source and source not in fused[code]["sources"]:
                fused[code]["sources"].append(source)
    return sorted(fused.values(), key=lambda x: -x["score"])[:limit]


def retrieve_for_config(query: str, cfg: dict[str, Any], api_key: str | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if cfg.get("triage"):
        raise RuntimeError("This experiment uses query rewrite / triage and is not runnable in the local trial form.")
    if not query.strip():
        return [], {}

    legs: list[tuple[str, list[dict[str, Any]], float]] = []
    if cfg.get("use_curated", False):
        leg = _curated_leg(query, limit)
        legs.append(("reference", leg, 1.0))

    fts_fn = _composite_fts_leg if cfg.get("use_composite") else _fts_leg
    leg = fts_fn(query, limit)
    legs.append(("fts_composite" if cfg.get("use_composite") else "fts", leg, 1.0))

    leg = _description_substring_leg(query, limit)
    legs.append(("substring", leg, 1.0))

    needs_embedding = bool(cfg.get("use_vector") or cfg.get("use_facts_vec") or cfg.get("use_kg_vec"))
    embedding = _embed_query(query, api_key) if needs_embedding else None

    if cfg.get("use_vector") and embedding is not None:
        vector_fn = _composite_vector_leg if cfg.get("use_composite") else _vector_leg
        leg = vector_fn(embedding, limit)
        legs.append(("vector_composite" if cfg.get("use_composite") else "vector", leg, 1.0))
    if cfg.get("use_facts"):
        leg = _facts_leg(query, limit)
        legs.append(("facts", leg, float(cfg.get("facts_cap", 0.5))))
    if cfg.get("use_kg_context"):
        leg = _kg_context_leg(query, limit)
        legs.append(("kg_context", leg, float(cfg.get("kg_cap", 0.5))))
    if cfg.get("use_facts_vec") and embedding is not None:
        leg = _facts_vec_leg(embedding, limit)
        legs.append(("facts_vec", leg, float(cfg.get("facts_vec_cap", 0.6))))
    if cfg.get("use_kg_vec") and embedding is not None:
        leg = _kg_vec_leg(embedding, limit)
        legs.append(("kg_vec", leg, float(cfg.get("kg_vec_cap", 0.6))))

    leg_counts = {name: len(rows) for name, rows, _cap in legs}
    fused = _rrf_fuse(legs, limit=limit, k=int(cfg.get("rrf_k", 60)))
    return fused, leg_counts


def run_trial(
    query: str,
    expected_code: str,
    api_key: str | None,
    run_label: str | None = None,
    retrieval_limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    query = query.strip()
    expected_flat = _flat_code(expected_code)
    if not query:
        raise ValueError("query is required")

    selected = select_experiment(run_label)

    limit = max(10, min(int(retrieval_limit or DEFAULT_LIMIT), DEFAULT_LIMIT))
    candidates, leg_counts = retrieve_for_config(query, selected["config"], api_key, limit)
    rank = None
    if expected_flat:
        for idx, row in enumerate(candidates, start=1):
            if _flat_code(row["commodity_code"]) == expected_flat:
                rank = idx
                break

    ranked_candidates = []
    for idx, row in enumerate(candidates, start=1):
        item = dict(row)
        item["rank"] = idx
        ranked_candidates.append(item)

    return {
        "query": query,
        "expected_code": expected_code,
        "expected_code_normalized": expected_flat,
        "evaluated": bool(expected_flat),
        "experiment": selected,
        "retrieval_limit": limit,
        "rank": rank,
        "hit_at_10": bool(rank and rank <= 10),
        "hit_at_100": bool(rank and rank <= 100),
        "hit_within_limit": rank is not None,
        "leg_counts": leg_counts,
        "lexical_specificity": query_lexical_specificity(query),
        "query_difficulty": query_difficulty_from_candidates(query, ranked_candidates, k=limit),
        "top_candidates": ranked_candidates[:DISPLAY_LIMIT],
        "candidates": ranked_candidates,
    }


def _matrix_cell_class(value: float | None) -> str:
    if value is None:
        return "empty"
    if value >= 90:
        return "great"
    if value >= 80:
        return "good"
    if value >= 65:
        return "mid"
    return "low"


def _matrix_format_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}%"


def _matrix_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<div class='empty'>No retrieval matrix rows available.</div>"
    body = []
    for row in rows:
        recall = row.get("headline_recall_pct")
        rank = row.get("rank") or "-"
        title = html.escape(str(row.get("title") or row.get("run_label") or ""))
        run_label = html.escape(str(row.get("run_label") or ""))
        desc = html.escape(str(row.get("description") or ""))
        cfg = row.get("config") or {}
        inputs = cfg.get("active_gold_count") or "-"
        fact_model = cfg.get("fact_author_model") or "-"
        fact_prompt = cfg.get("fact_prompt_version") or "-"
        baseline = " <span class='badge'>baseline</span>" if row.get("ott_baseline") else ""
        runnable = "yes" if row.get("runnable") else "no"
        body.append(
            "<tr>"
            f"<td class='rank'>#{rank}</td>"
            f"<td><div class='title'>{title}{baseline}</div><div class='run'>{run_label}</div><div class='desc'>{desc}</div></td>"
            f"<td class='num {_matrix_cell_class(recall)}'>{_matrix_format_pct(recall)}</td>"
            f"<td>{html.escape(str(inputs))}</td>"
            f"<td>{html.escape(str(fact_model))}</td>"
            f"<td>{html.escape(str(fact_prompt))}</td>"
            f"<td>{runnable}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Rank</th><th>Experiment</th><th>Recall@100</th><th>Inputs</th><th>Fact model</th><th>Fact prompt</th><th>Runnable</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def matrix_html() -> str:
    rows = sorted(_load_matrix_rows(), key=lambda r: (r.get("rank") is None, r.get("rank") or 999999, r.get("run_label") or ""))
    baseline_rows = [r for r in rows if r.get("ott_baseline")]
    baseline_html = ""
    if baseline_rows:
        baseline_html = "<h2>Baseline Comparators</h2>" + _matrix_table(baseline_rows)
    return f"""
<!doctype html>
<html><head><meta charset='utf-8'><title>Retrieval Experiment Matrix</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; background:#070b13; color:#e5e7eb; font-family:Inter,system-ui,sans-serif; padding:24px; }}
  h1 {{ margin:0 0 8px; font-size:24px; }} h2 {{ margin:26px 0 8px; font-size:16px; color:#cbd5e1; }}
  .sub {{ max-width:1100px; color:#94a3b8; line-height:1.6; }} code,.run {{ color:#93c5fd; }}
  table {{ width:100%; border-collapse:collapse; margin-top:14px; font-size:13px; }}
  th,td {{ border:1px solid #263243; padding:10px; text-align:left; vertical-align:top; }}
  th {{ background:#111827; color:#bfdbfe; position:sticky; top:0; z-index:1; }}
  tr:nth-child(even) td {{ background:#0b1220; }} .rank {{ color:#cbd5e1; font-weight:800; white-space:nowrap; }}
  .title {{ font-weight:800; color:#f8fafc; }} .run {{ margin-top:3px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; }}
  .desc {{ margin-top:6px; color:#94a3b8; line-height:1.45; max-width:850px; }} .num {{ font-weight:900; font-variant-numeric:tabular-nums; }}
  .great {{ background:#064e3b !important; color:#d1fae5; }} .good {{ background:#14532d !important; color:#dcfce7; }}
  .mid {{ background:#78350f !important; color:#fef3c7; }} .low {{ background:#7f1d1d !important; color:#fee2e2; }}
  .empty {{ border:1px solid #263243; background:#0b1220; padding:18px; margin-top:18px; }} .badge {{ color:#cbd5e1; border:1px solid #475569; padding:2px 6px; border-radius:4px; font-size:11px; margin-left:6px; }}
</style></head><body>
  <h1>Retrieval Experiment Matrix</h1>
  <div class='sub'>Rows are loaded from <code>{html.escape(str(MATRIX_CSV))}</code>. The headline metric is code-macro recall@100. The app workbench uses the same experiment catalog for selected-row metadata and runnable trials.</div>
  {baseline_html}
  <h2>All Ranked Rows</h2>
  {_matrix_table(rows)}
</body></html>
"""


def matrix_csv() -> str:
    return MATRIX_CSV.read_text()

