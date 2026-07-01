# EXPERIMENT RESULTS

Sample: 40 gold rows (40 distinct expected_codes, persona-rotated).
Metric: top-1 + top-10 on the full DECLARABLE 10-digit commodity code (exact match).

## Journey arm (Q&A oracle loop, kg.classify_runs)

| run_label | n | top-1 | top-10 | classify_calls | est_cost |
|---|---:|---:|---:|---:|---:|
| exp_converge_none_baseline | 40 | 22.5% | 27.5% | 40 | $0.80 |
| exp_converge_factskg_baseline | 40 | 32.5% | 32.5% | 44 | $0.89 |
| exp_converge_factskg_rulereasoning | 40 | 32.5% | 37.5% | 43 | $0.87 |
| exp_eliminate_none_baseline | 40 | 10.0% | 40.0% | 197 | $4.53 |
| exp_eliminate_factskg_baseline | 40 | 15.0% | 37.5% | 192 | $4.40 |
| exp_eliminate_factskg_rulereasoning | 40 | 12.5% | 37.5% | 193 | $4.43 |

## MCP arm (gpt-5.5 + OTT MCP, ranked top-10)

| perm | n | with_code | top-1 | top-10 |
|---|---:|---:|---:|---:|
| mcp_base | 40 | 38 | 65.0% | 92.5% |

