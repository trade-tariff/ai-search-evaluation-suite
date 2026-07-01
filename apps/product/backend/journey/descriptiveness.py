"""Measure query DESCRIPTIVENESS and test whether it (not expert-vs-novice)
drives recall@100.

Hypothesis (from the matrix): recall tracks how completely a query describes the
product, regardless of whether the phrasing is 'expert' or 'novice'. Terse expert
jargon ("mechanical seal") retrieves like a vague novice query ("metal machine
part"); a descriptive novice query beats a terse expert one.

For each kg.eval_gold query, compute:
  - n_words : token count (free verbosity proxy)
  - llm_spec: gpt-5-mini rating 0-10 of how completely the query pins down a
    single classifiable product FOR CUSTOMS, judged on the query ALONE (no gold
    code shown -> no leakage). 10 = a specialist could narrow to one commodity;
    0 = far too vague.

Persists kg.query_descriptiveness(gold_id, source_id, persona, n_words, llm_spec).

Then correlates per-query descriptiveness against whether the gold was retrieved
@100 (from kg.eval_run_results for a chosen multi-persona run), and prints
per-persona means + a point-biserial correlation. If descriptiveness correlates
with recall more tightly than the persona ordering does, the hypothesis holds.

Env: DESC_MODEL(gpt-5-mini) DESC_EFFORT(minimal) DESC_CONCURRENCY(8)
     DESC_RUN_LABEL(v2_plus_desc_vec)  - which run's @100 to correlate against
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

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
from openai import AsyncOpenAI

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = os.environ.get("DESC_MODEL", "gpt-5-mini")
EFFORT = os.environ.get("DESC_EFFORT", "minimal")
CONCURRENCY = int(os.environ.get("DESC_CONCURRENCY", "8"))
RUN_LABEL = os.environ.get("DESC_RUN_LABEL", "v2_plus_desc_vec")
PERSONA_ORDER = ["naive_vague", "naive_branded", "naive_specific",
                 "emu_generic", "emu_ordinary", "emu_specific", "original"]

_SYSTEM = """You rate how completely a product description would let a UK customs
classification specialist narrow it down to ONE commodity code - judging the text
ALONE, not whether it is correct. Consider whether it names material, function,
form, processing, and intended use. Score 0-10:
  10 = fully specified, a specialist could pin one commodity code
   5 = partial - names the kind of thing but missing discriminators
   0 = far too vague to classify ("an item", "metal thing")
Reply JSON only: {"spec": <integer 0-10>}"""


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kg.query_descriptiveness (
                gold_id   bigint PRIMARY KEY,
                source_id text,
                persona   text,
                n_words   int,
                llm_spec  numeric(4,1)
            )
            """
        )
    conn.commit()


async def score(client, sem, query: str) -> float | None:
    async with sem:
        for attempt in range(3):
            try:
                extra = {"reasoning_effort": EFFORT} if MODEL.startswith("gpt-5") else {}
                resp = await client.chat.completions.create(
                    model=MODEL, response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": _SYSTEM},
                              {"role": "user", "content": query}], **extra)
                v = json.loads(resp.choices[0].message.content or "{}").get("spec")
                return max(0.0, min(10.0, float(v))) if v is not None else None
            except Exception as exc:
                if attempt == 2:
                    print(f"  [score failed: {exc!r}]", flush=True)
                    return None
                await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr); sys.exit(1)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    _ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_id, persona, query FROM kg.eval_gold ORDER BY id")
        gold = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT gold_id FROM kg.query_descriptiveness")
        done = {r["gold_id"] for r in cur.fetchall()}
    todo = [g for g in gold if g["id"] not in done]
    print(f"{len(gold)} gold queries; scoring {len(todo)} (rest cached)")

    if todo:
        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2, timeout=45.0)
        sem = asyncio.Semaphore(CONCURRENCY)
        scores = await asyncio.gather(*[score(client, sem, g["query"]) for g in todo])
        with conn.cursor() as cur:
            for g, s in zip(todo, scores):
                cur.execute(
                    """INSERT INTO kg.query_descriptiveness (gold_id, source_id, persona, n_words, llm_spec)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (gold_id) DO UPDATE
                       SET n_words=EXCLUDED.n_words, llm_spec=EXCLUDED.llm_spec""",
                    (g["id"], g["source_id"], g["persona"], len(g["query"].split()), s),
                )
        conn.commit()
        print("  scored + persisted")

    # ---- per-persona means ----
    with conn.cursor() as cur:
        cur.execute(
            """SELECT persona, count(*) n, round(avg(n_words),1) words, round(avg(llm_spec),2) spec
               FROM kg.query_descriptiveness GROUP BY persona""")
        per = {r["persona"]: r for r in cur.fetchall()}
    print("\nPer-persona descriptiveness:")
    print(f"  {'persona':<16}{'avg_words':>10}{'avg_spec':>10}")
    for p in PERSONA_ORDER:
        if p in per:
            print(f"  {p:<16}{float(per[p]['words']):>10.1f}{float(per[p]['spec']):>10.2f}")

    # ---- correlation with recall@100 for the chosen run ----
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.persona, d.n_words, d.llm_spec,
                      (rr.rank_of_expected IS NOT NULL AND rr.rank_of_expected <= 100)::int AS hit100
               FROM kg.query_descriptiveness d
               JOIN kg.eval_gold g ON g.id = d.gold_id
               JOIN kg.eval_run_results rr ON rr.gold_id = d.gold_id
               JOIN kg.eval_runs er ON er.id = rr.run_id
               WHERE er.run_label = %s
                 AND er.id = (SELECT max(id) FROM kg.eval_runs WHERE run_label=%s)""",
            (RUN_LABEL, RUN_LABEL))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if rows:
        import statistics as st
        specs = [float(r["llm_spec"]) for r in rows if r["llm_spec"] is not None]
        hits = [r["hit100"] for r in rows if r["llm_spec"] is not None]
        hs = [s for s, h in zip(specs, hits) if h]
        ms = [s for s, h in zip(specs, hits) if not h]
        # point-biserial correlation between llm_spec and hit@100
        n = len(specs)
        if n > 2 and st.pstdev(specs) > 0:
            mean_all = sum(specs) / n
            p1 = sum(hits) / n
            r_pb = ((sum(hs)/len(hs) if hs else 0) - (sum(ms)/len(ms) if ms else 0)) / st.pstdev(specs) * (p1*(1-p1))**0.5
        else:
            r_pb = float("nan")
        print(f"\nCorrelation of descriptiveness with recall@100 (run '{RUN_LABEL}', n={n}):")
        print(f"  mean llm_spec when gold IS @100 : {sum(hs)/len(hs):.2f}" if hs else "  (no hits)")
        print(f"  mean llm_spec when gold MISSED  : {sum(ms)/len(ms):.2f}" if ms else "  (no misses)")
        print(f"  point-biserial r (spec vs hit@100): {r_pb:.3f}")
    else:
        print(f"\n(no eval_run_results for run_label '{RUN_LABEL}' to correlate against yet)")


if __name__ == "__main__":
    asyncio.run(main())
