-- Explicit surface/use scoping for KG evidence.
--
-- `authority_tier` answers "how reliable/binding is this source?"
-- `use_scopes` answers "which app surfaces may consume this row?"
--
-- Supported scopes:
--   retrieval      - candidate recall/ranking/search
--   classification - model or deterministic classification reasoning
--   qa             - trader-facing classification Q&A
--   valuation      - customs valuation journey
--   duty           - duty/import measure journey
--   landed_cost    - import-cost presentation and total payable calculation
--   declaration    - declaration/documents/compliance journey
--   audit          - evidence display, provenance, debugging

ALTER TABLE kg.commodity_facets
  ADD COLUMN IF NOT EXISTS use_scopes text[] NOT NULL
  DEFAULT ARRAY['retrieval','classification','qa','audit']::text[];

ALTER TABLE kg.kg_edges
  ADD COLUMN IF NOT EXISTS use_scopes text[] NOT NULL
  DEFAULT ARRAY['retrieval','classification','qa','audit']::text[];

CREATE INDEX IF NOT EXISTS commodity_facets_use_scopes_gin
  ON kg.commodity_facets USING gin(use_scopes);

CREATE INDEX IF NOT EXISTS kg_edges_use_scopes_gin
  ON kg.kg_edges USING gin(use_scopes);

-- Facets:
--   - Search References/common terms are excellent retrieval aliases, but not
--     product characteristics to ask a trader about.
--   - Measure/document/certificate/origin rows are useful later in duty or
--     declaration, but hostile as classification questions.
--   - Exclusion-like extracted facts can aid classification reasoning but
--     should generally not become trader-facing Q&A options.
UPDATE kg.commodity_facets
SET use_scopes = CASE
  WHEN source = 'search_reference' OR facet_key = 'common_term' THEN
    ARRAY['retrieval','audit']::text[]
  WHEN source = 'measure_condition'
    OR lower(facet_key) ~ '(origin|country|destination|geograph|consign|import_date|date|duty|vat|preference|quota|measure|certificate|document|licen[cs]e|relief|suspension)'
  THEN
    ARRAY['retrieval','duty','landed_cost','declaration','audit']::text[]
  WHEN lower(facet_key) ~ '(exclude|excluded|excludes|exclusion)' THEN
    ARRAY['retrieval','classification','audit']::text[]
  ELSE
    ARRAY['retrieval','classification','qa','audit']::text[]
END;

-- KG edges:
--   - Legal/HSEN/GIR/ATAR rationale edges support retrieval and
--     classification reasoning, and may inform Q&A wording.
--   - Duty-treatment edges belong to duty, not classification.
--   - Footnotes are retained for retrieval/declaration/audit unless separately
--     promoted to a classification rule.
UPDATE kg.kg_edges
SET use_scopes = CASE
  WHEN type = 'duty_treatment' THEN
    ARRAY['retrieval','duty','landed_cost','audit']::text[]
  WHEN type = 'footnote' OR authority_tier >= 7 THEN
    ARRAY['retrieval','declaration','audit']::text[]
  ELSE
    ARRAY['retrieval','classification','qa','audit']::text[]
END;
