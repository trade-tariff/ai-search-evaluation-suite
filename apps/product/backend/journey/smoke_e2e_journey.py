"""Smoke tests for the full app's e2e trader journey.

Run from the full app root:

    backend/.venv/bin/python backend/journey/smoke_e2e_journey.py

Add ``--require-live-kg`` for the local demo/EC2 path where the enriched
``uk`` + ``kg`` tariff database is expected to be reachable.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# No-spend default: route tests should prove deterministic fallback paths.
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("JOURNEY_CLASSIFY_MODE", "deterministic")
os.environ.setdefault("JOURNEY_PREFILL_MODE", "deterministic")

from fastapi.testclient import TestClient  # noqa: E402

from journey.main import app  # noqa: E402


COMPLEX_CODE = "1806907090"
COMPLEX_LABEL = "Complex protein powder"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _post(client: TestClient, path: str, payload: dict) -> dict:
    response = client.post(path, json=payload)
    _assert(response.status_code == 200, f"{path} returned {response.status_code}: {response.text[:400]}")
    return response.json()


def _get(client: TestClient, path: str) -> dict:
    response = client.get(path)
    _assert(response.status_code == 200, f"{path} returned {response.status_code}: {response.text[:400]}")
    return response.json()


def test_demo_personas(client: TestClient, require_live_kg: bool) -> tuple[dict, dict]:
    payload = _get(client, "/api/journey/examples")
    source = payload.get("source")
    if require_live_kg:
        _assert(source == "live_kg", f"expected live_kg examples, got {source!r}")
    config = payload.get("config", {})
    _assert(config.get("strategy") == "converge", f"demo config should use OTT staging converge strategy, got {config.get('strategy')!r}")
    _assert(config.get("qa_process_mode") == "ott_staging_kg", f"demo config should expose OTT staging + KG Q&A mode, got {config.get('qa_process_mode')!r}")
    _assert(config.get("use_kg_prompt_context") is True, "demo config should inject KG/facts into the shortlist prompt context")
    retrieval = (config or {}).get("retrieval") or {}
    _assert(retrieval.get("use_labels") is True, "demo retrieval should run against AI labels")
    _assert(retrieval.get("use_curated") is True, "demo retrieval should run against search references")
    _assert(retrieval.get("use_vector") is True, "demo retrieval should run semantic search")
    _assert(retrieval.get("use_composite") is True, "demo retrieval should run composite label/search text")
    _assert(retrieval.get("use_facts_leg") is True, "demo retrieval should include KG fact text")
    _assert(retrieval.get("use_kg_context_leg") is True, "demo retrieval should include KG rule/context text")
    _assert(retrieval.get("use_facts_vec_leg") is True, "demo retrieval should include embedded facts")
    _assert(retrieval.get("use_kg_vec_leg") is True, "demo retrieval should include embedded KG rules")
    retrieval_limit = retrieval.get("limit")
    _assert(int(retrieval_limit or 0) >= 80, f"e2e candidate shortlist should default to at least 80, got {retrieval_limit}")

    personas = payload.get("personas") or []
    if source == "live_kg":
        _assert(len(personas) == 7, f"expected 7 prompt personas, got {len(personas)}")
        _assert(payload.get("persona") == "emu_ordinary", f"default persona should be emu_ordinary, got {payload.get('persona')!r}")
        note = payload.get("note") or ""
        _assert("expected commodity code" in note, "examples note should explain facet_count is per expected CC")

    examples = payload.get("examples") or []
    _assert(examples, "expected at least one demo example")
    complex_example = next((e for e in examples if e.get("expected_code") == COMPLEX_CODE), None)
    if source == "live_kg":
        _assert(complex_example is not None, f"missing live KG example for {COMPLEX_CODE}")
        _assert(complex_example.get("label") == COMPLEX_LABEL, f"unexpected complex label: {complex_example}")
        _assert(int(complex_example.get("facet_count") or 0) >= 10, "complex example should expose enriched target-CC facts")
        seed = complex_example.get("seed") or {}
        _assert("country_of_origin" not in seed, "predefined prompts should not prefill country of origin")
        _assert((seed.get("meursing_inputs") or {}).get("additional_code") == "7046", "complex seed should carry Meursing code 7046")

        original = _get(client, "/api/journey/examples?persona=original")
        original_example = next((e for e in original.get("examples") or [] if e.get("expected_code") == COMPLEX_CODE), None)
        _assert(original.get("persona") == "original", "persona selector should return the requested level")
        _assert(original_example is not None, "original persona should include complex example")
        original_query = (original_example.get("query") or "").strip().lower()
        _assert(not original_query.startswith("product:"), "original persona prompt should not expose raw 'product:' extraction labels")
        _assert(not original_query.startswith("the product is"), "original persona prompt should be trader-readable")
        _assert(
            original_example.get("query") != complex_example.get("query"),
            "persona levels should change the displayed prompt text",
        )

    return payload, complex_example or examples[0]


def test_classification(client: TestClient, examples_payload: dict, example: dict) -> dict:
    config = dict(examples_payload.get("config") or {})
    config.update({
        "strategy": "eliminate",
        "qa_process_mode": "local_rules",
        "use_query_expansion": False,
        "use_llm_candidate_selection": False,
        "use_llm_question_wording": False,
        "retrieval": {
            **(config.get("retrieval") or {}),
            "use_labels": True,
            "use_curated": False,
            "use_vector": False,
            "use_composite": False,
            "use_facts_leg": True,
            "use_kg_context_leg": True,
            "use_facts_vec_leg": True,
            "use_kg_vec_leg": True,
        },
    })
    turn = _post(client, "/api/classify/start", {"query": example["query"], "config": config})
    candidates = turn.get("candidates") or []
    _assert(candidates, "classification should return candidates")
    codes = [c.get("commodity_code") for c in candidates]
    if example.get("expected_code") == COMPLEX_CODE:
        _assert(COMPLEX_CODE in codes, f"complex prompt should keep {COMPLEX_CODE} in the fallback shortlist, got {codes[:15]}")
    _assert(turn.get("mode") == "questions", f"classification should always start with Q&A, got {turn.get('mode')}")
    options = (turn.get("question") or {}).get("options") or []
    _assert(options, "first Q&A turn should include answer options")
    _assert(not any(re.match(r"^\d{10}\b", str(o)) for o in options), f"Q&A options should be trader-readable, not raw code descriptions: {options[:3]}")
    if example.get("expected_code") == COMPLEX_CODE:
        if examples_payload.get("source") == "live_kg":
            _assert(len(candidates) > 40, f"live e2e retrieval should consider more than 40 candidates, got {len(candidates)}")
        _assert(any("protein" in str(o).lower() for o in options), f"complex fallback question should expose a plain protein bucket, got {options}")
        _assert(all(str(c.get("description") or "").strip().lower() != "other" for c in candidates[:5]), "candidate descriptions should use contextualized DB text, not raw 'Other'")
    summary = turn.get("augmentation_summary") or {}
    _assert(summary.get("fallback") == "deterministic", "smoke test should exercise deterministic fallback")
    candidate_sources = {source for c in candidates for source in (c.get("sources") or [])}
    _assert("labels" in candidate_sources, f"fallback retrieval should include labels, got {sorted(candidate_sources)}")
    follow_up = _post(
        client,
        "/api/classify/answer",
        {
            "query": example["query"],
            "qa_history": [{"question": turn["question"]["question"], "answer": turn["question"]["options"][0]}],
            "config": config,
            "fixed_candidates": turn.get("fixed_candidates") or [],
        },
    )
    _assert(follow_up.get("mode") == "answers", f"follow-up after Q&A should return answers, got {follow_up.get('mode')}")
    return turn


def test_provider_guard_default(client: TestClient, examples_payload: dict, example: dict) -> None:
    old_key = os.environ.get("OPENAI_API_KEY")
    old_allow = os.environ.get("JOURNEY_ALLOW_PROVIDER_CALLS")
    old_mode = os.environ.get("JOURNEY_CLASSIFY_MODE")
    old_prefill = os.environ.get("JOURNEY_PREFILL_MODE")
    try:
        os.environ["OPENAI_API_KEY"] = "test-key-do-not-call"
        os.environ.pop("JOURNEY_ALLOW_PROVIDER_CALLS", None)
        os.environ.pop("JOURNEY_CLASSIFY_MODE", None)
        os.environ["JOURNEY_PREFILL_MODE"] = "auto"
        config = dict(examples_payload.get("config") or {})
        config.update({
            "use_llm_candidate_selection": True,
            "use_llm_question_wording": True,
            "use_query_expansion": True,
            "retrieval": {**(config.get("retrieval") or {}), "use_vector": True},
        })
        turn = _post(client, "/api/classify/start", {"query": example["query"], "config": config})
        selection = (turn.get("augmentation_summary") or {}).get("candidate_selection") or {}
        _assert(selection.get("mode") == "deterministic", f"provider guard should force deterministic classification, got {selection}")
        inferred = _post(
            client,
            "/api/duty/infer",
            {
                "commodity_code": "2204210600",
                "query": "12 bottles of still red wine from France, 750ml each, 12.5% ABV.",
                "customs_value_gbp": 500,
                "known_inputs": {},
            },
        )
        _assert(
            "prefill_mode" not in (inferred.get("sources") or {}),
            f"provider guard should keep duty prefill deterministic, got {inferred}",
        )
    finally:
        if old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old_key
        if old_allow is None:
            os.environ.pop("JOURNEY_ALLOW_PROVIDER_CALLS", None)
        else:
            os.environ["JOURNEY_ALLOW_PROVIDER_CALLS"] = old_allow
        if old_mode is None:
            os.environ.pop("JOURNEY_CLASSIFY_MODE", None)
        else:
            os.environ["JOURNEY_CLASSIFY_MODE"] = old_mode
        if old_prefill is None:
            os.environ.pop("JOURNEY_PREFILL_MODE", None)
        else:
            os.environ["JOURNEY_PREFILL_MODE"] = old_prefill


def test_candidate_selection_toggle(client: TestClient, examples_payload: dict, example: dict) -> None:
    config = dict(examples_payload.get("config") or {})
    config.update({
        "strategy": "eliminate",
        "qa_process_mode": "local_rules",
        "use_query_expansion": False,
        "use_llm_candidate_selection": False,
        "use_llm_question_wording": False,
        "retrieval": {**(config.get("retrieval") or {}), "use_labels": True, "use_vector": False, "use_composite": False},
    })
    config["candidate_selection_model"] = "gpt-5-nano"
    turn = _post(client, "/api/classify/start", {"query": example["query"], "config": config})
    selection = (turn.get("augmentation_summary") or {}).get("candidate_selection") or {}
    _assert(selection.get("mode") == "deterministic", f"candidate-selection toggle should force deterministic mode, got {selection}")
    _assert(turn.get("mode") == "questions", "local fallback mode should still go through Q&A first")


def test_valuation_methods(client: TestClient) -> None:
    known = _post(client, "/api/valuation", {"known_customs_value_gbp": 1234.567})
    _assert(known.get("customs_value_gbp") == 1234.57, f"known value should round to pennies, got {known}")
    _assert(known.get("method_code") == "known_customs_value", "known-value path should skip valuation-method arithmetic")

    cases = [
        (
            "method_1_transaction_value",
            {"has_sale_for_export": True, "has_usable_transaction_value": True},
            {"invoice_value": 1000, "freight_gbp": 80, "insurance_gbp": 20},
            1100.0,
        ),
        (
            "method_2_identical_goods",
            {"has_identical_goods_value": True},
            {"accepted_identical_value_gbp": 900, "quantity_adjustment_gbp": 10},
            910.0,
        ),
        (
            "method_3_similar_goods",
            {"has_similar_goods_value": True},
            {"accepted_similar_value_gbp": 850, "transport_adjustment_gbp": 25},
            875.0,
        ),
        (
            "method_4_deductive",
            {"has_uk_resale_price": True},
            {
                "uk_resale_price_gbp": 1300,
                "commissions_or_profit_gbp": 140,
                "uk_transport_gbp": 60,
                "uk_duties_taxes_gbp": 100,
            },
            1000.0,
        ),
        (
            "method_5_computed",
            {"has_uk_resale_price": True, "has_production_costs": True, "try_computed_before_deductive": True},
            {
                "materials_gbp": 600,
                "manufacturing_gbp": 180,
                "producer_profit_gbp": 90,
                "packing_gbp": 30,
                "transport_to_import_gbp": 70,
            },
            970.0,
        ),
        (
            "method_6_fallback",
            {},
            {"reasonable_base_value_gbp": 780, "adjustments_gbp": 15},
            795.0,
        ),
    ]
    for expected_method, flags, inputs, expected_value in cases:
        result = _post(client, "/api/valuation/guide", {**flags, "inputs": inputs})
        choice = result.get("choice") or {}
        _assert(choice.get("method_code") == expected_method, f"expected {expected_method}, got {choice}")
        calc = result.get("result") or {}
        _assert(calc.get("method_code") == expected_method, f"missing calculated result for {expected_method}")
        _assert(calc.get("customs_value_gbp") == expected_value, f"bad customs value for {expected_method}: {calc}")


def test_duty_input_inference(client: TestClient) -> None:
    inferred = _post(
        client,
        "/api/duty/infer",
        {
            "commodity_code": "2204210600",
            "query": "12 bottles of still red wine from France, 750ml each, 12.5% ABV.",
            "customs_value_gbp": 500,
            "known_inputs": {
                "country_of_origin": "DE",
                "has_proof_of_origin": False,
            },
        },
    )
    values = inferred.get("inferred") or {}
    sources = inferred.get("sources") or {}
    skipped = set(inferred.get("skipped_questions") or [])
    _assert(values.get("country_of_origin") == "DE", f"known country should override parsed text, got {values}")
    _assert(values.get("has_proof_of_origin") is False, f"known proof flag should carry forward, got {values}")
    _assert(values.get("customs_value_gbp") == 500, f"customs value should carry forward, got {values}")
    _assert(abs(float(values.get("excise_volume_litres") or 0) - 9.0) < 0.001, f"wine bottle volume should infer 9L, got {values}")
    _assert(abs(float(values.get("abv") or 0) - 12.5) < 0.001, f"ABV should infer from query, got {values}")
    _assert(sources.get("country_of_origin") == "Already elicited earlier in the journey.", f"known-input source missing, got {sources}")
    _assert({"country", "proof", "excise_abv"}.issubset(skipped), f"expected inferred steps to be skipped, got {skipped}")


def test_duty_landed_declaration(client: TestClient) -> None:
    reqs = _get(client, f"/api/duty/requirements/{COMPLEX_CODE}")
    _assert(reqs.get("needs_meursing_code") is True, "complex processed-food code should trigger Meursing inputs")

    duty = _post(
        client,
        "/api/duty",
        {
            "commodity_code": COMPLEX_CODE,
            "country_of_origin": "CN",
            "customs_value_gbp": 2065.0,
            "quantity_units": 120,
            "quantity_unit_type": "KGM",
            "has_proof_of_origin": False,
            "meursing_inputs": {
                "starch_glucose_pct": 18,
                "sucrose_invert_isoglucose_pct": 8,
                "milk_fat_pct": 1,
                "milk_protein_pct": 6,
            },
        },
    )
    _assert((duty.get("meursing") or {}).get("additional_code") == "7046", "duty should resolve Meursing additional code 7046")

    landed = _post(
        client,
        "/api/landed",
        {
            "customs_value_gbp": 2065.0,
            "customs_duty_gbp": duty.get("customs_duty_gbp") or 0,
            "excise_duty_gbp": duty.get("excise_duty_gbp") or 0,
            "vat_rate": duty.get("vat_rate") or 20,
        },
    )
    _assert(landed.get("vat_gbp") is not None, "landed calculation should return VAT")

    declaration = _post(
        client,
        "/api/declaration",
        {
            "commodity_code": COMPLEX_CODE,
            "description_of_goods": "Chocolate and soy protein isolate powder in 1kg retail tubs",
            "country_of_origin": "CN",
            "customs_value_gbp": 2065.0,
            "quantity_units": 120,
            "quantity_unit_type": "KGM",
            "net_mass_kg": 120,
            "duty_gbp": duty.get("customs_duty_gbp") or 0,
            "vat_gbp": landed.get("vat_gbp") or 0,
            "valuation_method": "method_1_transaction_value",
            "additional_codes": [{"type": "Meursing", "code": "7046"}],
            "original_query": "chocolate protein powder",
            "rejected_candidates": [{"code": "2106909849", "reason": "less specific than chocolate/cocoa preparation"}],
        },
    )
    boxes = declaration.get("cds_box_values") or {}
    _assert(boxes.get("DE 6/14 Commodity code (CN)") == COMPLEX_CODE[:8], "declaration should include the 8-digit CN code")
    _assert(boxes.get("DE 6/15 Commodity code (TARIC/additional digits)") == COMPLEX_CODE[8:10], "declaration should include the remaining TARIC/additional digits")
    _assert("7046" in (boxes.get("DE 6/16 Additional code(s)") or ""), "declaration should carry Meursing additional code")
    audit = declaration.get("audit_summary") or {}
    _assert((audit.get("scenario") or {}).get("additional_codes"), "audit summary should include additional codes")

    filing = _post(client, "/api/declaration/file-intent", {"declaration": declaration})
    _assert(filing.get("status") == "not_submitted", f"file-intent should not claim live submission, got {filing}")
    _assert(str(filing.get("reference") or "").startswith("DECL-"), f"file-intent should return demo reference, got {filing}")


def test_hydration(client: TestClient, classify_turn: dict) -> None:
    candidates = (classify_turn.get("candidates") or [{"commodity_code": COMPLEX_CODE}])[:5]
    run = _post(
        client,
        "/api/hydration/candidates",
        {
            "query": "chocolate protein powder",
            "candidates": candidates,
            "hydrate_limit": 0,
            "candidate_limit": len(candidates),
            "summarize": False,
        },
    )
    _assert(run.get("cache_write") is False, "live hydration should keep KG cache writes disabled in demo")
    _assert("does not invent commodity codes" in run.get("retrieval_guardrail", ""), "hydration guardrail should be explicit")
    hydrated = run.get("hydrated") or []
    _assert(hydrated, "hydration should return evidence for shortlisted candidates")
    _assert(len(hydrated) == len(candidates), f"hydrate_limit=0 should hydrate all provided candidates, got {len(hydrated)}/{len(candidates)}")
    selected = next((h for h in hydrated if (h.get("hydration") or {}).get("commodity_code") == COMPLEX_CODE), hydrated[0])
    hydration = selected.get("hydration") or {}
    _assert(hydration.get("ok") is True, "hydration should succeed for selected code")
    counts = (hydration.get("coverage") or {}).get("counts_by_kind") or {}
    _assert(sum(int(v) for v in counts.values()) > 0, "hydration should attach at least one evidence item")

    source_limited = _post(
        client,
        f"/api/commodity/{COMPLEX_CODE}/hydrate",
        {
            "summarize": False,
            "sources": {
                "facets": False,
                "footnotes": False,
                "measures": True,
                "section_notes": False,
                "chapter_notes": True,
                "hsen": False,
                "atar": False,
                "girs": False,
            },
        },
    )
    source_counts = (source_limited.get("coverage") or {}).get("counts_by_kind") or {}
    _assert(source_limited.get("sources_requested", {}).get("footnotes") is False, "source toggle should disable footnotes")
    _assert("footnote" not in source_counts, f"footnote source should be absent when disabled: {source_counts}")
    _assert(source_counts.get("measure", 0) >= 1, f"measure source should be present when enabled: {source_counts}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-live-kg", action="store_true", help="fail if the live uk/kg tariff database is unavailable")
    args = parser.parse_args()

    client = TestClient(app)
    examples_payload, complex_example = test_demo_personas(client, args.require_live_kg)
    classify_turn = test_classification(client, examples_payload, complex_example)
    test_provider_guard_default(client, examples_payload, complex_example)
    test_candidate_selection_toggle(client, examples_payload, complex_example)
    test_valuation_methods(client)
    test_duty_input_inference(client)
    test_duty_landed_declaration(client)
    test_hydration(client, classify_turn)

    print("e2e trader journey smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
