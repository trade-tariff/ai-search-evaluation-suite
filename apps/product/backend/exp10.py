"""Experiment 10: measure the dedup impact on the facts matrix (priciest KG block).
The facts matrix overlaps with sources already sent elsewhere:
 - description_llm facts <- extracted from the code's description, which is already in the candidate shortlist
 - atar facts          <- duplicate the ATAR rationale edges
Dedup = send the distilled fact OR the raw source, not both. Measures the token (=cost) saving.
Token-only (tiktoken), no LLM calls -> run over many covered queries cheaply.
"""
import os, statistics as st
import journey.classification as C
import tiktoken, psycopg
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))

DSN = os.environ["TARIFF_DB_DSN"]
with psycopg.connect(DSN) as conn:
    rows = conn.execute("""
        SELECT DISTINCT ON (g.expected_code) g.query, g.expected_code
        FROM kg.eval_gold g WHERE g.active AND g.persona='naive_specific'
          AND EXISTS (SELECT 1 FROM kg.commodity_facets cf WHERE cf.commodity_code=g.expected_code AND cf.source='description_llm')
        ORDER BY g.expected_code LIMIT 20
    """).fetchall()
QUERIES = [r[0] for r in rows]
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

res=[]
for q in QUERIES:
    try:
        cands,_=C.initial_candidates_for_eliminate(q,cfg,candidate_limit=100)
        codes=[c["commodity_code"] for c in cands]
        facts_all   = C._structured_facts_section(codes)
        facts_nodesc= facts_filtered(codes, lambda s: not s.startswith("description_llm"))
        facts_dedup = facts_filtered(codes, lambda s: not (s.startswith("description_llm") or s.startswith("atar:")))
        edges       = C._kg_edges_section(codes, include=ALL_EDGE)
        res.append({"q":q,"facts_all":ntok(facts_all),"facts_nodesc":ntok(facts_nodesc),
                    "facts_dedup":ntok(facts_dedup),"edges":ntok(edges)})
        print(".",end="",flush=True)
    except Exception as e:
        print("x",end="",flush=True)
print(f"\n\nn={len(res)} covered queries, N=100")
def a(k): return st.mean([r[k] for r in res])
fa,fn,fd,ed = a("facts_all"),a("facts_nodesc"),a("facts_dedup"),a("edges")
print(f"facts matrix (all sources):      {fa:7.0f} tok")
print(f"  - drop description_llm:        {fn:7.0f} tok   (saves {fa-fn:.0f}, {100*(fa-fn)/fa:.0f}% of facts)")
print(f"  - drop description_llm + atar: {fd:7.0f} tok   (saves {fa-fd:.0f}, {100*(fa-fd)/fa:.0f}% of facts)")
print(f"edges (raw grounding, kept):     {ed:7.0f} tok")
# cost impact: token saving on the all-KG block, priced gpt-5.5 input, 3.42 rounds, 310k
def mo_in(tok): return tok*5/1e6*3.42*310_000/1000   # input-side $/mo (k)
print(f"\nALL-KG block: full = {fa+ed:.0f} tok ; deduped = {fd+ed:.0f} tok")
print(f"DEDUP SAVING: {fa-fd:.0f} input tok/round -> ~${mo_in(fa-fd):.0f}k/mo @ gpt-5.5, 3.42 rounds, 310k (input side)")
print(f"  (ranking all-KG ~$80k -> ~${80-mo_in(fa-fd):.0f}k ; the facts matrix shrinks ~{100*(fa-fd)/fa:.0f}%)")
