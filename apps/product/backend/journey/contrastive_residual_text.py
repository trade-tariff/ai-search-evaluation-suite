"""Contrastive residual composite text for UK 'Other'/N.E.S. leaf codes (codex idea).

PROBLEM: residual leaf codes (description ILIKE 'other%' or "not elsewhere
specified"/N.E.S.) carry almost no self-text - the word "Other" is meaningless out
of context - so the vector leg can't match them. They are ~73-95% of our recall
misses. The AI-166 composite (kg.composite_search_text) enriches with
colloquial/synonyms/brands but is still mostly PARENT text, so two sibling residuals
under different parents look nearly identical in embedding space.

IDEA: build a CONTRASTIVE text per residual leaf that pins it to its exact place in
the tree by spelling out (a) the full breadcrumb, (b) the positive parent scope,
(c) what its NON-residual siblings are - as explicit exclusions ("not hinges, not
castors, not automatic door closers"), and (d) generic residual labels. The sibling
exclusions are the new signal: they push the embedding AWAY from the named siblings
and toward the "everything else under this parent" region a vague query lands in.

This module:
  1. builds + embeds that text into a SCRATCH table kg.contrastive_residual_text
     (NEVER production kg.composite_search_text), one row per residual leaf;
  2. runs an OFFLINE vector-leg recall@100 on the naive_vague residual gold subset,
     comparing the current composite embedding vs this contrastive embedding,
     apples-to-apples (same query embeddings, same candidate pool - only the
     residual rows' vectors differ).

Embed pattern mirrors journey/seed_composite.py (text-embedding-3-small, 1536-dim,
batched). Recall/residual conventions mirror journey/measure_residual_backfill.py.

Run:
  python -m journey.contrastive_residual_text --build   # populate + embed scratch table
  python -m journey.contrastive_residual_text --eval    # offline recall@100 delta
  python -m journey.contrastive_residual_text --example 7326909290   # print one text

Env: OPENAI_API_KEY (required for --build/--eval), TARIFF_DB_DSN,
     CONTRA_BATCH(256), CONTRA_FORCE(0), EVAL_PERSONA(naive_vague), EVAL_K(100).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

for _p in [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
]:
    if _p is not None and _p.exists():
        load_dotenv(_p)
        break

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = "text-embedding-3-small"
BATCH = int(os.environ.get("CONTRA_BATCH", "256"))
FORCE = os.environ.get("CONTRA_FORCE", "0") == "1"
SCRATCH = "kg.contrastive_residual_text"

RESIDUAL_LABELS = "other, not elsewhere specified, miscellaneous, catch-all, none of the above"


# --------------------------------------------------------------------------- #
# residual / normalisation helpers (mirror measure_residual_backfill.py)
# --------------------------------------------------------------------------- #
def norm(c: str) -> str:
    d = "".join(ch for ch in (c or "") if ch.isdigit())
    return d.ljust(10, "0")[:10] if d else (c or "")


def is_residual(descr: str) -> bool:
    d = (descr or "").strip().lower()
    return d.startswith("other") or "not elsewhere specified" in d or "n.e.s" in d


import re

_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    """Trim a description down to a contrast term: strip HTML (descriptions carry
    <br> etc.), collapse whitespace, drop trailing punctuation. Keeps it short so a
    sibling list stays embeddable."""
    s = _TAG.sub(" ", s or "")
    return " ".join(s.split()).strip(" :;,.-").strip()


# --------------------------------------------------------------------------- #
# data load
# --------------------------------------------------------------------------- #
def _load_corpus(conn) -> dict:
    """One pass over the declarable corpus.

    Returns code -> {descr, path(tuple of ancestor sids), pprefix(path[:-1]), residual}
    plus a sid -> description map (for breadcrumb) and a chapter-sid -> section-title
    map (for the section leg of the breadcrumb).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (g.goods_nomenclature_item_id)
                   g.goods_nomenclature_item_id AS code,
                   g.goods_nomenclature_sid     AS sid,
                   g.path,
                   d.description                AS descr
            FROM uk.goods_nomenclatures g
            LEFT JOIN uk.goods_nomenclature_descriptions d
              ON d.goods_nomenclature_sid = g.goods_nomenclature_sid AND d.language_id = 'EN'
            WHERE g.validity_end_date IS NULL AND g.producline_suffix = '80'
            ORDER BY g.goods_nomenclature_item_id, d.oid DESC NULLS LAST
        """)
        rows = cur.fetchall()

    # sid -> description (resolve breadcrumb ancestors, which may be suffix 10/20/.. too)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (g.goods_nomenclature_sid)
                   g.goods_nomenclature_sid AS sid, d.description AS descr
            FROM uk.goods_nomenclatures g
            JOIN uk.goods_nomenclature_descriptions d
              ON d.goods_nomenclature_sid = g.goods_nomenclature_sid AND d.language_id = 'EN'
            WHERE g.validity_end_date IS NULL
            ORDER BY g.goods_nomenclature_sid, d.oid DESC NULLS LAST
        """)
        sid_descr = {r["sid"]: _clean(r["descr"]) for r in cur.fetchall()}

    # chapter sid -> section title
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cs.goods_nomenclature_sid AS sid, s.title
            FROM uk.chapters_sections cs JOIN uk.sections s ON s.id = cs.section_id
        """)
        section_by_chapter_sid = {r["sid"]: _clean(r["title"]) for r in cur.fetchall()}

    meta = {}
    for r in rows:
        code = norm(r["code"])
        path = tuple(r["path"] or [])
        meta[code] = {
            "sid": r["sid"],
            "descr": _clean(r["descr"]),
            "path": path,
            "parent_sid": path[-1] if path else None,  # immediate parent; peers share it
            "residual": is_residual(r["descr"]),
        }
    return meta, sid_descr, section_by_chapter_sid


SIB_MAXLEN = 90   # cap a sibling term so one verbose CN description can't dominate
SIB_MAXN = 25     # cap how many siblings we list


def _siblings(code: str, meta: dict, drop: set) -> tuple[list[str], list[str]]:
    """True taxonomic peers: suffix-80 leaves sharing the SAME immediate parent
    (last path sid). NOT path[:-1] - that over-collapses and pulls in the whole
    chapter, injecting false contrast (e.g. 'not fishing rods' under a festive code).

    Returns (non_residual_descrs, residual_descrs), de-duplicated, excluding self,
    the leaf's own description, and any text already in the breadcrumb (`drop`).
    Each term is length-capped; non-residual ones become exclusions.
    """
    me = meta[code]
    psid = me["parent_sid"]
    if psid is None:
        return [], []
    seen_nr, seen_r = {}, {}
    self_descr = me["descr"].lower()
    for oc, m in meta.items():
        if oc == code or m["parent_sid"] != psid:
            continue
        d = m["descr"]
        dl = d.lower()
        if not dl or dl == self_descr or dl in drop:
            continue
        if len(d) > SIB_MAXLEN:
            d = d[:SIB_MAXLEN].rsplit(" ", 1)[0] + "..."
        bucket = seen_r if m["residual"] else seen_nr
        bucket.setdefault(dl, d)
    return list(seen_nr.values())[:SIB_MAXN], list(seen_r.values())[:SIB_MAXN]


def build_contrastive_text(code: str, meta: dict, sid_descr: dict,
                           section_by_chapter_sid: dict) -> str:
    """Compose the contrastive text for one residual leaf.

    Layout (each line is a labelled section so the embedding picks up the structure):
        Section: <section title>
        Category: <breadcrumb joined by ' > '>
        This is the residual ("other") code within: <immediate parent scope>
        It covers goods of that group that are not separately listed, specifically
          NOT: <sibling 1>; NOT <sibling 2>; ...        <- the contrastive signal
        Distinct from other residual groupings: <residual sibling descrs>
        Also known as: other, not elsewhere specified, miscellaneous, catch-all, ...
    Falls back gracefully when path / siblings are missing (chapter-level residuals).
    """
    m = meta[code]
    path = m["path"]
    # breadcrumb: ancestor descriptions in path order, dropping pure-"Other" links
    # (they add no scope) and collapsing consecutive duplicates (suffix 10/20 logical
    # nodes often repeat the parent label).
    crumbs: list[str] = []
    for s in path:
        d = sid_descr.get(s)
        if not d or is_residual(d):
            continue
        if crumbs and crumbs[-1].lower() == d.lower():
            continue
        crumbs.append(d)
    # parent scope = nearest meaningful (non-"Other") named ancestor
    parent_scope = crumbs[-1] if crumbs else ""
    section_title = section_by_chapter_sid.get(path[0]) if path else None

    drop = {c.lower() for c in crumbs}  # don't exclude text already in the breadcrumb
    nonresid_sibs, resid_sibs = _siblings(code, meta, drop)

    lines: list[str] = []
    if section_title:
        lines.append(f"Section: {section_title}")
    if crumbs:
        lines.append("Category: " + " > ".join(crumbs))
    if parent_scope:
        lines.append(f'This is the residual ("other") code within: {parent_scope}.')
    else:
        lines.append('This is a residual ("other") code.')

    if nonresid_sibs:
        # the contrastive signal: name the peers this code is NOT, so the embedding
        # is pushed away from them toward "everything else under this parent".
        lines.append("It covers goods of that group that are not separately listed elsewhere - "
                     + "specifically NOT: " + "; ".join(nonresid_sibs) + ".")
    else:
        lines.append("It covers goods of that group that are not separately listed elsewhere.")

    if resid_sibs:
        lines.append("Distinct from other residual groupings such as: "
                     + ", ".join(resid_sibs) + ".")

    lines.append("Also known as: " + RESIDUAL_LABELS + ".")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# build + embed scratch table
# --------------------------------------------------------------------------- #
def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCRATCH} (
                goods_nomenclature_item_id text PRIMARY KEY,
                contrastive_text text NOT NULL,
                contrastive_embedding vector(1536),
                stale boolean NOT NULL DEFAULT true
            )""")
    conn.commit()


def populate_text(conn, meta, sid_descr, section_by_chapter_sid) -> int:
    """Build contrastive_text for every residual leaf (idempotent upsert)."""
    residuals = [c for c, m in meta.items() if m["residual"]]
    n = 0
    with conn.cursor() as cur:
        for code in residuals:
            txt = build_contrastive_text(code, meta, sid_descr, section_by_chapter_sid)
            cur.execute(f"""
                INSERT INTO {SCRATCH} (goods_nomenclature_item_id, contrastive_text, stale)
                VALUES (%s,%s,true)
                ON CONFLICT (goods_nomenclature_item_id) DO UPDATE
                  SET contrastive_text = EXCLUDED.contrastive_text,
                      stale = ({SCRATCH}.contrastive_text IS DISTINCT FROM EXCLUDED.contrastive_text
                               OR {SCRATCH}.contrastive_embedding IS NULL)
            """, (code, txt))
            n += 1
    conn.commit()
    return n


def embed_stale(conn) -> int:
    """Embed stale rows with text-embedding-3-small, batched (seed_composite pattern)."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60.0, max_retries=4)
    with conn.cursor() as cur:
        cur.execute(f"SELECT goods_nomenclature_item_id code, contrastive_text FROM {SCRATCH} "
                    + ("" if FORCE else "WHERE stale OR contrastive_embedding IS NULL"))
        todo = cur.fetchall()
    print(f"  embedding {len(todo)} contrastive texts...", flush=True)
    done, t0 = 0, time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        resp = client.embeddings.create(model=MODEL, input=[c["contrastive_text"][:8000] for c in chunk])
        with conn.cursor() as cur:
            for c, e in zip(chunk, resp.data):
                vec = "[" + ",".join(f"{x:.6f}" for x in e.embedding) + "]"
                cur.execute(f"UPDATE {SCRATCH} SET contrastive_embedding=%s::vector, stale=false "
                            "WHERE goods_nomenclature_item_id=%s", (vec, c["code"]))
        conn.commit()
        done += len(chunk)
        if (i // BATCH) % 10 == 0:
            print(f"    {done}/{len(todo)} ({time.time()-t0:.0f}s)", flush=True)
    return done


def cmd_build():
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr); sys.exit(1)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    ensure_schema(conn)
    meta, sid_descr, section_by_chapter_sid = _load_corpus(conn)
    n = populate_text(conn, meta, sid_descr, section_by_chapter_sid)
    print(f"contrastive_text populated for {n} residual leaf codes")
    e = embed_stale(conn)
    print(f"embedded {e} contrastive texts -> {SCRATCH}")
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) n, count(contrastive_embedding) emb FROM {SCRATCH}")
        r = cur.fetchone(); print(f"total rows {r['n']}, embedded {r['emb']}")
    conn.close()


# --------------------------------------------------------------------------- #
# offline eval: vector-leg recall@K on the residual gold subset
# --------------------------------------------------------------------------- #
def _embed_query(client, text: str) -> str:
    r = client.embeddings.create(model=MODEL, input=text)
    e = r.data[0].embedding
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


def _vector_topk_composite(cur, qvec: str, k: int) -> list[str]:
    """Current production vector leg: cosine over kg.composite_search_text."""
    cur.execute("""
        SELECT goods_nomenclature_item_id code
        FROM kg.composite_search_text
        WHERE composite_embedding IS NOT NULL
        ORDER BY composite_embedding <=> %s::vector
        LIMIT %s
    """, (qvec, k))
    return [norm(r["code"]) for r in cur.fetchall()]


def _vector_topk_contrastive(cur, qvec: str, k: int) -> list[str]:
    """Same candidate pool as composite, but residual rows use the contrastive
    embedding instead of the composite one (non-residual rows unchanged).

    UNION over (a) contrastive rows and (b) composite rows for codes NOT in the
    contrastive (scratch) table, then top-K by cosine. This isolates the effect
    of swapping ONLY the residual vectors - the rest of the corpus is identical.
    """
    cur.execute("""
        WITH scored AS (
            SELECT goods_nomenclature_item_id code,
                   1 - (contrastive_embedding <=> %s::vector) AS sim
            FROM kg.contrastive_residual_text
            WHERE contrastive_embedding IS NOT NULL
          UNION ALL
            SELECT c.goods_nomenclature_item_id code,
                   1 - (c.composite_embedding <=> %s::vector) AS sim
            FROM kg.composite_search_text c
            WHERE c.composite_embedding IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM kg.contrastive_residual_text t
                              WHERE t.goods_nomenclature_item_id = c.goods_nomenclature_item_id)
        )
        SELECT code FROM scored ORDER BY sim DESC LIMIT %s
    """, (qvec, qvec, k))
    return [norm(r["code"]) for r in cur.fetchall()]


def cmd_eval():
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr); sys.exit(1)
    persona = os.environ.get("EVAL_PERSONA", "naive_vague")
    K = int(os.environ.get("EVAL_K", "100"))
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60.0, max_retries=4)

    conn = psycopg.connect(DSN, row_factory=dict_row)
    meta, _sd, _sc = _load_corpus(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT query, expected_code
            FROM kg.eval_gold WHERE persona = %s
        """, (persona,))
        gold = cur.fetchall()

    # residual subset: gold whose expected_code is a residual leaf we can retrieve
    subset = []
    for g in gold:
        code = norm(g["expected_code"])
        m = meta.get(code)
        if m and m["residual"]:
            subset.append((g["query"], code))
    if not subset:
        print(f"no residual gold for persona {persona}"); return

    print(f"persona={persona}  residual gold queries={len(subset)}  K={K}")
    print(f"comparing current composite vs contrastive (scratch {SCRATCH})\n", flush=True)

    comp_hits = contra_hits = 0
    comp_ranks, contra_ranks = [], []
    flipped_in, flipped_out = [], []
    t0 = time.time()
    with conn.cursor() as cur:
        for i, (query, gold_code) in enumerate(subset):
            qvec = _embed_query(client, query)
            comp = _vector_topk_composite(cur, qvec, K)
            contra = _vector_topk_contrastive(cur, qvec, K)
            in_comp = gold_code in comp
            in_contra = gold_code in contra
            comp_hits += in_comp
            contra_hits += in_contra
            comp_ranks.append(comp.index(gold_code) + 1 if in_comp else None)
            contra_ranks.append(contra.index(gold_code) + 1 if in_contra else None)
            if in_contra and not in_comp:
                flipped_in.append((gold_code, query))
            if in_comp and not in_contra:
                flipped_out.append((gold_code, query))
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(subset)} ({time.time()-t0:.0f}s)", flush=True)
    conn.close()

    n = len(subset)
    def rr(ranks):
        return sum((1.0 / r) for r in ranks if r) / n
    print(f"\n=== vector-leg recall@{K} on RESIDUAL gold subset (n={n}) ===")
    print(f"current composite:   {comp_hits}/{n} = {comp_hits/n:.3f}   MRR={rr(comp_ranks):.3f}")
    print(f"contrastive:         {contra_hits}/{n} = {contra_hits/n:.3f}   MRR={rr(contra_ranks):.3f}")
    delta = (contra_hits - comp_hits) / n * 100
    print(f"delta recall@{K}:     {delta:+.1f}pp   ({contra_hits-comp_hits:+d} queries)")
    print(f"rescued (in contrastive, missed by composite): {len(flipped_in)}")
    print(f"regressed (in composite, missed by contrastive): {len(flipped_out)}")
    if flipped_in:
        print("  rescued examples: " + ", ".join(c for c, _ in flipped_in[:10]))
    if flipped_out:
        print("  regressed examples: " + ", ".join(c for c, _ in flipped_out[:10]))


def cmd_example(code: str):
    conn = psycopg.connect(DSN, row_factory=dict_row)
    meta, sid_descr, section_by_chapter_sid = _load_corpus(conn)
    code = norm(code)
    if code not in meta:
        print(f"{code} not a current declarable leaf"); return
    if not meta[code]["residual"]:
        print(f"warning: {code} ('{meta[code]['descr']}') is not residual")
    print(f"--- contrastive text for {code} ({meta[code]['descr']}) ---\n")
    print(build_contrastive_text(code, meta, sid_descr, section_by_chapter_sid))
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="populate + embed scratch table")
    ap.add_argument("--eval", action="store_true", help="offline vector recall@K delta")
    ap.add_argument("--example", metavar="CODE", help="print contrastive text for one code")
    a = ap.parse_args()
    if a.example:
        cmd_example(a.example)
    elif a.build:
        cmd_build()
    elif a.eval:
        cmd_eval()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
