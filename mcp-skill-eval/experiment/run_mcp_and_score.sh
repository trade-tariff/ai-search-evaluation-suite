#!/usr/bin/env bash
set -uo pipefail
EXP=/app/exp_run
LOG=$EXP/mcp_run.log
st(){ echo "$(date -u +%FT%TZ) | $1" >> "$EXP/STATUS"; }
st "MCP ARM (new key) START"
rm -rf "$EXP/mcp_base"; mkdir -p "$EXP/mcp_base"
ARM_OUT_DIR="$EXP/mcp_base" ARM_MODEL=gpt-5.5 python3 "$EXP/arm_openai_top10.py" "$EXP/skill_blob.md" "$EXP/arm_sample.json" "$EXP/mcp_base.json" >> "$LOG" 2>&1
st "MCP ARM done: $(ls $EXP/mcp_base/*.json 2>/dev/null|wc -l) rows"
RUN_LABELS="exp_converge_none_baseline,exp_converge_factskg_baseline,exp_converge_factskg_rulereasoning,exp_eliminate_none_baseline,exp_eliminate_factskg_baseline,exp_eliminate_factskg_rulereasoning" MCP_DIRS="mcp_base=$EXP/mcp_base" SAMPLE_JSON="$EXP/sample40.json" OUT_MD="$EXP/EXPERIMENT-RESULTS.md" OUT_JSON="$EXP/score_summary.json" python3 "$EXP/score_experiment.py" >> "$LOG" 2>&1
st "PHASE 5 DONE - EXPERIMENT COMPLETE"
