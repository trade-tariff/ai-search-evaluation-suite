# Path to Production - AI Search Evaluation Suite

This plan comes from a full audit on 2026-07-02: a 31-agent automated review
of the deployed source code (what each part does, what is dead weight, what
would break in production) plus a hands-on check of every screen and API on
the live system. Everything below marked "verified" was checked against the
running system, not inferred from reading code.

## 1. Where we are (verified)

- One cloud server runs everything: the evaluation workbench, the trader
  journey app, the tariff database, the search index, and the web front
  door that handles the login prompt and HTTPS.
- Both apps are healthy. Every read-only page and API responds correctly.
  The workbench's money controls genuinely work: anything that would spend
  money on AI calls is refused unless explicitly allowed, and batch runs
  are rejected above an estimated $10.
- **Both apps rebuild cleanly from the code on the server** (proven
  2026-07-02). An earlier fear that the running system could not be rebuilt
  turned out to be false.
- The deployed code was NOT under version control. The server held a raw
  copy of the code, edited live over weeks, with 35 backup copies of files
  (about 34,000 lines) as the only history. The server copy and the GitHub
  repository had drifted apart in both directions: the core classification
  engine (18 files) existed only on the server, while the repository held
  database set-up scripts and experiment code the server never received.
- Branch `vm-sync-20260702` fixes that: the server's code is captured into
  git, 1,284 lines of confirmed-dead code are removed, the demo-blocking
  bugs are fixed, and the full database structure is recorded in
  `migrations/`. Deployed to the server on 2026-07-02.

## 2. Do this week

1. **Adopt the branch and the rule that comes with it.** Review and merge
   `vm-sync-20260702`. From then on, nothing lands on the server except
   through git (the deploy steps are in DEMO_PLAYBOOK.md section 2).
   Give the server a read-only deploy key so it can pull from GitHub
   itself - today it has no GitHub access, so deploys go over rsync from
   a laptop. Delete the 35 leftover backup files on the server (already
   archived twice).
2. **Rotate the journey app's OpenAI key and protect its folder.** The
   journey app's folder on the server has no ignore rules and its
   environment file contains the live OpenAI key and the database password
   in plain text - one careless `git init` and push would publish them.
   The key already exists on at least two machines: rotate it, then move
   runtime secrets to AWS's secret store (a fetch script already exists in
   `scripts/`, it just is not wired in).
3. **Backups.** First-ever off-server backups were taken 2026-07-02 (kept
   on the operator's laptop): the app's data volume - which includes the
   hand-curated test-question library, the one asset money cannot rebuild -
   plus the database structure and the journey app's data files. Treat the
   volume backup as secret: it contains a config file with API keys. Still
   missing: a scheduled database dump shipped to S3 (the extracted facts
   cost about $37 of AI calls to regenerate; the tariff data itself has no
   rebuild path except a snapshot) and a search-index backup.
4. **The database structure is now in the repo** (done on this branch:
   `migrations/000_baseline_uk_kg_schema.sql`, 187 tables). Before this, a
   fresh database produced silently empty dashboards, because the app hides
   missing tables instead of reporting them.

## 3. Before any external audience

1. **One money-control system instead of three.** Largely closed on
   2026-07-02: the workbench's operator switch is now the master (no
   request can spend while it is off, which defeats the front end's
   previously hardcoded "allow spend" on benchmark/preview/ingest), Q&A
   trials now respect the $10 cap, and the journey's daily counter is an
   enforced cutoff rather than a banner. Remaining: the journey counter
   resets if the container restarts (it is in-memory) - persist it if
   that ever matters.
2. **Turn on the second login layer.** Both apps contain a decent built-in
   login check that is fully wired up but dormant, because no environment
   variable activates it. Today the only protection is the single shared
   username/password at the front door. Setting two variables gives a free
   second layer. Before anyone external gets access: individual accounts
   and a proper domain name with a real certificate (today's address is an
   IP-based convenience domain owned by a third party).
3. **One journey codebase, not two.** The journey app on the server is a
   separate, hand-copied folder that duplicates the repository's journey
   code - byte-identical today (verified), but it will drift again.
   Rebuild the journey app from this repository's own build file and
   delete the parallel folder.
4. **Make the Knowledge tab safe.** It can edit and delete records in the
   shared knowledge base with one click, no confirmation, no login. Add a
   confirmation and a role check, or make those buttons read-only in the
   demo deployment.
5. **A build check on every change.** A single GitHub Actions job that
   builds both apps and compiles the code on every pull request would have
   caught most of what this audit found. Automatic deployment can come
   later; the build check alone prevents the "edited live until it no
   longer builds" failure mode.

## 4. Production shape (later)

- Move the front door to managed AWS services (load balancer + certificate),
  the database to RDS or at minimum scheduled disk snapshots, and either
  manage the search service properly or switch its security plugin back on
  (it is currently disabled).
- Everything runs on one machine, so any failure takes the whole service
  down. For an internal evaluation tool that is acceptable - the cheap
  insurance is a rehearsed restore-from-zero routine (restore the database
  snapshot, apply the migrations, rebuild the search index with the
  existing script) rather than buying high availability.
- Pin dependency versions. Both requirements files say "this version or
  newer", so every rebuild may pull different libraries.

## 5. What was removed, what is queued, what stays

Removed on this branch (each one adversarially checked - no imports, no
build references, no documentation references):

| Item | Size |
|---|---|
| A 527-line abandoned dashboard living inside the main server file | 527 lines |
| Two never-imported analysis modules (recall_curve, promote_llm_facts) | ~430 lines |
| A superseded data-seeding script (seed_facets_parallel) | ~300 lines |
| Browser-test tooling configured against a folder that does not exist | - |
| Backup files can no longer sneak into built images | - |

Deleted from the server after deploy: the 35 backup files (~34,000 lines,
archived on the server and on the operator's laptop).

Queued for a team decision (confirmed unused by the running apps, but they
carry knowledge):

- Ten one-off experiment scripts (`exp2.py` - `exp11.py`) that exist only in
  the repository. Recommend moving them to an `experiments/` folder or
  deleting them - git history keeps them either way.
- The `mcp-skill-eval` folder in the journey deployment (2.9 MB). Do NOT
  simply delete it: it holds the evidence behind the recorded
  MCP-versus-pipeline comparison (the 92.5% result), the auditable $180
  spend log, and a write-up of a LIVE bug in the production Trade Tariff
  service (its knowledge-graph query endpoint rejects every request) that
  should be raised as a Jira ticket. Archive the folder, raise the ticket,
  then delete.
- About 30 offline scripts inside the journey backend (data seeding, smoke
  tests, one-off measurements). Not used by the running app, but used to
  build the knowledge base. Keep them, move them into a `tools/` folder so
  the served code is visibly small.
- The journey deployment's duplicate copy of the workbench screens - goes
  away by itself with the consolidation in section 3.

Kept deliberately (all verified in use): all 16 workbench tabs (every one is
reachable and backed by a working API), both versions of the test-question
seeding script (both wired into the data pipeline), the local quickstart
script (documented, now fixed), the small duplicated login module (each app
imports its own copy), and the "suite primer" utility (referenced by
documentation - risky to delete).

## 6. Decisions needed

1. Merge `vm-sync-20260702` as-is, or review commit by commit? (The sync
   commit is large - 13,900 lines - because it captures the previously
   untracked classification engine.)
2. Journey answer speed: 2.5 minutes to the first question and about 5 to
   process an answer is the honest speed of the current evaluation
   configuration. Add a clearly-labelled fast "demo mode", or keep it
   honest and narrate the wait?
3. The experiment scripts and offline tools: `tools/` folder or delete?
4. Who owns rotating the exposed OpenAI key and moving secrets to the AWS
   secret store?
5. Approve roughly $10-16 of AI spend to re-extract facts for the ~5,763
   catch-all ("Other") commodity codes from their contextualised
   descriptions - the known next step for search quality (see the
   benchmarking write-up in the project notes).

## 7. Reference numbers (verified 2026-07-02)

- Search quality: the best configuration finds the correct code in its top
  100 results 96.3% of the time on the 700-question rulings test set. On
  the harder retail test set, June's knowledge-base enrichment lifted the
  top-100 figure from 64.1% to 70.1% (top-500: 78.6% to 81.2%; easy items
  now 100%).
- Knowledge base coverage: 21,868 commodity codes carry extracted facts
  (98.6% of all real, declarable codes); 105,694 facts are indexed for
  search.
- Live journey check: correct code (0207141000, frozen boneless chicken)
  reached in one question round with the stronger model; the value ->
  duty -> import-cost -> declaration chain is arithmetically consistent
  end to end; duty-by-weight verified (5,000 kg at 107 GBP per 100 kg =
  £5,350.00).
- Database: 25,609 commodities loaded, 25,606 indexed for AI search.
