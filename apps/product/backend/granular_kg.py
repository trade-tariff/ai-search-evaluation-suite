"""Granular KG cost breakdown on the live VM.

For each query, freeze the 80-candidate set, then build each KG sub-block in
isolation (facts by source; edges by category) and fire a REAL gpt-5.5 medium
call per config, capturing resp.usage. Also tiktoken-counts each block for the
exact per-part INPUT contribution. Price: gpt-5.5 = $5/M in, $30/M out (real).
"""
import os, json, threading, statistics as st
from concurrent.futures import ThreadPoolExecutor, as_completed
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
import openai.resources.responses as RR
import tiktoken
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))

PIN, POUT = 5.0, 30.0  # $/1M, confirmed gpt-5.5
QUERIES = ["rubber sole sneakers", "red wine", "lithium battery for a laptop",
           "chocolate protein powder", "stainless steel machine part"]
CAND = 80
FACTS_OFF = "Disabled by config."
EDGES_OFF = "No KG edges applicable to this candidate set (with current config)."
ALL_EDGE = {"chapter_notes": True, "section_notes": True, "girs": True,
            "atar_rationales": True, "heading_rules": True, "other_global": True, "hsen": True}
def only(*keys):
    d = {k: False for k in ALL_EDGE}
    for k in keys: d[k] = True
    return d

# capture usage per thread
_orig = RR.Responses.create
_tl = threading.local()
def _cap(self, *a, **k):
    r = _orig(self, *a, **k)
    try: _tl.usage = r.usage.model_dump()
    except Exception: _tl.usage = None
    return r
RR.Responses.create = _cap

cfg = dict(C.DEFAULT_CLASSIFY_CONFIG)
cfg["use_session_facts"] = False

def facts_filtered(codes, pred):
    """Render the facts matrix but only for facets whose source matches pred."""
    orig = C.db_facets_for_codes
    def patched(cs):
        out = {}
        for code, facets in orig(cs).items():
            keep = [f for f in facets if pred(str(f.get("source") or ""))]
            if keep: out[code] = keep
        return out
    C.db_facets_for_codes = patched
    try:
        return C._structured_facts_section(codes)
    finally:
        C.db_facets_for_codes = orig

def build_blocks(q):
    cands, _ = C.initial_candidates_for_eliminate(q, cfg, candidate_limit=CAND)
    codes = [c["commodity_code"] for c in cands]
    facts_all = C._structured_facts_section(codes)
    blocks = {
        "facts_all":   facts_all,
        "facts_desc":  facts_filtered(codes, lambda s: s.startswith("description_llm")),
        "facts_atar":  facts_filtered(codes, lambda s: s.startswith("atar:")),
        "facts_llm":   facts_filtered(codes, lambda s: "commodity_llm" in s),
        "edges_all":   C._kg_edges_section(codes, include=ALL_EDGE),
        "edges_notes": C._kg_edges_section(codes, include=only("chapter_notes", "section_notes")),
        "edges_head":  C._kg_edges_section(codes, include=only("heading_rules")),
        "edges_girs":  C._kg_edges_section(codes, include=only("girs")),
        "edges_hsen":  C._kg_edges_section(codes, include=only("hsen")),
        "edges_atar":  C._kg_edges_section(codes, include=only("atar_rationales")),
        "edges_glob":  C._kg_edges_section(codes, include=only("other_global")),
    }
    return cands, blocks

# config = (label, facts_block_key_or_OFF, edges_block_key_or_OFF)
CONFIGS = [
    ("no_kg",            None, None),
    ("FULL",             "facts_all", "edges_all"),
    ("facts_only(all)",  "facts_all", None),
    ("edges_only(all)",  None, "edges_all"),
    ("facts:desc_llm",   "facts_desc", None),
    ("facts:atar",       "facts_atar", None),
    ("facts:commod_llm", "facts_llm", None),
    ("edges:notes(ch+sec)", None, "edges_notes"),
    ("edges:heading",    None, "edges_head"),
    ("edges:girs",       None, "edges_girs"),
    ("edges:hsen",       None, "edges_hsen"),
    ("edges:atar_ration",None, "edges_atar"),
    ("edges:other_glob", None, "edges_glob"),
]

def run_one(q, cands, blocks, label, fk, ek):
    ftext = blocks.get(fk, FACTS_OFF) if fk else FACTS_OFF
    etext = blocks.get(ek, EDGES_OFF) if ek else EDGES_OFF
    _tl.usage = None
    parsed, dbg = C._llm_eliminate(
        raw_query=q, candidates=cands, qa_history=[], session_facts=[],
        structured_facts_text=ftext, kg_edges_text=etext,
        prompt_mode="baseline", kg_include=None, model="gpt-5.5",
        max_questions=7, require_question=True)
    u = getattr(_tl, "usage", None) or {}
    return {
        "q": q, "label": label,
        "facts_tok": ntok(ftext) if fk else 0,
        "edges_tok": ntok(etext) if ek else 0,
        "in": u.get("input_tokens"), "out": u.get("output_tokens"),
        "reason": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "err": dbg.get("api_error"),
    }

print("provider_calls_allowed=", C.provider_calls_allowed())
PER_Q = {}
tasks = []
for q in QUERIES:
    cands, blocks = build_blocks(q)
    PER_Q[q] = (cands, blocks)
    for (label, fk, ek) in CONFIGS:
        tasks.append((q, label, fk, ek))
print(f"queries={len(QUERIES)} configs={len(CONFIGS)} total_calls={len(tasks)}")

results = []
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(run_one, q, PER_Q[q][0], PER_Q[q][1], label, fk, ek): (q, label)
            for (q, label, fk, ek) in tasks}
    for fut in as_completed(futs):
        q, label = futs[fut]
        try:
            results.append(fut.result())
        except Exception as e:
            results.append({"q": q, "label": label, "err": repr(e)})
        print(".", end="", flush=True)
print("\n")

# aggregate per config
def agg(label):
    rows = [r for r in results if r["label"] == label and r.get("in")]
    if not rows: return None
    def m(k): return st.mean([r[k] for r in rows if r.get(k) is not None]) if any(r.get(k) is not None for r in rows) else 0
    return {
        "n": len(rows), "facts_tok": m("facts_tok"), "edges_tok": m("edges_tok"),
        "in": m("in"), "out": m("out"), "reason": m("reason"),
    }

base = agg("no_kg")
print("="*110)
print(f"{'config':22} {'n':>2} {'facts_tk':>8} {'edges_tk':>8} {'in_tok':>7} {'out_tok':>7} {'reason':>6} "
      f"{'$/round':>8} {'dIN_tok':>7} {'dIN$/mo@300k*3.42r':>18}")
print("-"*110)
for (label, _, _) in CONFIGS:
    a = agg(label)
    if not a:
        print(f"{label:22} (no data / empty)"); continue
    cost = a["in"]*PIN/1e6 + a["out"]*POUT/1e6
    d_in = a["in"] - (base["in"] if base else 0)
    d_in_mo = d_in*PIN/1e6 * 3.42 * 300_000  # input-$ delta if this part added, 3.42 rounds, 300k
    print(f"{label:22} {a['n']:>2} {a['facts_tok']:>8.0f} {a['edges_tok']:>8.0f} {a['in']:>7.0f} "
          f"{a['out']:>7.0f} {a['reason']:>6.0f} {cost:>8.4f} {d_in:>7.0f} {d_in_mo:>16,.0f}")
print("="*110)
print("dIN_tok = input tokens vs no_kg floor (the part's marginal input). "
      "dIN$/mo = that input delta priced at $5/M x 3.42 rounds x 300k searches.")
