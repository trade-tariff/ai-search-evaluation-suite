# E2E Demo Playbook - AI Search Evaluation Suite

Last verified live: 2026-07-02 (every endpoint in this playbook was probed
against the running deployment; latencies and costs below are measured, not
estimated).

Two apps, one EC2 host (`ai-search-evaluation-suite-ec2`):

| App | URL | Container | Port |
|---|---|---|---|
| Evaluation workbench (16 tabs) | https://18.175.148.215.sslip.io/ | classification-evals-classification-evals-1 | 8100 |
| Trader journey (classify -> value -> duty -> landed -> declare) | https://journey.18.175.148.215.sslip.io/ | journey-app | 8200 |

Both sit behind Caddy basicauth (user `tariff`; password is in your private
store - never in this repo). Supporting containers: `tariff-db` (pgvector,
uk + kg schemas), `opensearch` (tariff_commodities index).

---

## 1. Pre-flight checklist (10 minutes, run the morning of the demo)

```bash
# 1. All four containers healthy
ssh ai-search-evaluation-suite-ec2 'sudo docker ps --format "{{.Names}} {{.Status}}"'

# 2. Both apps answer
ssh ai-search-evaluation-suite-ec2 'curl -s http://127.0.0.1:8100/api/health | head -c 200; echo; curl -s http://127.0.0.1:8200/api/health'

# 3. DB + embeddings live (expect ~25,609 commodities, ~25,606 embedded)
ssh ai-search-evaluation-suite-ec2 'curl -s http://127.0.0.1:8200/api/db/health'

# 4. Today's journey spend is near zero (cap $5/day, advisory only)
ssh ai-search-evaluation-suite-ec2 'curl -s http://127.0.0.1:8200/api/cost'

# 5. External auth wall works (expect 401 without creds)
curl -s -o /dev/null -w "%{http_code}\n" https://18.175.148.215.sslip.io/
```

Spend posture (verified 2026-07-02):

- Workbench: `AI_FAN_OUT_WORKBENCH_SPEND_ENABLED=0`, job cap
  `CLASSIFY_EVAL_MAX_EST_USD=10`. Provider-backed routes correctly return
  403 without `allow_spend` (probed live).
- Journey: `JOURNEY_ALLOW_PROVIDER_CALLS=1` with a live OpenAI key. A full
  demo journey pass costs roughly $0.08-0.15.

## 2. Deploy the demo fixes first (one-time)

Branch `vm-sync-20260702` contains fixes found during the live probe. Until
it is deployed, the "Get more detail on all N codes" button in the journey
app WILL hang for minutes (sequential per-candidate hydration over ~80
candidates) and can 500 on unscored candidates.

```bash
# From this checkout (after review):
git push origin vm-sync-20260702

# On the VM - suite app:
ssh ai-search-evaluation-suite-ec2
cd /home/ubuntu/repo-sync && git fetch origin && git checkout vm-sync-20260702
sudo rsync -a --delete --exclude .env --exclude var /home/ubuntu/repo-sync/apps/ /opt/ai-search-evaluation-suite/apps/
cd /opt/ai-search-evaluation-suite/apps/classification-evals
sudo docker compose build classification-evals && sudo docker compose up -d classification-evals

# On the VM - journey app (same fixed files, drifted tree layout):
sudo cp /home/ubuntu/repo-sync/apps/product/backend/journey/main.py \
        /home/ubuntu/repo-sync/apps/product/backend/journey/classification.py \
        /opt/ai-search-evaluation-suite-journey/apps/full/backend/journey/
cd /opt/ai-search-evaluation-suite-journey/apps/full
sudo docker compose build && sudo docker compose up -d

# Optional cleanup (35 backup files, archived in ~/backups/bak_archive_20260702.tgz):
sudo find /opt/ai-search-evaluation-suite /opt/ai-search-evaluation-suite-journey -name "*.bak*" -type f -delete
```

Both images were rebuilt from the deployed trees on 2026-07-02 to confirm the
build path works (SUITE-BUILD-OK / JOURNEY-BUILD-OK).

---

## 3. Demo script A - Evaluation workbench (~20 min, zero to low spend)

Open https://18.175.148.215.sslip.io/ (basicauth). Suggested order:

1. **Experiments** (landing tab, the flagship). The ranked retrieval
   experiment catalogue loads; top config is
   `all_legs_on_gpt54mini_scope_qna_plus_facts` (~96.3% recall@100 on the
   700-input sample). Type a goods description (e.g. "frozen boneless
   chicken breast fillets"), optionally an expected 10-digit code, select
   the keyword baseline `baseline_fts_only`, and Try - free, instant,
   returns ranked candidates with hit@10/@100 and difficulty/specificity
   scores. Selecting a provider-backed config without enabling spend
   returns a clean 403 message - this is worth showing on purpose: the
   spend guard is a feature.
2. **Matrix**. Static snapshot of the full retrieval experiment matrix
   (configs x 7 personas, code-macro recall) + CSV export link. No spend.
3. **Q&A Matrix / E2E Matrix**. Live server-rendered matrices from
   `kg.e2e_eval_runs` / `kg.e2e_eval_results`: prompt mode, reasoning
   effort, gold preservation, top-1, rank lift, rounds. No spend.
4. **Financial**. Project-level cost roll-up (fact extraction, embeddings,
   E2E runs, classification runs) from the kg spend ledgers; auto-refreshes
   every 30s. Good closing slide for "we track what we spend".
5. **Intercepts** (no-spend path). Pick a SAVED run from the dropdown:
   728-term HMRC complexity workbench with per-term KPIs, entropy bars,
   treemap, scatter plots. Do NOT click "Analyze selected" unless you want
   live spend.
6. **Complexity**. Pick the most recent commodity sweep: two
   server-rendered charts (14k-point complexity-by-chapter scatter with
   intercept overlay + template density). Gold-recall audit panel below.
7. **Knowledge**. Coverage stats over `kg.commodity_facets` (98.6% of
   declarable codes now carry facts), facet browser, cytoscape graph view.
   CAUTION: edit/delete here writes to the shared kg tables - browse only
   during demos.
8. **Prompts -> Search References -> Simulator -> Judge -> Benchmark ->
   Analysis** (the benchmark loop, show only if time allows). Prompts =
   gold prompt library (feeds Benchmark); ATaR tab can ingest real HMRC
   rulings as new gold prompts (LLM spend). Benchmark runs prompts x models
   with a live SSE console - it hardcodes spend on, so only run with a
   small selection. Analysis renders leaderboards/radar charts of stored
   runs with adjustable scoring weights - works with zero new spend if a
   stored run exists.

API-only extras worth a terminal window:

```bash
# Input-quality scoring (free, keyword baseline)
curl -s -X POST http://127.0.0.1:8100/api/input/score -H 'Content-Type: application/json' \
  -d '{"query":"footwear with rubber soles","run_label":"baseline_fts_only"}'

# Spend gates in action (expect 403s)
curl -s -X POST http://127.0.0.1:8100/api/evals/classification/trial \
  -H 'Content-Type: application/json' -d '{"gold_id":1,"model":"gpt-5-mini","simulator_model":"gpt-5-nano"}'

# Long-running eval jobs (list; POST launches cost-capped subprocess runs)
curl -s http://127.0.0.1:8100/api/jobs
```

## 4. Demo script B - Trader journey e2e (~25 min, ~$0.15)

Open https://journey.18.175.148.215.sslip.io/ (basicauth). This script was
walked in the deployed UI end-to-end on 2026-07-02; timings and figures
below are what actually happened.

**Model choice (decide before you start).** The UI default in "Advanced
settings" is GPT-5 Nano (fastest/cheapest) with the "OTT staging + KG
context" process. In the verified run, Nano's BEST MATCH for frozen
chicken breast fillets was 0207441000 - frozen boneless DUCK - with the
correct chicken code not in the top five. The same query through gpt-5.5
returned the correct 0207141000. For an accuracy demo select GPT-5.5 and
accept the latency; for a speed demo keep Nano and be ready to talk about
the ranked alternates instead of the top-1 (this maps directly to the
Q&A-loop RCA finding).

1. **Classify.** Type your goods (verified: "frozen boneless chicken
   breast fillets, raw, packed for retail") and click Start. A status
   line shows "Retrieving candidates and building the first Q&A turn..." -
   **measured ~2.5 minutes to the first question** in the UI. The question
   card shows "Question 1 of up to 7", "80 candidate codes considered",
   A-E description options plus "None of these", and expandable "What &
   why" / "All matches we considered (80)" panels - good narration
   material while the audience reads.
2. **Answer.** Click an option. KNOWN ISSUE (pre-deploy): the answer
   round shows a static "Processing your answer..." for **~5 minutes** -
   the SSE stream emits no progress events and the client quietly falls
   back to a second, duplicate non-stream call (double latency, double
   spend). It DOES resolve: "Classification Q&A resolved" with a BEST
   MATCH card and ranked ALSO-POSSIBLE alternates. Plan a talking segment
   here (e.g. the "What & why" panel from turn 1), do not dead-air it.
3. **Get more detail.** Post-deploy: hydrates the top 24 candidates in
   parallel. Pre-deploy: do NOT click "Get more detail on all 80 codes"
   (minutes-long hang); single-code hydration (~1s) is safe and shows
   notes, KG edges, facets, measures and live GOV.UK ATAR rulings.
4. **Value.** Click "Use <code> -> Customs value" -> "I know the customs
   value" -> enter 12920 -> Review answers -> Calculate. Renders
   "Customs value £12,920.00" instantly with the method reasoning table.
5. **Duty details.** Carries the code + value across. Steps: import date
   (prefilled today) -> country of origin (full live geo list; verified
   Thailand) -> proof of origin (the app correctly detected a Thailand
   preference exists and explained proofs of origin) -> review -> Calculate
   duty. VAT auto-fills from the commodity (0% for poultry - zero-rated
   food; 20% default otherwise). For a quantity-based duty the wizard adds
   a quantity step (post-deploy - see known issues) and converts kg to the
   duty unit: verified 5,000 kg at 107 GBP/DTN = **£5,350.00**. An
   ad-valorem example: 0102291090 from US = 10% MFN = £1,292.00.
6. **Import costs (landed).** Freight/insurance/other charges -> VAT base
   and total: verified £12,920 value + £1,292 duty -> VAT base £14,212,
   VAT £2,842.40, **total landed £17,054.40** - the numbers add up on
   stage.
7. **Declare.** CDS box values (DE 1/1 IM, DE 1/10 4000, ...), document
   codes; "file intent" returns a DECL-xxxx reference and explicitly says
   nothing is submitted to HMRC; download exports the declaration JSON.
8. **Eval views** (the "this is an eval framework" close): browse to
   `/eval/matrix` on the journey host - ranked retrieval configs with the
   two OTT baselines pinned on top; `/eval/classify-matrix` is the Q&A
   analogue. The cost banner top-right shows "Est. AI spend today $x / $5".

Demo-product tip (pre-deploy): pick goods whose duty is ad-valorem (most
manufactured goods - footwear, furniture, electronics) so the duty stage
needs no quantity. Meat, sugar and dairy carry specific (per-100kg) duties
and dead-end the wizard until the branch is deployed.

## 5. Latency and spend reference (measured 2026-07-02)

| Action | Time | Cost |
|---|---|---|
| Workbench retrieval trial (keyword baseline) | <1s | $0 |
| Matrix / Q&A / E2E matrix tabs | <1s | $0 |
| Journey classify: first question (UI, default config) | ~2.5 min | ~$0.02-0.05 |
| Journey classify: answer round (UI, pre-deploy - includes duplicate fallback run) | ~5 min | ~$0.04-0.10 |
| Journey classify via raw API (backend default config) | 105s start / 68s answer | ~$0.02-0.05 each |
| Single-code hydration | ~1s | $0 (deterministic summary) |
| Hydrate 24 candidates (post-fix, parallel) | ~5-10s | $0 unless LLM summaries on |
| Valuation / duty / landed / declaration | <1s each | $0 |
| Full journey pass (UI, one Q&A round) | ~10-12 min | ~$0.10-0.20 |

## 6. Known issues and workarounds

| Issue | State | Workaround |
|---|---|---|
| "Get more detail on all N codes" hangs minutes | FIXED on `vm-sync-20260702`, deploy per section 2 | Pre-fix: use single-code hydration only |
| Hydration 500 (KeyError 'score') on unscored candidates | FIXED on branch | Pre-fix: only hydrate from a fresh classify turn |
| Specific-duty commodities dead-end the duty wizard (no quantity step, /api/duty 422) | FIXED on branch (requirements now detect per-unit duties; kg->DTN conversion added) | Pre-fix: demo ad-valorem goods (footwear, furniture); avoid meat/sugar/dairy |
| Answer round: static "Processing..." ~5 min, stream falls back to a duplicate non-stream call | Open (UI walkthrough 2026-07-02): answer/stream sends only keep-alives + final turn; client aborts ~14s in and re-runs via fallback | Plan a talking segment; investigate postSseStream keep-alive handling + make the stream emit milestones |
| UI default model (GPT-5 Nano) can top-rank the wrong code (duck vs chicken, verified) | Open - matches the Q&A-loop RCA | Select GPT-5.5 for accuracy demos; use ranked alternates as the story otherwise |
| Journey bundle's hidden workbench panels 404 on /api/config etc. (13 console errors on load) | Open - cosmetic, invisible unless devtools open | None needed; dies with the P1 journey/workbench consolidation |
| Classify first question ~2.5 min in UI | By design at the current eval config | Narrate the status line; see model-choice note |
| Workbench error toasts show only "500 Internal Server Error" | FIXED on branch (api.ts now surfaces detail) | Pre-fix: check container logs |
| Knowledge tab delete buttons write to shared kg tables | Open | Browse-only during demos |
| Benchmark tab hardcodes allow_spend | Open (P1 in PATH_TO_PROD) | Run with few prompts/models |
| `./start.sh` local quickstart broken | FIXED on branch (PRODUCT_APP_ROOT was literal "-e") | Use Docker/EC2 |

## 7. Troubleshooting

```bash
# App logs (last 100 lines, skip health checks)
ssh ai-search-evaluation-suite-ec2 'sudo docker logs journey-app --tail 100 2>&1 | grep -v "GET /api/health"'
ssh ai-search-evaluation-suite-ec2 'cd /opt/ai-search-evaluation-suite/apps/classification-evals && sudo docker compose logs --tail=100 classification-evals'

# Restart an app (DB/OpenSearch keep running; journey needs the suite network up)
ssh ai-search-evaluation-suite-ec2 'cd /opt/ai-search-evaluation-suite/apps/classification-evals && sudo docker compose up -d classification-evals'
ssh ai-search-evaluation-suite-ec2 'cd /opt/ai-search-evaluation-suite-journey/apps/full && sudo docker compose up -d'
```

- 401 in the browser: basicauth creds (user `tariff`) from your private store.
- Empty matrices: check `to_regclass` silently hides missing kg tables -
  `migrations/000_baseline_uk_kg_schema.sql` is the schema reference.
- Journey app refuses to start: it joins the external Docker network
  `classification-evals_default` - the suite stack must be up first.
