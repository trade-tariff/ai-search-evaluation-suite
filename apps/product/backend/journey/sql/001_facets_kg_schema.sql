-- Knowledge layer for AI-Guided Search augmentation.
--
-- These tables live in a DEDICATED `kg` schema, isolated from the TARIC core
-- in `uk` / `xi`. The production dump only emits DROP statements for tables
-- that exist in the source DB, but using a separate schema makes the
-- isolation explicit and survives even an aggressive `DROP SCHEMA uk CASCADE`
-- refresh. See trader-journey-poc/backend/scripts/backup_kg.sh for a
-- defence-in-depth dump/restore helper around `tariff-db.md`'s refresh.

CREATE SCHEMA IF NOT EXISTS kg;

CREATE TABLE IF NOT EXISTS kg.facet_definitions (
    key text PRIMARY KEY,
    label text NOT NULL,
    short_label text,
    value_set jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{value, label}, ...]
    applies_to_chapters text[] DEFAULT ARRAY[]::text[],
    rank int NOT NULL DEFAULT 99,
    created_at timestamp NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kg.commodity_facets (
    id bigserial PRIMARY KEY,
    commodity_code text NOT NULL,           -- 10-digit flat code, e.g. '6402200000'
    facet_key text NOT NULL REFERENCES kg.facet_definitions(key) ON DELETE CASCADE,
    facet_value text NOT NULL,
    source text NOT NULL,                   -- 'hand', 'description_llm', 'atar:<ref>', 'chapter_notes_llm', ...
    confidence numeric(3,2) DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    evidence text,                          -- the original snippet that justified this fact
    authority_tier int DEFAULT 6 CHECK (authority_tier BETWEEN 1 AND 8),
    use_scopes text[] NOT NULL DEFAULT ARRAY['audit']::text[],
    evidence_roles text[] NOT NULL DEFAULT ARRAY['unknown']::text[],
    provenance jsonb,
    updated_at timestamp,
    superseded_by bigint REFERENCES kg.commodity_facets(id) ON DELETE SET NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT commodity_facets_unique UNIQUE (commodity_code, facet_key, facet_value, source)
);
CREATE INDEX IF NOT EXISTS commodity_facets_by_code ON kg.commodity_facets(commodity_code);
CREATE INDEX IF NOT EXISTS commodity_facets_by_key ON kg.commodity_facets(facet_key);
CREATE INDEX IF NOT EXISTS commodity_facets_use_scopes_gin ON kg.commodity_facets USING gin(use_scopes);
CREATE INDEX IF NOT EXISTS commodity_facets_evidence_roles_gin ON kg.commodity_facets USING gin(evidence_roles);

CREATE TABLE IF NOT EXISTS kg.kg_edges (
    id text PRIMARY KEY,                    -- e.g. 'ch64_outer_sole_first', 'ch64_note_1a', 'atar:600014923_rationale'
    type text NOT NULL,                     -- classification_order | discriminator | exclusion | definition | duty_treatment | rationale
    scope text NOT NULL,                    -- 'chapter:64', 'section:XI', 'heading:6402', 'global'
    title text NOT NULL,
    body text NOT NULL,
    source text NOT NULL,                   -- 'HSEN Chapter 64', 'Chapter 64 Note 1(a)', 'ATAR 600014923', ...
    authority_tier int DEFAULT 3 CHECK (authority_tier BETWEEN 1 AND 8),
    use_scopes text[] NOT NULL DEFAULT ARRAY['audit']::text[],
    evidence_roles text[] NOT NULL DEFAULT ARRAY['unknown']::text[],
    provenance jsonb,
    updated_at timestamp,
    superseded_by text REFERENCES kg.kg_edges(id) ON DELETE SET NULL,
    created_at timestamp NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kg_edges_by_scope ON kg.kg_edges(scope);
CREATE INDEX IF NOT EXISTS kg_edges_use_scopes_gin ON kg.kg_edges USING gin(use_scopes);
CREATE INDEX IF NOT EXISTS kg_edges_evidence_roles_gin ON kg.kg_edges USING gin(evidence_roles);

CREATE TABLE IF NOT EXISTS kg.kg_edge_commodities (
    edge_id text NOT NULL REFERENCES kg.kg_edges(id) ON DELETE CASCADE,
    commodity_code text NOT NULL,
    PRIMARY KEY (edge_id, commodity_code)
);
CREATE INDEX IF NOT EXISTS kg_edge_commodities_by_code ON kg.kg_edge_commodities(commodity_code);

CREATE TABLE IF NOT EXISTS kg.evidence_label_definitions (
  label_kind text NOT NULL CHECK (label_kind IN ('use_scope', 'evidence_role')),
  key text NOT NULL,
  label text NOT NULL,
  description text NOT NULL,
  created_at timestamp NOT NULL DEFAULT now(),
  PRIMARY KEY (label_kind, key)
);
