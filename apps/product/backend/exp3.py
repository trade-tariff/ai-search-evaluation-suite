"""Experiment 3: FIX HSEN for a small subset of covered CCs, then test cost.

Distill each raw HSEN note (gpt-5-mini, one-time/offline) into a compact
COVERS/EXCLUDES/TESTS summary, swap it into the rules block, and compare
RAW vs DISTILLED vs NO-HSEN on real gpt-5.5 medium eliminate calls.
Price gpt-5.5 = $5/M in, $30/M out; distill model gpt-5-mini.
"""
import os, threading, statistics as st
from concurrent.futures import ThreadPoolExecutor, as_completed
os.environ["CLASSIFY_REASONING_EFFORT"] = "medium"
import journey.classification as C
import openai.resources.responses as RR
from openai import OpenAI
import tiktoken
ENC = tiktoken.get_encoding("o200k_base")
def ntok(s): return len(ENC.encode(s or ""))
PIN, POUT = 5.0, 30.0
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

QUERIES = ["stainless steel rotary pump mechanical seal ring", "rubber sole sneakers", "red wine"]
ALL_EDGE = {"chapter_notes": True, "section_notes": True, "girs": True,
            "atar_rationales": True, "heading_rules": True, "other_global": True, "hsen": True}
HSEN_ONLY = {**{k: False for k in ALL_EDGE}, "hsen": True}

DISTILL_SYS = (
    "You distil a customs HS Explanatory Note into a compact classification aid. "
    "Output AT MOST 55 words, exactly these fields:\n"
    "COVERS: <essence of what this heading covers>\n"
    "EXCLUDES: <key things explicitly excluded, keep any heading numbers>\n"
    "TESTS: <decisive discriminators, if any>\n"
    "Drop the subheading list, examples, and boilerplate. Be terse.")

def distill(body):
    try:
        r = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "system", "content": DISTILL_SYS},
                      {"role": "user", "content": (body or "")[:6000]}],
            reasoning_effort="low", max_completion_tokens=600)
        return (r.choices[0].message.content or "").strip(), r.usage
    except Exception as e:
        return None, repr(e)

# capture gpt-5.5 usage
_orig = RR.Responses.create
_tl = threading.local()
def _cap(self, *a, **k):
    r = _orig(self, *a, **k)
    try: _tl.usage = r.usage.model_dump()
    except Exception: _tl.usage = None
    return r
RR.Responses.create = _cap

cfg = dict(C.DEFAULT_CLASSIFY_CONFIG); cfg["use_session_facts"] = False
_orig_edges = C.db_kg_edges
DISTILLED = {}

def is_hsen(e):
    return str(e.get("id", "")).startswith("hsen:") or str(e.get("type", "")).startswith("hsen")

def edges_distilled(codes, include=None):
    out = []
    for e in _orig_edges(codes, include=include):
        e2 = dict(e)
        if is_hsen(e) and e.get("id") in DISTILLED:
            e2["body"] = DISTILLED[e["id"]]
        out.append(e2)
    return out

# 1) collect distinct HSEN edges across the covered pools (cap 50), distil them once
POOL = {}
hsen_edges = {}
for q in QUERIES:
    pool, _ = C.initial_candidates_for_eliminate(q, cfg, candidate_limit=80)
    POOL[q] = pool
    codes = [c["commodity_code"] for c in pool]
    for e in _orig_edges(codes, include=HSEN_ONLY):
        if is_hsen(e) and e.get("id"):
            hsen_edges[e["id"]] = e
ids = list(hsen_edges)[:50]
print(f"distinct HSEN edges to fix: {len(hsen_edges)} (distilling {len(ids)})")

distill_cost = 0.0
def do_distill(eid):
    raw = hsen_edges[eid].get("body") or ""
    txt, usage = distill(raw)
    return eid, raw, txt, usage
with ThreadPoolExecutor(max_workers=6) as ex:
    for eid, raw, txt, usage in ex.map(do_distill, ids):
        if txt:
            DISTILLED[eid] = txt
            if hasattr(usage, "prompt_tokens"):
                distill_cost += usage.prompt_tokens*0.75/1e6 + usage.completion_tokens*4.5/1e6
print(f"distilled {len(DISTILLED)} notes; one-time distill cost ~${distill_cost:.4f} (gpt-5-mini)")
# show one example
for eid in DISTILLED:
    print(f"\n--- EXAMPLE {eid} ---\nRAW (first 360): {(hsen_edges[eid].get('body') or '')[:360]}")
    print(f"FIXED: {DISTILLED[eid]}")
    break

def hsen_block_tokens(q, distilled):
    codes = [c["commodity_code"] for c in POOL[q]]
    C.db_kg_edges = edges_distilled if distilled else _orig_edges
    try:
        return ntok(C._kg_edges_section(codes, include=HSEN_ONLY))
    finally:
        C.db_kg_edges = _orig_edges

def run(q, mode):  # mode: raw|fixed|off
    pool = POOL[q]; codes = [c["commodity_code"] for c in pool]
    ftext = C._structured_facts_section(codes)
    if mode == "off":
        C.db_kg_edges = _orig_edges
        etext = C._kg_edges_section(codes, include={**ALL_EDGE, "hsen": False})
    else:
        C.db_kg_edges = edges_distilled if mode == "fixed" else _orig_edges
        try: etext = C._kg_edges_section(codes, include=ALL_EDGE)
        finally: C.db_kg_edges = _orig_edges
    _tl.usage = None
    _, dbg = C._llm_eliminate(raw_query=q, candidates=pool, qa_history=[], session_facts=[],
        structured_facts_text=ftext, kg_edges_text=etext, prompt_mode="baseline",
        kg_include=None, model="gpt-5.5", max_questions=7, require_question=True)
    u = getattr(_tl, "usage", None) or {}
    return {"q": q, "mode": mode, "edges_tok": ntok(etext),
            "in": u.get("input_tokens"), "out": u.get("output_tokens"),
            "reason": (u.get("output_tokens_details") or {}).get("reasoning_tokens"), "err": dbg.get("api_error")}

# HSEN-only block sizes (raw vs fixed)
print("\nHSEN-only block tokens (raw -> fixed):")
for q in QUERIES:
    print(f"  {q[:46]:46} {hsen_block_tokens(q, False):>5} -> {hsen_block_tokens(q, True):>5}")

tasks = [(q, m) for q in QUERIES for m in ("raw", "fixed", "off")]
results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(run, q, m): (q, m) for (q, m) in tasks}
    for fut in as_completed(futs):
        try: results.append(fut.result())
        except Exception as e: results.append({"q": futs[fut][0], "mode": futs[fut][1], "err": repr(e)})
        print(".", end="", flush=True)
print("\n")

def agg(mode):
    rows = [r for r in results if r.get("mode") == mode and r.get("in")]
    if not rows: return None
    m = lambda k: st.mean([r[k] for r in rows if r.get(k) is not None])
    return {"n": len(rows), "edges_tok": m("edges_tok"), "in": m("in"), "out": m("out"), "reason": m("reason")}

print("="*92)
print("HSEN: RAW vs FIXED(distilled) vs OFF  [gpt-5.5 medium, covered codes, N=80]")
print(f"{'mode':6} {'n':>2} {'edges_tk':>8} {'in_tok':>7} {'out_tok':>7} {'$/round':>8} {'$/srch@3.42r':>12}")
print("-"*92)
for mode in ("raw", "fixed", "off"):
    a = agg(mode)
    if not a: print(f"{mode:6} (no data)"); continue
    cost = a["in"]*PIN/1e6 + a["out"]*POUT/1e6
    print(f"{mode:6} {a['n']:>2} {a['edges_tok']:>8.0f} {a['in']:>7.0f} {a['out']:>7.0f} {cost:>8.4f} {cost*3.42:>12.3f}")
print("="*92)
print(f"one-time distill cost for {len(DISTILLED)} HSEN notes: ~${distill_cost:.4f} (amortised over all future searches)")
