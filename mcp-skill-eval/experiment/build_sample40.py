#!/usr/bin/env python3
"""Build a 40-row sample: 40 DISTINCT expected_codes from kg.eval_gold, one
persona each rotating (max code coverage + persona spread).

For each distinct expected_code (ordered), pick exactly one gold row, cycling
the persona through the 7 personas so coverage is spread. If the chosen persona
has no row for that code, fall back to any persona for that code (deterministic
by id). Writes 40 rows -> sample40.json: {id, persona, query, expected_code, source_id}.

Run in the journey-app container (has TARIFF_DB_DSN + psycopg).
"""
import json, os
import psycopg
from psycopg.rows import dict_row

DSN = os.environ["TARIFF_DB_DSN"]
OUT = os.environ.get("SAMPLE_OUT", "/opt/exp/sample40.json")
N = int(os.environ.get("SAMPLE_N", "40"))

PERSONAS = ["naive_vague", "naive_specific", "naive_branded",
            "emu_generic", "emu_ordinary", "emu_specific", "original"]

conn = psycopg.connect(DSN, row_factory=dict_row)
with conn.cursor() as cur:
    # all atar gold rows, deterministic order
    cur.execute(
        "SELECT id, persona, query, expected_code, source_id "
        "FROM kg.eval_gold WHERE source_type='atar' ORDER BY expected_code, id"
    )
    rows = cur.fetchall()
conn.close()

# group by expected_code, preserving first-seen order
by_code = {}
for r in rows:
    by_code.setdefault(r["expected_code"], []).append(r)

codes = list(by_code.keys())[:N]
sample = []
for i, code in enumerate(codes):
    want_persona = PERSONAS[i % len(PERSONAS)]
    cands = by_code[code]
    chosen = next((r for r in cands if r["persona"] == want_persona), cands[0])
    sample.append({
        "id": chosen["id"],
        "persona": chosen["persona"],
        "query": chosen["query"],
        "expected_code": chosen["expected_code"],
        "source_id": chosen["source_id"],
    })

with open(OUT, "w") as f:
    json.dump(sample, f, indent=1)

# report
from collections import Counter
pc = Counter(r["persona"] for r in sample)
print("wrote %d rows -> %s" % (len(sample), OUT))
print("distinct codes:", len({r["expected_code"] for r in sample}))
print("persona spread:", dict(pc))
print("ids:", ",".join(str(r["id"]) for r in sample))
