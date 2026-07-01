"""Experiment 9: rounds across ~50 queries spanning trader personas (input quality).
Real staging interactive_search loop @ N=100, gpt-5.5 medium, oracle simulator.
Sample = ~8 products x 7 personas (naive_vague/branded/specific, emu_generic/ordinary/specific, original).
Measures rounds + per-search tokens, broken down by persona / input-quality bucket.
"""
import os, json, re, statistics as st
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
from openai import OpenAI
import tiktoken, psycopg
ENC = tiktoken.get_encoding("o200k_base")
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MAXQ = 7
DSN = os.environ["TARIFF_DB_DSN"]
with psycopg.connect(DSN) as conn:
    TEMPLATE = conn.execute("select value#>>'{}' from uk.admin_configurations where name='search_context'").fetchone()[0]
    rows = conn.execute("""
        WITH picked AS (
          SELECT DISTINCT ON (left(expected_code,2)) expected_code
          FROM kg.eval_gold g WHERE active
            AND EXISTS (SELECT 1 FROM kg.commodity_facets cf WHERE cf.commodity_code=g.expected_code AND cf.source='description_llm')
          ORDER BY left(expected_code,2), expected_code LIMIT 8)
        SELECT DISTINCT ON (g.expected_code, g.persona) g.persona, g.query, g.expected_code
        FROM kg.eval_gold g JOIN picked p ON p.expected_code=g.expected_code
        WHERE g.active ORDER BY g.expected_code, g.persona
    """).fetchall()
QUERIES = [(r[0], r[1], r[2]) for r in rows]
QUALITY = {"naive_vague":"shit","emu_generic":"shit","naive_branded":"mid","emu_ordinary":"mid",
           "naive_specific":"good","emu_specific":"good","original":"good"}
cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"]=False

def cand_json(c): return json.dumps([{"commodity_code":x["commodity_code"],"description":x.get("description"),"score":round(float(x.get("score",0)),3)} for x in c])
def fill(query,cj,qa):
    qaj = json.dumps([{"index":i,"question":x["q"],"answer":x["a"]} for i,x in enumerate(qa)]) if qa else "[]"
    return (TEMPLATE.replace("%{search_input}",query).replace("%{expanded_query}",query)
            .replace("%{answers_opensearch}",cj).replace("%{questions}",qaj))
def parse_json(t):
    t=re.sub(r"```(json)?","",t or ""); m=re.search(r"\{.*\}",t,re.S)
    try: return json.loads(m.group(0)) if m else None
    except: return None
def gpt55(p):
    r=client.responses.create(model="gpt-5.5",input=[{"role":"user","content":p}],reasoning={"effort":"medium"},timeout=300)
    u=r.usage.model_dump(); return r.output_text,u.get("input_tokens",0),u.get("output_tokens",0)
def simulate(query,oracle,q,opts):
    m=(f"You are the trader. Product: {query}. Correct commodity code: {oracle}. Question: {q}\nOptions: {opts}\nReply ONLY the single best option text, verbatim.")
    r=client.chat.completions.create(model="gpt-5-mini",messages=[{"role":"user","content":m}],reasoning_effort="low",max_completion_tokens=500)
    return (r.choices[0].message.content or "").strip()

def run(item):
    persona,query,oracle=item
    try:
        cands,_=C.initial_candidates_for_eliminate(query,cfg,candidate_limit=100)
        cj=cand_json(cands); qa=[]; tin=tout=0; rounds=0; mode="?"
        for rnd in range(1,MAXQ+1):
            rounds=rnd; txt,i,o=gpt55(fill(query,cj,qa)); tin+=i; tout+=o
            p=parse_json(txt) or {}
            if p.get("answers"): mode="answers"; break
            if p.get("questions"):
                q=p["questions"][0]; qa.append({"q":q.get("question",""),"a":simulate(query,oracle,q.get("question",""),q.get("options",[]))})
            else: mode=p.get("error","noparse"); break
        print(f"  {persona:15} {query[:30]:30} rounds={rounds} {mode}", flush=True)
        return {"persona":persona,"quality":QUALITY.get(persona,"?"),"rounds":rounds,"mode":mode,"in":tin,"out":tout}
    except Exception as e:
        print(f"  ERR {persona} {query[:25]}: {repr(e)[:70]}", flush=True)
        return {"persona":persona,"err":repr(e)[:80]}

print(f"queries={len(QUERIES)} (N=100 staging loop, max {MAXQ} rounds each)")
res=[]
with ThreadPoolExecutor(max_workers=8) as ex:
    for r in ex.map(run, QUERIES): res.append(r)
ok=[r for r in res if r.get("rounds")]
def stats(rows):
    rd=[r["rounds"] for r in rows]
    return (len(rows), st.mean(rd), st.median(rd), sum(1 for r in rows if r["mode"]=="answers"),
            st.mean([r["in"] for r in rows]), st.mean([r["out"] for r in rows]))
print("\n=== ROUNDS by INPUT QUALITY (staging loop, N=100, gpt-5.5) ===")
for bucket in ["shit","mid","good"]:
    rows=[r for r in ok if r["quality"]==bucket]
    if rows:
        n,mu,md,conv,ai,ao=stats(rows)
        print(f"  {bucket:5}: n={n:>2}  mean_rounds={mu:.2f}  median={md}  converged={conv}/{n}  in/search={ai:.0f} out/search={ao:.0f}")
print("\n=== by PERSONA ===")
for p in ["naive_vague","emu_generic","naive_branded","emu_ordinary","naive_specific","emu_specific","original"]:
    rows=[r for r in ok if r["persona"]==p]
    if rows:
        n,mu,md,conv,ai,ao=stats(rows); print(f"  {p:15} n={n:>2} mean_rounds={mu:.2f} median={md} converged={conv}/{n}")
n,mu,md,conv,ai,ao=stats(ok)
print(f"\n=== OVERALL n={n} mean_rounds={mu:.2f} median={md} converged={conv}/{n} in/search={ai:.0f} out/search={ao:.0f} ===")
print("rounds dist:", sorted([r["rounds"] for r in ok]))
errs=[r for r in res if r.get("err")]
if errs: print(f"errors: {len(errs)}")
