# Path to Production - AI Search Evaluation Suite

Produced by the 2026-07-02 deep-dive audit: 31-agent workflow over the
deployed source (capability map, adversarially verified bloat audit, prod-gap
sweep) plus a live probe of every route on both deployed apps. Everything
below marked "verified" was checked against the running system, not inferred.

## 1. Where we are (verified)

- One EC2 host runs everything: workbench app (8100), journey app (8200),
  pgvector Postgres (uk + kg schemas incl. 105k fact embeddings), OpenSearch,
  Caddy TLS/basicauth edge on sslip.io wildcard DNS.
- Both apps are healthy; all read-only routes return 200; spend gates on the
  workbench work as designed (403 without allow_spend, $10 job cap).
- **Both images rebuild cleanly from the deployed trees** (verified 2026-07-02).
  Earlier "cannot rebuild" fears are false.
- The deployed source was NOT in version control: `/opt` trees are raw copies,
  edited live, with 35 `.bak*` snapshot files (~34k lines) as the only history.
  Two-way drift vs GitHub: 18 `classification_core` modules existed only on
  the VM (now captured); the repo has the kg schema SQL + experiment scripts
  the VM lacks.
- Branch `vm-sync-20260702` (this checkout) reconciles that: deployed state
  synced in, verified dead code removed (-1,284 lines), demo blockers fixed,
  and the full uk+kg schema baseline committed to `migrations/`.

## 2. P0 - do this week

1. **Push and adopt the sync branch.** `git push origin vm-sync-20260702`,
   review, merge. From then on the rule is: no edit lands on the VM except
   via git (deploy = fetch + rsync + compose build, per DEMO_PLAYBOOK
   section 2). Delete the `.bak*` files on the VM (archived in
   `~/backups/bak_archive_20260702.tgz` and locally).
2. **Rotate the journey OpenAI key and gitignore the journey tree.** The
   deployed journey tree has NO .gitignore and `.env.journey` contains the
   live key + DB password in plaintext; a naive git init + push would leak
   it. The key already exists on at least two machines - rotate it, then
   move runtime secrets to SSM (`fetch_runtime_secrets_from_ssm.sh` exists
   but is unwired).
3. **Backups.** Taken 2026-07-02 and stored off-host (laptop:
   `ai-fan-out/backups-20260702/`): product volumes tarball (includes the
   irreplaceable curated gold prompt library `search_contexts.json` and
   `config.json` - treat as secret), uk+kg schema-only dump, journey data
   dir. Still missing: a scheduled full pg dump (the kg fact embeddings are
   ~$37 of LLM spend to regenerate; uk schema has NO rebuild path except a
   snapshot) and an OpenSearch snapshot, both shipped to S3 on cron. Extend
   `export_eval_state.sh` to cover the two product volumes.
4. **Commit the schema baseline** (done on this branch:
   `migrations/000_baseline_uk_kg_schema.sql`, 187 CREATE TABLEs). This
   closes the "greenfield DB renders silently empty dashboards" trap - the
   app probes tables with `to_regclass` and hides errors.

## 3. P1 - before any external audience

1. **Consolidate spend guards.** Three coexisting systems: workbench
   `_require_workbench_spend`, classification_core `provider_guard`
   (subprocess env), journey's advisory in-memory day counter (resets on
   restart, estimates only). Frontend hardcodes `allow_spend: true` in three
   places (benchmark start, search preview, ATaR ingest), silently defeating
   the per-request gate. Target: one server-side policy - per-day USD budget
   per app, enforced (not advisory), with per-request opt-in on top.
   Q&A trials currently bypass the $10 job cap entirely.
2. **Activate the dormant in-app auth.** `auth.py` (bearer/basicauth
   middleware, hmac.compare_digest) is wired into all three apps but no env
   var activates it anywhere - the only wall is Caddy's single shared user.
   Setting `AI_FAN_OUT_BASIC_AUTH_*` in the suite `.env` is a free second
   layer. Real per-user auth + real DNS/TLS (not sslip.io) when this faces
   anyone external.
3. **Consolidate the journey deployment.** `/opt/ai-search-evaluation-suite-journey`
   (apps/full layout) duplicates the repo's `apps/product/backend/journey`
   package - verified byte-identical for the core modules today, but it will
   drift again. Rebuild journey-app from this repo's `Dockerfile.journey` and
   delete the parallel tree.
4. **Knowledge tab safety.** One-click DELETE on shared `kg.commodity_facets`
   / edges with no confirmation and no auth. Add confirm + role gate, or make
   the demo deployment read-only on kg writes.
5. **CI.** A single GitHub Actions workflow: build both images + `python -m
   compileall` + frontend build on PR. Deploy job (push to ECR + SSM command
   to the VM) can come later; the build check alone prevents the "edited
   live, image no longer builds" class of failure.

## 4. P2 - production shape

- Split the edge (ALB/CloudFront + ACM), move Postgres to RDS or at minimum
  EBS snapshots on schedule, OpenSearch to a managed domain or re-enable the
  security plugin (currently DISABLE_SECURITY_PLUGIN=true).
- One instance is a total SPOF (app + DB + search + edge). Acceptable for an
  eval tool; document the restore-from-zero path instead of buying HA:
  restore snapshot -> apply migrations -> `hydrate_opensearch_index.py
  --recreate` (rehearse once - it imports asyncpg, which is in requirements
  but was missing from the stale running container).
- Dependency pinning: both requirements.txt files are `>=` only; pin or lock.

## 5. Bloat ledger

Removed on this branch (adversarially verified - no imports, no Dockerfile
COPY, no doc references):

| Item | Size |
|---|---|
| `app.py index()` - orphaned pre-React inline HTML dashboard | 527 lines |
| `classification_core/recall_curve.py` + its Dockerfile COPY | ~250 lines |
| `classification_core/promote_llm_facts.py` | ~180 lines |
| `etl_seeders/seed_facets_parallel.py` (superseded by extraction_pipeline) | ~300 lines |
| Playwright config + devDep + script (testDir does not exist) | - |
| `.dockerignore` now blocks `*.bak*` from images | - |

Still on the VM, delete after merge: 35 `.bak*` files (~34k lines, archived).

Staged for a team decision (verified unreferenced by the deployed apps, but
they carry knowledge):

- `apps/product/backend/exp2-exp11.py` - one-off experiment scripts, repo
  only. Recommend: move to an `experiments/` dir or delete (git keeps them).
- Journey tree `mcp-skill-eval/` (2.9MB) - DO NOT plain-delete: it holds the
  canonical 40-row MCP-vs-pipeline eval evidence (the 92.5% result), the
  auditable $180 spend log, and `MCP-note_mentions-BUG.md` documenting a LIVE
  OTT production bug (knowledge_graph/queries 422s for every subject) that
  should become a Jira ticket. Archive to S3/repo, raise the ticket, then
  delete from the VM.
- Journey backend offline harness (~15 seed_*.py, smoke_*, measure_*,
  run_exp6, train_vec_adapter...) - not imported by the served app but used
  for KG builds. Keep, but move under a `tools/` dir so the served package
  is obviously small.
- `apps/full` workbench duplicates (13 panels + backend modules identical to
  apps/product) - dies naturally with P1.3 consolidation.
- Root `var/` in the suite tree - host residue incl. dangling symlinks;
  gitignored on this branch's rule set; delete on VM.

Kept deliberately (verified live): the 16-tab workbench frontend (all tabs
reachable and backed by working routes), both seed_eval_queries variants
(both wired into extraction_pipeline steps), `start.sh` (documented
quickstart - now fixed), dual `auth.py` copies (each imported by its own
app), `suite_primer.py` (docs-referenced dry-run utility; RISKY to delete).

## 6. Decisions needed

1. Merge `vm-sync-20260702` to main, or review-then-cherry-pick? (The sync
   commit is 13.9k lines - mostly the classification_core capture.)
2. Journey classify latency: 60-105s/round is the honest eval config. Add a
   "demo mode" preset (smaller model / lower reasoning) clearly labelled as
   non-eval, or keep it honest and narrate the wait?
3. `exp*.py` and the journey offline harness: `tools/` dir or delete?
4. Who owns rotating the leaked-to-two-machines OpenAI key and the SSM move?
5. Approve the ~$10-16 re-enrichment of the 5,763 "Other" codes from
   contextualised descriptions (see bench_sheet RCA) - separate workstream,
   unlocked by the same backup/deploy hygiene.

## 7. Verified reference numbers (2026-07-02)

- Retrieval: top config ~96.3% recall@100 (ATAR 700-sample); retail E0 after
  the June KG enrichment: recall@100 70.1% (was 64.1%), recall@500 81.2%
  (was 78.6%), Easy 100% / Medium 57.1% / Complex 65.1%.
- KG coverage: 21,868 codes enriched with description-LLM facts (98.6% of
  declarable estate), 105,694 facts embedded.
- Live journey probe: correct code (0207141000) in one Q&A round; full
  deterministic chain valuation->duty->landed->declaration arithmetically
  consistent; journey day-spend counter and $5 advisory cap working.
- DB: 25,609 commodities, 25,606 embedded (uk schema).
