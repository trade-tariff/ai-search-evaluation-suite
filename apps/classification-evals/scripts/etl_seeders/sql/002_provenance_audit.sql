-- Authority tier + structured provenance + audit log for the KG.
-- Tier reference (lower = more authoritative):
--   1 Binding legal rule (GIRs, chapter notes, section notes)
--   2 Binding ruling for a product (ATARs)
--   3 Authoritative interpretive guidance (HSEN, subheading notes, HMRC notices)
--   4 Curated expert classification (search_references, manuals)
--   5 AI-derived from authoritative source (LLM-extracted from ATAR description+justification)
--   6 AI-derived from descriptive text (LLM-extracted from TARIC descriptions)
--   7 Footnotes / measure-attached metadata (mixed authority)
--   8 External / unverified

ALTER TABLE kg.commodity_facets
  ADD COLUMN IF NOT EXISTS authority_tier int DEFAULT 6 CHECK (authority_tier BETWEEN 1 AND 8),
  ADD COLUMN IF NOT EXISTS provenance jsonb,
  ADD COLUMN IF NOT EXISTS updated_at timestamp,
  ADD COLUMN IF NOT EXISTS superseded_by bigint REFERENCES kg.commodity_facets(id) ON DELETE SET NULL;

ALTER TABLE kg.kg_edges
  ADD COLUMN IF NOT EXISTS authority_tier int DEFAULT 3 CHECK (authority_tier BETWEEN 1 AND 8),
  ADD COLUMN IF NOT EXISTS provenance jsonb,
  ADD COLUMN IF NOT EXISTS updated_at timestamp,
  ADD COLUMN IF NOT EXISTS superseded_by text REFERENCES kg.kg_edges(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS commodity_facets_by_tier ON kg.commodity_facets(authority_tier);
CREATE INDEX IF NOT EXISTS kg_edges_by_tier ON kg.kg_edges(authority_tier);

CREATE TABLE IF NOT EXISTS kg.audit_log (
  id bigserial PRIMARY KEY,
  table_name text NOT NULL,
  row_id text NOT NULL,
  action text NOT NULL CHECK (action IN ('create','update','delete','verify','supersede')),
  old_value jsonb,
  new_value jsonb,
  actor text NOT NULL,
  reason text,
  created_at timestamp NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_by_row ON kg.audit_log(table_name, row_id);
CREATE INDEX IF NOT EXISTS audit_log_by_created ON kg.audit_log(created_at DESC);

-- Audit trigger: writes a create/update/delete row on every mutation.
-- Caller can override the actor via SET LOCAL kg.actor = '...' inside a transaction;
-- defaults to current_user otherwise.
CREATE OR REPLACE FUNCTION kg.fn_audit_trigger()
RETURNS trigger AS $$
DECLARE
  v_actor text;
  v_row_id text;
BEGIN
  -- Read session-local actor if set; fall back to db user.
  BEGIN
    v_actor := current_setting('kg.actor', true);
    IF v_actor IS NULL OR v_actor = '' THEN
      v_actor := 'db:' || current_user;
    END IF;
  EXCEPTION WHEN OTHERS THEN
    v_actor := 'db:' || current_user;
  END;

  IF TG_OP = 'DELETE' THEN
    v_row_id := COALESCE(OLD.id::text, '');
    INSERT INTO kg.audit_log (table_name, row_id, action, old_value, actor)
    VALUES (TG_TABLE_NAME, v_row_id, 'delete', to_jsonb(OLD), v_actor);
    RETURN OLD;
  ELSIF TG_OP = 'UPDATE' THEN
    v_row_id := COALESCE(NEW.id::text, '');
    NEW.updated_at := now();
    INSERT INTO kg.audit_log (table_name, row_id, action, old_value, new_value, actor)
    VALUES (TG_TABLE_NAME, v_row_id, 'update', to_jsonb(OLD), to_jsonb(NEW), v_actor);
    RETURN NEW;
  ELSE  -- INSERT
    v_row_id := COALESCE(NEW.id::text, '');
    INSERT INTO kg.audit_log (table_name, row_id, action, new_value, actor)
    VALUES (TG_TABLE_NAME, v_row_id, 'create', to_jsonb(NEW), v_actor);
    RETURN NEW;
  END IF;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_commodity_facets ON kg.commodity_facets;
CREATE TRIGGER audit_commodity_facets
  AFTER INSERT OR UPDATE OR DELETE ON kg.commodity_facets
  FOR EACH ROW EXECUTE FUNCTION kg.fn_audit_trigger();

DROP TRIGGER IF EXISTS audit_kg_edges ON kg.kg_edges;
CREATE TRIGGER audit_kg_edges
  AFTER INSERT OR UPDATE OR DELETE ON kg.kg_edges
  FOR EACH ROW EXECUTE FUNCTION kg.fn_audit_trigger();

-- --- Retrofit tiers + provenance for existing rows ----------------------

-- Facets
UPDATE kg.commodity_facets SET
  authority_tier = CASE
    WHEN source = 'hand' THEN 4
    WHEN source = 'verified' THEN 4
    WHEN source = 'description_llm' THEN 6
    WHEN source LIKE 'atar:%' THEN 5      -- LLM-extracted from a binding ruling
    WHEN source = 'footnote_llm' THEN 7
    WHEN source = 'measure_condition' THEN 3
    WHEN source = 'search_reference' THEN 4
    ELSE 6
  END,
  provenance = jsonb_build_object(
    'source_type', CASE
      WHEN source LIKE 'atar:%' THEN 'atar'
      WHEN source = 'description_llm' THEN 'tariff_description'
      WHEN source = 'hand' THEN 'hand_authored'
      WHEN source = 'verified' THEN 'human_verified'
      WHEN source = 'footnote_llm' THEN 'footnote'
      WHEN source = 'measure_condition' THEN 'measure_condition'
      WHEN source = 'search_reference' THEN 'search_reference'
      ELSE 'unknown'
    END,
    'source_id', CASE
      WHEN source LIKE 'atar:%' THEN substring(source from 'atar:(.*)')
      ELSE NULL
    END,
    'extracted_by', CASE
      WHEN source IN ('description_llm','footnote_llm') OR source LIKE 'atar:%' THEN 'gpt-5.5'
      ELSE NULL
    END
  )
WHERE authority_tier IS NULL OR authority_tier = 6;  -- only touch defaulted rows on retrofit

-- KG edges
UPDATE kg.kg_edges SET
  authority_tier = CASE
    WHEN id LIKE 'gir_%' THEN 1                                    -- GIRs are top-of-stack
    WHEN scope LIKE 'chapter:%' AND id ~ '_note_' THEN 1          -- decomposed chapter notes are binding
    WHEN scope LIKE 'section:%' AND id ~ '_note_' THEN 1          -- section notes are binding
    WHEN scope LIKE 'chapter:%' AND id ~ '_notes$' THEN 1         -- legacy blob chapter notes still binding
    WHEN scope LIKE 'section:%' AND id ~ '_notes$' THEN 1
    WHEN id LIKE 'atar_%' THEN 2                                   -- ATARs are binding for that product
    WHEN type = 'duty_treatment' THEN 3                            -- guidance, not always legally binding
    WHEN scope LIKE 'heading:%' THEN 3                             -- heading-level interpretive
    ELSE 3
  END,
  provenance = jsonb_build_object(
    'source_type', CASE
      WHEN id LIKE 'gir_%' THEN 'gir'
      WHEN id LIKE 'atar_%' THEN 'atar'
      WHEN id ~ '_note_' OR id ~ '_notes$' THEN 'chapter_section_note'
      ELSE 'manual'
    END,
    'source_id', CASE
      WHEN id LIKE 'atar_%' THEN substring(id from 'atar_(.*)')
      ELSE NULL
    END,
    'scope_ref', scope
  )
WHERE authority_tier IS NULL OR authority_tier = 3;

-- Quick verification view
CREATE OR REPLACE VIEW kg.facet_tier_summary AS
SELECT authority_tier, source, count(*) FROM kg.commodity_facets GROUP BY 1, 2 ORDER BY 1, 2;
CREATE OR REPLACE VIEW kg.edge_tier_summary AS
SELECT authority_tier, type, count(*) FROM kg.kg_edges GROUP BY 1, 2 ORDER BY 1, 2;
