"""Local/manual convenience entrypoint — the only place in this app that
calls TradeTariffBackendClient.create_run (with a freshly generated Idempotency-Key).

Creating the run makes trade-tariff-backend start it in this app (the same ingress
endpoint the admin screens use), so this CLI does not execute the run itself. It follows
the run's status in trade-tariff-backend until it finishes. Running it here as well would
make duplicate provider calls and overwrite the same results.

This app must be running and reachable from trade-tariff-backend, or the run ends as
failed with the reason in error_summary.

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

TERMINAL_STATUSES = frozenset({"completed", "partially_failed", "failed", "cancelled"})


async def create_and_run(
    experiment_name: str,
    run_time_overrides: dict,
    gold_query_set_id: int,
    poll_interval_seconds: float = 2.0,
) -> dict:
    client = TradeTariffBackendClient()
    try:
        experiment = await client.create_experiment(experiment_name, gold_query_set_id)
        run = await client.create_run(
            experiment_id=experiment["id"], run_time_overrides=run_time_overrides,
            idempotency_key=str(uuid.uuid4()),
        )
        while True:
            current = await client.get_run(run["id"])
            if current["status"] in TERMINAL_STATUSES:
                return current
            await asyncio.sleep(poll_interval_seconds)
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
