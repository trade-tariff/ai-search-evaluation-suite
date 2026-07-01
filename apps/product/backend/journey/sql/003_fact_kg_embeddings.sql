-- 003: per-fact and per-edge embeddings for semantic retrieval.
--
-- Following codex review: per-fact embeddings (NOT per-code aggregates).
-- Codex's reasoning: per-code bags mix unrelated facts and hide which fact
-- matched. Per-fact embedded -> aggregate to code at fusion time gives us
-- attribution ("the L001 CITES fact for this code was hit by the query").
--
-- Dim 1536 (text-embedding-3-small) so it matches uk.goods_nomenclature_self_texts.
-- HNSW (m=16, ef_construction=64) chosen to match the rest of the codebase.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE kg.commodity_facets
    ADD COLUMN IF NOT EXISTS embedding vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_stale boolean NOT NULL DEFAULT true;

ALTER TABLE kg.kg_edges
    ADD COLUMN IF NOT EXISTS embedding vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_stale boolean NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS commodity_facets_embedding_hnsw
    ON kg.commodity_facets USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS kg_edges_embedding_hnsw
    ON kg.kg_edges USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS commodity_facets_embedding_stale
    ON kg.commodity_facets (embedding_stale) WHERE embedding_stale = true;
CREATE INDEX IF NOT EXISTS kg_edges_embedding_stale
    ON kg.kg_edges (embedding_stale) WHERE embedding_stale = true;

-- Trigger: any UPDATE that changes the text contributing to the embedding
-- flips embedding_stale back to true. Cheaper than re-embedding eagerly.
CREATE OR REPLACE FUNCTION kg.fn_mark_facet_embedding_stale() RETURNS trigger AS $$
BEGIN
    IF NEW.facet_key IS DISTINCT FROM OLD.facet_key
       OR NEW.facet_value IS DISTINCT FROM OLD.facet_value
       OR NEW.evidence IS DISTINCT FROM OLD.evidence THEN
        NEW.embedding := NULL;
        NEW.embedding_stale := true;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION kg.fn_mark_edge_embedding_stale() RETURNS trigger AS $$
BEGIN
    IF NEW.title IS DISTINCT FROM OLD.title
       OR NEW.body IS DISTINCT FROM OLD.body THEN
        NEW.embedding := NULL;
        NEW.embedding_stale := true;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mark_facet_embedding_stale ON kg.commodity_facets;
CREATE TRIGGER mark_facet_embedding_stale
    BEFORE UPDATE ON kg.commodity_facets
    FOR EACH ROW EXECUTE FUNCTION kg.fn_mark_facet_embedding_stale();

DROP TRIGGER IF EXISTS mark_edge_embedding_stale ON kg.kg_edges;
CREATE TRIGGER mark_edge_embedding_stale
    BEFORE UPDATE ON kg.kg_edges
    FOR EACH ROW EXECUTE FUNCTION kg.fn_mark_edge_embedding_stale();
