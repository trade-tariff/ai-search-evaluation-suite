"""Experiment 11: dedup accuracy eval (gold set, dedup_prompt_facts on/off).
Paired: same gold query + same retrieved candidates, run the eliminate round with
FULL facts vs DEDUPED facts (drop description_llm + atar:), gpt-5.5 medium.
Measures whether the gold code survives and how it ranks - does dropping the
distilled facts hurt discrimination?
"""
import os, json, threading, statistics as st
from concurrent.futures import ThreadPoolExecutor, as_completed
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
import psycopg

DSN = os.environ["TARIFF_DB_DSN"]
with psycopg.connect(DSN) as conn:
    rows = conn.execute("""
        SELECT DISTINCT ON (g.expected_code, g.persona) g.query, g.expected_code, g.persona
        FROM kg.eval_gold g WHERE g.active
          AND g.persona IN ('naive_specific','naive_vague','original','emu_specific')
          AND EXISTS (SELECT 1 FROM kg.commodity_facets cf WHERE cf.commodity_code=g.expected_code AND cf.source='description_llm')
        ORDER BY g.expected_code, g.persona LIMIT 40
    """).fetchall()
QUERIES = [(r[0], r[1], r[2]) for r in rows]
cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"] = False
ALL_EDGE = {"chapter_notes":True,"section_notes":True,"girs":True,"atar_rationales":True,"heading_rules":True,"other_global":True,"hsen":True}

def facts_filtered(codes, pred):
    orig = C.db_facets_for_codes
    def patched(cs):
        out={}
        for code,facets in orig(cs).items():
            keep=[f for f in facets if pred(str(f.get("source") or ""))]
            if keep: out[code]=keep
        return out
    C.db_facets_for_codes = patched
    try: return C._structured_facts_section(codes)
    finally: C.db_facets_for_codes = orig

def survivors_of(query, cands, ftext, etext):
    parsed, dbg = C._llm_eliminate(raw_query=query, candidates=cands, qa_history=[], session_facts=[],
        structured_facts_text=ftext, kg_edges_text=etext, prompt_mode="baseline",
        kg_include=None, model="gpt-5.5", max_questions=7, require_question=False)
    if not parsed: return None
    surv = parsed.get("survivors") or []
    return [s.get("commodity_code") for s in surv]

def run(item):
    query, gold, persona = item
    try:
        cands,_ = C.initial_candidates_for_eliminate(query, cfg, candidate_limit=100)
        codes=[c["commodity_code"] for c in cands]
        if gold not in codes:
            return {"persona":persona,"retrieved":False}   # retrieval miss - not a facts issue
        edges = C._kg_edges_section(codes, include=ALL_EDGE)
        facts_full  = C._structured_facts_section(codes)
        facts_dedup = facts_filtered(codes, lambda s: not (s=="description_llm" or s.startswith("atar:")))
        out={"persona":persona,"retrieved":True,"gold":gold}
        for cond, ft in [("full",facts_full),("dedup",facts_dedup)]:
            sc = survivors_of(query, cands, ft, edges)
            if sc is None: out[cond]={"err":True}; continue
            rank = sc.index(gold) if gold in sc else None
            out[cond]={"gold_in":gold in sc,"rank":rank,"top1":(sc[0]==gold if sc else False),
                       "top5":(gold in sc[:5]),"nsurv":len(sc)}
        print(".",end="",flush=True); return out
    except Exception as e:
        print("x",end="",flush=True); return {"persona":persona,"err":repr(e)[:80]}

print(f"queries={len(QUERIES)} (paired full vs dedup = {2*len(QUERIES)} gpt-5.5 calls)")
res=[]
with ThreadPoolExecutor(max_workers=6) as ex:
    for r in ex.map(run, QUERIES): res.append(r)
print("\n")
ev=[r for r in res if r.get("retrieved") and r.get("full") and r.get("dedup") and not r["full"].get("err") and not r["dedup"].get("err")]
miss=[r for r in res if r.get("retrieved")==False]
print(f"evaluable (gold retrieved + both ran): {len(ev)} ; retrieval misses excluded: {len(miss)} ; n_total={len(res)}")
def rate(cond,key): return 100*sum(1 for r in ev if r[cond][key])/len(ev) if ev else 0
def meanrank(cond):
    rs=[r[cond]["rank"] for r in ev if r[cond]["rank"] is not None]; return st.mean(rs) if rs else None
print("="*72)
print(f"{'metric':22}{'FULL facts':>14}{'DEDUP facts':>14}{'delta':>10}")
for label,key in [("gold retained %","gold_in"),("gold top-1 %","top1"),("gold top-5 %","top5")]:
    f,d=rate("full",key),rate("dedup",key); print(f"{label:22}{f:>13.0f}%{d:>13.0f}%{d-f:>+9.0f}pp")
mf,md=meanrank("full"),meanrank("dedup")
print(f"{'mean gold rank':22}{mf:>14.1f}{md:>14.1f}{(md-mf):>+10.1f}")
# per-query flips: dedup lost a gold that full kept
lost=[r for r in ev if r["full"]["gold_in"] and not r["dedup"]["gold_in"]]
gained=[r for r in ev if not r["full"]["gold_in"] and r["dedup"]["gold_in"]]
print(f"\ndedup LOST gold (full kept, dedup dropped): {len(lost)} ; dedup GAINED: {len(gained)}")
print("="*72)
print("VERDICT: dedup is SAFE if gold-retained/top-1 hold (delta ~0) and few/no losses.")
