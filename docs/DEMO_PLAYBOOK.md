# Demo Playbook - AI Search Evaluation Suite

This is the run-sheet for demoing the evaluation apps end to end. Everything
in it was checked against the live system on 2026-07-02 - every screen was
opened, every number below is what actually came back, and every timing was
measured, not guessed.

There are two apps, both running on the same cloud server:

- **The evaluation workbench** - https://18.175.148.215.sslip.io/
  Sixteen tabs for running and inspecting search-quality experiments,
  question-and-answer trials, benchmarks and costs.
- **The trader journey** - https://journey.18.175.148.215.sslip.io/
  The end-to-end demo a trader would see: describe your goods, get a
  commodity code, work out the customs value, the duty, the total import
  cost, and a draft customs declaration.

Both URLs ask for a username and password (username `tariff`; the password
is in your private notes - it is deliberately not written down in this
repository).

---

## 1. Pre-flight checklist (10 minutes, the morning of the demo)

Run these five commands. Each one should come back with the expected result;
if any of them does not, go to the Troubleshooting section before demoing.

```bash
# 1. All four services are up and report "healthy"
ssh ai-search-evaluation-suite-ec2 'sudo docker ps --format "{{.Names}} {{.Status}}"'

# 2. Both apps respond
ssh ai-search-evaluation-suite-ec2 'curl -s http://127.0.0.1:8100/api/health | head -c 200; echo; curl -s http://127.0.0.1:8200/api/health'

# 3. The tariff database is loaded (expect roughly 25,609 commodities, 25,606 searchable)
ssh ai-search-evaluation-suite-ec2 'curl -s http://127.0.0.1:8200/api/db/health'

# 4. Today's AI spend is near zero (the journey app has a $5-a-day allowance)
ssh ai-search-evaluation-suite-ec2 'curl -s http://127.0.0.1:8200/api/cost'

# 5. The password wall is up (expect the number 401, meaning "login required")
curl -s -o /dev/null -w "%{http_code}\n" https://18.175.148.215.sslip.io/
```

What the money controls look like (checked 2026-07-02):

- **Workbench:** paid AI calls are switched off by default. Anything that
  would spend money refuses politely until you explicitly allow it, and
  batch runs are rejected if their estimated cost exceeds $10.
- **Journey:** paid AI calls are switched on (it needs them to classify
  goods). A complete demo run costs roughly 10 to 20 cents. The $5-a-day
  counter in the top corner is informational only - it does not cut
  anything off.

## 2. Deploy the fixes first (one-time step)

The branch `vm-sync-20260702` contains fixes for problems found while
rehearsing this demo. Until it is deployed, two things on the journey app
are broken: the "Get more detail on all N codes" button hangs for minutes,
and goods taxed by weight (meat, sugar, dairy) hit a dead end at the duty
step. Section 6 lists the safe workarounds if you must demo before
deploying.

```bash
# From this checkout (after review):
git push origin vm-sync-20260702

# On the server - workbench app:
ssh ai-search-evaluation-suite-ec2
cd /home/ubuntu/repo-sync && git fetch origin && git checkout vm-sync-20260702
# Note: no --delete on purpose - the server keeps runtime data files that git does not track
sudo rsync -a --exclude .env --exclude var /home/ubuntu/repo-sync/apps/ /opt/ai-search-evaluation-suite/apps/
cd /opt/ai-search-evaluation-suite/apps/classification-evals
sudo docker compose build classification-evals && sudo docker compose up -d classification-evals

# On the server - journey app (same fixed files, copied into its separate folder):
sudo cp /home/ubuntu/repo-sync/apps/product/backend/journey/main.py \
        /home/ubuntu/repo-sync/apps/product/backend/journey/classification.py \
        /home/ubuntu/repo-sync/apps/product/backend/journey/duty.py \
        /opt/ai-search-evaluation-suite-journey/apps/full/backend/journey/
cd /opt/ai-search-evaluation-suite-journey/apps/full
sudo docker compose build && sudo docker compose up -d

# Optional tidy-up: delete 35 leftover backup files (already archived twice)
sudo find /opt/ai-search-evaluation-suite /opt/ai-search-evaluation-suite-journey -name "*.bak*" -type f -delete
```

Both apps were rebuilt from scratch on 2026-07-02 to prove the rebuild works,
so this deploy carries low risk.

---

## 3. Demo script A - the evaluation workbench (about 20 minutes, free or nearly free)

Open https://18.175.148.215.sslip.io/ and log in. Suggested order:

1. **Experiments** (the tab that opens first, and the highlight). A ranked
   league table of every search configuration we have tried loads
   automatically; the winner finds the correct commodity code in its top
   100 results 96.3% of the time across a 700-question test set. Type a
   goods description (for example "frozen boneless chicken breast
   fillets"), optionally the code you expect, pick the "baseline_fts_only"
   configuration (plain keyword search, costs nothing) and press Try. You
   get a ranked list of candidate codes instantly, with hit-or-miss
   markers and difficulty scores. Then deliberately pick one of the
   AI-powered configurations WITHOUT ticking the spend box: the app
   refuses with a clear message. Show that on purpose - "it cannot spend
   money unless you let it" is a feature.
2. **Matrix.** The full results table of every search experiment: one row
   per configuration, one column per way of phrasing the question, with a
   download link. Free to browse.
3. **Q&A Matrix / E2E Matrix.** The same idea for the question-asking
   stage and for complete start-to-finish runs: which prompt style, which
   model, how often the right answer survived, how many questions were
   asked. Drawn live from the results database. Free to browse.
4. **Financial.** A running total of what every part of the project has
   spent on AI calls, broken down by activity, refreshing every 30
   seconds. A good closing screen: "we track what we spend".
5. **Intercepts.** Analysis of the 728 search terms HMRC flagged as
   difficult. Pick a PREVIOUSLY SAVED run from the dropdown: per-term
   scores, charts showing how scattered the candidate codes are, and a
   drill-down per term. Do not press "Analyze selected" unless you intend
   to spend money live.
6. **Complexity.** Two big charts summarising how hard classification is
   across the whole tariff (14,000+ points, one per commodity heading),
   with the difficult terms overlaid. Below them, an audit of how often
   the known-correct answer was found.
7. **Knowledge.** A browsable view of the extracted facts about each
   commodity (98.6% of real, declarable codes now have facts), plus an
   interactive graph view. CAUTION: the edit and delete buttons here
   change the shared database with no undo - look, don't touch, during
   demos.
8. **The benchmark suite** (Prompts, Search References, Simulator, Judge,
   Benchmark, Analysis) - only if time allows. Prompts holds the library
   of test questions with known answers; ATaR can pull real HMRC rulings
   off GOV.UK and turn them into new test questions (costs money).
   Benchmark runs the questions against your chosen models with a live
   progress log - it spends money the moment you press Run, so keep the
   selection small. Analysis turns any stored run into leaderboards and
   charts, free, with sliders to reweight the scoring.

If you want to show the raw interface from a terminal:

```bash
# Score how well-phrased a query is (free)
curl -s -X POST http://127.0.0.1:8100/api/input/score -H 'Content-Type: application/json' \
  -d '{"query":"footwear with rubber soles","run_label":"baseline_fts_only"}'

# Prove the spend guard: this is refused because spending is not enabled
curl -s -X POST http://127.0.0.1:8100/api/evals/classification/trial \
  -H 'Content-Type: application/json' -d '{"gold_id":1,"model":"gpt-5-mini","simulator_model":"gpt-5-nano"}'

# List long-running evaluation jobs
curl -s http://127.0.0.1:8100/api/jobs
```

## 4. Demo script B - the trader journey (about 25 minutes, roughly 15 cents)

Open https://journey.18.175.148.215.sslip.io/ and log in. This script was
performed click-by-click in the live app on 2026-07-02; the timings and
figures are what actually happened.

**Pick your model before you start.** Under "Advanced settings" the app
defaults to GPT-5 Nano, the fastest and cheapest option. Be aware of the
trade-off we hit in rehearsal: for "frozen boneless chicken breast fillets",
Nano's top suggestion was 0207441000 - which is frozen boneless DUCK - and
the correct chicken code was not in the top five. The same question through
GPT-5.5 (slower, more expensive) returned the correct 0207141000. So:
choose GPT-5.5 if the demo is about accuracy and you can carry the wait;
keep Nano if the demo is about speed, and present the ranked list of
suggestions rather than the single top answer.

1. **Classify.** Type your goods (rehearsed wording: "frozen boneless
   chicken breast fillets, raw, packed for retail") and press Start. A
   status line reads "Retrieving candidates and building the first Q&A
   turn..." - allow **about 2.5 minutes** before the first question
   appears. The question card is worth narrating while people read it:
   "Question 1 of up to 7", "80 candidate codes considered", five
   descriptions to choose from plus "None of these", and fold-out panels
   showing the reasoning and the full list of 80 candidates.
2. **Answer the question.** Click an option. UNTIL THE FIXES ARE DEPLOYED,
   expect the screen to sit on "Processing your answer..." for **about 5
   minutes** with no movement (the live-progress connection drops and the
   app quietly retries the slow way - see Known issues). It does finish:
   "Classification Q&A resolved", a BEST MATCH card, and a ranked list of
   other possibilities. Have a talking segment ready - walk through the
   "What & why" panel from the first question rather than watching a
   spinner.
3. **"Get more detail."** After the fixes are deployed this reads the full
   tariff entry for the top 24 candidates in one go (5-10 seconds). Before
   then, do NOT press "Get more detail on all 80 codes" - it hangs for
   minutes. Getting detail on a single code is safe and quick (about a
   second) and shows the chapter notes, related facts, duty measures and
   real GOV.UK rulings for that code.
4. **Value.** Press "Use <code> -> Customs value", choose "I know the
   customs value", enter 12920, review, calculate. "Customs value
   £12,920.00" appears instantly with a table explaining how it was
   arrived at.
5. **Duty details.** The code and value carry over automatically. The
   steps: import date (already filled with today) -> country of origin
   (full country list; we used Thailand) -> proof of origin (the app
   noticed the UK has a trade arrangement with Thailand and explained
   what proof would earn the reduced rate) -> review -> Calculate duty.
   VAT is filled in from the commodity itself - 0% here, because basic
   foods are VAT-free; most goods get the standard 20%. Some goods are
   taxed by weight rather than by value: for those the wizard asks "how
   much are you importing?" (after the fixes are deployed) and does the
   conversion - we verified 5,000 kg of this poultry code, taxed at 107
   pounds per 100 kg, comes out at exactly **£5,350.00**. A
   percentage-based example: live cattle (0102291090) from the US at 10%
   of a £12,920 value = **£1,292.00**.
6. **Import costs.** Add freight and insurance if you like; the app then
   shows the VAT calculation and the total. Rehearsed numbers: £12,920
   value + £1,292 duty gives a VAT base of £14,212, VAT of £2,842.40 and
   a **total landed cost of £17,054.40** - the arithmetic checks out on
   stage.
7. **Declare.** A draft customs declaration: the official CDS form fields
   filled in box by box, the document codes you would need, and a "file
   intent" button that returns a reference number while stating clearly
   that nothing is actually submitted to HMRC. The download button saves
   the declaration as a file.
8. **The closing shot - it is an evaluation framework.** Browse to
   /eval/matrix on the journey address: the league table of search
   configurations with the two current-live-service baselines pinned at
   the top, so the audience can see exactly how much better the new
   approach scores. /eval/classify-matrix shows the same for the
   question-asking stage. Point at the corner banner: "Est. AI spend
   today $x / $5.00 cap".

Choosing a demo product BEFORE the fixes are deployed: pick something taxed
as a percentage of its value - most manufactured goods (footwear, furniture,
electronics) are. Meat, sugar and dairy are taxed by weight and will hit the
duty-step dead end described above.

## 5. How long things take and what they cost (measured 2026-07-02)

| Action | Time | Cost |
|---|---|---|
| Workbench search trial (keyword configuration) | under 1s | free |
| Matrix / Q&A Matrix / E2E Matrix tabs | under 1s | free |
| Journey: first question appears (app defaults) | ~2.5 min | ~2-5 cents |
| Journey: answer processed (before fixes - includes the hidden retry) | ~5 min | ~4-10 cents |
| Journey via the raw API (server defaults) | 105s first question / 68s answer | ~2-5 cents each |
| Detail on a single code | ~1s | free |
| Detail on 24 codes (after fixes) | ~5-10s | free unless AI summaries switched on |
| Value / duty / import cost / declaration steps | under 1s each | free |
| Complete journey run in the browser | ~10-12 min | ~10-20 cents |

## 6. Known issues and what to do about them

| Issue | Status | What to do |
|---|---|---|
| "Get more detail on all N codes" hangs for minutes | Fixed on branch `vm-sync-20260702` - deploy per section 2 | Before then: only get detail on single codes |
| Getting detail can fail with a server error if the candidates lack scores | Fixed on the branch | Before then: only use the button straight after a classification |
| Goods taxed by weight dead-end the duty step (no quantity asked, then an error) | Fixed on the branch (the wizard now asks for a quantity and converts kg) | Before then: demo goods taxed by value - footwear, furniture; avoid meat/sugar/dairy |
| Answering a question shows a frozen "Processing..." for ~5 min | Open. The live-progress connection sends nothing until the very end, the app gives up on it after ~14s and silently re-runs the whole thing the slow way - doubling both the wait and the cost | Plan a talking segment over the wait; the fix is to make the server send progress updates and the app tolerate quiet periods |
| The default model (GPT-5 Nano) can put the wrong code on top (duck for chicken, seen in rehearsal) | Open - consistent with our root-cause analysis of the question-asking stage | Use GPT-5.5 when accuracy is the story; otherwise present the ranked list, not just the top answer |
| Browser console shows 13 errors when the journey loads | Open - harmless. The journey ships with hidden workbench screens that ask for pages its server does not have. Invisible unless someone opens developer tools | Nothing needed; goes away when the two apps are consolidated |
| First question takes ~2.5 minutes | This is the honest speed of the current evaluation configuration | Narrate the status line; see the model-choice note |
| Workbench error messages used to say only "500 Internal Server Error" | Fixed on the branch - real reasons now shown | Before then: read the server logs |
| The Knowledge tab can edit/delete shared data with no confirmation | Open | Browse only during demos |
| The Benchmark tab spends money as soon as you press Run | FIXED: it now asks for confirmation, and the server refuses all workbench spend unless the operator switch is on | Flip AI_FAN_OUT_WORKBENCH_SPEND_ENABLED=1 for benchmark demos |
| The local `./start.sh` quickstart was broken | Fixed on the branch | Use the server deployment meanwhile |

## 7. Troubleshooting

```bash
# App logs, most recent 100 lines, health-check noise removed
ssh ai-search-evaluation-suite-ec2 'sudo docker logs journey-app --tail 100 2>&1 | grep -v "GET /api/health"'
ssh ai-search-evaluation-suite-ec2 'cd /opt/ai-search-evaluation-suite/apps/classification-evals && sudo docker compose logs --tail=100 classification-evals'

# Restart an app (the database and search index stay up)
ssh ai-search-evaluation-suite-ec2 'cd /opt/ai-search-evaluation-suite/apps/classification-evals && sudo docker compose up -d classification-evals'
ssh ai-search-evaluation-suite-ec2 'cd /opt/ai-search-evaluation-suite-journey/apps/full && sudo docker compose up -d'
```

- Browser asks for a login: username `tariff`, password from your private
  notes.
- A matrix tab is empty: the app hides missing database tables instead of
  erroring. The reference copy of the full database structure is in
  `migrations/000_baseline_uk_kg_schema.sql`.
- The journey app will not start: it shares a network with the workbench
  stack, so the workbench must be started first.
