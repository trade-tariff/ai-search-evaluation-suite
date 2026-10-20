# AI Search Evaluation Suite

The application is the backend-driven Admin evaluation worker. It does not serve
a UI. Trade Tariff Backend starts runs and stores results.

| Entrypoint | Purpose |
|---|---|
| `backend.worker_app:app` | Worker. Uses `requirements-worker.txt` and backend HTTP APIs. |
| `classification_core.trade_tariff_backend.cli` | Optional CLI for authorized local diagnosis. |

## Local Worker Start

```bash
cd apps/classification-evals
# Create .env from ../../.env.example if needed. Do not replace existing secrets.
./start.sh
```

In another terminal:

```bash
curl -fsS http://127.0.0.1:8100/api/health
```

The launcher installs worker requirements in this directory's `.venv`. It does
not remove packages from an existing environment. The worker does not use local
PostgreSQL, OpenSearch, or `var/jobs.sqlite`. `/` returns 404. Health checks
source presence, not backend connectivity or evaluation quality. Send the
configured authorization header if health is protected.

See the [worker contract](../../README.md#worker-contract) for run ownership,
authentication, cancellation, scoring, and spend boundaries.

## Tests

```bash
python -m unittest discover -s tests
```

The tests run offline against fixture-based HTTP responses. They do not need a
backend, provider keys, or a database.
