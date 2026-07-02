-- Vector (HNSW) search tuning - applied to the deployed DB on 2026-07-02.
--
-- Why: pgvector's HNSW index returns at most hnsw.ef_search rows per scan
-- (default 40). Every vector leg asking for more than 40 candidates was
-- silently truncated to 40; after raising ef_search, the planner then chose
-- a multi-second sequential scan for one leg because the default
-- random_page_cost (4.0) overprices SSD reads.
--
-- Measured on the deployed instance (warm):
--   uk.goods_nomenclature_self_texts, depth 200:  40 rows/183ms -> 200 rows/5.6ms
--   kg.commodity_facets,             depth 500:  40 rows capped -> 500 rows/14.2ms
--
-- iterative_scan (pgvector >= 0.8) lets scans continue past ef_search when a
-- larger LIMIT demands it, so depth-500 legs stay correct without paying
-- ef_search=500 on every query.
--
-- Note: intercept_retrieval.py deliberately runs SET LOCAL hnsw.ef_search=100
-- per transaction to mirror the production VectorRetrievalService - that
-- override still applies and is intentional.
--
-- Comparability: runs before 2026-07-02 had vector legs capped at 40
-- candidates; retrieval-dependent metrics may improve after this change.

ALTER DATABASE tariff_db SET hnsw.ef_search = 200;
ALTER DATABASE tariff_db SET hnsw.iterative_scan = 'relaxed_order';
ALTER DATABASE tariff_db SET random_page_cost = 1.1;
