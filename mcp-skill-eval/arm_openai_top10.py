#!/usr/bin/env python3
"""Arm A: run the classifier skill end-to-end with an OpenAI model (gpt-5.5),
driving the OTT MCP via function-calls + an oracle for clarifying questions.
Runs on the EC2 (only place with gpt-5.5). Read-only against the MCP.

Usage: arm_openai.py <skill_blob.md> <sample.json> <out.json>
Env: OPENAI_API_KEY (container), OTT_HUB_CLIENT_ID, OTT_HUB_CLIENT_SECRET.
     ARM_MODEL (default gpt-5.5), ARM_EFFORT (default medium), ORACLE_MODEL (gpt-5-mini),
     ARM_MAXIT (default 16), ARM_LIMIT (rows; default all).
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error

OPENAI = "https://api.openai.com/v1/chat/completions"
MODEL = os.environ.get("ARM_MODEL", "gpt-5.5")
EFFORT = os.environ.get("ARM_EFFORT", "medium")
ORACLE_MODEL = os.environ.get("ORACLE_MODEL", "gpt-5-mini")
MCP = "https://mcp.trade-tariff.service.gov.uk/"
TOKURL = "https://auth.id.trade-tariff.service.gov.uk/oauth2/token"
MAXIT = int(os.environ.get("ARM_MAXIT", "16"))
MAXQ = 5
_tok = {"v": None, "t": 0.0}


def _post(url, data, headers, timeout=180):
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)


def ott_token(force=False):
    if force or not _tok["v"] or time.time() - _tok["t"] > 3000:
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
            "client_id": os.environ["OTT_HUB_CLIENT_ID"], "client_secret": os.environ["OTT_HUB_CLIENT_SECRET"],
            "scope": "tariff/read"}).encode()
        r = _post(TOKURL, body, {"Content-Type": "application/x-www-form-urlencoded"}, 30)
        _tok["v"] = json.load(r)["access_token"]; _tok["t"] = time.time()
    return _tok["v"]


def mcp(tool, args):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}}).encode()
    for attempt in (1, 2):
        try:
            r = _post(MCP, payload, {"Authorization": "Bearer " + ott_token(force=attempt == 2),
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, 60)
            d = json.load(r); c = d.get("result", {}).get("content", [])
            return c[0]["text"] if c else json.dumps(d)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                continue
            return json.dumps({"error": "HTTP %d" % e.code})
        except Exception as e:
            return json.dumps({"error": str(e)[:120]})
    return json.dumps({"error": "mcp failed"})


def openai_call(model, messages, tools=None, effort=None):
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    # gpt-5.5 rejects reasoning_effort + function tools on /v1/chat/completions, so only
    # send it when there are no tools (oracle calls). Tool turns run at the model default.
    if effort and not tools and (model.startswith("gpt-5") or model.startswith("o")):
        body["reasoning_effort"] = effort
    r = _post(OPENAI, json.dumps(body).encode(),
              {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"}, 180)
    return json.load(r)


def oracle(facts, q):
    sysp = ("You are simulating an importer answering a customs classifier's questions about YOUR product. "
            "Answer ONLY the question, 1-2 short sentences, using ONLY these facts; if not covered say 'Not specified.' "
            "NEVER state or hint a commodity/HS code, heading, or chapter number.\n\nFACTS:\n" + facts)
    d = openai_call(ORACLE_MODEL, [{"role": "system", "content": sysp}, {"role": "user", "content": q}])
    return d["choices"][0]["message"]["content"].strip()


def _t(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}
S = {"type": "string"}
TOOLS = [
    _t("classification_search", "Search candidate commodity codes for a product description.",
       {"query": S, "limit": {"type": "integer"}, "expanded_query": S}, ["query"]),
    _t("note_mentions", "Section/chapter notes for candidate codes.",
       {"goods_nomenclature_item_ids": {"type": "array", "items": S}}, ["goods_nomenclature_item_ids"]),
    _t("show_heading", "Heading text + children (4-digit id).", {"heading_id": S}, ["heading_id"]),
    _t("show_chapter", "Chapter text (2-digit id).", {"chapter_id": S}, ["chapter_id"]),
    _t("lookup_commodity", "Commodity detail; confirms declarable.", {"commodity_code": S}, ["commodity_code"]),
    _t("navigate_hierarchy", "Any 4-10 digit entry.", {"code": S}, ["code"]),
    _t("ask_importer", "Ask the importer one product question.", {"question": S}, ["question"]),
    _t("submit_classification", "Submit the final answer and finish.",
       {"commodity_code": S, "gir": S,
        "ranked_codes": {"type": "array", "items": S,
            "description": "Top-10 candidate 10-digit declarable codes, best-first; index 0 == commodity_code."},
        "alternatives": {"type": "array", "items": S}},
       ["commodity_code", "gir", "ranked_codes"]),
]
MCP_TOOLS = {"classification_search", "note_mentions", "show_heading", "show_chapter", "lookup_commodity", "navigate_hierarchy"}


def run_row(skill, row):
    msgs = [{"role": "system", "content": skill + "\n\n--- EVAL HARNESS ---\nClassify the product below to ONE "
             "declarable 10-digit UK commodity code (service=uk). Use the tools to search, check notes, and verify "
             "in the hierarchy; resolve by the GIRs. Ask the importer only if a missing fact changes the code (max 5). "
             "Finish by calling submit_classification with commodity_code AND a ranked_codes "
             "top-10 (best-first, index 0 == your chosen code, all 10-digit declarable). "
             "Act via tools, not prose."},
            {"role": "user", "content": "Product: " + row["query"]}]
    qn = calls = 0
    for _ in range(MAXIT):
        try:
            d = openai_call(MODEL, msgs, TOOLS, EFFORT)
        except Exception as e:
            return {"final": None, "err": str(e)[:160], "q": qn, "calls": calls}
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
                top1 = (a.get("commodity_code") or "").replace(" ", "")
                ranked = [str(c).replace(" ", "") for c in (a.get("ranked_codes") or []) if c]
                if top1 and (not ranked or ranked[0] != top1):
                    ranked = [top1] + [c for c in ranked if c != top1]
                ranked = ranked[:10]
                return {"final": top1, "gir": a.get("gir"), "ranked_codes": ranked,
                        "alts": a.get("alternatives", []), "q": qn, "calls": calls}
            if fn == "ask_importer":
                qn += 1
                out = oracle(row["oracle_text"], a.get("question", "")) if qn <= MAXQ else "No more questions; decide now."
            elif fn in MCP_TOOLS:
                if fn == "note_mentions" and os.environ.get("SKIP_NOTE_MENTIONS") == "1":
                    out = json.dumps({"skipped": "note_mentions disabled (known 422); use show_chapter/show_heading instead"})
                else:
                    calls += 1; a["service"] = "uk"
                    out = mcp(fn, a)[:6000]
            else:
                out = "unknown tool"
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    return {"final": None, "gir": "(max iterations)", "ranked_codes": [], "alts": [], "q": qn, "calls": calls}


def main():
    skill = open(sys.argv[1]).read()
    sample = json.load(open(sys.argv[2]))
    outdir = os.environ.get("ARM_OUT_DIR", "/tmp/arm_a_results")
    os.makedirs(outdir, exist_ok=True)
    W = int(os.environ.get("ARM_WORKER", "0"))      # this worker's index
    N = int(os.environ.get("ARM_NWORKERS", "1"))    # total workers; shard by id-index % N
    for i, row in enumerate(sample):
        if i % N != W:
            continue
        fp = os.path.join(outdir, str(row["id"]) + ".json")
        if os.path.exists(fp):
            print("skip " + str(row["id"]), flush=True)
            continue
        t0 = time.time()
        try:
            r = run_row(skill, row)
        except Exception as e:
            r = {"final": None, "err": str(e)[:160]}
        r.update({"id": row["id"], "persona": row["persona"], "expected": row["expected_code"], "secs": round(time.time() - t0)})
        json.dump(r, open(fp, "w"))  # one file per row -> safe for parallel workers + resumable
        print("[w%d %s] exp=%s -> %s (%ss, calls=%s, q=%s)%s" % (
            W, row["persona"], row["expected_code"], r.get("final"),
            r.get("secs"), r.get("calls"), r.get("q"), " ERR:" + r["err"] if r.get("err") else ""), flush=True)


if __name__ == "__main__":
    main()
