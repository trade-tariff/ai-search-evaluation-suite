"""Experiment 2 (covered CCs): prune lever + HSEN raw-vs-noise.

A. CANDIDATE-COUNT SWEEP (the deterministic-prune proxy): nest the same pool to
   N=80/40/20/10, run real gpt-5.5 medium, measure how OUTPUT (dominant cost)
   shrinks as candidates are pruned.
B. HSEN on/off at N=80 & N=40 - HSEN's marginal cost on covered sets.
C. OFFLINE: of the HSEN tokens we actually send, how many are subheading-listing
   NOISE vs operative prose. Quantifies "bulk raw useless data".
Price gpt-5.5 = $5/M in, $30/M out. Queries land on FACT-COVERED codes.
"""
import os, re, threading, statistics as st
from concurrent.futures import ThreadPoolExecutor, as_completed
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
import openai.resources.responses as RR
import tiktoken
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))
PIN, POUT = 5.0, 30.0

# covered eval-gold queries (expected code has description_llm facts)
QUERIES = [
    "stainless steel rotary pump mechanical seal ring",  # 7326909290 ch73
    "rubber sole sneakers",                               # ch64 footwear
    "nitrile rubber bellows for pump mechanical seal",    # 4016999790 ch40
    "red wine",                                           # ch22
]
ALL_EDGE = {"chapter_notes": True, "section_notes": True, "girs": True,
            "atar_rationales": True, "heading_rules": True, "other_global": True, "hsen": True}
def edge_inc(hsen): d = dict(ALL_EDGE); d["hsen"] = hsen; return d

_orig = RR.Responses.create
_tl = threading.local()
def _cap(self, *a, **k):
    r = _orig(self, *a, **k)
    try: _tl.usage = r.usage.model_dump()
    except Exception: _tl.usage = None
    return r
RR.Responses.create = _cap

cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"] = False

def run_one(q, pool, label, N, hsen_on):
    cands = pool[:N]
    codes = [c["commodity_code"] for c in cands]
    ftext = C._structured_facts_section(codes)
    etext = C._kg_edges_section(codes, include=edge_inc(hsen_on))
    _tl.usage = None
    _, dbg = C._llm_eliminate(raw_query=q, candidates=cands, qa_history=[], session_facts=[],
        structured_facts_text=ftext, kg_edges_text=etext, prompt_mode="baseline",
        kg_include=None, model="gpt-5.5", max_questions=7, require_question=True)
    u = getattr(_tl, "usage", None) or {}
    return {"q": q, "label": label, "N": N, "hsen": hsen_on,
            "facts_tok": ntok(ftext), "edges_tok": ntok(etext),
            "in": u.get("in") or u.get("input_tokens"), "out": u.get("output_tokens"),
            "reason": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
            "err": dbg.get("api_error")}

NOMEN = re.compile(r"^\s*(\d{4}(\.\d{2})?\s*-|-+\s)")  # subheading-listing / dash lines
def hsen_noise(pool):
    """Offline: of HSEN body chars we'd send (cap 600/edge), share that is nomenclature noise."""
    codes = [c["commodity_code"] for c in pool]
    edges = [e for e in C.db_kg_edges(codes, include={**{k: False for k in ALL_EDGE}, "hsen": True})]
    noise_tok = sig_tok = 0
    for e in edges:
        body = (e.get("body") or "")[:600]   # what the renderer would send
        for ln in body.split("\n"):
            t = ntok(ln)
            if NOMEN.match(ln): noise_tok += t
            else: sig_tok += t
    return len(edges), noise_tok, sig_tok

CONFIGS = [("N80_full",80,True),("N40_full",40,True),("N20_full",20,True),
           ("N10_full",10,True),("N80_noHSEN",80,False),("N40_noHSEN",40,False)]

print("provider_calls_allowed=", C.provider_calls_allowed())
POOL = {}; noise_rows = []
for q in QUERIES:
    pool, _ = C.initial_candidates_for_eliminate(q, cfg, candidate_limit=80)
    POOL[q] = pool
    ne, ntk, stk = hsen_noise(pool)
    noise_rows.append((q, ne, ntk, stk))
print(f"queries={len(QUERIES)} configs={len(CONFIGS)} calls={len(QUERIES)*len(CONFIGS)}")

results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(run_one, q, POOL[q], lbl, N, h): (q, lbl)
            for q in QUERIES for (lbl, N, h) in CONFIGS}
    for fut in as_completed(futs):
        try: results.append(fut.result())
        except Exception as e: results.append({"err": repr(e), "label": futs[fut][1]})
        print(".", end="", flush=True)
print("\n")

def agg(label):
    rows = [r for r in results if r.get("label") == label and r.get("in")]
    if not rows: return None
    m = lambda k: st.mean([r[k] for r in rows if r.get(k) is not None])
    return {"n": len(rows), "in": m("in"), "out": m("out"), "reason": m("reason"),
            "facts_tok": m("facts_tok"), "edges_tok": m("edges_tok")}

print("="*104)
print("A+B  COST vs CANDIDATE COUNT (prune lever) and HSEN on/off  [gpt-5.5 medium, covered codes]")
print(f"{'config':12} {'N':>3} {'hsen':>4} {'facts_tk':>8} {'edges_tk':>8} {'in_tok':>7} {'out_tok':>7} {'$/round':>8} {'$/srch@1r':>9} {'$/srch@3.42r':>12}")
print("-"*104)
base_out = None
for (lbl, N, h) in CONFIGS:
    a = agg(lbl)
    if not a: print(f"{lbl:12} (no data)"); continue
    cost = a["in"]*PIN/1e6 + a["out"]*POUT/1e6
    if lbl == "N80_full": base_out = a["out"]
    print(f"{lbl:12} {N:>3} {str(h):>4} {a['facts_tok']:>8.0f} {a['edges_tok']:>8.0f} {a['in']:>7.0f} {a['out']:>7.0f} {cost:>8.4f} {cost:>9.3f} {cost*3.42:>12.3f}")
print("-"*104)
if base_out:
    for (lbl, N, h) in CONFIGS:
        a = agg(lbl)
        if a and h: print(f"  out(N={N}) / out(N=80) = {a['out']/base_out:.2f}")
print("="*104)
print("C  HSEN content quality (offline; of the <=600 chars/edge we send):")
print(f"{'query':50} {'hsen_edges':>10} {'noise_tok':>9} {'signal_tok':>10} {'noise%':>7}")
for (q, ne, ntk, stk) in noise_rows:
    tot = ntk + stk
    print(f"{q[:50]:50} {ne:>10} {ntk:>9} {stk:>10} {100*ntk/tot if tot else 0:>6.0f}%")
print("noise = subheading-listing / dash lines (redundant with the shortlist); signal = operative prose.")
