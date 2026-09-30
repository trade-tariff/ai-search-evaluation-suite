"""Local/manual convenience entrypoint — the only place in this app that
calls TradeTariffBackendClient.create_run (with a freshly generated Idempotency-Key).
The ingress-triggered flow (main.py's POST /api/evaluation/runs/{run_id}/start)
never creates a run; it only executes one Rails already created.

Usage: python -m classification_core.trade_tariff_backend.cli "my-experiment-name" --gold-query-set 5

The gold query set must already exist and be ready in trade-tariff-backend (create one in
the Admin app or with `rake tariff:evaluation:generate_gold_queries`). Every run scores
the set its experiment points at.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from .client import TradeTariffBackendClient
from .execute_run import execute_run


async def create_and_run(experiment_name: str, run_time_overrides: dict, gold_query_set_id: int) -> dict:
    client = TradeTariffBackendClient()
    try:
        experiment = await client.create_experiment(experiment_name, gold_query_set_id)
        run = await client.create_run(
            experiment_id=experiment["id"], run_time_overrides=run_time_overrides,
            idempotency_key=str(uuid.uuid4()),
        )
        return await execute_run(run["id"], client)
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_name")
    parser.add_argument(
        "--gold-query-set", type=int, required=True, dest="gold_query_set_id",
        help="id of the gold query set to score the run against",
    )
    args = parser.parse_args()

    summary = asyncio.run(create_and_run(args.experiment_name, run_time_overrides={}, gold_query_set_id=args.gold_query_set_id))
    print(summary)


if __name__ == "__main__":
    main()
