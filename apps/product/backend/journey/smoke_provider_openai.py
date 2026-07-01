"""Opt-in provider-backed smoke for the full-app trader journey.

Default journey tests are no-spend. Run this only when a paid/provider-backed
check is approved:

    backend/.venv/bin/python backend/journey/smoke_provider_openai.py --allow-spend

The script never prints the API key. It proves two things:

1. the configured OpenAI key can make a tiny JSON chat completion;
2. the trader journey classification route enters the OTT staging Q&A path,
   including provider-backed search/query expansion and provider-led
   ask-vs-answer classification.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPORT_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))


def _load_env() -> None:
    for env_path in (
        EXPORT_ROOT / ".env",
        BACKEND_DIR / ".env",
        Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    ):
        if env_path and env_path.exists():
            load_dotenv(env_path, override=False)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _post(client, path: str, payload: dict) -> dict:
    response = client.post(path, json=payload)
    _assert(response.status_code == 200, f"{path} returned {response.status_code}: {response.text[:400]}")
    return response.json()


def _get(client, path: str) -> dict:
    response = client.get(path)
    _assert(response.status_code == 200, f"{path} returned {response.status_code}: {response.text[:400]}")
    return response.json()


def _openai_json_probe(model: str) -> None:
    from openai import OpenAI

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": 'Return exactly {"ok": true}.'},
        ],
        "response_format": {"type": "json_object"},
    }
    response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).chat.completions.create(**kwargs)
    payload = json.loads(response.choices[0].message.content or "{}")
    _assert(payload.get("ok") is True, f"OpenAI JSON probe returned unexpected payload: {payload}")


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-spend", action="store_true", help="Required: confirms this smoke may call OpenAI once or twice.")
    parser.add_argument("--model", default=os.environ.get("QUESTION_WORDING_MODEL") or os.environ.get("CLASSIFY_LLM_MODEL") or "gpt-5-nano")
    args = parser.parse_args()

    if not args.allow_spend:
        raise SystemExit("Refusing provider-backed smoke without --allow-spend.")

    _load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not configured.")

    os.environ["JOURNEY_ALLOW_PROVIDER_CALLS"] = "1"
    os.environ.pop("JOURNEY_CLASSIFY_MODE", None)
    os.environ["QUESTION_WORDING_MODEL"] = args.model
    os.environ.setdefault("CLASSIFY_LLM_MODEL", args.model)

    _openai_json_probe(args.model)

    from fastapi.testclient import TestClient
    from journey.main import app

    client = TestClient(app)
    health = _get(client, "/api/health")
    _assert(health.get("openai_key_present") is True, f"backend did not see OPENAI_API_KEY: {health}")
    _assert(health.get("journey_provider_calls_allowed") is True, f"provider guard still disabled: {health}")

    examples = _get(client, "/api/journey/examples")
    target_example = next(
        (e for e in examples.get("examples") or [] if e.get("expected_code") == "2106909849"),
        None,
    )
    _assert(target_example is not None, "missing pre-workout powder demo example")

    config = dict(examples.get("config") or {})
    config.update({
        "strategy": "converge",
        "qa_process_mode": "ott_staging_kg",
        "prompt_mode": "facet_soft_score",
        "use_kg_prompt_context": True,
        "use_llm_candidate_selection": True,
        "candidate_selection_model": args.model,
        "use_query_expansion": True,
        "query_expansion_model": "gpt-4.1-mini-2025-04-14",
        "query_expansion_prompt_variant": "staging",
        "retrieval": {
            **((config.get("retrieval") or {})),
            "use_labels": True,
            "use_curated": True,
            "use_vector": True,
            "use_composite": True,
            "use_facts_leg": True,
            "use_kg_context_leg": True,
            "use_facts_vec_leg": True,
            "use_kg_vec_leg": True,
        },
    })

    turn = _post(client, "/api/classify/start", {"query": target_example["query"], "config": config})
    _assert(turn.get("mode") in {"questions", "answers"}, f"expected staging Q&A or answers, got {turn.get('mode')}")
    candidates = turn.get("candidates") or []
    candidate_codes = [c.get("commodity_code") for c in candidates]
    _assert(candidates, "staging path should return a non-empty candidate shortlist")
    retrieval_legs = (((turn.get("augmentation_summary") or {}).get("debug") or {}).get("retrieval_legs") or [])
    _assert("labels" in retrieval_legs, "staging+KG retrieval should include labels")
    _assert(any(leg in retrieval_legs for leg in ("facts", "facts_vec", "kg_context", "kg_vec")), f"staging+KG retrieval should include KG/fact legs: {retrieval_legs}")
    _assert((turn.get("augmentation_summary") or {}).get("kg_prompt_context_enabled") is True, "staging+KG should inject KG prompt context")
    _assert(target_example["expected_code"] in candidate_codes, f"target code dropped out of staging shortlist: {candidate_codes[:20]}")
    selection = (turn.get("augmentation_summary") or {}).get("candidate_selection") or {}
    _assert(selection.get("mode") == "llm", f"staging classification did not enter provider mode: {selection}")
    _assert(selection.get("model") == args.model, f"staging classification used unexpected model: {selection}")

    if turn.get("mode") == "questions":
        question = turn.get("question") or {}
        _assert(question.get("question"), f"missing provider question: {turn}")
        _assert(question.get("options"), f"missing provider question options: {turn}")
        follow_up = _post(
            client,
            "/api/classify/answer",
            {
                "query": target_example["query"],
                "qa_history": [{"question": question["question"], "answer": question["options"][0]}],
                "config": config,
                "fixed_candidates": turn.get("fixed_candidates") or [],
            },
        )
        _assert(follow_up.get("mode") in {"answers", "questions"}, f"unexpected staging follow-up mode: {follow_up.get('mode')}")
    else:
        _assert(turn.get("answers"), f"staging direct-answer turn should include answers: {turn}")

    print(f"provider OpenAI smoke: ok (model={args.model}, key_present=true)")


if __name__ == "__main__":
    run()
