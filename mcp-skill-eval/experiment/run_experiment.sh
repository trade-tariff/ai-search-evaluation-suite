#!/usr/bin/env bash
# Autonomous classification experiment orchestrator. Runs DETACHED inside the
# journey-app container (launched via docker exec -d from the local mac).
#
# Phases:
#   1. 6 Phase-1 config cells over the 40 gold_ids (journey Q&A oracle loop)
#   2. MCP arm (gpt-5.5 + OTT MCP), 2 perms (base, +note_mentions-skip), ranked top-10
#   5. Scorer -> EXPERIMENT-RESULTS.md + score_summary.json
#
# Requires in env (passed by docker exec -e): OPENAI_API_KEY (already in container),
#   OTT_HUB_CLIENT_ID, OTT_HUB_CLIENT_SECRET.
# SMOKE=1 + SMOKE_IDS=<csv> restricts everything to a 2-row smoke (1 journey cell + MCP 2 rows).
set -uo pipefail

# This script runs INSIDE journey-app (launched via docker exec -d). The host
# /opt eval dir is NOT mounted into the container, so all execution + results
# live under $EXP (container-local). Retrieve results to the host with:
#   sudo docker cp journey-app:/app/exp_run/. \
#        /opt/ai-search-evaluation-suite-journey/mcp-skill-eval/experiment/
EXP=/app/exp_run
BACKEND=/app/backend
SAMPLE=$EXP/sample40.json
STATUS=$EXP/STATUS
LOG=$EXP/run.log
SKILL_BLOB=$EXP/skill_blob.md
SKILLDIR=$EXP/customs-skills/uk-commodity-code-classifier
ARM=$EXP/arm_openai_top10.py
SCORER=$EXP/score_experiment.py

DSN="${TARIFF_DB_DSN:-postgresql:///tariff_db}"
# psql CLI is NOT installed in journey-app, so cost-guard queries go through
# psycopg (python), which the container has. classify_runs lives in the same db
# the journey app writes to, reachable via TARIFF_DB_DSN from inside journey-app.
PYQ() { python3 -c "import os,psycopg;c=psycopg.connect(os.environ['TARIFF_DB_DSN']);cur=c.cursor();cur.execute('''$1''');print(cur.fetchone()[0]);c.close()" 2>>"$LOG"; }

ts() { date -u +%FT%TZ; }
status() { echo "$(ts) | $*" >> "$STATUS"; echo "$(ts) | $*" >> "$LOG"; }

mkdir -p "$EXP"
: > "$LOG"
echo "$(ts) | EXPERIMENT START" > "$STATUS"

# ---- cost guard ----------------------------------------------------------
HARD_CAP_USD=200
RUN_TAG_LIKE="exp_%"
cost_so_far() {
  python3 /app/exp_run/cost_so_far.py 2>/dev/null || echo 0
}
abort_if_over() {
  local c; c=$(cost_so_far)
  status "cost-guard: journey est_cost so far = \$${c} (cap \$${HARD_CAP_USD})"
  awk -v c="$c" -v cap="$HARD_CAP_USD" 'BEGIN{exit !(c+0 > cap+0)}'
  if [ $? -eq 0 ]; then
    status "ABORT: est_cost \$${c} exceeds hard cap \$${HARD_CAP_USD}"
    exit 9
  fi
}

# ---- gold-id list --------------------------------------------------------
if [ "${SMOKE:-0}" = "1" ]; then
  GOLD_IDS="${SMOKE_IDS:?SMOKE_IDS required in smoke mode}"
  MAXIDS=$GOLD_IDS
else
  GOLD_IDS=$(python3 -c "import json;print(','.join(str(r['id']) for r in json.load(open('$SAMPLE'))))")
fi
status "gold_ids = $GOLD_IDS"
export GOLD_IDS

# ---- Phase 1: journey config cells --------------------------------------
# label : strategy prompt aug
if [ "${SMOKE:-0}" = "1" ]; then
  CELLS=( "exp_eliminate_factskg_baseline:eliminate:baseline:facts+kg" )
else
  CELLS=(
    "exp_converge_none_baseline:converge:baseline:none"
    "exp_converge_factskg_baseline:converge:baseline:facts+kg"
    "exp_eliminate_none_baseline:eliminate:baseline:none"
    "exp_eliminate_factskg_baseline:eliminate:baseline:facts+kg"
    "exp_eliminate_factskg_rulereasoning:eliminate:rule_reasoning:facts+kg"
    "exp_converge_factskg_rulereasoning:converge:rule_reasoning:facts+kg"
  )
fi

RUN_LABELS=""
status "PHASE 1 START (${#CELLS[@]} cells)"
cd "$BACKEND" || { status "FATAL cd $BACKEND"; exit 2; }
for cell in "${CELLS[@]}"; do
  IFS=':' read -r label strat prompt aug <<< "$cell"
  RUN_LABELS="${RUN_LABELS:+$RUN_LABELS,}$label"
  status "phase1 cell: $label ($strat/$prompt/$aug)"
  abort_if_over
  python -m journey.run_classify_matrix_ids \
      --run-label "$label" --strategy "$strat" --prompt-mode "$prompt" \
      --augmentation "$aug" --model gpt-5.5 --candidate-limit 100 \
      --concurrency 4 --max-rounds 5 \
      --personas naive_vague,naive_specific,naive_branded,emu_generic,emu_ordinary,emu_specific,original \
      >> "$LOG" 2>&1
  rc=$?
  status "phase1 cell done: $label rc=$rc"
done
status "PHASE 1 DONE. labels=$RUN_LABELS"
export RUN_LABELS

# ---- Phase 2: MCP arm ----------------------------------------------------
status "PHASE 2 START (MCP arm)"
# Build the skill_blob = SKILL.md + references concatenated.
{ cat "$SKILLDIR/SKILL.md"; for f in "$SKILLDIR"/references/*.md; do echo; echo "--- $(basename "$f") ---"; cat "$f"; done; } > "$SKILL_BLOB"

if [ "${SMOKE:-0}" = "1" ]; then
  ARM_SAMPLE=$EXP/smoke_sample.json
  python3 -c "import json;ids=set('$SMOKE_IDS'.split(','));s=[r for r in json.load(open('$SAMPLE')) if str(r['id']) in ids];json.dump(s,open('$ARM_SAMPLE','w'))"
  PERMS=( "mcp_base::" )
else
  ARM_SAMPLE=$EXP/arm_sample.json
  # arm needs oracle_text: enrich sample40 with kg_edges body per source_id.
  python3 - "$SAMPLE" "$ARM_SAMPLE" <<'PY'
import json,sys,os,psycopg
src,out=sys.argv[1],sys.argv[2]
rows=json.load(open(src))
conn=psycopg.connect(os.environ.get("TARIFF_DB_DSN","postgresql:///tariff_db"))
ids=sorted({r["source_id"] for r in rows})
cur=conn.cursor();cur.execute("SELECT id,body FROM kg.kg_edges WHERE id=ANY(%s)",(ids,))
body={i:b for i,b in cur.fetchall()}
for r in rows: r["oracle_text"]=body.get(r["source_id"],"")
json.dump(rows,open(out,"w"));print("arm sample rows",len(rows))
PY
  PERMS=( "mcp_base::" "mcp_skipnm:SKIP_NOTE_MENTIONS=1:" )
fi
# enrich smoke sample with oracle_text too
if [ "${SMOKE:-0}" = "1" ]; then
  python3 - "$ARM_SAMPLE" <<'PY'
import json,sys,os,psycopg
out=sys.argv[1];rows=json.load(open(out))
conn=psycopg.connect(os.environ.get("TARIFF_DB_DSN","postgresql:///tariff_db"))
ids=sorted({r["source_id"] for r in rows})
cur=conn.cursor();cur.execute("SELECT id,body FROM kg.kg_edges WHERE id=ANY(%s)",(ids,))
body={i:b for i,b in cur.fetchall()}
for r in rows: r["oracle_text"]=body.get(r["source_id"],"")
json.dump(rows,open(out,"w"));print("smoke arm rows",len(rows))
PY
fi

MCP_DIRS=""
for perm in "${PERMS[@]}"; do
  IFS=':' read -r pname penv _ <<< "$perm"
  OUTDIR=$EXP/$pname
  mkdir -p "$OUTDIR"
  MCP_DIRS="${MCP_DIRS:+$MCP_DIRS,}$pname=$OUTDIR"
  status "mcp perm: $pname env=[$penv]"
  env $penv ARM_OUT_DIR="$OUTDIR" ARM_MODEL=gpt-5.5 \
      python3 "$ARM" "$SKILL_BLOB" "$ARM_SAMPLE" "$OUTDIR.json" >> "$LOG" 2>&1
  status "mcp perm done: $pname rc=$?"
done
status "PHASE 2 DONE. mcp_dirs=$MCP_DIRS"

# ---- Phase 5: scorer -----------------------------------------------------
status "PHASE 5 START (scorer)"
RUN_LABELS="$RUN_LABELS" MCP_DIRS="$MCP_DIRS" SAMPLE_JSON="$SAMPLE" \
  OUT_MD="$EXP/EXPERIMENT-RESULTS.md" OUT_JSON="$EXP/score_summary.json" \
  python3 "$SCORER" >> "$LOG" 2>&1
status "PHASE 5 DONE rc=$?"
status "EXPERIMENT COMPLETE. final cost(journey est)=\$$(cost_so_far)"
