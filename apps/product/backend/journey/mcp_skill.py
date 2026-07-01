"""AI-914: productionised 'mcp_skill' strategy for the classification eval harness.

An LLM classifier-skill agent drives the Trade Tariff MCP (via the gated
journey.mcp_client) to commit a declarable 10-digit code. Ported from the
standalone bench_sheet/arm_openai.py, adapted to the qa_loop strategy contract:
run_qa_session dispatches strategy=="mcp_skill" here, and run_classify_matrix_ids
scores + writes the returned dict to kg.classify_runs unchanged.

Gated: needs JOURNEY_ALLOW_MCP_CALLS=1 + OTT_HUB_CLIENT_ID/SECRET (via mcp_client).
If MCP is disabled the session returns a clean no_candidates result rather than
crashing an offline harness run.
"""
import os, re, json, asyncio, urllib.request

from journey import mcp_client

OPENAI = "https://api.openai.com/v1/chat/completions"

def _model():
    return os.environ.get("CLASSIFY_LLM_MODEL") or os.environ.get("ARM_MODEL") or "gpt-5.5"

def _oracle_model():
    return os.environ.get("QA_SIMULATOR_MODEL") or os.environ.get("ORACLE_MODEL") or "gpt-5-mini"

def _effort():
    return os.environ.get("CLASSIFY_REASONING_EFFORT") or os.environ.get("ARM_EFFORT") or "medium"

MAXIT = int(os.environ.get("ARM_MAXIT", "16"))
MAXQ = 5

def _dig(x):
    return re.sub(r"\D", "", str(x or ""))

def _post(url, data, headers, timeout=180):
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)

def _openai_call(model, messages, tools=None, effort=None):
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    # gpt-5.5 rejects reasoning_effort + function tools on /v1/chat/completions; only
    # send effort on the tool-less (oracle) turns.
    if effort and not tools and (model.startswith("gpt-5") or model.startswith("o")):
        body["reasoning_effort"] = effort
    r = _post(OPENAI, json.dumps(body).encode(),
              {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"}, 180)
    return json.load(r)

def _oracle(facts, q):
    sysp = ("You are simulating an importer answering a customs classifier's questions about YOUR product. "
            "Answer ONLY the question, 1-2 short sentences, using ONLY these facts; if not covered say 'Not specified.' "
            "NEVER state or hint a commodity/HS code, heading, or chapter number.\n\nFACTS:\n" + (facts or ""))
    d = _openai_call(_oracle_model(), [{"role": "system", "content": sysp}, {"role": "user", "content": q}])
    return d["choices"][0]["message"]["content"].strip()

def _t(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}
_S = {"type": "string"}
TOOLS = [
    _t("classification_search", "Search candidate commodity codes for a product description.",
       {"query": _S, "limit": {"type": "integer"}, "expanded_query": _S}, ["query"]),
    _t("note_mentions", "Section/chapter notes for candidate codes.",
       {"goods_nomenclature_item_ids": {"type": "array", "items": _S}}, ["goods_nomenclature_item_ids"]),
    _t("show_heading", "Heading text + children (4-digit id).", {"heading_id": _S}, ["heading_id"]),
    _t("show_chapter", "Chapter text (2-digit id).", {"chapter_id": _S}, ["chapter_id"]),
    _t("lookup_commodity", "Commodity detail; confirms declarable.", {"commodity_code": _S}, ["commodity_code"]),
    _t("navigate_hierarchy", "Any 4-10 digit entry.", {"code": _S}, ["code"]),
    _t("ask_importer", "Ask the importer one product question.", {"question": _S}, ["question"]),
    _t("submit_classification", "Submit the final answer and finish.",
       {"commodity_code": _S, "gir": _S, "alternatives": {"type": "array", "items": _S}}, ["commodity_code", "gir"]),
]
MCP_TOOLS = {"classification_search", "note_mentions", "show_heading", "show_chapter", "lookup_commodity", "navigate_hierarchy"}

def _load_skill():
    for p in ("/app/backend/journey/skill_blob.md", "/tmp/skill_blob.md", "/home/ubuntu/bench_sheet/skill_blob.md"):
        try:
            with open(p) as f:
                return f.read()
        except OSError:
            continue
    return "You are a UK commodity-code classifier. Use the tools to search, check notes, and verify in the hierarchy; resolve by the GIRs."

def run_mcp_skill_row(query, oracle_text):
    """Synchronous agent loop (blocking; run in a threadpool). Returns
    {final, gir, alts, q, calls, err}. final is a bare code string or None."""
    skill = _load_skill()
    model, effort = _model(), _effort()
    msgs = [{"role": "system", "content": skill + "\n\n--- EVAL HARNESS ---\nClassify the product below to ONE "
             "declarable 10-digit UK commodity code (service=uk). Use the tools to search, check notes, and verify "
             "in the hierarchy; resolve by the GIRs. Ask the importer only if a missing fact changes the code (max 5). "
             "Finish by calling submit_classification. Act via tools, not prose."},
            {"role": "user", "content": "Product: " + query}]
    qn = calls = 0
    for _ in range(MAXIT):
        try:
            d = _openai_call(model, msgs, TOOLS, effort)
        except Exception as e:
            return {"final": None, "err": str(e)[:160], "q": qn, "calls": calls, "gir": None, "alts": []}
        m = d["choices"][0]["message"]
        msgs.append(m)
        tcs = m.get("tool_calls")
        if not tcs:
            msgs.append({"role": "user", "content": "Call submit_classification to finish, or use a tool."})
            continue
        for tc in tcs:
            fn = tc["function"]["name"]
            try:
                a = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                a = {}
            if fn == "submit_classification":
                return {"final": (a.get("commodity_code") or "").replace(" ", ""), "gir": a.get("gir"),
                        "alts": a.get("alternatives", []), "q": qn, "calls": calls, "err": None}
            if fn == "ask_importer":
                qn += 1
                try:
                    out = _oracle(oracle_text, a.get("question", "")) if qn <= MAXQ else "No more questions; decide now."
                except Exception as e:
                    out = "Not specified."
            elif fn in MCP_TOOLS:
                calls += 1
                try:
                    out = mcp_client.mcp(fn, a)[:6000]
                except Exception as e:
                    out = json.dumps({"error": str(e)[:120]})
            else:
                out = "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    return {"final": None, "gir": "(max iterations)", "alts": [], "q": qn, "calls": calls, "err": None}

def _answers(final, alts):
    """Wrap the arm's bare code + alternatives into the scorer contract:
    a best-first list of {'commodity_code': <10-digit>} dicts, deduped."""
    out, seen = [], set()
    for c in [final] + list(alts or []):
        d = _dig(c)
        if d and d not in seen:
            seen.add(d)
            out.append({"commodity_code": d})
    return out

async def run_mcp_skill_session(query, max_rounds, oracle_text, config, human_answers):
    """Strategy entrypoint matching _run_eliminate_session's contract."""
    if not mcp_client.enabled():
        return {"final_mode": "no_candidates", "strategy": "mcp_skill", "rounds": [],
                "qa_history": [], "facts": [], "final_answers": None, "final_question": None,
                "candidates_final_round": [], "survivor_count": 0, "survivors_final": [],
                "total_classify_calls": 0, "total_simulator_calls": 0,
                "error": "MCP disabled (set JOURNEY_ALLOW_MCP_CALLS=1 + OTT_HUB creds)"}
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, lambda: run_mcp_skill_row(query, oracle_text))
    answers = _answers(r.get("final"), r.get("alts"))
    mode = "answers" if answers else ("simulator_failed" if r.get("err") else "no_candidates")
    return {
        "final_mode": mode, "strategy": "mcp_skill",
        "rounds": [{"question": None, "answer": None}] if answers else [],
        "qa_history": [], "facts": [],
        "final_answers": answers or None,
        "final_question": None,
        "candidates_final_round": answers,
        "survivor_count": len(answers),
        "survivors_final": answers,
        "total_classify_calls": r.get("calls", 0),
        "total_simulator_calls": r.get("q", 0),
        "gir": r.get("gir"), "error": r.get("err"),
    }
