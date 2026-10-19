# AI Search Evaluation Suite

The default application is the backend-driven Admin evaluation worker.
The workbench and local-DB evaluation tools remain separate research source.

| Entrypoint | Purpose |
|---|---|
| `backend.worker_app:app` | Default worker. Uses `requirements-worker.txt` and backend HTTP APIs. |
| `backend.app:app` | Native research job and matrix server. Needs full source and research requirements. |
| `backend.research_app:app` | Optional research app that also registers product research APIs. |

Both worker images exclude the workbench, research APIs, research scripts, and
seed data. The existing extraction profiles use the separate `extraction-tools`
image target. The worker CLI remains in the worker backend package.

## Local Worker Start

```bash
cd apps/classification-evals
# Create .env from .env.example if needed. Do not replace existing secrets.
./start.sh
```

In another terminal:

```bash
curl -fsS http://127.0.0.1:8100/api/health
```

The launcher installs worker requirements in this directory's `.venv`. It does
not remove packages from an existing environment. The worker does not use local
PostgreSQL, OpenSearch, or `var/jobs.sqlite`. `/` returns 404; it is not a UI.
Health checks source presence, not backend connectivity or evaluation quality.
Send the configured authorization header if health is protected.
See the [worker contract](../../README.md#worker-contract) for run ownership,
authentication, cancellation, scoring, and spend boundaries.

## Local Research Start

Use the [optional research setup](../../README.md#optional-research-workbench).
It installs the full `requirements.txt`, builds the frontend, and starts
`backend.research_app:app` on port 8000. `start.sh` does not start this app.

The retained research app serves workbench APIs for KG, ATaR, prompts, retrieval,
simulation, judging, benchmarks, analysis, intercepts, and local evaluation jobs.
The native job runner wraps
`apps/product/backend/classification_core/run_classify_matrix.py`. It writes
canonical experiment rows to `kg.classify_runs` and local job state to
`var/jobs.sqlite` and `var/jobs/*.log`.

- Research Q&A/E2E contract: `docs/QNA_E2E_CONTRACT.md`
- Retained extraction CLI: `scripts/extraction_pipeline.py`

## Research Runtime State

- `TARIFF_DB_DSN` pointing at a Postgres database with `uk` and `kg` schemas.
- `kg.eval_gold`, `kg.kg_edges`, facet/KG retrieval tables, and pgvector support.
- `OPENAI_API_KEY` for paid research jobs and OpenAI-backed product actions.
- Optional multi-provider keys for benchmark and product fan-out.

Research jobs require `allow_spend: true`, an OpenAI key, and the research caps
below. Keep `AI_FAN_OUT_WORKBENCH_SPEND_ENABLED` off unless research provider spend
is approved. These request gates and caps do not govern backend-driven Admin
runs. Backend search controls and the worker simulator gate apply separately.

Keep `.env` files git-ignored and mode `0600`. Never copy keys into docs, logs,
images, AMIs, or commits. Deployed OpenAI requests use the UK host configured by
`OPENAI_BASE_URL`; that host requires a compatible region-scoped key. Leave the
variable unset for a standard local OpenAI key.

## Research Database

These operations prepare retained research and extraction data, not the worker.
Restore and profile-apply commands can delete data. Confirm the target and obtain
approval before using them.

For the minimum-spend EC2 research shape, use one encrypted root EBS volume with
`DeleteOnTermination=false`. Run the eval runner and Postgres on that same
volume. A separate data EBS is cleaner for reattachment, but it adds provisioned
GB-month cost and is not necessary for the initial eval box.

The Compose file starts `pgvector/pgvector:pg16` and a single-node local
OpenSearch container. Both bind to `127.0.0.1` on the VM; the runner reaches
them over the private Compose network.

```bash
cd apps/classification-evals
test -f .env || cp .env.example .env
# edit POSTGRES_PASSWORD and auth/provider keys
docker compose up -d tariff-db
```

Load a fresh snapshot before starting eval jobs:

```bash
./scripts/restore_tariff_snapshot.sh \
  --target-dsn "postgresql://postgres:<password>@127.0.0.1:5432/tariff_db" \
  --snapshot ./var/db-snapshots/latest.dump \
  --confirm-drop
./scripts/apply_deploy_kg_profile.py \
  --dsn "postgresql://postgres:<password>@127.0.0.1:5432/tariff_db"
./scripts/apply_deploy_kg_profile.py \
  --dsn "postgresql://postgres:<password>@127.0.0.1:5432/tariff_db" \
  --apply
docker compose up -d tariff-db opensearch
```

After the tariff snapshot is loaded, hydrate the local OpenSearch keyword
index from the VM Postgres database. The hydrator indexes only current-valid,
non-stale commodity self-text rows according to tariff validity dates:

```bash
docker compose --profile manual run --rm extraction-pipeline \
  python scripts/hydrate_opensearch_index.py --recreate
```

The research app uses OpenSearch for the keyword leg and pgvector for the
semantic leg, then fuses the two result sets with reciprocal rank fusion. If OpenSearch is
not configured or unavailable, prompt/search preview falls back to Postgres
full-text search.

## Extraction Pipeline

The `extraction-tools` target retains extraction source, seed data, and full
research requirements. The existing manual and scheduler profiles select this
target. Neither default worker image includes these scripts or data.

Check status without mutating data:

```bash
docker compose --profile manual run --rm extraction-pipeline
```

Write a dry-run manifest for the safe no-provider profile:

```bash
docker compose --profile manual run --rm extraction-pipeline \
  python scripts/extraction_pipeline.py run --profile safe --dry-run
```

Run the safe profile deliberately:

```bash
docker compose --profile manual run --rm extraction-pipeline \
  python scripts/extraction_pipeline.py run --profile safe
```

Provider-backed extraction steps are not enabled by default. To run them, set
`EXTRACTION_ALLOW_PROVIDER_CALLS=1`, pass `--allow-spend`, and use only after
agreeing spend limits. GOV.UK ATAR scrape steps also require
`EXTRACTION_ALLOW_NETWORK=1` or `--allow-network`.

The latest manifest is written to `var/extraction/manifest.json`, including the
selected DAG profile, step statuses, blockers, command tails, and before/after
KG table counts. An optional scheduler profile exists but is not started by the
default deployment:

```bash
EXTRACTION_RUN_INTERVAL_SECONDS=86400 docker compose --profile scheduler up -d extraction-scheduler
```

The first `apply_deploy_kg_profile.py` call is a dry run. It reports how many
KG rows would be removed or relabelled. The second call commits the deployable
KG profile after the counts look right. The profile keeps consumer scopes for
retrieval, classification, and Q&A, keeps audit labels for curation/provenance,
and drops copied KG rows that are not consumed by this app.

The restore creates the required `vector` and `pg_trgm` extensions before
loading the snapshot. Research and extraction use this database and the retained
state/data/results volumes. These are not worker dependencies. The Compose
`classification-evals` service is still the worker; it does not start a research
web app.

## Teardown And Preservation

Before stopping or terminating the VM, export the run outputs and runner state:

```bash
cd /opt/ai-search-evaluation-suite/apps/classification-evals
./scripts/export_eval_state.sh \
  --target-dsn "postgresql://postgres:<password>@127.0.0.1:5432/tariff_db"
```

This writes `var/teardown-exports/latest.eval-runs.dump` for the eval DB rows
and `var/teardown-exports/latest.runner-state.tgz` for `jobs.sqlite` plus job
logs. Those files live on the preserved root volume and can also be copied to a
private S3 bucket.

For the cheapest pause, stop the instance. Compute billing stops; EBS storage
continues billing:

```bash
./scripts/aws_teardown_eval_host.sh \
  --instance-id i-... \
  --region eu-west-2 \
  --mode stop
```

For a harder teardown, terminate the instance while preserving the root EBS:

```bash
./scripts/aws_teardown_eval_host.sh \
  --instance-id i-... \
  --region eu-west-2 \
  --mode terminate \
  --snapshot-root
```

Use an Elastic IP only if a stable URL is worth the extra IPv4 charge. Otherwise
use the instance public DNS while it is running and accept that the URL can
change after stop/start.

## Refreshing The Snapshot

Create the snapshot from whichever source DB is the current truth on your
machine or staging. The script dumps both `uk` and `kg`, excludes
`kg.audit_log` and previous eval/classification run rows by default, writes a
manifest with table counts, and updates `var/db-snapshots/latest.dump`.

```bash
./scripts/dump_tariff_snapshot.sh \
  --source-dsn "postgresql:///tariff_db" \
  --yes
```

Copy the resulting `*.dump` file to the EC2
`apps/classification-evals/var/db-snapshots/` directory, then run the restore
command above on the VM. If the source contains sensitive local experiment
labels or prompts, either clean them first or set
`TARIFF_SNAPSHOT_EXCLUDE_TABLE_DATA` with extra comma-separated table patterns
before dumping. Pass `--include-run-history` only when you explicitly want the
EC2 instance to inherit historical `kg.classify_runs`, `kg.eval_runs`,
`kg.eval_run_results`, and `kg.exp6_qa_runs` data.

Simple transfer options:

```bash
scp ./var/db-snapshots/latest.dump ubuntu@<ec2-host>:/opt/ai-search-evaluation-suite/apps/classification-evals/var/db-snapshots/
```

or, once AWS credentials and a private bucket are configured:

```bash
aws s3 cp ./var/db-snapshots/latest.dump s3://<private-bucket>/tariff/latest.dump
aws s3 cp s3://<private-bucket>/tariff/latest.dump ./var/db-snapshots/latest.dump
```

## Auth

Local use is open by default. Before exposing the runner on EC2, set one of:

```bash
AI_FAN_OUT_BASIC_AUTH_USER=demo
AI_FAN_OUT_BASIC_AUTH_PASSWORD=<strong-password>
```

or:

```bash
AI_FAN_OUT_BEARER_TOKEN=<long-random-token>
```

When auth is enabled, every route is protected by default. Set
`AI_FAN_OUT_AUTH_PUBLIC_PATHS=/api/health` only when an unauthenticated load
balancer health check is required.

The Docker Compose file binds the host port to `127.0.0.1` by default. Put
Caddy/Nginx/ALB with TLS and auth in front when exposing it externally.

## Provider API Keys

Do not paste provider keys into chat, commit them, or bake them into AMIs. For
EC2, the minimum-spend default is AWS SSM Parameter Store `SecureString`,
encrypted by KMS, with the instance profile allowed to read only that one
parameter. Secrets Manager is also supported if we decide the extra monthly
secret cost is worth managed secret lifecycle/rotation.

Create the parameter from a trusted shell:

```bash
aws ssm put-parameter \
  --region eu-west-2 \
  --name /ai-search-evaluation-suite/classification-evals/openai-api-key \
  --type SecureString \
  --value "$OPENAI_API_KEY" \
  --overwrite
```

On the EC2 host, fetch it into `/run` before starting Compose:

```bash
./scripts/fetch_runtime_secrets_from_ssm.sh \
  --region eu-west-2 \
  --openai-param /ai-search-evaluation-suite/classification-evals/openai-api-key
set -a
. /run/ai-search-evaluation-suite/secrets.env
set +a
docker compose up -d --build
```

If using Secrets Manager instead:

```bash
aws secretsmanager create-secret \
  --region eu-west-2 \
  --name ai-search-evaluation-suite/classification-evals/openai-api-key \
  --secret-string "$OPENAI_API_KEY"

./scripts/fetch_runtime_secrets_from_ssm.sh \
  --region eu-west-2 \
  --openai-secret-id ai-search-evaluation-suite/classification-evals/openai-api-key
```

`/run` is tmpfs, so the key is not persisted on the preserved EBS volume. Docker
still receives the key as an environment variable, so keep SSH users limited and
do not add untrusted users to the Docker group.

The suite can also use `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
`XAI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`,
`DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, and `SAMBANOVA_API_KEY`. Store those the
same way if needed. Runtime env keys override blank saved product config values
without being written back to the saved product config file.

## Research Spend And Execution Caps

These are server-side controls for research jobs, not client hints. They are not
budget limits for backend-driven worker runs:

| Variable | Default | Purpose |
|---|---:|---|
| `CLASSIFY_EVAL_ALLOWED_MODELS` | `gpt-5-nano,gpt-5-mini,gpt-5.5` | Strict allowlist for classifier and trader-emulator models. |
| `CLASSIFY_EVAL_MAX_RUNNING_JOBS` | `1` | Prevent concurrent long-running jobs. |
| `CLASSIFY_EVAL_MAX_CONCURRENCY` | `4` | Max child-process session concurrency. |
| `CLASSIFY_EVAL_MAX_ROUNDS` | `8` | Max Q&A rounds per session. |
| `CLASSIFY_EVAL_MAX_CANDIDATE_LIMIT` | `200` | Max retrieved candidates per session. |
| `CLASSIFY_EVAL_MAX_SESSIONS` | `50` | Max estimated sessions per submitted job. |
| `CLASSIFY_EVAL_EST_USD_PER_SESSION` | `0.05` | Conservative estimate used for budget gating. |
| `CLASSIFY_EVAL_MAX_EST_USD` | `10.00` | Max estimated spend for one job. |
| `CLASSIFY_EVAL_ALLOW_SWEEP` | `0` | Sweeps are rejected unless explicitly enabled. |

When a job passes these gates, the runner sets
`CLASSIFICATION_ALLOW_PROVIDER_CALLS=1`, `CLASSIFY_LLM_MODEL=<selected model>`, and
`QA_SIMULATOR_MODEL=<selected Q&A simulator model>` only for that subprocess.
The workbench spend switch applies to research routes. It is not a global
budget switch for backend search requests or worker runs.

## Console Progress Logging (evaluation runs)

The worker logs accepted requests and run lifecycle events as structured console
logs. Set `EVAL_PROGRESS_LOGGING` for additional per-gold-query progress:
the total gold query count at the start, a line when each gold
query begins (position, ATaR reference, expected code, persona, query text),
a line when it ends (final code, gold_in_top1/gold_in_top5, questions
answered, or the error on failure), and a one-line summary when the whole
run finishes.

Same convention as `CLASSIFICATION_ALLOW_PROVIDER_CALLS` above: unset/empty
is off, one of `1`/`true`/`yes`/`on` is on. **Off by default everywhere.**

- **Local dev:** set `EVAL_PROGRESS_LOGGING=1` in `apps/classification-evals/.env`
  (gitignored).
- **Deployed (ECS):** stays off by default. The configured CloudWatch log group
  collects container stdout. Detailed progress increases log volume and can
  include query text. Enable it only for approved diagnosis through the service
  environment, then disable it when no longer needed.

This is console output only — it never changes what gets persisted via
`post_result`/`update_run`.

## Worker API

| Route | Purpose |
|---|---|
| `POST /api/evaluation/runs/{run_id}/start` | Start an existing backend run. Return 202, or 409 for an active duplicate. |
| `GET /api/health` | Status, auth state, and product backend source presence. No research DB initialization. |
| `GET /api/live` | Liveness status. |

The worker does not expose the following research routes or the workbench.

## Research API

These routes belong to the optional research process, not the worker:

| Route | Purpose |
|---|---|
| `GET /api/health` | Runner readiness, auth status, profile, caps, and key-presence check. |
| `GET /api/live` | Minimal liveness response. |
| `GET /api/options` | Supported strategy, persona, prompt, augmentation, and model options. |
| `GET /api/retrieval/experiments` | Retrieval experiment catalog, including deploy-runnable rewrite rows. |
| `GET /api/retrieval/top-experiment` | Top retrieval matrix row, decorated for this app. |
| `POST /api/retrieval/search` | Run one fresh retrieval trial. Rewrite/vector rows require `allow_spend: true`. |
| `GET /api/evals/classification/gold-examples` | Load ATAR-backed persona queries from `kg.eval_gold`. |
| `POST /api/evals/classification/trial` | Run one Q&A classifier + simulator session. Requires `allow_spend: true`. |
| `POST /api/jobs` | Start a classification eval subprocess. Requires `allow_spend: true`. |
| `GET /api/jobs` | List latest jobs. |
| `GET /api/jobs/{job_id}` | Job metadata and command. |
| `GET /api/jobs/{job_id}/log` | Tail the subprocess log. |
| `POST /api/jobs/{job_id}/stop` | Send SIGTERM to the job process group. |
| `GET /eval/classify-matrix` | Server-rendered classification matrix from `kg.classify_runs`. |
| `GET /` | Bundled React product UI. |

`backend.research_app:app` also registers product research routes. Native research
routes take precedence where paths overlap. The UI needs built frontend assets.

Example dry-sized paid job payload:

```json
{
  "run_label": "demo_classify_eval",
  "strategy": "converge",
  "prompt_mode": "baseline",
  "augmentation": "facts+kg",
  "model": "gpt-5-mini",
  "simulator_model": "gpt-5-mini",
  "candidate_limit": 40,
  "personas": ["naive_vague"],
  "limit": 5,
  "concurrency": 2,
  "max_rounds": 5,
  "allow_spend": true
}
```

## Docker

Build the secondary worker from the repository root:

```bash
docker build -f apps/classification-evals/Dockerfile -t ai-search-evaluation-suite .
docker run --env-file apps/classification-evals/.env -p 127.0.0.1:8100:8100 ai-search-evaluation-suite
```

The default target is `worker`, not a workbench server. The repository-root
Dockerfile is the separate TLS worker image on port 8443.

Compose keeps its existing database services and volumes, but its default app
is still the worker. The manual and scheduler profiles select `extraction-tools`.
No research web service is added:

```bash
cd apps/classification-evals
docker compose up -d --build
```
