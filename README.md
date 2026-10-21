# AI Search Evaluation Suite

This repository contains the evaluation worker that Trade Tariff Admin and
Trade Tariff Backend use to run search evaluations. The service is an API only.
It does not serve a UI.

## Worker Contract

Admin creates and tracks runs through Rails. Trade Tariff Backend starts the
worker. Admin polls Rails for status and results.

| Application route | Contract |
|---|---|
| `POST /api/evaluation/runs/{run_id}/start` | Accept an existing backend run. Return 202 with `started` and `run_id`. Return 409 for a duplicate active run on the same worker. |
| `GET /api/health` | Return `status`, `auth_enabled`, and `product_backend_present`. This checks source presence, not backend connectivity. |
| `GET /api/live` | Return `status: ok`. |

The worker fetches the run and its gold queries from the backend. It submits
searches, polls queued searches, runs the trader simulator when needed, scores
results, and posts results and terminal run status back to the backend. It
checks backend cancellation between gold queries. The backend owns gold-set
generation and the persistent run record. The worker has no status or
cancellation API of its own.

Set `TRADE_TARIFF_BACKEND_BASE_URL` for the target backend. Simulator calls need
`CLASSIFICATION_ALLOW_PROVIDER_CALLS` enabled and an OpenAI key. A run can select
`simulator_model`; the shared default is `gpt-5-mini`. Backend searches have their
own provider configuration and controls. Agree the target and budget before any
live evaluation.

The worker keeps optional bearer/basic authentication and structured logging.
The root image uses its TLS entrypoint on port 8443. The secondary image uses
HTTP on port 8100. Both install the `ai-search-evaluation-worker` package. The
worker CLI is `worker.backend.cli`.

## Repository Layout

| Path | Purpose |
|---|---|
| `src/worker/app.py` | Worker application, authentication, logging, and health routes. |
| `src/worker/ingress.py` | Evaluation start endpoint and active-run guard. |
| `src/worker/simulator.py` | Trader simulator. |
| `src/worker/backend/` | Backend HTTP client, run executor, Q&A loop, and CLI. |
| `Dockerfile` | Deployed TLS worker image. |
| `Dockerfile.http` | HTTP worker image for local use. |
| `compose.yml` | Local worker service. |
| `terraform/` | ECS service definition for the deployed worker. |
| `tests/` | Offline worker tests. |

## Checks, contributions and licence

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the fork workflow, review process and
private security reporting. After `pip install -e .`, run:

```sh
python -m unittest discover -s tests
python -m compileall -q src
```

Live evaluations are separate from unit tests. They can send prompts to external
providers, consume paid API quota and change shared evaluation data. Use
non-sensitive fixtures and confirm the target and budget before running them.

### Licence

The code and associated documentation use the [MIT licence](LICENCE.md), with
Crown copyright (HM Revenue & Customs). Models, evaluation datasets and
third-party dependencies retain their own terms.

## Local Development Quickstart

The worker needs Python 3.11+. It does not need Node.js, local PostgreSQL, or
OpenSearch. From the repository root:

```bash
# Create .env from .env.example if needed. Do not replace existing secrets.
./start.sh
```

In another terminal:

```bash
curl -fsS http://127.0.0.1:8100/api/health
```

`start.sh` creates `.venv` in the repository root and installs the package in
editable mode. Health does not require provider keys and does not start an evaluation. Send the
configured authorization header if health is protected. `/` returns 404.

## Trade Tariff Backend Integration

`src/worker/backend/` talks to
Trade Tariff Backend over HTTP. It does not use a local database.

### Running an experiment end-to-end

Admin provides the normal run workflow. The following manual path is for
authorized diagnosis through a backend Rails console and the worker ingress.
It can generate shared data and incur provider costs. It is not a health check.

**0. Have a gold query set or a synthetic input set.** A run scores one saved
set, chosen on its experiment, and fails as soon as it starts if its
experiment has neither (AI-1445). Create one of the following in the Admin
app, or from a shell in `trade-tariff-backend`:

*Gold query set* — sampled and written up by a model, generated in the
background:

```bash
NAME="Set A" SIZE=10 bundle exec rake tariff:evaluation:generate_gold_queries
```

Generation runs in the background, so wait until its status is `ready` before you start
a run (`EvaluationGoldQuerySet[<id>].status` in a Rails console). Starting a run earlier
scores only the gold queries generated so far.

*Synthetic input set* — a frozen sample of synthetic ATaRs (real,
HMRC-analyst-written product descriptions, used as-is with no persona
rewrite). Sampling is synchronous, so the set is ready as soon as this
returns (Rails console):

```ruby
synthetic_input_set = Evaluation::SyntheticInputSetCreator.call(
  name: "Set B", requested_size: 10, created_by: "console",
)
synthetic_input_set.actual_size  # lower than requested_size if fewer synthetic ATaRs exist
```

**1. Create an experiment** (`trade-tariff-backend` Rails console,
`bin/rails console`). Set whichever id matches the set you created in step 0
— an experiment can have at most one of the two:

```ruby
experiment = EvaluationExperiment.create(
  name: "my-manual-test-#{Time.now.to_i}",  # must be unique: validates_unique :name
  description: "Manual end-to-end test",
  gold_query_set_id: EvaluationGoldQuerySet.first(name: "Set A").id,  # for a gold query set, or:
  # synthetic_input_set_id: synthetic_input_set.id,                  # for a synthetic input set
  enabled: true,
  configuration_overrides: {},              # optional: merged over the admin-config baseline; see EvaluationConfiguration::ALLOWED_OVERRIDE_KEYS for what is allowed here
)
experiment.id  # note this, it is needed for step 2
```

**2. Create a run for that experiment** (same Rails console):

```ruby
run = EvaluationRun.start!(
  experiment:,
  triggered_by: "console",
  idempotency_key: SecureRandom.uuid,       # required, see AI-1184; a fresh UUID per attempt, not reused
  run_time_overrides: {},                   # optional: merged over experiment.configuration_overrides
)
run.id      # note this, it is needed for step 3
run.status  # => "queued": nothing has executed yet
```

**3. Execute the run.** The backend starts the worker for Admin-created runs.
For a run created directly in a Rails console, call the ingress explicitly.
Start the worker with `./start.sh`. Set
`TRADE_TARIFF_BACKEND_BASE_URL` to the backend used in steps 1-2 and set `RUN_ID`
to the approved run ID:

```bash
curl -X POST "http://127.0.0.1:8100/api/evaluation/runs/${RUN_ID}/start"
# => 202 {"started": true, "run_id": "..."}; 409 if already active
```

This returns immediately: execution happens in the background
(`asyncio.create_task`). There is no separate status-polling endpoint on this
app to watch. Progress lives in `trade-tariff-backend` itself (next step),
because that is the one persistent record of the run regardless of which of the
two trigger paths above started it.

If this app is deployed with `AI_FAN_OUT_BEARER_TOKEN` or
`AI_FAN_OUT_BASIC_AUTH_USER`/`_PASSWORD` set (see `## Configuration` below), the
caller must send the matching `Authorization` header. The Rails caller uses the
same authentication as a manual caller.

**4. Confirm the results landed in `trade-tariff-backend`** (same Rails
console as steps 1-2: re-fetch, do not reuse the in-memory `run` object, since
the eval app updated it out-of-process):

```ruby
run = EvaluationRun[run.id]
run.status              # => "completed" (or "partially_failed"/"failed") once the background task finishes
run.result_count        # => set by reconcile_aggregates! when the run finishes
run.evaluation_results_dataset.count
run.evaluation_results_dataset.first.values
```

**Alternative for local/manual testing only:**
`worker.backend.cli` creates the experiment and run
over HTTP, then follows the run's status until it finishes. It does not execute
the run itself: creating the run makes trade-tariff-backend start it in this
app, the same as the admin screens do, so this app must be running and reachable
from trade-tariff-backend. Use the walkthrough above when you want to exercise
the Rails-console and ingress-endpoint split directly.

```bash
export TRADE_TARIFF_BACKEND_BASE_URL=http://127.0.0.1:3000  # defaults to this already
python -m worker.backend.cli "my-local-test-experiment" --gold-query-set 5
```

### Run-level cost and latency (retrieval side only)

Each `/searches` round from `trade-tariff-backend` includes a `meta.usage`
object (`total_cost_usd`, `duration_ms`, `provider_calls`) whenever that
round made at least one LLM or embedding call. It is absent, not zeroed, on a
short-circuit round with no call. `qa_loop.run_qa_session_via_trade_tariff_backend`
sums this across every round of a gold query's Q&A session, and
`execute_run.py` sends the totals (`cost_usd`, `latency_seconds`,
`provider_calls`) on the successful `post_result` call. A finished run's
`total_cost_usd`/`total_latency_seconds`/`total_provider_calls` in
`trade-tariff-backend` therefore reflect real spend and timing (AI-1225 on the
Rails side, this repository's half of the same design).

This covers the retrieval and question-asking cost only: the Rails-side LLM
calls behind `/searches`. The simulator side of this app (the
`simulate_trader_answer` calls imported from
`worker.simulator`) has no cost or latency tracking of its
own yet. It is tracked separately as AI-1226, since it needs its own small
$/token pricing table in Python (`AiUsage::PricingCalculator` is not callable
from this repository). Until AI-1226 lands, a run's totals understate a gold
query's true cost by whatever the simulator's own calls cost.

### Running the tests

```bash
python -m unittest discover -s tests -v
```

This also runs automatically in CI on every PR (`.github/workflows/ci.yml`).
The `worker.backend` tests are fixture-based. They
mock the HTTP layer with `httpx.MockTransport` against response shapes captured
from a real `trade-tariff-backend` during development, not a live Rails server.
`trade-tariff-backend` is therefore not a test dependency and the tests run
without external services. They prove that the client handles those exact
response shapes, not that the backend still returns those shapes today. If the
backend API contract changes, these tests will not catch that on their own.

## Configuration

Copy the example environment file and fill in only the values needed for the
run mode:

```bash
test -f .env || cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `TRADE_TARIFF_BACKEND_BASE_URL` | Worker target backend URL. |
| `CLASSIFICATION_ALLOW_PROVIDER_CALLS` | Explicit worker simulator provider-call gate. Backend search spend is controlled separately. |
| `OPENAI_API_KEY` | Provider key for simulator calls. |
| `OPENAI_BASE_URL` | Leave unset locally (standard OpenAI host, standard key). Deployed environments set this to `https://gb.api.openai.com/v1`, a UK data-residency host that requires a UK-region-scoped key. |
| `QA_SIMULATOR_MODEL` | Optional simulator model override. Defaults to `gpt-5-mini`. |
| `AI_FAN_OUT_BEARER_TOKEN` | Optional deployment bearer token. |
| `AI_FAN_OUT_BASIC_AUTH_USER` / `AI_FAN_OUT_BASIC_AUTH_PASSWORD` | Optional deployment basic auth. |
| `AI_FAN_OUT_AUTH_PUBLIC_PATHS` | Comma-separated paths that skip authentication. |
| `CLASSIFY_EVAL_HOST` / `CLASSIFY_EVAL_PORT` | Local launcher bind address and port. Defaults to `127.0.0.1:8100`. |
| `EVAL_PROGRESS_LOGGING` | Set to `1` to log progress details. Logs can include query text. |

Use placeholders such as `<backend-url>`, `<provider-api-key>`, and
`<strong-password>` in examples and docs. Never commit real values.

## Deployment Notes

The platform builds the repository-root `Dockerfile` and runs the backend-driven
worker with TLS on port 8443. Health checks use `/api/health`, not `/` or
`/healthcheckz`. This is an evaluation worker, not a placeholder.

`Dockerfile.http` builds the same worker for port 8100.
Terraform deploys one ECS service, `eval`. The worker does not depend on a
database, OpenSearch, or object storage. It uses the Trade Tariff Backend HTTP
API only.

Before running paid or provider-backed runs in a deployment:

- Put TLS and authentication in front of the app.
- Configure secrets through environment injection or a managed secret store.
- Keep the simulator provider-call gate disabled unless the deployment is
  intended to spend.
- Agree the backend target and budget before a live run.

## Security And Data Hygiene

- Do not commit `.env` files, runtime config, provider keys, database passwords,
  bearer tokens, private URLs, local machine paths, screenshots containing
  secrets, or generated dumps.
- Store secrets in environment variables or a secret store; inject them at
  runtime.
- Keep fixtures and example data free of personal data, trader-identifying
  details, private prompts, and credentials.
- Put auth in front of every non-local deployment. Leave only health checks
  public when required by infrastructure.
- Treat model-provider calls as billable actions. Require an explicit spend
  opt-in.
- Review logs and results before sharing. They can contain query text, provider
  metadata, or source snippets.

## Further Reading

- [`CONTRIBUTING.md`](CONTRIBUTING.md) for the fork, review, and licensing workflow.
