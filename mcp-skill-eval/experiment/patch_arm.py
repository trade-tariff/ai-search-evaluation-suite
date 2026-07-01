#!/usr/bin/env python3
"""Minimal patch of arm_openai.py -> arm_openai_top10.py:
adds a required `ranked_codes` top-10 field to submit_classification so top-10
is measurable, persists it in the per-id JSON, and skips note_mentions when
SKIP_NOTE_MENTIONS=1 (broken/422). Everything else unchanged.
"""
import pathlib

SRC = pathlib.Path("/opt/ai-search-evaluation-suite-journey/mcp-skill-eval/arm_openai.py")
DST = pathlib.Path("/opt/ai-search-evaluation-suite-journey/mcp-skill-eval/arm_openai_top10.py")
src = SRC.read_text()

# 1. submit_classification tool: add ranked_codes (top-10, best-first) + require it.
old_tool = ('    _t("submit_classification", "Submit the final answer and finish.",\n'
            '       {"commodity_code": S, "gir": S, "alternatives": {"type": "array", "items": S}}, '
            '["commodity_code", "gir"]),')
new_tool = ('    _t("submit_classification", "Submit the final answer and finish.",\n'
            '       {"commodity_code": S, "gir": S,\n'
            '        "ranked_codes": {"type": "array", "items": S,\n'
            '            "description": "Top-10 candidate 10-digit declarable codes, best-first; index 0 == commodity_code."},\n'
            '        "alternatives": {"type": "array", "items": S}},\n'
            '       ["commodity_code", "gir", "ranked_codes"]),')
assert old_tool in src, "submit tool not found"
src = src.replace(old_tool, new_tool, 1)

# 2. Capture ranked_codes on submit; normalise (strip spaces), ensure top1 present at index 0.
old_ret = ('            if fn == "submit_classification":\n'
           '                return {"final": (a.get("commodity_code") or "").replace(" ", ""), "gir": a.get("gir"),\n'
           '                        "alts": a.get("alternatives", []), "q": qn, "calls": calls}')
new_ret = ('            if fn == "submit_classification":\n'
           '                top1 = (a.get("commodity_code") or "").replace(" ", "")\n'
           '                ranked = [str(c).replace(" ", "") for c in (a.get("ranked_codes") or []) if c]\n'
           '                if top1 and (not ranked or ranked[0] != top1):\n'
           '                    ranked = [top1] + [c for c in ranked if c != top1]\n'
           '                ranked = ranked[:10]\n'
           '                return {"final": top1, "gir": a.get("gir"), "ranked_codes": ranked,\n'
           '                        "alts": a.get("alternatives", []), "q": qn, "calls": calls}')
assert old_ret in src, "submit return not found"
src = src.replace(old_ret, new_ret, 1)

# 3. Tell the model in the harness prompt to also emit the ranked top-10.
old_sys = ('"Finish by calling submit_classification. Act via tools, not prose."')
new_sys = ('"Finish by calling submit_classification with commodity_code AND a ranked_codes "\n'
           '             "top-10 (best-first, index 0 == your chosen code, all 10-digit declarable). "\n'
           '             "Act via tools, not prose."')
assert old_sys in src, "sys prompt tail not found"
src = src.replace(old_sys, new_sys, 1)

# 4. note_mentions skip (broken/422). When SKIP_NOTE_MENTIONS=1, short-circuit.
old_mcp = ('            elif fn in MCP_TOOLS:\n'
           '                calls += 1; a["service"] = "uk"\n'
           '                out = mcp(fn, a)[:6000]')
new_mcp = ('            elif fn in MCP_TOOLS:\n'
           '                if fn == "note_mentions" and os.environ.get("SKIP_NOTE_MENTIONS") == "1":\n'
           '                    out = json.dumps({"skipped": "note_mentions disabled (known 422); use show_chapter/show_heading instead"})\n'
           '                else:\n'
           '                    calls += 1; a["service"] = "uk"\n'
           '                    out = mcp(fn, a)[:6000]')
assert old_mcp in src, "mcp dispatch not found"
src = src.replace(old_mcp, new_mcp, 1)

# 5. max-iterations fallthrough: include empty ranked_codes for shape consistency.
old_fall = ('    return {"final": None, "gir": "(max iterations)", "alts": [], "q": qn, "calls": calls}')
new_fall = ('    return {"final": None, "gir": "(max iterations)", "ranked_codes": [], "alts": [], "q": qn, "calls": calls}')
assert old_fall in src, "fallthrough not found"
src = src.replace(old_fall, new_fall, 1)

DST.write_text(src)
print("wrote", DST)
