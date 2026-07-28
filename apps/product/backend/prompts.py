from __future__ import annotations

import json
from pathlib import Path

from schemas import PromptInfo

DATA_PATH = Path(__file__).parent.parent / "data" / "search_contexts.json"

_cached_data: dict | None = None
_cached_mtime: float | None = None


def _load_raw() -> dict:
    """Load search_contexts.json, reloading whenever the file changes on disk.

    This file is MUTABLE state, not config - approve_draft() appends to it. The
    cache used to be invalidated only by the process that did the writing, so
    any other worker served stale prompts indefinitely. Keying on mtime makes
    every process pick up writes, whoever made them.
    """
    global _cached_data, _cached_mtime
    try:
        mtime = DATA_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _cached_data is None or mtime != _cached_mtime:
        _cached_data = json.loads(DATA_PATH.read_text())
        _cached_mtime = mtime
    return _cached_data


def list_prompts() -> list[PromptInfo]:
    data = _load_raw()
    out = []
    for q in data["queries"]:
        facts = q.get("gold_facts") or []
        out.append(PromptInfo(
            index=q["index"],
            raw_query=q["raw_query"],
            result_count=q["result_count"],
            gold_code=str(q["gold_code"]) if q.get("gold_code") else None,
            has_oracle_text=bool(q.get("oracle_text")),
            gold_facts_count=len(facts) if isinstance(facts, list) else 0,
            source=q.get("source"),
        ))
    return out


def get_gold_code(prompt_index: int) -> str | None:
    """Return the ground-truth commodity code for this prompt, if set."""
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            code = q.get("gold_code")
            return str(code) if code else None
    return None


def get_oracle_text(prompt_index: int) -> str | None:
    """Return the authoritative product description (e.g. full ATaR ruling
    body) for this prompt, if set. Used by the simulator as a ground-truth
    source for any question not covered by pre-seeded facts.
    """
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            txt = q.get("oracle_text")
            return str(txt) if txt else None
    return None


def get_gold_facts(prompt_index: int) -> list[dict]:
    """Return the user-approved fact sheet for this prompt.

    Each fact: {slot: str, answer: str, source_question?: str}. These are
    pre-committed into the FactStore at run start so candidate Q&A is
    consistent without burning an LLM call per question for known slots.
    """
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            facts = q.get("gold_facts") or []
            if not isinstance(facts, list):
                return []
            return [f for f in facts if isinstance(f, dict) and "slot" in f and "answer" in f]
    return []


def _format_opensearch_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        desc = r["description"].replace("<br>", " ").replace("\n", " ")
        lines.append(f"{i}. {r['commodity_code']} - {desc} (score: {r['score']:.3f})")
    return "\n".join(lines)


def _format_qa_history(qa_history: list[dict]) -> str:
    """Format Q&A history for inclusion in the prompt template.

    Each entry: {"question": "...", "options": [...], "answer": "..."}
    """
    if not qa_history:
        return "No previous questions."

    lines = []
    for i, qa in enumerate(qa_history, 1):
        lines.append(f"Q{i}: {qa['question']}")
        lines.append(f"A{i}: {qa['answer']}")
    return "\n".join(lines)


def build_prompt_messages(
    prompt_index: int,
    qa_history: list[dict] | None = None,
    opensearch_limit: int = 80,
) -> list[dict]:
    """Build messages for the LLM, optionally including Q&A history from prior rounds."""
    data = _load_raw()
    template = data["context_template"]
    query = None
    for q in data["queries"]:
        if q["index"] == prompt_index:
            query = q
            break
    if query is None:
        raise ValueError(f"Prompt index {prompt_index} not found")

    results = query["formatted_results"][:opensearch_limit]
    opensearch_text = _format_opensearch_results(results)
    qa_text = _format_qa_history(qa_history or [])

    filled = (
        template.replace("%{search_input}", query["raw_query"])
        .replace("%{expanded_query}", query.get("processed_query", query["raw_query"]))
        .replace("%{answers_opensearch}", opensearch_text)
        .replace("%{questions}", qa_text)
    )

    return [
        {"role": "system", "content": filled},
        {"role": "user", "content": f"Classify: {query['raw_query']}"},
    ]


def get_formatted_results(prompt_index: int, opensearch_limit: int = 80) -> list[dict]:
    """Retrieved candidates for a prompt, in retrieval order. Used by the
    forced-answer fallback when the model will not commit to a code."""
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            return (q.get("formatted_results") or [])[:opensearch_limit]
    return []


def gold_is_retrievable(prompt_index: int, opensearch_limit: int = 80) -> bool | None:
    """Is the gold code actually present in this prompt's candidate set?

    The system prompt tells the model not to go beyond the retrieved results
    ("don't go beyond these search results ... even if you know the results
    are incorrect"), so when gold is absent NO model can score a hit. Those
    prompts cap the achievable accuracy, and reading a score without knowing
    the cap makes a retrieval problem look like a model problem.

    Returns None when the prompt has no gold code to check against.
    """
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            gold = q.get("gold_code")
            if not gold:
                return None
            codes = {
                str(r.get("commodity_code"))
                for r in (q.get("formatted_results") or [])[:opensearch_limit]
            }
            return str(gold) in codes
    return None


def get_raw_query(prompt_index: int) -> str:
    """Get just the raw query text for a prompt index."""
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            return q["raw_query"]
    return ""


def get_prompt_detail(prompt_index: int) -> dict:
    data = _load_raw()
    for q in data["queries"]:
        if q["index"] == prompt_index:
            facts = q.get("gold_facts") or []
            if not isinstance(facts, list):
                facts = []
            return {
                "index": q["index"],
                "raw_query": q["raw_query"],
                "processed_query": q.get("processed_query", ""),
                "result_count": q["result_count"],
                "top_results": q["formatted_results"],
                "gold_code": str(q["gold_code"]) if q.get("gold_code") else None,
                "oracle_text": q.get("oracle_text") or None,
                "gold_facts": [
                    f for f in facts if isinstance(f, dict) and "slot" in f and "answer" in f
                ],
                "source": q.get("source"),
            }
    raise ValueError(f"Prompt index {prompt_index} not found")
