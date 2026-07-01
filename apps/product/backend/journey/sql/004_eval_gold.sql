-- Gold-standard eval set for retrieval.
--
-- Each row = one (query, expected_commodity_code) pair. Multiple paraphrases
-- per source case so we can test trader-vocabulary -> tariff-vocabulary
-- bridging, not just verbatim recall of the customs-language original.
--
-- source_type values:
--   'atar'              - paraphrased ATAR product description
--   'search_reference'  - a curated common_term from uk.search_references (used
--                         with curated leg DISABLED to test the semantic legs)
--   'manual'            - hand-authored cases for edge coverage
--
-- persona values (for atar-derived rows):
--   'original'        - verbatim ATAR product description (sanity baseline)
--   'naive_vague'     - 2-5 words, generalist guess ("shoes", "metal bracket")
--   'naive_branded'   - colloquial / brand / market language ("Crocs", "Nespresso pod")
--   'naive_specific'  - novice attempt at being precise (some details right, some wrong)

CREATE TABLE IF NOT EXISTS kg.eval_gold (
    id            bigserial PRIMARY KEY,
    source_type   text NOT NULL,                 -- atar / search_reference / manual
    source_id     text,                          -- e.g. 'atar_600014698' or the search_reference id
    persona       text,                          -- original / naive_vague / naive_branded / naive_specific
    query         text NOT NULL,
    expected_code text NOT NULL,                 -- 10-digit
    expected_description text,
    notes         text,
    generator     text,                          -- 'gpt-5.5' / 'human' / 'verbatim'
    created_at    timestamp NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, persona, query)
);

CREATE INDEX IF NOT EXISTS eval_gold_by_source ON kg.eval_gold (source_type, source_id);
CREATE INDEX IF NOT EXISTS eval_gold_by_code ON kg.eval_gold (expected_code);

-- Eval-run results, so we can compare configs over time.
CREATE TABLE IF NOT EXISTS kg.eval_runs (
    id            bigserial PRIMARY KEY,
    run_label     text NOT NULL,                  -- e.g. 'all_legs_on' / 'semantic_off' / 'hsen_off'
    config_json   jsonb NOT NULL,
    started_at    timestamp NOT NULL DEFAULT now(),
    finished_at   timestamp,
    n_queries     int,
    recall_at_1   numeric(5,4),
    recall_at_5   numeric(5,4),
    recall_at_10  numeric(5,4),
    -- looser matches: 8-digit prefix (subheading) and 4-digit (heading)
    recall_at_5_subheading numeric(5,4),
    recall_at_5_heading    numeric(5,4),
    mrr           numeric(5,4),
    notes         text
);

CREATE TABLE IF NOT EXISTS kg.eval_run_results (
    id            bigserial PRIMARY KEY,
    run_id        bigint NOT NULL REFERENCES kg.eval_runs(id) ON DELETE CASCADE,
    gold_id       bigint NOT NULL REFERENCES kg.eval_gold(id),
    expected_code text NOT NULL,
    top_codes     text[] NOT NULL,                 -- top-10 returned codes
    top_sources   jsonb,                           -- which legs contributed for each
    rank_of_expected int,                          -- 1-based rank if found in top-10, else NULL
    rank_subheading int,                           -- 1-based rank where SUBSTRING(code, 1, 8) matches
    rank_heading int                               -- 1-based rank where SUBSTRING(code, 1, 4) matches
);

CREATE INDEX IF NOT EXISTS eval_run_results_run ON kg.eval_run_results (run_id);
