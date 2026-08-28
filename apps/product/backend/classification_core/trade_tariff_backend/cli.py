"""Local/manual convenience entrypoint — the only place in this app that
calls TradeTariffBackendClient.create_run (with a freshly generated Idempotency-Key).
The ingress-triggered flow (main.py's POST /api/evaluation/runs/{run_id}/start)
never creates a run; it only executes one Rails already created.

Usage: python -m classification_core.trade_tariff_backend.cli "my-experiment-name"
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from .client import TradeTariffBackendClient
from .execute_run import execute_run


async def create_and_run(experiment_name: str, run_time_overrides: dict) -> dict:
    client = TradeTariffBackendClient()
    try:
        experiment = await client.create_experiment(experiment_name)
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
    args = parser.parse_args()

    summary = asyncio.run(create_and_run(args.experiment_name, run_time_overrides={}))
    print(summary)


if __name__ == "__main__":
    main()
