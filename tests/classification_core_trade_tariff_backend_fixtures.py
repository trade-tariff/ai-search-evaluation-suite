"""Realistic response bodies for classification_core.trade_tariff_backend tests, shaped to
match the real trade-tariff-backend serializers exactly (verified against
app/serializers/api/admin/search/evaluation/*.rb and
app/serializers/api/internal/*.rb in trade-tariff-backend, not guessed)."""

GOLD_QUERIES_RESPONSE = {
    "data": [
        {
            "id": "1",
            "type": "evaluation_gold_query",
            "attributes": {
                "source_type": "atar",
                "source_id": "600004365",
                "persona": "emu_generic",
                "query": "women's trainers",
                "expected_code": "6404199000",
                "expected_code_digits": 10,
                "expected_description": "Women's trainers",
                "notes": "ported emulator",
                "generator": "gpt-5-mini-2025-08-07",
                "active": True,
                "created_at": "2026-08-14T09:00:00.000Z",
            },
        },
    ],
    "meta": {"pagination": {"page": 1, "per_page": 20, "total_count": 1}},
}

_GOLD_QUERY_ROW_2 = {
    "id": "2",
    "type": "evaluation_gold_query",
    "attributes": {
        "source_type": "atar",
        "source_id": "600004366",
        "persona": "emu_generic",
        "query": "leather boots",
        "expected_code": "640391",
        "expected_code_digits": 6,
        "expected_description": "Leather boots covering the ankle",
        "notes": "ported emulator",
        "generator": "gpt-5-mini-2025-08-07",
        "active": True,
        "created_at": "2026-08-14T09:00:00.000Z",
    },
}

# Two pages of the SAME collection (total_count 2, one row per page) — proves
# the client keeps requesting pages until it has every row, rather than
# silently stopping at the Rails controller's per-page limit.
GOLD_QUERIES_RESPONSE_PAGE_1 = {
    "data": GOLD_QUERIES_RESPONSE["data"],
    "meta": {"pagination": {"page": 1, "per_page": 1, "total_count": 2}},
}

GOLD_QUERIES_RESPONSE_PAGE_2 = {
    "data": [_GOLD_QUERY_ROW_2],
    "meta": {"pagination": {"page": 2, "per_page": 1, "total_count": 2}},
}

# A gold query whose expected_code is only 6 digits — the granularity the
# source ruling actually published. Search results are always 10 digits, so
# scoring has to compare on the first expected_code_digits characters.
GOLD_QUERY_SIX_DIGIT = _GOLD_QUERY_ROW_2["attributes"] | {"id": _GOLD_QUERY_ROW_2["id"]}

ATAR_RULING_RESPONSE = {
    "data": {
        "id": "600004365",
        "type": "atar",
        "attributes": {
            "ref": "600004365",
            "commodity_code": "6404199000",
            "goods_nomenclature_item_id": "6404199000",
            "description": "Women's lace-up trainers, uppers of textile material.",
            "justification": "Classified in accordance with GIR 1.",
            "keywords": ["TRAINERS", "TEXTILE UPPER"],
            "validity_start_date": "2026-01-01",
            "validity_end_date": "2029-01-01",
            "source_url": "https://www.tax.service.gov.uk/search-for-advance-tariff-rulings/ruling/600004365",
            "fetched_at": "2026-08-01T09:00:00.000Z",
            "updated_at": "2026-08-01T09:00:00.000Z",
        },
    },
}

RUN_SHOW_RESPONSE = {
    "data": {
        "id": "107",
        "type": "run",
        "attributes": {
            "experiment_id": "42",
            "status": "queued",
            "triggered_by": "operator",
            "configuration_digest": "abc123def4567890",
            "effective_configuration": {"question_model": "gpt-5-mini-2025-08-07", "max_rounds": 3},
            "question_model": "gpt-5-mini-2025-08-07",
            "simulator_model": None,
            "started_at": None,
            "completed_at": None,
            "total_cost_usd": None,
            "total_provider_calls": 0,
            "total_latency_seconds": None,
            "result_count": 0,
            "error_count": 0,
            "error_summary": None,
            "aggregate_metrics": {},
            "created_at": "2026-08-18T09:00:00.000Z",
            "idempotency_key": "11111111-1111-1111-1111-111111111111",
        },
    },
}

EXPERIMENT_CREATE_RESPONSE = {
    "data": {
        "id": "42",
        "type": "experiment",
        "attributes": {
            "name": "ai1073-test-experiment",
            "description": None,
            "enabled": True,
            "configuration_overrides": {},
            "default_scope": {},
            "created_at": "2026-08-18T09:00:00.000Z",
            "created_by": None,
        },
    },
}

RUN_CREATE_RESPONSE = RUN_SHOW_RESPONSE  # same shape — RunSerializer is shared across create/show

RUN_UPDATE_RESPONSE = {
    "data": {
        "id": "107",
        "type": "run",
        "attributes": {**RUN_SHOW_RESPONSE["data"]["attributes"], "status": "running"},
    },
}

VALIDATION_ERROR_RESPONSE = {"errors": [{"detail": "max_rounds must be an Integer"}]}

_USAGE_ROUND_1 = {"total_cost_usd": 0.0021, "total_tokens": 320, "duration_ms": 540, "provider_calls": 1, "pricing_known": True}
_USAGE_ROUND_2 = {"total_cost_usd": 0.0035, "total_tokens": 410, "duration_ms": 610, "provider_calls": 1, "pricing_known": True}

SEARCH_RESPONSE_PENDING_QUESTION = {
    "data": [
        {"id": "1", "type": "goods_nomenclature", "attributes": {
            "goods_nomenclature_item_id": "6404110000", "description": "Trainers", "confidence": None,
        }},
    ],
    "meta": {
        "interactive_search": {
            "query": "women's trainers",
            "request_id": "req-1",
            "attempt": 1,
            "model": "gpt-5-mini-2025-08-07",
            "result_limit": 20,
            "answers": [
                {"question": "What are the uppers made of?", "options": ["Leather", "Textile", "Man-made", "Other"], "answer": None},
            ],
        },
        # Shaped to match SearchesController#summed_usage in trade-tariff-backend
        # exactly (app/controllers/api/admin/search/evaluation/searches_controller.rb)
        # -- present whenever at least one LLM/embedding call happened this round.
        "usage": _USAGE_ROUND_1,
    },
}

SEARCH_RESPONSE_CONVERGED = {
    "data": [
        {"id": "1", "type": "goods_nomenclature", "attributes": {
            "goods_nomenclature_item_id": "6404199000", "description": "Trainers, textile upper", "confidence": "strong",
        }},
    ],
    "meta": {
        "interactive_search": {
            "query": "women's trainers",
            "request_id": "req-1",
            "attempt": 2,
            "model": "gpt-5-mini-2025-08-07",
            "result_limit": 20,
            "answers": [
                {"question": "What are the uppers made of?", "options": ["Leather", "Textile", "Man-made", "Other"], "answer": "Textile"},
            ],
        },
        "usage": _USAGE_ROUND_2,
    },
}

# A converged response with NO usage key at all -- the short-circuit paths
# (single_result/no_results/disabled) never made an LLM call, so
# SearchesController#with_usage_meta leaves meta.usage absent entirely rather
# than sending a zeroed-out object.
SEARCH_RESPONSE_CONVERGED_NO_USAGE = {
    **SEARCH_RESPONSE_CONVERGED,
    "meta": {k: v for k, v in SEARCH_RESPONSE_CONVERGED["meta"].items() if k != "usage"},
}

RESULT_POST_RESPONSE = {
    "data": [
        {
            "id": "9",
            "type": "result",
            "attributes": {
                "run_id": "107", "source_type": "atar", "source_id": "600004365", "persona": "emu_generic",
                "expected_code": "6404199000", "final_code": "6404199000", "final_rank": 1,
                "gold_in_top1": True, "gold_in_top5": True, "latency_seconds": "4.2", "cost_usd": "0.01",
                "error": None, "trace": [], "created_at": "2026-08-18T09:05:00.000Z",
            },
        },
    ],
}

RESULT_POST_VALIDATION_ERROR_RESPONSE = {"data": [{"error": "run not found", "index": 0}]}
