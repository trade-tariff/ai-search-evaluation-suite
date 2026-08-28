# Eval App ECS Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the `ai-search-evaluation-suite` eval app actually deployable to ECS in the `development` AWS account, running the real classification-evals app (not a placeholder), reachable from `trade-tariff-backend` over the internal network in both directions.

**Architecture:** Four independent-as-possible phases, each its own PR against a different piece of the system: (1) the missing AWS-account-level prerequisite (a secret) plus removing an orphaned public ALB route, in `trade-tariff-platform-aws-terraform`; (2) replacing the placeholder Docker image with the real app, with TLS added to match every other service in the account; (3) wiring the eval app's own Terraform to the new secret and the real backend URL; (4) the actual deploy and end-to-end validation. Phase 3 has a hard dependency on Phase 1 (it references the secret Phase 1 creates). Phases 1 and 2 are independent of each other and can be built in either order or in parallel. Phase 4 depends on 1, 2, and 3 all being merged.

**Tech Stack:** Terraform for AWS infra — Terragrunt-wrapped in `trade-tariff-platform-aws-terraform` (Phase 1), plain Terraform with an S3 backend in `ai-search-evaluation-suite`'s own `terraform/` (Phase 3) — Docker for the eval app image, FastAPI/uvicorn for the app itself, GitHub Actions (`trade-tariff-tools`' shared `deploy-ecs.yml` workflow) for CI/CD.

**Spec:** `docs/superpowers/specs/2026-08-26-eval-app-ecs-deployment-design.md` (this repo, branch `eval-ecs-deployment-design`, as corrected in commit `d37026d`) — read it in full before starting. This plan implements that spec's "Changes required" section, adjusted for two things the spec got wrong (see below).

## Global Constraints

- **Development only.** Do not touch staging or production Terraform in any phase. The spec's "Staging/production follow-up" section is deferred, explicit follow-up work — not part of this plan.
- **Never commit a real secret value.** `OPENAI_API_KEY`'s actual value is hand-populated into Secrets Manager after `terraform apply`, exactly like every other per-app secret in this account. It must never appear in Terraform state, a commit, a PR description, or a CI log.
- **`trade-tariff-platform-aws-terraform` auto-applies to development on PR open.** Per that repo's own README: "Open a Pull Request with your changes. This will deploy the feature over the development environment to proof that `terraform apply` runs without failure." This means Phase 1's PR is not passively reviewable — opening it changes real AWS infrastructure in the development account automatically, before any human approves the code. Flag this to Hadleigh before opening that PR; do not open it silently.
- **Two corrections to the spec, already verified against current `main` of both repos (see spec commit `d37026d` for full detail):**
  1. The ECR repo entry and GitHub OIDC deploy permission the spec said were "missing" already exist (shipped by an earlier commit, `3b2706f`, AI-1064, 2026-07-21, confirmed present in development, staging, *and* production). **Do not re-add them or touch `modules/ecr/locals.tf` / `iam-roles.tf` in any environment.**
  2. That same commit also added a public ALB route for eval (`ai_eval` block in `alb.tf`, all three environments) that the spec's own design explicitly rules out (internal-only, no public route). Phase 1 removes it in development; staging/production removal is noted as follow-up, not built here.
- **Pre-commit hooks:** both repos run `pre-commit` including a `trufflehog` hook that fails locally with an SSL cert error installing its Go toolchain (known, pre-existing, unrelated to this work). Use `SKIP=trufflehog git commit -m "..."` — never `--no-verify`, which would skip every hook.

---

## Phase 1 — AI-1235: AWS prerequisites (`trade-tariff-platform-aws-terraform`)

**Repo:** `/Users/Hadleigh.Wallenberg/Documents/ROR/trade-tariff-platform-aws-terraform`
**Branch:** `AI-1235-eval-aws-prerequisites` off `main`
**PR:** standalone, `low-risk`/Green — additive secret shell (no value in state) + removal of an unused ALB rule with nothing currently attached to it.

### Task 1.1: Add the `eval_api_configuration` secret

**Files:**
- Modify: `environments/development/common/secrets.tf`

**Interfaces:**
- Produces: a Secrets Manager secret named `eval-api-configuration` (empty shell — no `secret_string` set in Terraform, matching every other per-app configuration secret in this file except `ecs_tls_certificate`). Phase 3 will reference this secret by name (`data "aws_secretsmanager_secret" "eval_api_configuration" { name = "eval-api-configuration" }`), so the name here must match exactly.

- [ ] **Step 1: Add the module block**

Add this immediately after the `backend_job_configuration` block (keeps it grouped with the other per-app configuration secrets, before the "Other non-configuration secrets" comment):

```hcl
module "eval_api_configuration" {
  source          = "../../../modules/secret/"
  name            = "eval-api-configuration"
  kms_key_arn     = aws_kms_key.secretsmanager_kms_key.arn
  recovery_window = local.development_secret_recovery_window
}
```

This mirrors `backend_uk_api_configuration` exactly (same module source, same `kms_key_arn`/`recovery_window` locals already defined elsewhere in this file — no new locals needed).

- [ ] **Step 2: Format and validate**

Run: `terraform fmt -recursive environments/development/common/`
Expected: no changes (already correctly formatted) or auto-formats cleanly.

Run (from `environments/development/common/`): `terragrunt init -upgrade=false` then `terragrunt validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add environments/development/common/secrets.tf
SKIP=trufflehog git commit -m "feat(secrets): add eval-api-configuration secret shell for development

AI-1235: the eval app's own Terraform (AI-1237) will read this secret
for its OPENAI_API_KEY. Value is hand-populated after apply, never in
state."
```

### Task 1.2: Remove the orphaned `ai_eval` ALB route

**Files:**
- Modify: `environments/development/common/alb.tf`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing — this is a removal. After this task, `data.aws_lb_target_group.this` (name `ai-eval-https`) in the eval app's own repo will resolve to a target group that no longer exists. That's expected and is fixed by Phase 3, which deletes that data source. **Do not merge this phase's PR after Phase 3 without also deploying Phase 3** — or do merge it, but be aware `terraform plan` for the eval app's own repo will start failing (missing data source) until Phase 3 lands. Since these are two separate repos with independent state, there's no hard ordering enforced by Terraform itself; this is a process note, not a blocker.

- [ ] **Step 1: Remove the block**

In the `services` map inside the `module "alb"` block, delete this entire entry (currently sits between `mcp` and `frontend`):

```hcl
    ai_eval = {
      hosts            = ["eval.*"]
      healthcheck_path = "/healthcheckz"
      priority         = 27
    }

```

Leave every other entry (including `frontend`'s `priority = 99` comment) untouched. Don't renumber any other service's `priority` — they're independent per-service values, not a contiguous sequence.

- [ ] **Step 2: Format and validate**

Run: `terraform fmt -recursive environments/development/common/`

Run (from `environments/development/common/`): `terragrunt validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add environments/development/common/alb.tf
SKIP=trufflehog git commit -m "fix(alb): remove orphaned public route for eval in development

AI-1235: this repo's own design decision is internal-only ingress for
eval (no public ALB route) - see docs/superpowers/specs/2026-08-26-
eval-app-ecs-deployment-design.md in ai-search-evaluation-suite. This
block was added by an earlier commit before that decision was made and
was never removed."
```

### Task 1.3: Open the PR

- [ ] **Step 1: Push and confirm scope with Hadleigh before opening**

```bash
git push -u origin AI-1235-eval-aws-prerequisites
```

Before running `gh pr create`: tell Hadleigh explicitly that opening this PR triggers a real `terraform apply` against the development AWS account (per this repo's README), and confirm he wants that to happen now.

- [ ] **Step 2: Open the PR**

Use this repo's standard PR template (What/Why/Ticket/Risk). Risk: 🟢 Green — additive empty secret shell (no value, no app depends on it yet) plus removing an ALB rule with nothing currently attached (eval isn't deployed yet, so nothing routes through it today). Ticket: `[AI-1235](https://transformuk.atlassian.net/browse/AI-1235)`.

- [ ] **Step 3: Confirm the automatic development apply succeeds**

Watch the PR's CI check (the reusable `deploy-ecs.yml`-style workflow this repo runs on PR). Confirm `terraform apply` completes without error before merging.

---

## Phase 2 — AI-1236: Replace the placeholder image with the real app (`ai-search-evaluation-suite`)

**Repo:** `/Users/Hadleigh.Wallenberg/Documents/python/ai-search-evaluation-suite`
**Branch:** `AI-1236-real-eval-deploy-image` off `main`
**PR:** standalone, `low-risk`/Green — only affects what gets deployed for a service not yet live in any environment.

This phase supersedes the already-merged PR #8 (commit `ab8a79d`, "drop unused pip and patch OpenSSL to clear Trivy findings") — that fix patched the placeholder's root `Dockerfile`, which this phase deletes outright. No conflict expected; it just becomes moot once this merges.

### Task 2.1: Add a TLS-terminating entrypoint script for the container

**Files:**
- Create: `apps/classification-evals/docker-entrypoint.sh`

**Interfaces:**
- Consumes: `SSL_CERT_PEM` and `SSL_KEY_PEM` environment variables (PEM-encoded cert and private key; both required, sourced from the existing `ecs-tls-certificate` secret via `terraform/locals.tf` — already wired, no Terraform change needed for this).
- Produces: execs `uvicorn` bound to `0.0.0.0:8443` with TLS terminated using those files. This is the container's actual startup command once wired into the Dockerfile in Task 2.3.

Uses uvicorn's native `--ssl-keyfile`/`--ssl-certfile` flags rather than hand-rolling a Python `ssl.SSLContext` wrapper (the approach the placeholder's bare `http.server` needed, because it had no built-in TLS support) — uvicorn already does this natively, so a shell wrapper that just materializes the cert/key to disk is the smallest correct fix.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${SSL_CERT_PEM:?SSL_CERT_PEM is required}"
: "${SSL_KEY_PEM:?SSL_KEY_PEM is required}"

CERT_DIR="$(mktemp -d)"
trap 'rm -rf "$CERT_DIR"' EXIT

printf '%s' "$SSL_CERT_PEM" > "$CERT_DIR/certificate.pem"
printf '%s' "$SSL_KEY_PEM" > "$CERT_DIR/private-key.pem"
chmod 600 "$CERT_DIR/certificate.pem" "$CERT_DIR/private-key.pem"

exec uvicorn backend.app:app \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-keyfile "$CERT_DIR/private-key.pem" \
  --ssl-certfile "$CERT_DIR/certificate.pem"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x apps/classification-evals/docker-entrypoint.sh
```

- [ ] **Step 3: Commit**

```bash
git add apps/classification-evals/docker-entrypoint.sh
SKIP=trufflehog git commit -m "feat(classification-evals): add TLS-terminating container entrypoint

AI-1236: every other ECS service in this account terminates TLS
itself using the shared ecs-tls-certificate secret. uvicorn supports
this natively via --ssl-keyfile/--ssl-certfile; this script just
writes the env-provided PEM content to disk first."
```

### Task 2.2: Point the root Dockerfile at the real app

**Files:**
- Modify: `Dockerfile` (repo root)
- Delete: `apps/deployment/` (directory: `__init__.py`, `app.py`, `__pycache__/`)
- Delete: `tests/test_deployment_app.py` if it exists (check first — the placeholder may have had its own test file outside `apps/deployment/`)

**Interfaces:**
- Consumes: `apps/classification-evals/docker-entrypoint.sh` from Task 2.1.
- Produces: the repo-root `Dockerfile` that `trade-tariff-tools`' shared `build-and-push` action builds (`docker build .` from repo root, no `-f` override — this is why the root `Dockerfile` has to *be* the real app rather than reference `apps/classification-evals/Dockerfile` some other way). Listens on 8443 with TLS. This is what CI's `ci.yml` step (`docker build --tag ai-search-evaluation-suite:test .`) will validate going forward — no change needed to `ci.yml` itself, it already builds root context.

- [ ] **Step 1: Check for a placeholder test file outside `apps/deployment/`**

```bash
find . -iname "*test*deployment*" -o -iname "*test*placeholder*" 2>/dev/null | grep -v node_modules
```

If anything turns up outside `apps/deployment/` itself, note it for deletion in Step 3.

- [ ] **Step 2: Replace the root `Dockerfile`'s content**

Replace the entire contents of the repo-root `Dockerfile` with `apps/classification-evals/Dockerfile`'s content, with these three changes on top (all paths inside stay root-relative and need no adjustment — confirmed by reading the file directly, every `COPY` source is already written as `apps/product/...` / `apps/classification-evals/...` relative to repo root, not relative to the `apps/classification-evals/` directory):

1. `EXPOSE 8100` → `EXPOSE 8443`
2. Add a `HEALTHCHECK` instruction (the current `apps/classification-evals/Dockerfile` has none — it's only ever been built standalone for local/CI use, not deployed). The real app's health route is `/api/health` (confirmed by reading `apps/classification-evals/backend/app.py:464` — **not** `/healthcheckz`, which is what the placeholder used and what the now-removed ALB rule pointed at). Add, right after the existing `RUN mkdir -p ... && useradd ... && chown -R ...` block and before `WORKDIR .../apps/classification-evals`:
   ```dockerfile
   HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
     CMD ["python", "-c", "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/api/health', context=ssl._create_unverified_context()).read()"]
   ```
   (`python`, not `python3` — matches the exact invocation the placeholder's own `HEALTHCHECK` used; both resolve to the same interpreter in this base image, no functional difference, just consistency.)
3. `CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8100"]` → `CMD ["./docker-entrypoint.sh"]` (the `WORKDIR` is already `/srv/ai-search-evaluation-suite/apps/classification-evals` at this point in the file, and that directory is already copied wholesale via the existing `COPY apps/classification-evals apps/classification-evals` line, so `docker-entrypoint.sh` from Task 2.1 is already present in the image — no new `COPY` needed, just make sure it's executable in the image too, since `chmod +x` on the host doesn't always survive `COPY` depending on how the file was created):
   ```dockerfile
   RUN chmod +x docker-entrypoint.sh
   ```
   Add this line immediately before the `CMD` line.

The full resulting root `Dockerfile` should read exactly like the current `apps/classification-evals/Dockerfile`, with those three changes applied.

- [ ] **Step 3: Delete the placeholder**

```bash
git rm -r apps/deployment
# plus any test file(s) found in Step 1, e.g.:
# git rm tests/test_deployment_app.py
```

- [ ] **Step 4: Build and smoke-test locally**

```bash
docker build -t eval-app-real-image .
```
Expected: builds successfully (this is a `pip install`-based Debian image, not Alpine/`apk` — no known local network issues with this build, unlike the Alpine-based backend Dockerfile).

```bash
docker run --rm -e SSL_CERT_PEM="$(openssl req -x509 -newkey rsa:2048 -nodes -keyout /dev/stdout -out /dev/stdout -days 1 -subj '/CN=localhost' 2>/dev/null | awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/')" \
  -e SSL_KEY_PEM="$(openssl req -x509 -newkey rsa:2048 -nodes -keyout /dev/stdout -out /dev/null -days 1 -subj '/CN=localhost' 2>/dev/null | awk '/BEGIN PRIVATE KEY/,/END PRIVATE KEY/')" \
  -p 18443:8443 eval-app-real-image &
sleep 3
curl -sk https://127.0.0.1:18443/api/health
```
Expected: a JSON response from the health endpoint (confirms uvicorn started, TLS handshake succeeded, and the app's own routing works). Stop the container afterward (`docker stop` on the container ID, or `kill %1` if backgrounded via `&`).

If generating a throwaway self-signed cert/key pair inline is awkward, an alternative: write a tiny local script that runs `openssl req` once to two files, `export SSL_CERT_PEM=$(cat cert.pem)` / `export SSL_KEY_PEM=$(cat key.pem)`, then `docker run -e SSL_CERT_PEM -e SSL_KEY_PEM ...`. Either way, the goal of this step is confirming the entrypoint script's cert-materialization and uvicorn's TLS startup both work before this ever reaches ECS.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile
git add -A apps/deployment  # stages the deletion
SKIP=trufflehog git commit -m "fix(deploy): build and run the real classification-evals app

AI-1236: the deploy pipeline (trade-tariff-tools' build-and-push
action) always builds the repo-root Dockerfile with no way to
override the path. That Dockerfile was a placeholder health-check
server left over from proving the pipeline mechanics before the real
app existed - a deploy would succeed while running the wrong
container entirely. This makes the root Dockerfile the real app,
listening on 8443 with TLS (matching every other service in this
account) instead of plain HTTP on 8100, and removes the now-obsolete
placeholder."
```

### Task 2.3: Open the PR

- [ ] **Step 1: Push and open**

```bash
git push -u origin AI-1236-real-eval-deploy-image
```

Risk: 🟢 Green — this repo's own template criteria list "dependency bumps with no API changes" and "refactors with full test coverage" as green; this is closer to "fixing a deploy artifact that was never actually exercised in production" since eval isn't deployed anywhere yet. No trader-facing or live-journey impact. Ticket: `[AI-1236](https://transformuk.atlassian.net/browse/AI-1236)`.

- [ ] **Step 2: Confirm CI's existing `docker build --tag ai-search-evaluation-suite:test .` step in `ci.yml` passes**

No change to `ci.yml` needed — it already builds root context, so it now build-validates the real image automatically.

---

## Phase 3 — AI-1237: Wire the eval app's own Terraform (`ai-search-evaluation-suite`)

**Repo:** `/Users/Hadleigh.Wallenberg/Documents/python/ai-search-evaluation-suite`
**Branch:** `AI-1237-eval-terraform-wiring` off `main` (branch from `main` after Phase 2 merges, or branch now and rebase before opening the PR — either is fine since this phase's files don't overlap with Phase 2's)
**PR:** standalone. **Depends on Phase 1 being merged and applied** — this phase's `terraform plan` will fail (missing secret) until the `eval-api-configuration` secret exists in development.
**Risk:** 🟠 Amber, not Green — per this repo's own template, this is a "new or modified API endpoint... infrastructure changes that alter networking" category in spirit (it changes how the service is reachable — removing the only ingress path it currently has and replacing it with none, i.e. internal-only). Socialise briefly before merging, per the repo's Amber criteria.

### Task 3.1: Point `terraform/data.tf` at the new secret, drop the ALB reference

**Files:**
- Modify: `terraform/data.tf`

**Interfaces:**
- Consumes: the `eval-api-configuration` secret created in Phase 1 (Task 1.1) — by name, so this task only works correctly once that secret exists in the development account.
- Produces: `data.aws_secretsmanager_secret_version.eval_api_configuration.secret_string`, consumed by Task 3.3.

- [ ] **Step 1: Remove the ALB target group data source**

Delete this block entirely:
```hcl
data "aws_lb_target_group" "this" {
  name = "ai-eval-https"
}
```

- [ ] **Step 2: Add the new secret data sources**

Add, in the same style as the existing `ecs_tls_certificate` block immediately below it:
```hcl
data "aws_secretsmanager_secret" "eval_api_configuration" {
  name = "eval-api-configuration"
}

data "aws_secretsmanager_secret_version" "eval_api_configuration" {
  secret_id = data.aws_secretsmanager_secret.eval_api_configuration.id
}
```

- [ ] **Step 3: Format and validate**

This app's own `terraform/` directory uses plain Terraform with an S3 backend (`terraform/backends/*.tfbackend`) — **not** Terragrunt, unlike `trade-tariff-platform-aws-terraform`. Confirmed via `terraform/versions.tf` (`backend "s3" {}`) and `terraform/backends/development.tfbackend`.

```bash
cd terraform
terraform fmt -recursive .
terraform init -backend-config=backends/development.tfbackend -reconfigure
terraform validate
```
Expected: `Success! The configuration is valid.` Note this only fully `plan`s clean once Phase 1's secret exists in development — `validate` (syntax/type-checking only, no AWS calls) should pass regardless; a full `terraform plan` needs Phase 1 merged first.

- [ ] **Step 4: Update the stale terraform README**

`terraform/README.md` currently describes the placeholder ("Deploys the backend-only `eval` placeholder service... serves `OKAY` from `/` and `/healthcheckz`"). Replace its first paragraph with something accurate now that the ALB/placeholder references are gone:

```markdown
# AI Search Evaluation Suite Terraform

Deploys the `ai-search-evaluation-suite` classification-evals app to the shared Trade Tariff ECS platform. The service registers as `eval.tariff.internal` for private service-to-service access only (no public route). It reads its OpenAI API key from the `eval-api-configuration` Secrets Manager secret and the shared in-container TLS certificate, same as every other service in this account.
```

Leave the second paragraph (about `.ruby-version` existing only for the shared deploy workflow's compatibility) unchanged — still accurate.

- [ ] **Step 5: Commit**

```bash
git add terraform/data.tf terraform/README.md
SKIP=trufflehog git commit -m "feat(terraform): read the new eval secret instead of the removed ALB route

AI-1237: eval is internal-only (no public ALB route, see AI-1235),
and needs its own OPENAI_API_KEY the same way every other app in this
account gets one - a per-app Secrets Manager secret, value never in
state. Also updates the terraform README, which still described the
placeholder service this replaces."
```

### Task 3.2: Grant the execution role access to the new secret

**Files:**
- Modify: `terraform/iam.tf`

**Interfaces:**
- Consumes: `data.aws_secretsmanager_secret.eval_api_configuration.arn` (Task 3.1).
- Produces: nothing new consumed elsewhere — this is purely a permissions grant.

- [ ] **Step 1: Add a second `secretsmanager` statement**

In `data "aws_iam_policy_document" "execution"`, add a second `secretsmanager` statement (keep the existing one for `ecs_tls_certificate` as-is — this is additive, not a replacement, since the container still needs the TLS secret too):

```hcl
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:GetSecretValue",
      "secretsmanager:ListSecretVersionIds",
    ]
    resources = [data.aws_secretsmanager_secret.eval_api_configuration.arn]
  }
```

- [ ] **Step 2: Format and validate**

```bash
cd terraform
terraform fmt -recursive .
terraform validate
```
(No need to re-run `terraform init` if Task 3.1's Step 3 already ran it in this working copy.)

- [ ] **Step 3: Commit**

```bash
git add terraform/iam.tf
SKIP=trufflehog git commit -m "feat(iam): grant the eval execution role access to its new secret

AI-1237: additive alongside the existing ecs-tls-certificate grant -
the task still needs both secrets."
```

### Task 3.3: Wire the secret's values and the backend URL into the container's env vars

**Files:**
- Modify: `terraform/locals.tf`

**Interfaces:**
- Consumes: `data.aws_secretsmanager_secret_version.eval_api_configuration.secret_string` (Task 3.1).
- Produces: `local.service_environment` — a list of `{name, value}` objects, consumed by `terraform/main.tf`'s `service_environment_config = local.service_environment` (already wired, no change needed in `main.tf` for this — see Task 3.4 for `main.tf`'s only actual change).

- [ ] **Step 1: Decode the secret and add the env vars**

Replace the current `locals` block:
```hcl
locals {
  tls_secret = jsondecode(data.aws_secretsmanager_secret_version.ecs_tls_certificate.secret_string)

  service_environment = [
    {
      name  = "SSL_KEY_PEM"
      value = local.tls_secret.private_key
    },
    {
      name  = "SSL_CERT_PEM"
      value = local.tls_secret.certificate
    },
    {
      name  = "SSL_PORT"
      value = "8443"
    },
  ]
}
```

with:
```hcl
locals {
  tls_secret = jsondecode(data.aws_secretsmanager_secret_version.ecs_tls_certificate.secret_string)

  tls_env_vars = [
    {
      name  = "SSL_KEY_PEM"
      value = local.tls_secret.private_key
    },
    {
      name  = "SSL_CERT_PEM"
      value = local.tls_secret.certificate
    },
    {
      name  = "SSL_PORT"
      value = "8443"
    },
  ]

  eval_secret_value = try(data.aws_secretsmanager_secret_version.eval_api_configuration.secret_string, "{}")
  eval_secret_map   = jsondecode(local.eval_secret_value)
  eval_secret_env_vars = [
    for key, value in local.eval_secret_map : {
      name  = key
      value = value
    }
  ]

  backend_url_env_vars = [
    {
      name  = "TRADE_TARIFF_BACKEND_BASE_URL"
      value = "https://backend-uk.tariff.internal:8443"
    },
  ]

  service_environment = concat(local.eval_secret_env_vars, local.tls_env_vars, local.backend_url_env_vars)
}
```

This mirrors `trade-tariff-backend`'s own `terraform/locals.tf` pattern exactly (`backend_uk_secret_value` → `backend_uk_secret_map` → `backend_uk_secret_env_vars` → `concat(...)` into the service's final env var list) — same shape, same `try(..., "{}")` guard so `terraform plan` doesn't fail if the secret's value hasn't been hand-populated yet.

`TRADE_TARIFF_BACKEND_BASE_URL` is read by `TradeTariffBackendClient` (built in AI-1073) — confirmed by reading its source, it already defaults to `http://127.0.0.1:3000` for local dev and takes this env var with no code change needed on the eval side.

- [ ] **Step 2: Format and validate**

```bash
cd terraform
terraform fmt -recursive .
terraform validate
```
(No need to re-run `terraform init` if Task 3.1's Step 3 already ran it in this working copy.)

- [ ] **Step 3: Commit**

```bash
git add terraform/locals.tf
SKIP=trufflehog git commit -m "feat(terraform): pass OPENAI_API_KEY and the real backend URL to the container

AI-1237: OPENAI_API_KEY comes from the new eval-api-configuration
secret (AI-1235); TRADE_TARIFF_BACKEND_BASE_URL points
TradeTariffBackendClient (AI-1073) at backend's real internal address
instead of localhost, which was only ever a local-dev default."
```

### Task 3.4: Drop the target group attachment

**Files:**
- Modify: `terraform/main.tf`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing — pure removal, mirrors Task 1.2's ALB removal on the platform-repo side.

- [ ] **Step 1: Remove the line**

Delete:
```hcl
  target_group_arn = data.aws_lb_target_group.this.arn
```
from the `module "service"` block. The `ecs-service` module treats `target_group_arn` as optional (defaults to `null`; the load-balancer attachment block is skipped entirely when unset — confirmed in the spec's investigation), so no other change is needed in this file.

- [ ] **Step 2: Format and validate**

```bash
cd terraform
terraform fmt -recursive .
terraform validate
```
(No need to re-run `terraform init` if Task 3.1's Step 3 already ran it in this working copy.)

- [ ] **Step 3: Commit**

```bash
git add terraform/main.tf
SKIP=trufflehog git commit -m "fix(terraform): stop attaching eval to the removed ALB target group

AI-1237: pairs with AI-1235's removal of the ai_eval ALB route -
eval is internal-only, reachable via Cloud Map DNS
(eval.tariff.internal) rather than a public route."
```

### Task 3.5: Open the PR

- [ ] **Step 1: Confirm Phase 1 has merged and applied successfully first**

Check the `eval-api-configuration` secret exists in the development account (via the AWS console, or `aws secretsmanager describe-secret --secret-id eval-api-configuration` if you have development credentials configured) before opening this PR — otherwise `terraform plan` for this repo will fail on the missing secret.

- [ ] **Step 2: Push and open**

```bash
git push -u origin AI-1237-eval-terraform-wiring
```

Risk: 🟠 Amber (see rationale above). Ticket: `[AI-1237](https://transformuk.atlassian.net/browse/AI-1237)`. In the Why section, explicitly note the dependency on AI-1235 having merged first.

---

## Phase 4 — AI-1238: Deploy to development and validate

**Scope:** spans both repos. Primarily manual verification against real AWS infrastructure, not code — there's no meaningful way to "write a failing test first" for a live ECS deploy. Do this only after Phases 1, 2, and 3 have all merged.

- [ ] **Step 1: Hand-populate the new secret's value**

Via the AWS console (Secrets Manager, `eval-api-configuration`, development account) or CLI with development credentials — **never commit this value anywhere**:
```bash
aws secretsmanager put-secret-value \
  --secret-id eval-api-configuration \
  --secret-string '{"OPENAI_API_KEY":"<copy of backend'"'"'s current OpenAI key>"}'
```
Get the value to copy from wherever backend's current key is stored (its own `backend-uk-api-configuration` secret) — coordinate with Hadleigh on the exact source rather than guessing which of backend's env vars holds it.

- [ ] **Step 2: Trigger the deploy**

In GitHub, run `ai-search-evaluation-suite`'s `deploy-to-development.yml` via `workflow_dispatch` (already exists, already wired — confirmed in the spec's investigation, no new trigger needed).

- [ ] **Step 3: Confirm the ECS service reaches steady state**

In the AWS console (ECS, `trade-tariff-cluster-development`, `eval` service) or via CLI:
```bash
aws ecs describe-services --cluster trade-tariff-cluster-development --services eval \
  --query 'services[0].deployments[0].rolloutState'
```
Expected: `COMPLETED`, with the running task passing its Docker `HEALTHCHECK` (added in Phase 2, Task 2.2).

- [ ] **Step 4: Confirm backend can reach eval**

From inside the VPC — ECS Exec into a running `backend-uk` task:
```bash
aws ecs execute-command --cluster trade-tariff-cluster-development \
  --task <backend-uk-task-id> --container backend-uk --interactive \
  --command "curl -sk https://eval.tariff.internal:8443/api/health"
```
Expected: a JSON response from eval's health endpoint (`-k` because the shared TLS cert is self-signed — matches the pattern every other internal service-to-service call in this account already uses).

- [ ] **Step 5: Confirm eval can reach backend and complete a run**

Either trigger a real run from backend against eval (`POST eval.tariff.internal:8443/api/jobs`, per the spec's documented data flow — exact trigger mechanism depends on what's exposed via the admin console per AI-1234's original request), or ECS Exec into the `eval` task directly and manually exercise `TradeTariffBackendClient` against the real backend URL. Confirm gold queries are fetched and results post back successfully — the same flow already built and tested in AI-1073, just pointed at a real address instead of `localhost`.

- [ ] **Step 6: Confirm the OpenAI key is present without ever having touched the repo or CI logs**

```bash
aws ecs execute-command --cluster trade-tariff-cluster-development \
  --task <eval-task-id> --container eval --interactive \
  --command "curl -sk https://127.0.0.1:8443/api/health"
```
Expected: the response's existing `openai_key_present` field is `true`. Separately, grep recent CI logs and `git log` for either repo to confirm the key value itself never appeared anywhere in this process.

- [ ] **Step 7: Report back**

Once all four acceptance criteria from AI-1234's ticket are confirmed, comment on AI-1234 (and transition AI-1235–1238 to Done) summarizing what was verified, and note explicitly (per AI-1234's own acceptance criteria) that staging/production are deliberately deferred, listing the spec's "Staging/production follow-up" items as the next tickets to raise if/when that work is picked up.

---

## Self-Review Notes

- **Spec coverage:** every item in the corrected spec's "Changes required" section has a task above (Dockerfile replacement, TLS, secret creation, IAM grant, locals wiring, target-group removal on both sides, ALB route removal). The one addition beyond the spec's literal text — a `HEALTHCHECK` instruction using the correct `/api/health` path — is called out explicitly in Task 2.2 with its own rationale, since the spec didn't mention it and the real app's Dockerfile currently has none.
- **Type/name consistency checked:** `eval_api_configuration` is the data source name used consistently across `data.tf`, `iam.tf`, and `locals.tf` (Phase 3); `eval-api-configuration` (hyphenated) is the actual AWS secret name, matching Phase 1's Terraform resource and Task 3.1's data source `name` argument exactly.
- **No placeholders:** every code block above is complete, copy-pasteable content, not a description of what to write.
