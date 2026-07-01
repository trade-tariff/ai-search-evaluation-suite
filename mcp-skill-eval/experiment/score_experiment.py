#!/usr/bin/env python3
"""Phase-5 scorer for the autonomous classification experiment.

Reads:
  - kg.classify_runs rows for THIS experiment's run-labels (env RUN_LABELS,
    comma-list). top-1 = gold_in_top1; top-10 = gold_rank present and <=10
    within the final committed set.
  - MCP per-id JSONs (one dir per arm perm). top-1 = exact 10-digit `final`;
    top-10 = expected in `ranked_codes` (10-digit declarable).

All matching is on the full DECLARABLE 10-digit code (exact). Writes a top-1 +
top-10 table per config + arm to EXPERIMENT-RESULTS.md and score_summary.json.

Env:
  RUN_LABELS       comma-list of classify_runs run_labels (the 6 Phase-1 cells)
  MCP_DIRS         comma-list of name=dir  (e.g. base=/opt/exp/mcp_base,skipnm=/opt/exp/mcp_skipnm)
  SAMPLE_JSON      sample40.json (defines the gold id universe + expected codes)
  OUT_MD           EXPERIMENT-RESULTS.md path
  OUT_JSON         score_summary.json path
  TARIFF_DB_DSN    db dsn
"""
import json, os, glob
import psycopg
from psycopg.rows import dict_row

DSN = os.environ["TARIFF_DB_DSN"]
RUN_LABELS = [x.strip() for x in os.environ.get("RUN_LABELS", "").split(",") if x.strip()]
SAMPLE = json.load(open(os.environ["SAMPLE_JSON"]))
OUT_MD = os.environ.get("OUT_MD", "/opt/exp/EXPERIMENT-RESULTS.md")
OUT_JSON = os.environ.get("OUT_JSON", "/opt/exp/score_summary.json")

EXPECTED = {r["id"]: r["expected_code"] for r in SAMPLE}
N = len(SAMPLE)


def norm(c):
    return (str(c or "").replace(" ", "") or None)


def pct(x, n):
    return round(x / n, 3) if n else 0.0


# ---- classify_runs (journey arm cells) -----------------------------------
classify_results = {}
conn = psycopg.connect(DSN, row_factory=dict_row)
with conn.cursor() as cur:
    for label in RUN_LABELS:
        cur.execute(
            "SELECT gold_id, expected_code, final_top1, final_set, gold_in_top1, gold_rank "
            "FROM kg.classify_runs WHERE run_label = %s", (label,))
        rows = cur.fetchall()
        seen = {}
        for r in rows:  # last write wins if duplicate
            seen[r["gold_id"]] = r
        n = len(seen)
        top1 = sum(1 for r in seen.values() if r["gold_in_top1"])
        top10 = sum(1 for r in seen.values()
                    if r["gold_rank"] is not None and r["gold_rank"] <= 10)
        # cost / calls pulled from classify_runs too
        cur.execute(
            "SELECT coalesce(sum(classify_calls),0) cc, coalesce(sum(simulator_calls),0) sc, "
            "coalesce(sum(est_cost_usd),0) cost FROM kg.classify_runs WHERE run_label=%s", (label,))
        agg = cur.fetchone()
        classify_results[label] = {
            "kind": "journey", "n": n,
            "top1": top1, "top1_pct": pct(top1, n),
            "top10": top10, "top10_pct": pct(top10, n),
            "classify_calls": int(agg["cc"]), "sim_calls": int(agg["sc"]),
            "est_cost_usd": float(agg["cost"]),
        }
conn.close()

# ---- MCP arms ------------------------------------------------------------
mcp_results = {}
for spec in [s for s in os.environ.get("MCP_DIRS", "").split(",") if s.strip()]:
    name, d = spec.split("=", 1)
    files = glob.glob(os.path.join(d, "*.json"))
    n = top1 = top10 = with_code = 0
    for f in files:
        rid = int(os.path.basename(f)[:-5])
        if rid not in EXPECTED:
            continue
        try:
            j = json.load(open(f))
        except Exception:
            j = {}
        exp = EXPECTED[rid]
        final = norm(j.get("final"))
        ranked = [norm(c) for c in (j.get("ranked_codes") or []) if norm(c)]
        if final and final not in ranked:
            ranked = [final] + ranked
        n += 1
        if final:
            with_code += 1
        if final == exp:
            top1 += 1
        if exp in ranked[:10]:
            top10 += 1
    mcp_results[name] = {
        "kind": "mcp", "n": n, "with_code": with_code,
        "top1": top1, "top1_pct": pct(top1, n),
        "top10": top10, "top10_pct": pct(top10, n),
    }

# ---- write outputs -------------------------------------------------------
summary = {"sample_n": N, "journey": classify_results, "mcp": mcp_results}
json.dump(summary, open(OUT_JSON, "w"), indent=1)

lines = []
lines.append("# EXPERIMENT RESULTS")
lines.append("")
lines.append("Sample: %d gold rows (40 distinct expected_codes, persona-rotated)." % N)
lines.append("Metric: top-1 + top-10 on the full DECLARABLE 10-digit commodity code (exact match).")
lines.append("")
lines.append("## Journey arm (Q&A oracle loop, kg.classify_runs)")
lines.append("")
lines.append("| run_label | n | top-1 | top-10 | classify_calls | est_cost |")
lines.append("|---|---:|---:|---:|---:|---:|")
for label in RUN_LABELS:
    r = classify_results.get(label, {})
    if not r:
        lines.append("| %s | 0 | - | - | - | - |" % label)
        continue
    lines.append("| %s | %d | %.1f%% | %.1f%% | %d | $%.2f |" % (
        label, r["n"], r["top1_pct"] * 100, r["top10_pct"] * 100,
        r["classify_calls"], r["est_cost_usd"]))
lines.append("")
lines.append("## MCP arm (gpt-5.5 + OTT MCP, ranked top-10)")
lines.append("")
lines.append("| perm | n | with_code | top-1 | top-10 |")
lines.append("|---|---:|---:|---:|---:|")
for name, r in mcp_results.items():
    lines.append("| %s | %d | %d | %.1f%% | %.1f%% |" % (
        name, r["n"], r["with_code"], r["top1_pct"] * 100, r["top10_pct"] * 100))
lines.append("")
open(OUT_MD, "w").write("\n".join(lines) + "\n")
print("wrote", OUT_MD, "and", OUT_JSON)
print(json.dumps(summary, indent=1))
