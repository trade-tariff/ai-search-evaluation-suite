#!/usr/bin/env python3
"""Hydrate the local OpenSearch tariff commodity index from Postgres.

This script is intended to run inside the classification-evals Compose network:

    docker compose exec classification-evals \
      python scripts/hydrate_opensearch_index.py --recreate

It indexes only commodity search text from the local VM Postgres database. It
does not read local developer-machine files and does not require AWS-managed
OpenSearch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import asyncpg
import httpx


DEFAULT_DSN = "postgresql://postgres:postgres@tariff-db:5432/tariff_db"
DEFAULT_URL = "http://opensearch:9200"
DEFAULT_INDEX = "tariff_commodities"


MAPPING: dict[str, Any] = {
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "analysis": {
            "analyzer": {
                "tariff_english": {
                    "type": "standard",
                    "stopwords": "_english_",
                }
            }
        },
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "goods_nomenclature_sid": {"type": "integer"},
            "commodity_code": {
                "type": "keyword",
                "fields": {"text": {"type": "text", "analyzer": "tariff_english"}},
            },
            "self_text": {"type": "text", "analyzer": "tariff_english"},
            "search_text": {"type": "text", "analyzer": "tariff_english"},
            "generation_type": {"type": "keyword"},
            "validity_start_date": {"type": "date"},
            "validity_end_date": {"type": "date"},
        },
    },
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


async def wait_for_opensearch(url: str, timeout_seconds: int) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with httpx.AsyncClient(timeout=5) as http:
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                res = await http.get(f"{url}/_cluster/health")
                if res.is_success:
                    return
                last_error = f"HTTP {res.status_code}: {res.text[:120]}"
            except Exception as exc:  # pragma: no cover - operational path
                last_error = str(exc)
            await asyncio.sleep(2)
    raise RuntimeError(f"OpenSearch not ready at {url}: {last_error}")


async def ensure_index(url: str, index: str, recreate: bool) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        exists = (await http.head(f"{url}/{index}")).status_code == 200
        if exists and recreate:
            res = await http.delete(f"{url}/{index}")
            res.raise_for_status()
            exists = False
        if not exists:
            res = await http.put(f"{url}/{index}", json=MAPPING)
            res.raise_for_status()


async def fetch_batch(
    conn: asyncpg.Connection,
    last_sid: int,
    batch_size: int,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT
            st.goods_nomenclature_sid,
            st.goods_nomenclature_item_id,
            st.self_text,
            coalesce(st.search_text, st.self_text) AS search_text,
            st.generation_type,
            gn.validity_start_date::date AS validity_start_date,
            gn.validity_end_date::date AS validity_end_date
        FROM uk.goods_nomenclature_self_texts st
        JOIN uk.goods_nomenclatures gn
          ON gn.goods_nomenclature_sid = st.goods_nomenclature_sid
        WHERE st.goods_nomenclature_sid > $1
          AND st.self_text IS NOT NULL
          AND length(trim(st.self_text)) > 0
          AND coalesce(st.expired, false) = false
          AND coalesce(st.stale, false) = false
          AND gn.validity_start_date::date <= current_date
          AND (
            gn.validity_end_date IS NULL
            OR gn.validity_end_date::date >= current_date
          )
        ORDER BY st.goods_nomenclature_sid
        LIMIT $2
        """,
        last_sid,
        batch_size,
    )


def make_bulk_body(index: str, rows: list[asyncpg.Record]) -> str:
    lines: list[str] = []
    for row in rows:
        sid = int(row["goods_nomenclature_sid"])
        doc = {
            "goods_nomenclature_sid": sid,
            "commodity_code": str(row["goods_nomenclature_item_id"]),
            "self_text": str(row["self_text"]),
            "search_text": str(row["search_text"]),
            "generation_type": str(row["generation_type"]),
            "validity_start_date": row["validity_start_date"].isoformat(),
            "validity_end_date": row["validity_end_date"].isoformat()
            if row["validity_end_date"]
            else None,
        }
        lines.append(json.dumps({"index": {"_index": index, "_id": str(sid)}}))
        lines.append(json.dumps(doc, ensure_ascii=False))
    return "\n".join(lines) + "\n"


async def bulk_index(url: str, index: str, rows: list[asyncpg.Record]) -> None:
    body = make_bulk_body(index, rows)
    async with httpx.AsyncClient(timeout=60) as http:
        res = await http.post(
            f"{url}/_bulk",
            content=body.encode("utf-8"),
            headers={"content-type": "application/x-ndjson"},
        )
        res.raise_for_status()
        payload = res.json()
        if payload.get("errors"):
            first = next((item for item in payload.get("items", []) if item.get("index", {}).get("error")), None)
            raise RuntimeError(f"bulk index error: {json.dumps(first)[:1000]}")


async def refresh_and_count(url: str, index: str) -> int:
    async with httpx.AsyncClient(timeout=30) as http:
        refresh = await http.post(f"{url}/{index}/_refresh")
        refresh.raise_for_status()
        count = await http.get(f"{url}/{index}/_count")
        count.raise_for_status()
        return int(count.json().get("count", 0))


async def run(args: argparse.Namespace) -> int:
    url = args.opensearch_url.rstrip("/")
    await wait_for_opensearch(url, args.wait_seconds)
    await ensure_index(url, args.index, args.recreate)

    conn = await asyncpg.connect(args.dsn)
    total = 0
    last_sid = 0
    try:
        while True:
            rows = await fetch_batch(conn, last_sid, args.batch_size)
            if not rows:
                break
            await bulk_index(url, args.index, rows)
            total += len(rows)
            last_sid = int(rows[-1]["goods_nomenclature_sid"])
            print(f"indexed={total} last_sid={last_sid}", flush=True)
    finally:
        await conn.close()

    count = await refresh_and_count(url, args.index)
    print(json.dumps({"index": args.index, "indexed_this_run": total, "count": count}))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=_env("TARIFF_DB_DSN", DEFAULT_DSN))
    parser.add_argument("--opensearch-url", default=_env("OPENSEARCH_URL", DEFAULT_URL))
    parser.add_argument("--index", default=_env("OPENSEARCH_INDEX", DEFAULT_INDEX))
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--recreate", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
