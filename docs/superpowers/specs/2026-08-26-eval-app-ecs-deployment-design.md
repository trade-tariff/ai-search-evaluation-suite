# Eval app ECS deployment — design

Status: approved by Hadleigh 2026-08-26, ready for ticket breakdown.
Repos touched: this repo (`ai-search-evaluation-suite`) and
`trade-tariff-platform-aws-terraform`. No changes needed in
`trade-tariff-tools` or `trade-tariff-backend`.

## Scope

Get the eval app's ECS service actually deployable to the `development` AWS
account via the existing `deploy-to-development.yml` → `trade-tariff-tools`
reusable workflow, reachable from `trade-tariff-backend`'s ECS service over
the private network, with its own `OPENAI_API_KEY`. Concretely:

- Fix the deploy pipeline so it builds and runs the real eval app, not the
  placeholder health-check server that's there today.
- Wire up the AWS-account-level prerequisites (ECR repo, GitHub OIDC trust,
  a secret to hold `OPENAI_API_KEY`) that the eval app's own Terraform
  already assumes exist but don't yet.
- Confirm/document the two directions of traffic between backend and eval
  (backend triggering a run; eval calling back into backend for gold
  queries/results, per AI-1073).

This is a design for **development only**. Staging and production need the
same category of change (see "Staging/production follow-up" below) but are
deliberately deferred until development is proven to work end-to-end.

## Out of scope

- Any public-facing route for the eval app (ALB target group, public
  hostname). Traffic stays inside the VPC, backend ↔ eval, over Cloud Map
  service discovery.
- The FastAPI app's existing optional bearer-auth middleware
  (`install_optional_auth` in `apps/classification-evals/backend/auth.py`).
  It already exists and can be turned on later; enabling it isn't required
  for a VPC-internal-only service and isn't part of this piece of work.
- Staging and production Terraform changes (OIDC trust policy entry, new
  secret). Recorded as explicit follow-up, not built now.
- Any change to `trade-tariff-tools`' shared `build-and-push` action. The
  chosen approach (below) needs none.
- Key rotation/rotation automation for the new secret. It's seeded by hand
  once, same as every other per-app secret in this account today.

## Current state (what's already there vs. what's missing)

A prior session already scaffolded a fair amount of this before the gaps
below were found:

**Already in place:**
- `terraform/main.tf` in this repo wires an `ecs-service` module call
  identical in shape to `trade-tariff-backend`'s `backend_uk` service — same
  cluster (`trade-tariff-cluster-development`), same shared security group,
  same Cloud Map namespace (`tariff.internal`).
- `.github/workflows/deploy-to-development.yml` already calls
  `trade-tariff-tools/.github/workflows/deploy-ecs.yml@main`, the same
  reusable workflow `trade-tariff-backend` uses, and already has
  `workflow_dispatch:` — so "trigger a deploy from the GitHub Actions UI" is
  already available, no new workflow trigger needed.

**Missing or wrong, found during this design's investigation:**

1. **The deploy pipeline builds the wrong Docker image.**
   `trade-tariff-tools`' `build-and-push` action always runs
   `docker build .` from the repo root, with no way to override the
   Dockerfile path. This repo's root `Dockerfile` is a placeholder
   (`apps/deployment/app.py` — a bare Python `http.server` that only
   answers `/` and `/healthcheckz` with `"OKAY"`), left over from an
   earlier session that proved the deploy pipeline mechanics before the
   real app existed. The actual eval app is
   `apps/classification-evals/Dockerfile`. As wired today, a deploy would
   succeed (health checks pass) while running the wrong container
   entirely.

2. **Port/TLS mismatch between the placeholder and the real app.**
   The placeholder listens on 8443 and terminates TLS itself using
   `SSL_CERT_PEM`/`SSL_KEY_PEM` env vars (the same pattern every other ECS
   service in this account uses, sourced from the shared
   `ecs-tls-certificate` secret). The real eval app
   (`apps/classification-evals/Dockerfile`) runs plain `uvicorn` on 8100
   with no TLS handling at all. `terraform/main.tf`'s `container_port = 8443`
   and `terraform/locals.tf`'s SSL env vars currently describe the
   placeholder's shape, not the real app's.

3. ~~ECR repository doesn't exist~~ — **correction, 2026-08-28: already done.**
   `ai-search-evaluation-suite` was added to
   `trade-tariff-platform-aws-terraform`'s `modules/ecr/locals.tf`
   `applications` map back in commit `3b2706f` ("feat(platform): provision
   AI evaluation service", Jira AI-1064, 2026-07-21) — before this design
   was even written. That commit predates this document; the gap above was
   found against a local checkout that hadn't pulled recent `main`. No
   change needed here.

4. ~~GitHub OIDC trust policy blocks the deploy outright~~ — **correction,
   2026-08-28: already done.** The same `3b2706f` commit also added
   `"repo:trade-tariff/ai-search-evaluation-suite:*"` to
   `GithubActions-ECS-Deployments-Role`'s trust policy in
   `environments/development/common/iam-roles.tf`. No change needed here.

5. **No secret holds `OPENAI_API_KEY` for this app.** Every other app in
   this account gets its own per-app JSON-blob secret in Secrets Manager
   (`backend-uk-api-configuration`, `admin-configuration`, etc.), created as
   an empty shell via `trade-tariff-platform-aws-terraform`'s shared
   `modules/secret` and hand-populated afterwards — the actual key values
   are never in Terraform state. This repo's `terraform/iam.tf` currently
   only grants access to the shared TLS-cert secret; there's no equivalent
   secret or IAM grant for an OpenAI key.

**Confirmed already fine, no change needed:**

- **Network path between backend and eval.** The shared
  `trade-tariff-ecs-security-group-development` security group (defined
  once in `trade-tariff-platform-aws-terraform`'s `modules/security-group`)
  allows ingress on 8443 from the entire VPC CIDR block (`10.0.0.0/16`) and
  unrestricted egress. Both `backend-uk` and `eval` are members of this
  exact same security group, in the same private subnets. Once eval is
  actually running, backend can already reach it — and eval can already
  reach backend — with zero new security group rules, in either direction.
- **`target_group_arn` is optional in the `ecs-service` module** (defaults
  to `null`; the load-balancer attachment block is skipped entirely when
  unset). Choosing internal-only means we don't need an ALB target group at
  all, so the existing `data.aws_lb_target_group.this` reference in this
  repo's own `terraform/data.tf` can simply be deleted rather than pointed
  at a new resource.

**Correction, 2026-08-28 — this one wasn't fine after all:** the same
`3b2706f` commit that fixed the ECR/OIDC gaps above *also* added a public
ALB route for eval (an `ai_eval` block in `alb.tf`, in development,
staging, **and** production, each producing a target group named
`ai-eval-https` — exactly what `data.aws_lb_target_group.this` above
resolves to today). That directly contradicts this design's internal-only
decision. Deleting the eval app's own reference to it (as already planned)
leaves that ALB route live but pointing at nothing — not broken, but dead
infrastructure nobody asked for. See "Changes required" and "Staging/
production follow-up" below: the `ai_eval` block is being removed from
`environments/development/common/alb.tf` as part of this round of work;
staging/production removal is added to the deferred follow-up list.
- **`TradeTariffBackendClient` (AI-1073) is already configurable.** It
  reads its target from `TRADE_TARIFF_BACKEND_BASE_URL`, defaulting to
  `http://127.0.0.1:3000` for local dev. No code change needed — just an
  ECS env var pointed at backend's real internal address once deployed.

## Decisions

1. **Ingress path: internal-only, via Cloud Map DNS.** Backend calls eval
   at `eval.tariff.internal:8443` (the ECS module registers the service
   under `service_name = "eval"` in the `tariff.internal` namespace with a
   10s-TTL `A` record). No public ALB route. Simpler, nothing internet-
   facing, and matches how backend already reaches other internal services.

2. **`OPENAI_API_KEY` sourcing: a new, dedicated per-app secret.** Create
   `eval-api-configuration` the same way `backend-uk-api-configuration` was
   created, and seed it with a **copy** of the same key value backend uses
   today. This matches the account's established one-secret-per-app
   convention (no app reads another app's secret anywhere in this account
   today) and — per Hadleigh — the eval app is expected to get its own,
   distinct OpenAI key in the future anyway, so a dedicated secret avoids
   having to re-plumb this later.

3. **Environment scope: development only, for now.** The OIDC trust-policy
   entry and the new secret both live in per-environment Terraform (dev,
   staging, and production are separate AWS accounts with independent
   state). Only development is built here; staging/production are recorded
   as follow-up work, done once development is proven end-to-end (see
   below).

4. **Fixing the Dockerfile mismatch: replace the root `Dockerfile`.**
   Rather than extend `trade-tariff-tools`' shared `build-and-push` action
   with a configurable Dockerfile path (which would touch a workflow every
   other app's deploy pipeline also depends on), make
   `apps/classification-evals/Dockerfile`'s content the repo's root
   `Dockerfile`, and delete `apps/deployment/` (the placeholder app and its
   tests) entirely — its only purpose was to prove the pipeline mechanics
   before the real app existed, and that job is done once this ships. This
   is the smallest possible fix and needs no change outside this repo.

5. **TLS: match the account-wide convention.** Every other ECS service in
   this account terminates TLS itself using the shared self-signed cert
   (`SSL_CERT_PEM`/`SSL_KEY_PEM`, from the existing `ecs-tls-certificate`
   secret this app's Terraform already reads). Add the same to the real
   eval app rather than leave it as the one plain-HTTP service in the
   account. `container_port` stays 8443, matching what `terraform/main.tf`
   already assumes — no Terraform port change needed, only the
   Dockerfile/app-side change.

## Changes required

### `ai-search-evaluation-suite` (this repo)

- **Root `Dockerfile`**: replace with `apps/classification-evals/Dockerfile`'s
  content (adjusting any repo-root-relative `COPY` paths as needed since it
  moves up a directory level).
- **Delete `apps/deployment/`** (placeholder app + its tests). No change
  needed to `ci.yml`'s existing `docker build --tag
  ai-search-evaluation-suite:test .` step — it already builds root context,
  so once the root `Dockerfile` is the real app, that step starts
  build-validating the actual deployed image in CI instead of the
  placeholder, which is a net improvement with no extra work.
- **Add TLS termination to the real app's startup path**: read
  `SSL_CERT_PEM`/`SSL_KEY_PEM` from the environment, write them to a temp
  dir, and pass `--ssl-keyfile`/`--ssl-certfile` to `uvicorn` (mirroring
  what `apps/deployment/app.py`'s `create_server` did for the placeholder).
  Port changes from 8100 to 8443 in the Docker `CMD`.
- **`terraform/data.tf`**:
  - Remove `data "aws_lb_target_group" "this"` (no longer needed).
  - Add `data "aws_secretsmanager_secret" "eval_api_configuration"` and
    `data "aws_secretsmanager_secret_version" "eval_api_configuration"`,
    matching the `ecs_tls_certificate` pattern already in this file.
- **`terraform/iam.tf`**: add a `secretsmanager:GetSecretValue` (etc.,
  matching the existing statement shape for the TLS secret) statement
  scoped to the new secret's ARN.
- **`terraform/locals.tf`**: decode the new secret's JSON, and concat its
  env vars — plus an explicit `TRADE_TARIFF_BACKEND_BASE_URL` entry — into
  `service_environment`, the same way `trade-tariff-backend`'s
  `backend_uk_service_env_vars` concats `backend_uk_secret_env_vars` with
  `api_service_env_vars`.
- **`terraform/main.tf`**: remove the `target_group_arn` line.

### `trade-tariff-platform-aws-terraform`, development only

- ~~`modules/ecr/locals.tf`: add an `"ai-search-evaluation-suite"` entry~~ —
  already done (`3b2706f`, AI-1064). No change needed.
- ~~`environments/development/common/iam-roles.tf`: add the repo to the
  trust policy~~ — already done (`3b2706f`, AI-1064). No change needed.
- **`environments/development/common/secrets.tf`**: add an
  `eval_api_configuration` module block, matching the shape of
  `backend_uk_api_configuration`. After apply, hand-populate the secret's
  value with `{"OPENAI_API_KEY": "<copy of backend's key>"}` — same manual
  step every other app's configuration secret already required (values are
  never in Terraform state in this account).
- **`environments/development/common/alb.tf`**: remove the `ai_eval` block
  (added by `3b2706f`, produces the now-unwanted `ai-eval-https` target
  group). Development only for now — see follow-up below for
  staging/production.

## Data flow

**Triggering a run (backend → eval):** backend's Rails app (or a developer,
via the console) sends a request to `POST eval.tariff.internal:8443/api/jobs`.
No new code needed on the eval side — this route already exists.

**Running the evaluation (eval → backend):** `TradeTariffBackendClient`
(AI-1073) calls `TRADE_TARIFF_BACKEND_BASE_URL` (set to
`https://backend-uk.tariff.internal:8443` in eval's ECS env vars) for gold
queries, ATaR rulings, and posting results back — exactly the flow already
built and tested, just pointed at a real internal address instead of
`localhost`.

Both directions cross the same shared security group, which already
permits them.

## Testing / validation plan

1. Deploy to development via `workflow_dispatch` and confirm the ECS
   service reaches steady state (container health check passing).
2. From inside the VPC (ECS Exec into the `backend-uk` task), `curl
   https://eval.tariff.internal:8443/api/health` and confirm a response.
3. Trigger a run from backend (or manually via ECS Exec into eval) and
   confirm eval's calls back into backend (`TradeTariffBackendClient`)
   succeed against the real backend service, not `localhost`.
4. Confirm `OPENAI_API_KEY` is actually present in the running task's
   environment (`/api/health`'s existing `openai_key_present` field) without
   ever appearing in Terraform state, CI logs, or the repo.

## Staging/production follow-up (not built now)

Once development is proven end-to-end, the following
`trade-tariff-platform-aws-terraform` changes need repeating in
`environments/staging/common/` and `environments/production/common/`:

- ~~Add the `ai-search-evaluation-suite` repo to the OIDC trust policy~~ —
  **correction, 2026-08-28:** already done for all three environments by
  `3b2706f` (AI-1064). Confirmed by reading `iam-roles.tf` directly in
  staging and production, not just development. Nothing to do here.
- Add an `eval_api_configuration` secret module block to each environment's
  `secrets.tf`, then hand-populate it (a separate OpenAI key per
  environment, or a copy of the same one — worth revisiting given the
  design decision above already anticipates a dedicated eval key
  eventually).
- Remove the `ai_eval` block from `alb.tf` in each environment — same
  orphaned-target-group cleanup as development, just not done yet because
  staging/production are out of scope for this round of work.
- The ECR repo entry (`modules/ecr/locals.tf`) is already shared across all
  three environments (also `3b2706f`), so nothing further needed there.
- `ai-search-evaluation-suite`'s own `deploy-to-staging.yml` /
  `deploy-to-production.yml` workflows already exist and follow the same
  `deploy-ecs.yml` pattern — no changes anticipated there beyond what
  development proves out.
