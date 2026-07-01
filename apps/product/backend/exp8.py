"""Experiment 8: REAL staging interactive_search multi-turn loop @ N=100.
Replicates OTT InteractiveSearchService: fill the live search_context template
with 100 candidates + Q&A, call gpt-5.5 medium (JSON), parse answers/questions,
oracle-simulate the answer (gpt-5-mini), loop to max 7. Measures REAL rounds +
per-search token totals (summed over the loop) for base and +ALL-KG @ N=100.
"""
import os, json, re, statistics as st
from concurrent.futures import ThreadPoolExecutor
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
from openai import OpenAI
import tiktoken, psycopg
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MAXQ = 7
with psycopg.connect(os.environ["TARIFF_DB_DSN"]) as conn:
    TEMPLATE = conn.execute("select value#>>'{}' from uk.admin_configurations where name='search_context'").fetchone()[0]

QUERIES = [
    ("freeze dried fig slices in resealable snack bags","0804209000"),
    ("raw frozen marinated chicken fillets in vacuum bag","1602321190"),
    ("chocolate soy and whey protein powder 1kg tub","1806907090"),
    ("epdm rubber bellows for water pump seal","4016999790"),
    ("fried red green and yellow bell pepper crisps","2005998094"),
    ("50ml peptide moisturiser in glass bottle","3304990000"),
    ("10cm wooden skewers for canapes and bbq","4421910000"),
    ("2mm zinc coated embossed steel sheet panels","7210300090"),
]
ALL_EDGE = {"chapter_notes":True,"section_notes":True,"girs":True,"atar_rationales":True,"heading_rules":True,"other_global":True,"hsen":True}
cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"]=False

def cand_json(cands):
    return json.dumps([{"commodity_code":c["commodity_code"],"description":c.get("description"),"score":round(float(c.get("score",0)),3)} for c in cands])

def fill(query, cj, qa, kg=""):
    qaj = json.dumps([{"index":i,"question":x["q"],"answer":x["a"]} for i,x in enumerate(qa)]) if qa else "[]"
    p = (TEMPLATE.replace("%{search_input}",query).replace("%{expanded_query}",query)
         .replace("%{answers_opensearch}",cj).replace("%{questions}",qaj))
    return p + kg

def parse_json(text):
    text = re.sub(r"```(json)?","",text or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m: return None
    try: return json.loads(m.group(0))
    except: return None

def gpt55(prompt):
    r = client.responses.create(model="gpt-5.5", input=[{"role":"user","content":prompt}], reasoning={"effort":"medium"}, timeout=300)
    u = r.usage.model_dump()
    return r.output_text, u.get("input_tokens",0), u.get("output_tokens",0)

def simulate(query, oracle, question, options):
    msg = (f"You are the trader. Product: {query}. The correct commodity code is {oracle}. "
           f"Question: {question}\nOptions: {options}\nReply with ONLY the single best option text, verbatim.")
    r = client.chat.completions.create(model="gpt-5-mini", messages=[{"role":"user","content":msg}], reasoning_effort="low", max_completion_tokens=500)
    return (r.choices[0].message.content or "").strip()

def run_loop(query, oracle):
    cands,_ = C.initial_candidates_for_eliminate(query, cfg, candidate_limit=100)
    cj = cand_json(cands)
    codes=[c["commodity_code"] for c in cands]
    kg_block = "\n\n## Knowledge context\n"+C._structured_facts_section(codes)+"\n"+C._kg_edges_section(codes, include=ALL_EDGE)
    qa=[]; tin=tout=0; rounds=0; mode="?"
    for rnd in range(1, MAXQ+1):
        rounds=rnd
        txt,i,o = gpt55(fill(query,cj,qa))
        tin+=i; tout+=o
        p = parse_json(txt) or {}
        if p.get("answers"): mode="answers"; break
        if p.get("questions"):
            q=p["questions"][0]; ans=simulate(query,oracle,q.get("question",""),q.get("options",[]))
            qa.append({"q":q.get("question",""),"a":ans})
        else: mode=p.get("error","noparse"); break
    # one all-KG round-1 call @ N=100 for KG input size
    kt_in=kt_out=0
    try:
        _,kt_in,kt_out = gpt55(fill(query,cj,[],kg=kg_block))
    except Exception: pass
    return {"q":query,"ncand":len(cands),"rounds":rounds,"mode":mode,
            "base_in_total":tin,"base_out_total":tout,
            "base_in_perround":round(tin/rounds),"kg_in_round1":kt_in,"kg_out_round1":kt_out}

print(f"queries={len(QUERIES)} N=100 (base loop to max {MAXQ} + 1 all-KG call each)")
results=[]
with ThreadPoolExecutor(max_workers=4) as ex:
    for r in ex.map(lambda qc: run_loop(*qc), QUERIES):
        results.append(r); print(json.dumps(r), flush=True)

def avg(k):
    xs=[r[k] for r in results if isinstance(r.get(k),(int,float))]
    return st.mean(xs) if xs else 0
print("\n=== STAGING interactive_search @ N=100 (gpt-5.5 medium, REAL loop) ===")
print(f"  rounds: mean={avg('rounds'):.2f} median={st.median([r['rounds'] for r in results])} dist={sorted([r['rounds'] for r in results])}")
print(f"  converged(answers): {sum(1 for r in results if r['mode']=='answers')}/{len(results)}")
print(f"  BASE per-search totals: input={avg('base_in_total'):.0f}  output={avg('base_out_total'):.0f}  (per-round in={avg('base_in_perround'):.0f})")
print(f"  +ALL-KG per-round @N=100: input={avg('kg_in_round1'):.0f}  output={avg('kg_out_round1'):.0f}")
