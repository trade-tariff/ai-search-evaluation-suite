"""Experiment 7: measure the REAL OTT staging interactive_search prompt + output.
Reads the live search_context template from uk.admin_configurations, fills it with
50 retrieved candidates (staging format), measures input (tiktoken) + real output
(gpt-5.5 medium, current staging model). Also measures the +ALL-KG variant.
Then the cost matrix = these tokens priced across the top-4 models.
"""
import os, json, threading, statistics as st
from concurrent.futures import ThreadPoolExecutor, as_completed
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
import openai.resources.responses as RR
from openai import OpenAI
import tiktoken, psycopg
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# 1) real staging prompt template from the DB
dsn = os.environ.get("TARIFF_DB_DSN")
with psycopg.connect(dsn) as conn:
    TEMPLATE = conn.execute("select value#>>'{}' from uk.admin_configurations where name='search_context'").fetchone()[0]
print("template chars:", len(TEMPLATE), " tokens:", ntok(TEMPLATE))

QUERIES = ["stainless steel rotary pump mechanical seal ring", "rubber sole sneakers",
           "chocolate soy and whey protein powder", "epdm rubber bellows for water pump seal",
           "fried red green and yellow bell pepper crisps"]
ALL_EDGE = {"chapter_notes": True, "section_notes": True, "girs": True,
            "atar_rationales": True, "heading_rules": True, "other_global": True, "hsen": True}
cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"] = False

def staging_prompt(query, cands):
    cj = json.dumps([{"commodity_code": c["commodity_code"],
                      "description": c.get("description"), "score": round(float(c.get("score", 0)), 3)} for c in cands])
    return (TEMPLATE.replace("%{search_input}", query).replace("%{expanded_query}", query)
            .replace("%{answers_opensearch}", cj).replace("%{questions}", "[]"))

def call(prompt):
    r = client.responses.create(model="gpt-5.5",
        input=[{"role": "user", "content": prompt}], reasoning={"effort": "medium"}, timeout=300)
    u = r.usage.model_dump()
    return u.get("input_tokens"), u.get("output_tokens"), (u.get("output_tokens_details") or {}).get("reasoning_tokens")

def one(query):
    cands, _ = C.initial_candidates_for_eliminate(query, cfg, candidate_limit=50)
    base = staging_prompt(query, cands)
    codes = [c["commodity_code"] for c in cands]
    kg_block = "\n\n## Knowledge context\n" + C._structured_facts_section(codes) + "\n" + C._kg_edges_section(codes, include=ALL_EDGE)
    kg = base + kg_block
    out = {"q": query, "ncand": len(cands), "base_in_tk": ntok(base), "kg_in_tk": ntok(kg), "kg_block_tk": ntok(kg_block)}
    try:
        i, o, r = call(base); out.update(base_in_real=i, base_out=o, base_reason=r)
    except Exception as e: out["base_err"] = repr(e)[:100]
    try:
        i, o, r = call(kg); out.update(kg_in_real=i, kg_out=o, kg_reason=r)
    except Exception as e: out["kg_err"] = repr(e)[:100]
    return out

print(f"queries={len(QUERIES)} (base + all-KG each = {2*len(QUERIES)} gpt-5.5 calls)")
results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    for r in ex.map(one, QUERIES):
        results.append(r); print(".", end="", flush=True)
print("\n")
for r in results:
    print(json.dumps(r))
print("\n=== AVERAGES (real staging interactive_search, gpt-5.5 medium) ===")
def avg(k):
    xs=[r[k] for r in results if isinstance(r.get(k),(int,float))]
    return st.mean(xs) if xs else None
print(f"  candidates: {avg('ncand'):.0f}")
print(f"  BASE  input(real usage)={avg('base_in_real')}  output={avg('base_out')}  reasoning={avg('base_reason')}")
print(f"  +KG   input(real usage)={avg('kg_in_real')}  output={avg('kg_out')}  reasoning={avg('kg_reason')}")
print(f"  base_in tiktoken={avg('base_in_tk'):.0f}  kg_block tiktoken={avg('kg_block_tk'):.0f}")
