# AI Search Evaluation Suite

Single-process app for AI search evaluation, retrieval experimentation, and
classification eval runs. It serves:

- the React/Vite product UI at `/`;
- KG coverage/facet/edge/commodity/graph/audit browse and edit routes;
- ATaR ingest/draft/fact extraction/approval routes;
- prompt/config/search-reference, simulator, judge, benchmark, analysis,
  financial, intercept, and complexity product routes;
- retrieval matrix review, fresh retrieval trials, classification Q&A matrices,
  and long-running classification experiment/simulation jobs.

- Q&A/E2E handover contract: `docs/QNA_E2E_CONTRACT.md`
- Extraction pipeline runner: `scripts/extraction_pipeline.py`

The runner wraps the classification matrix module:

```text
apps/product/backend/classification_core/run_classify_matrix.py
```

Jobs write their canonical experiment rows to `kg.classify_runs` in the tariff
database and store local runner status/logs in `var/jobs.sqlite` and
`var/jobs/*.log`.

The local playground OpenAI key, when needed on this machine, is stored in
`apps/classification-evals/.env`. That file is git-ignored, must remain
`0600`, and must never be copied into docs, screenshots, command logs, Docker
images, AMIs, or commits.

## Local Start

```bash
cd apps/classification-evals
cp .env.example .env
./start.sh
```

Open `http://127.0.0.1:8100`.

Host `./start.sh` defaults `AI_FAN_OUT_KG_LABEL_PROFILE=full`, so local runs
can inspect all KG scopes in the local tariff DB. Docker Compose and the image
default to `AI_FAN_OUT_KG_LABEL_PROFILE=deployable` for EC2.

## Required Runtime State

- `TARIFF_DB_DSN` pointing at a Postgres database with `uk` and `kg` schemas.
- `kg.eval_gold`, `kg.kg_edges`, facet/KG retrieval tables, and pgvector support.
- `OPENAI_API_KEY` for actual classification eval jobs and OpenAI-backed
  product actions.
- Optional multi-provider keys for benchmark and product fan-out.

The API refuses to start a paid job unless the request includes
`allow_spend: true`, an `OPENAI_API_KEY` is present, and the request fits the
server-side caps below.

Provider-backed actions are blocked unless the request includes
`allow_spend: true` where supported or `AI_FAN_OUT_WORKBENCH_SPEND_ENABLED=1`
is set for the process. Keep that flag off by default on EC2.

## Self-Contained EC2 Database

For the minimum-spend EC2 shape, use one encrypted root EBS volume with
`DeleteOnTermination=false`. Run the eval runner and Postgres on that same
volume. A separate data EBS is cleaner for reattachment, but it adds provisioned
GB-month cost and is not necessary for the initial eval box.

The Compose file starts `pgvector/pgvector:pg16` and a single-node local
OpenSearch container. Both bind to `127.0.0.1` on the VM; the runner reaches
them over the private Compose network.

```bash
cd apps/classification-evals
cp .env.example .env
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
docker compose up -d --build classification-evals
```

After the tariff snapshot is loaded, hydrate the local OpenSearch keyword
index from the VM Postgres database. The hydrator indexes only current-valid,
non-stale commodity self-text rows according to tariff validity dates:

```bash
docker compose exec classification-evals \
  python scripts/hydrate_opensearch_index.py --recreate
```

The app uses OpenSearch for the keyword leg and pgvector for the semantic leg,
then fuses the two result sets with reciprocal rank fusion. If OpenSearch is
not configured or unavailable, prompt/search preview falls back to Postgres
full-text search.

## Extraction Pipeline

The deployed image includes the KG extraction seeders, SQL migrations, bundled
seed data, and an operational manifest runner under `scripts/etl_seeders` and
`scripts/extraction_pipeline.py`.

Check status without mutating data:

```bash
docker compose --profile manual run --rm extraction-pipeline
curl -k -u "$APP_USERNAME:$APP_PASSWORD" \
  https://18.175.148.215.sslip.io/api/extraction/status
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
loading the snapshot. The app is then self-contained on the VM: product and
eval APIs use the local Postgres volume, runner logs/status live in the
`classification_eval_state` volume, and product runtime data/results live in
`product_app_data` and `product_app_results`.

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

## Spend And Execution Caps

These are server-side operator controls, not client hints:

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
The suite remains no-spend by default unless
`AI_FAN_OUT_WORKBENCH_SPEND_ENABLED=1` is explicitly enabled.

## Console Progress Logging (evaluation runs)

A `trade-tariff-backend`-driven evaluation run (`execute_run.py`) can take
several minutes and, by default, prints nothing to the console while it's
running. Set `EVAL_PROGRESS_LOGGING` to see per-gold-query progress as it
happens: the total gold query count at the start, a line when each gold
query begins (position, ATaR reference, expected code, persona, query text),
a line when it ends (final code, gold_in_top1/gold_in_top5, questions
answered, or the error on failure), and a one-line summary when the whole
run finishes.

Same convention as `CLASSIFICATION_ALLOW_PROVIDER_CALLS` above: unset/empty
is off, one of `1`/`true`/`yes`/`on` is on. **Off by default everywhere.**

- **Local dev:** set `EVAL_PROGRESS_LOGGING=1` in `apps/classification-evals/.env`
  (gitignored).
- **Deployed (ECS):** stays off by default — the service already ships
  container stdout to CloudWatch for free (`cloudwatch_log_group_name` in
  `terraform/main.tf`), so there's no need to pay extra ingestion cost or add
  log noise for every run when nothing's wrong. To turn it on for a specific
  deploy (e.g. to debug a remote run), add an entry to `terraform/locals.tf`'s
  `service_environment` list — `{ name = "EVAL_PROGRESS_LOGGING", value = "1" }` —
  and redeploy. Once it's printing, CloudWatch picks it up automatically like
  any other console output; nothing further to configure there.

This is console output only — it never changes what gets persisted via
`post_result`/`update_run`.

## API

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

Product routes are also registered in this same FastAPI process. Deploy-owned
routes above take precedence where paths overlap.

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

Build from the repository root so the image can include the product modules and
the runner:

```bash
docker build -f apps/classification-evals/Dockerfile -t ai-search-evaluation-suite .
docker run --env-file apps/classification-evals/.env -p 8100:8100 ai-search-evaluation-suite
```

Prefer Compose for EC2 because it also starts the local pgvector database:

```bash
cd apps/classification-evals
docker compose up -d --build
```
