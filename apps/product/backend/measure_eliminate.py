"""Measure the REAL eliminate-round prompt size (input + output) on the live VM.

Monkeypatches openai.OpenAI to capture the exact system+user prompt that
_llm_eliminate builds and the real resp.usage (ground-truth input/output tokens),
then runs the actual eliminate_step pipeline. Repeats with KG context dropped to
measure the TRUE marginal cost of the KG block.
"""
import os, json, copy
import openai
import journey.classification as c

QUERY = "chocolate protein powder"
CAND_LIMIT = 80

# ---- capture wrapper -------------------------------------------------------
_real_OpenAI = openai.OpenAI
_cap = {}

class _WrapResponses:
    def __init__(self, real): self._real = real
    def create(self, **kw):
        _cap["input"] = copy.deepcopy(kw.get("input"))
        _cap["model"] = kw.get("model")
        _cap["reasoning"] = kw.get("reasoning")
        resp = self._real.create(**kw)
        try: _cap["usage"] = resp.usage.model_dump()
        except Exception: _cap["usage"] = getattr(resp, "usage", None)
        return resp

class _WrapClient:
    def __init__(self, *a, **k):
        self._real = _real_OpenAI(*a, **k)
        self.responses = _WrapResponses(self._real.responses)
        self.chat = self._real.chat
        self.embeddings = self._real.embeddings
    def __getattr__(self, n): return getattr(self._real, n)

openai.OpenAI = _WrapClient

# ---- base config: force live eliminate (gpt-5.5), isolate ONE call ----------
cfg = copy.deepcopy(c.DEFAULT_CLASSIFY_CONFIG)
cfg.update({
    "use_llm_candidate_selection": True,
    "candidate_selection_model": "gpt-5.5",
    "qa_mode": "ask_first",
    "use_session_facts": False,   # isolate: no extra extraction call
    "strategy": "eliminate",
})

print(f"provider_calls_allowed={c.provider_calls_allowed()}  query={QUERY!r}  cand_limit={CAND_LIMIT}")
candidates, _enr = c.initial_candidates_for_eliminate(QUERY, config=cfg, candidate_limit=CAND_LIMIT)
print(f"retrieved_candidates={len(candidates)}")

def section_chars(inp):
    sysmsg = inp[0]["content"]
    usermsg = inp[1]["content"]
    parts = {"_system": len(sysmsg), "_user_total": len(usermsg)}
    # split user on the "## " headers
    cur = None; buf = {}
    for line in usermsg.split("\n"):
        if line.startswith("## "):
            cur = line[3:].strip(); buf[cur] = 0
        if cur is not None:
            buf[cur] += len(line) + 1
    parts.update(buf)
    return parts, sysmsg, usermsg

def run(label, effort, drop_kg):
    os.environ["CLASSIFY_REASONING_EFFORT"] = effort
    _cap.clear()
    sf_orig, kg_orig = c._structured_facts_section, c._kg_edges_section
    if drop_kg:
        c._structured_facts_section = lambda *a, **k: "Disabled by config."
        c._kg_edges_section = lambda *a, **k: "Disabled by config."
    try:
        c.eliminate_step(QUERY, [], candidates, config=cfg)
    finally:
        c._structured_facts_section, c._kg_edges_section = sf_orig, kg_orig
    if "usage" not in _cap:
        print(f"\n### {label}: NO responses call captured (api_error?) cap={list(_cap)}"); return None
    parts, sysmsg, usermsg = section_chars(_cap["input"])
    u = _cap["usage"]
    print(f"\n### {label}  (effort={effort}, drop_kg={drop_kg}, model={_cap.get('model')})")
    print(f"  CHARS  system={parts['_system']}  user={parts['_user_total']}  total={parts['_system']+parts['_user_total']}")
    for k,v in parts.items():
        if not k.startswith("_"):
            print(f"     - {k}: {v} chars")
    print(f"  USAGE  {json.dumps(u)}")
    tot_chars = parts['_system']+parts['_user_total']
    it = u.get("input_tokens"); ot = u.get("output_tokens")
    if it: print(f"  RATIO  {tot_chars/it:.2f} chars/input-token   (input_tokens={it}, output_tokens={ot})")
    return {"label": label, "parts": parts, "usage": u}

R = []
R.append(run("A medium + KG", "medium", False))
R.append(run("B high + KG",   "high",   False))
R.append(run("C medium, NO KG","medium", True))

print("\n=================  SUMMARY  =================")
for r in R:
    if not r: continue
    u = r["usage"]
    print(f"{r['label']:16}  in={u.get('input_tokens'):>6}  out={u.get('output_tokens'):>6}  "
          f"reasoning={ (u.get('output_tokens_details') or {}).get('reasoning_tokens') }  "
          f"cached_in={ (u.get('input_tokens_details') or {}).get('cached_tokens') }")
