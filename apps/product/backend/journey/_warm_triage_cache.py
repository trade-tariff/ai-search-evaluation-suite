"""One-shot: pre-warm kg.triage_cache for every DISTINCT eval_gold query.

Run: .venv/bin/python -u -m journey._warm_triage_cache

Calls the cache-aware triage.expand_query concurrently (asyncio, semaphore=8,
per-request timeout 40s). Each call computes-and-stores on a cold key, or returns
the cached rewrite on a warm key, so re-runs are cheap/idempotent. Temporary
helper - safe to delete after the cache is populated.
"""
from __future__ import annotations

import asyncio
import time

import psycopg
from psycopg.rows import dict_row

from . import run_eval  # triggers load_dotenv -> OPENAI_API_KEY
from . import triage

CONCURRENCY = 8
PER_REQUEST_TIMEOUT = 40.0


def _distinct_queries() -> list[str]:
    conn = psycopg.connect(run_eval.DSN, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT query FROM kg.eval_gold "
                "WHERE query IS NOT NULL AND btrim(query) <> '' ORDER BY query"
            )
            return [r["query"] for r in cur.fetchall()]
    finally:
        conn.close()


async def main() -> None:
    queries = _distinct_queries()
    total = len(queries)
    print(f"[warm] distinct queries to warm: {total}", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    errors = 0
    lock = asyncio.Lock()
    started = time.time()

    async def worker(q: str) -> None:
        nonlocal done, errors
        async with sem:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(triage.expand_query, q),
                    timeout=PER_REQUEST_TIMEOUT,
                )
            except Exception as exc:  # timeout or any failure - log, keep going
                errors += 1
                print(f"[warm] error on {q[:60]!r}: {exc!r}", flush=True)
            finally:
                async with lock:
                    done += 1
                    if done % 100 == 0 or done == total:
                        rate = done / max(time.time() - started, 1e-9)
                        print(
                            f"[warm] {done}/{total}  errors={errors}  "
                            f"({rate:.1f}/s, {time.time() - started:.0f}s elapsed)",
                            flush=True,
                        )

    await asyncio.gather(*(worker(q) for q in queries))

    elapsed = time.time() - started
    conn = psycopg.connect(run_eval.DSN, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM kg.triage_cache")
            cached = cur.fetchone()["n"]
    finally:
        conn.close()
    print(
        f"[warm] DONE in {elapsed:.0f}s  processed={done} errors={errors}  "
        f"kg.triage_cache rows={cached}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
