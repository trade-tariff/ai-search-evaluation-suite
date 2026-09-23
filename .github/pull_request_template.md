## What:
<!-- A brief description of what this PR does -->

## Why:
<!-- The reasoning or context behind this change -->

## Ticket:
<!-- Link to the relevant Jira/ticket, or 'N/A' if not applicable -->

## Risk:
**Risk level:** 🟢 / 🟠 / 🔴 <!-- delete as appropriate -->

**Reason for rating:**
<!-- One or two sentences explaining your assessment, especially for Amber or Red -->

───────────────────────────────────────────────────

Rate the overall risk of deploying this change:

🟢 Green  – Low risk. Good to go, standard review applies.

🟠 Amber  – Medium risk. Socialise with the team before merging.

🔴 Red    – High risk. Requires explicit approval from Thor or Neil before merging.

───────────────────────────────────────────────────

This evaluation app is unused. It does not serve the live Trade Tariff service. A change to this app is low risk unless it rotates secrets, destroys data, or cannot be rolled back. CI/CD steps, container health checks, logs, and image layout for this app are low risk.

🟢 GREEN – things that are typically low risk:
───────────────────────────────────────────────────
- Changes to this unused evaluation app, including its CI/CD steps, container health check, logs, and image layout
- Dependency bumps with no API changes (e.g. minor/patch packages)
- Copy or documentation changes
- Adding or updating CloudWatch alarms or dashboards (read-only observability)
- New tests or improved test coverage with no production code changes
- Config/env var additions that are purely additive and have safe defaults
- Refactors with full test coverage and no behaviour change
- Terraform formatting or variable renaming with no resource recreation
- S3 lifecycle rule additions (non-destructive, time-delayed effect)

🟠 AMBER – things that need a team conversation first:
───────────────────────────────────────────────────
- Changes to evaluation behaviour, scoring, or experiment configuration
- New or modified API endpoints consumed by other services
- Adding or changing feature flags that affect live user journeys
- Infrastructure changes that alter networking, security groups, or IAM permissions in non-production first
- Terraform changes that will cause a resource replacement (check plan output carefully)
- Changes to CI/CD pipeline steps or deployment order dependencies
- S3 bucket policy or access control changes
- Removing or deprecating an endpoint or field that may still be consumed

🔴 RED – requires explicit approval from Thor or Neil:
───────────────────────────────────────────────────
- Dangerous database migrations, especially destructive ones (dropping columns/tables, removing indexes)
- Any change to production AWS infrastructure that cannot be easily rolled back
- Secrets rotation or changes to how credentials are stored, scoped, or accessed
- Changes that affect trader-facing regulatory content or legally significant data
- Significant architectural shifts (e.g. new service boundaries, queue/event topology changes)
