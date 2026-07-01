"""Experiment 4: cost/month @ 300k by KG config. Real config: N=100, gpt-5.5 medium,
covered eval-gold codes. Captures real resp.usage in/out per config + exact tiktoken input.
Base = no-KG (staging AI search). KG options layered. Price gpt-5.5 = $5/M in, $30/M out.
"""
import os, threading, statistics as st
from concurrent.futures import ThreadPoolExecutor, as_completed
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
import openai.resources.responses as RR
import tiktoken
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))
PIN, POUT = 5.0, 30.0
N = 100

QUERIES = [
    "stainless steel rotary pump mechanical seal ring",  # 7326909290 ch73
    "rubber sole sneakers",                               # ch64
    "nitrile rubber bellows for pump mechanical seal",    # 4016999790 ch40
    "red wine",                                           # ch22
    "recycled plastic bracelet with metal clasp",         # 7117900000 ch71
    "metal machine part",                                 # ch73/84
]
FACTS_OFF = "Disabled by config."
EDGES_OFF = "No KG edges applicable to this candidate set (with current config)."
ALLE = {"chapter_notes": True, "section_notes": True, "girs": True,
        "atar_rationales": True, "heading_rules": True, "other_global": True, "hsen": True}
def only(*keys):
    d = {k: False for k in ALLE};
    for k in keys: d[k] = True
    return d

_orig = RR.Responses.create
_tl = threading.local()
def _cap(self, *a, **k):
    r = _orig(self, *a, **k)
    try: _tl.usage = r.usage.model_dump()
    except Exception: _tl.usage = None
    return r
RR.Responses.create = _cap
cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"] = False

def facts_filtered(codes, pred):
    orig = C.db_facets_for_codes
    def patched(cs):
        out = {}
        for code, facets in orig(cs).items():
            keep = [f for f in facets if pred(str(f.get("source") or ""))]
            if keep: out[code] = keep
        return out
    C.db_facets_for_codes = patched
    try: return C._structured_facts_section(codes)
    finally: C.db_facets_for_codes = orig

def build(q):
    pool, _ = C.initial_candidates_for_eliminate(q, cfg, candidate_limit=N)
    codes = [c["commodity_code"] for c in pool]
    facts_all = C._structured_facts_section(codes)
    facts_atar = facts_filtered(codes, lambda s: s.startswith("atar:"))
    cfgs = {
        "base_no_kg": (FACTS_OFF, EDGES_OFF),
        "kg_notes":   (FACTS_OFF, C._kg_edges_section(codes, include=only("chapter_notes", "section_notes"))),
        "kg_atar":    (facts_atar, C._kg_edges_section(codes, include=only("atar_rationales"))),
        "kg_hsen":    (FACTS_OFF, C._kg_edges_section(codes, include=only("hsen"))),
        "kg_girs":    (FACTS_OFF, C._kg_edges_section(codes, include=only("girs"))),
        "kg_facts":   (facts_all, EDGES_OFF),
        "kg_all":     (facts_all, C._kg_edges_section(codes, include=ALLE)),
    }
    return pool, cfgs

def run(q, pool, label, ftext, etext):
    _tl.usage = None
    _, dbg = C._llm_eliminate(raw_query=q, candidates=pool, qa_history=[], session_facts=[],
        structured_facts_text=ftext, kg_edges_text=etext, prompt_mode="baseline",
        kg_include=None, model="gpt-5.5", max_questions=7, require_question=True)
    u = getattr(_tl, "usage", None) or {}
    return {"q": q, "label": label, "in": u.get("input_tokens"), "out": u.get("output_tokens"),
            "kg_in": ntok(ftext if ftext != FACTS_OFF else "") + ntok(etext if etext != EDGES_OFF else ""),
            "err": dbg.get("api_error")}

print("provider_calls_allowed=", C.provider_calls_allowed(), "N=", N)
POOL = {}; tasks = []
for q in QUERIES:
    pool, cfgs = build(q); POOL[q] = (pool, cfgs)
    for label, (ft, et) in cfgs.items():
        tasks.append((q, label, ft, et))
print(f"queries={len(QUERIES)} configs=7 calls={len(tasks)}")

results = []
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(run, q, POOL[q][0], lbl, ft, et): lbl for (q, lbl, ft, et) in tasks}
    for fut in as_completed(futs):
        try: results.append(fut.result())
        except Exception as e: results.append({"label": futs[fut], "err": repr(e)})
        print(".", end="", flush=True)
print("\n")

ORDER = ["base_no_kg", "kg_notes", "kg_atar", "kg_hsen", "kg_girs", "kg_facts", "kg_all"]
def agg(label):
    rows = [r for r in results if r.get("label") == label and r.get("in")]
    if not rows: return None
    m = lambda k: st.mean([r[k] for r in rows if r.get(k) is not None])
    return {"n": len(rows), "in": m("in"), "out": m("out"), "kg_in": m("kg_in")}

base = agg("base_no_kg")
print("="*112)
print(f"COST BY KG CONFIG  [gpt-5.5 medium, N={N}, covered codes]   price $5/M in, $30/M out")
print(f"{'config':12} {'n':>2} {'kg_in_tk':>8} {'in_tok':>7} {'out_tok':>7} {'$/round':>8}  "
      f"{'$/mo @300k:':>11} {'r=1':>8} {'r=2':>9} {'r=3.42':>10}")
print("-"*112)
for label in ORDER:
    a = agg(label)
    if not a: print(f"{label:12} (no data)"); continue
    cost = a["in"]*PIN/1e6 + a["out"]*POUT/1e6
    mo = lambda r: cost*r*300_000
    print(f"{label:12} {a['n']:>2} {a['kg_in']:>8.0f} {a['in']:>7.0f} {a['out']:>7.0f} {cost:>8.4f}  "
          f"{'':>11} {mo(1):>8,.0f} {mo(2):>9,.0f} {mo(3.42):>10,.0f}")
print("="*112)
print("kg_in_tk = tiktoken tokens the KG block adds (exact). $/mo = $/round x rounds x 300,000.")
print("NOTE: output is the dominant + noisy term; KG deltas live mostly in kg_in (input).")
