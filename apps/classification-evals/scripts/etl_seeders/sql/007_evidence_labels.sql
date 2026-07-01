-- Evidence labels v2: separate consumer scopes from semantic roles.
--
-- use_scopes says where evidence may be consumed.
-- evidence_roles says what kind of evidence the row represents.
--
-- This migration intentionally tightens the first use_scopes pass:
--   - measure/document facts are not candidate-retrieval facts;
--   - broad HSEN general notes are not trader-facing Q&A;
--   - classification-order rules are for reasoning, not Q&A options.

CREATE TABLE IF NOT EXISTS kg.evidence_label_definitions (
  label_kind text NOT NULL CHECK (label_kind IN ('use_scope', 'evidence_role')),
  key text NOT NULL,
  label text NOT NULL,
  description text NOT NULL,
  created_at timestamp NOT NULL DEFAULT now(),
  PRIMARY KEY (label_kind, key)
);

INSERT INTO kg.evidence_label_definitions (label_kind, key, label, description)
VALUES
  ('use_scope','retrieval','Retrieval','Candidate retrieval and ranking from trader product text.'),
  ('use_scope','classification','Classification','Classification reasoning and final candidate selection.'),
  ('use_scope','qa','Q&A','Trader-facing classification questions and answer options.'),
  ('use_scope','valuation','Value','Customs valuation method selection, inputs, and value calculation.'),
  ('use_scope','duty','Duty','Duty, excise, VAT-rate, measure, preference, and additional-code calculation.'),
  ('use_scope','landed_cost','Landed Cost','Import-cost presentation: customs value, duty, excise, VAT, charges, and total cost.'),
  ('use_scope','declaration','Declaration','Certificates, documents, footnotes, declaration handoff, and compliance data.'),
  ('use_scope','audit','Audit','Evidence display, provenance, explanation, and debugging.'),
  ('evidence_role','alias','Alias','Trader/common vocabulary that points to a commodity code.'),
  ('evidence_role','product_identity','Product Identity','What the goods are, product family, or category.'),
  ('evidence_role','material_composition','Material / Composition','Material, ingredient, substance, or composition facts.'),
  ('evidence_role','form_presentation','Form / Presentation','Physical form, processing state, construction, or presentation.'),
  ('evidence_role','function_use','Function / Use','Intended use, function, end use, or application.'),
  ('evidence_role','packaging_quantity','Packaging / Quantity','Pack size, container, volume, weight, dimensions, or thresholds.'),
  ('evidence_role','composition_threshold','Composition Threshold','Threshold values such as alcohol, fat, protein, sugar, starch, or solids.'),
  ('evidence_role','additional_code','Additional Code','Meursing or other additional-code input or result.'),
  ('evidence_role','origin_or_region','Origin / Region','Origin, consignment, preference geography, appellation, or region.'),
  ('evidence_role','legal_definition','Legal Definition','Legal definition or term meaning from GIRs, notes, or HSEN.'),
  ('evidence_role','legal_inclusion','Legal Inclusion','Rule saying goods are included in a scope.'),
  ('evidence_role','legal_exclusion','Legal Exclusion','Rule saying goods are excluded from a scope.'),
  ('evidence_role','classification_order','Classification Order','Ordering rule such as GIR order or chapter/section precedence.'),
  ('evidence_role','classification_rationale','Rationale','Ruling or extracted rationale for classification.'),
  ('evidence_role','interpretive_guidance','Interpretive Guidance','HSEN or other interpretive guidance.'),
  ('evidence_role','heading_guidance','Heading Guidance','Heading-level interpretive guidance.'),
  ('evidence_role','measure_condition','Measure Condition','Measure condition or operational requirement.'),
  ('evidence_role','document_requirement','Document Requirement','Certificate, licence, document, or footnote requirement.'),
  ('evidence_role','duty_rate_measure','Duty Rate / Measure','Duty, VAT-rate, excise, quota, suspension, or preference measure.'),
  ('evidence_role','valuation_input','Valuation Input','Invoice, freight, insurance, comparator, deductive, computed, or fallback value input.'),
  ('evidence_role','valuation_method','Valuation Method','One of the customs valuation method rules or choices.'),
  ('evidence_role','landed_cost_component','Landed Cost Component','Customs value, duty, excise, VAT, broker fees, port charges, or totals used in import-cost presentation.'),
  ('evidence_role','landed_cost_result','Landed Cost Result','Calculated landed-cost, VAT taxable amount, or total payable result.'),
  ('evidence_role','declaration_data','Declaration Data','Data carried into declaration/handoff.'),
  ('evidence_role','footnote','Footnote','Footnote text attached to a commodity or measure.'),
  ('evidence_role','index_text','Index Text','Search-only text artifact, not itself a fact.'),
  ('evidence_role','unknown','Unknown','Unclassified evidence role.')
ON CONFLICT (label_kind, key) DO UPDATE
SET label = EXCLUDED.label,
    description = EXCLUDED.description;

ALTER TABLE kg.commodity_facets
  ADD COLUMN IF NOT EXISTS use_scopes text[] NOT NULL DEFAULT ARRAY['audit']::text[],
  ADD COLUMN IF NOT EXISTS evidence_roles text[] NOT NULL DEFAULT ARRAY['unknown']::text[];

ALTER TABLE kg.kg_edges
  ADD COLUMN IF NOT EXISTS use_scopes text[] NOT NULL DEFAULT ARRAY['audit']::text[],
  ADD COLUMN IF NOT EXISTS evidence_roles text[] NOT NULL DEFAULT ARRAY['unknown']::text[];

CREATE INDEX IF NOT EXISTS commodity_facets_use_scopes_gin
  ON kg.commodity_facets USING gin(use_scopes);

CREATE INDEX IF NOT EXISTS kg_edges_use_scopes_gin
  ON kg.kg_edges USING gin(use_scopes);

CREATE INDEX IF NOT EXISTS commodity_facets_evidence_roles_gin
  ON kg.commodity_facets USING gin(evidence_roles);

CREATE INDEX IF NOT EXISTS kg_edges_evidence_roles_gin
  ON kg.kg_edges USING gin(evidence_roles);

-- Repair known tier drift before assigning labels.
UPDATE kg.commodity_facets
SET authority_tier = CASE
  WHEN source LIKE 'atar:%' THEN 5
  WHEN source = 'measure_condition' THEN 3
  WHEN source = 'search_reference' THEN 4
  WHEN source IN ('hand', 'verified') THEN 4
  WHEN source = 'description_llm' THEN 6
  ELSE authority_tier
END;

UPDATE kg.commodity_facets
SET provenance = jsonb_build_object(
    'source_type', 'atar',
    'source_id', substring(source from 'atar:(.*)'),
    'extracted_by', COALESCE(provenance->>'extracted_by', 'unknown')
)
WHERE source LIKE 'atar:%'
  AND (provenance IS NULL OR provenance = '{}'::jsonb);

UPDATE kg.kg_edges
SET authority_tier = CASE
  WHEN id LIKE 'gir_%' THEN 1
  WHEN source LIKE 'UK Tariff Chapter % Notes' OR source LIKE 'UK Tariff Section % Notes' THEN 1
  WHEN id ~ '^(ch|sec)[0-9XVIxvi]+_note_' THEN 1
  WHEN id LIKE 'atar_%' THEN 2
  WHEN type LIKE 'hsen_%' THEN 3
  WHEN type = 'duty_treatment' THEN 3
  ELSE authority_tier
END;

UPDATE kg.kg_edges
SET provenance = jsonb_build_object(
    'source_type', 'atar',
    'source_id', substring(id from 'atar_([^_]+)'),
    'scope_ref', scope
)
WHERE id LIKE 'atar_%'
  AND (provenance IS NULL OR provenance = '{}'::jsonb);

UPDATE kg.kg_edges
SET provenance = jsonb_build_object(
    'source_type', 'chapter_section_note',
    'scope_ref', scope
)
WHERE (source LIKE 'UK Tariff Chapter % Notes' OR source LIKE 'UK Tariff Section % Notes'
       OR id ~ '^(ch|sec)[0-9XVIxvi]+_note_')
  AND (provenance IS NULL OR provenance = '{}'::jsonb);

-- Safer defaults for future insertions that forget labels. Seeders should still
-- supply explicit labels.
ALTER TABLE kg.commodity_facets
  ALTER COLUMN use_scopes SET DEFAULT ARRAY['audit']::text[],
  ALTER COLUMN evidence_roles SET DEFAULT ARRAY['unknown']::text[];

ALTER TABLE kg.kg_edges
  ALTER COLUMN use_scopes SET DEFAULT ARRAY['audit']::text[],
  ALTER COLUMN evidence_roles SET DEFAULT ARRAY['unknown']::text[];

UPDATE kg.commodity_facets
SET
  use_scopes = CASE
    WHEN source = 'search_reference' OR facet_key = 'common_term' THEN
      ARRAY['retrieval','audit']::text[]
    WHEN source = 'measure_condition' OR facet_key = 'requires_certificate' THEN
      ARRAY['duty','declaration','audit']::text[]
    WHEN lower(facet_key) ~ '(invoice|freight|insurance|customs_value|value|price|cost|fx|resale|computed|deductive|fallback)' THEN
      ARRAY['valuation','landed_cost','audit']::text[]
    WHEN lower(facet_key) ~ '(duty|vat|quota|measure|certificate|document|licen[cs]e|relief|suspension|proof)' THEN
      ARRAY['duty','landed_cost','declaration','audit']::text[]
    WHEN lower(facet_key) ~ '(country_of_origin|country_of_dispatch|destination|geographical_area|geograph.*area|consign|import_origin|preference|quota)' THEN
      ARRAY['duty','declaration','audit']::text[]
    WHEN lower(facet_key) ~ '(origin|region|appellation|pdo|pgi|designation)' THEN
      ARRAY['retrieval','classification','qa','audit']::text[]
    WHEN lower(facet_key) ~ '(exclude|excluded|excludes|exclusion)' THEN
      ARRAY['retrieval','classification','audit']::text[]
    WHEN lower(facet_key) ~ '(meursing|additional_code|starch|sucrose|glucose|milk_fat|milk_protein|milk_solids)' THEN
      ARRAY['retrieval','classification','qa','duty','landed_cost','declaration','audit']::text[]
    ELSE
      ARRAY['retrieval','classification','qa','audit']::text[]
  END,
  evidence_roles = CASE
    WHEN source = 'search_reference' OR facet_key = 'common_term' THEN
      ARRAY['alias']::text[]
    WHEN source = 'measure_condition' OR facet_key = 'requires_certificate' THEN
      ARRAY['measure_condition','document_requirement']::text[]
    WHEN lower(facet_key) ~ '(invoice|freight|insurance|customs_value|value|price|cost|fx|resale|computed|deductive|fallback)' THEN
      ARRAY['valuation_input']::text[]
    WHEN lower(facet_key) ~ '(duty|vat|quota|measure|certificate|document|licen[cs]e|relief|suspension|proof)' THEN
      ARRAY['duty_rate_measure','landed_cost_component']::text[]
    WHEN lower(facet_key) ~ '(country_of_origin|country_of_dispatch|destination|geographical_area|geograph.*area|consign|import_origin|preference|quota)' THEN
      ARRAY['origin_or_region']::text[]
    WHEN lower(facet_key) ~ '(origin|region|appellation|pdo|pgi|designation)' THEN
      ARRAY['origin_or_region']::text[]
    WHEN lower(facet_key) ~ '(exclude|excluded|excludes|exclusion)' THEN
      ARRAY['legal_exclusion']::text[]
    WHEN lower(facet_key) ~ '(meursing|additional_code|starch|sucrose|glucose|milk_fat|milk_protein|milk_solids)' THEN
      ARRAY['additional_code','composition_threshold']::text[]
    WHEN lower(facet_key) ~ '(protein|fat|sugar|alcohol|abv|content|carbon)' THEN
      ARRAY['composition_threshold']::text[]
    WHEN lower(facet_key) ~ '(material|composition|ingredient|component|substance)' THEN
      ARRAY['material_composition']::text[]
    WHEN lower(facet_key) ~ '(form|state|processing|process|prepared|presentation|construction|manufactur|coating|fermentation)' THEN
      ARRAY['form_presentation']::text[]
    WHEN lower(facet_key) ~ '(function|use|purpose|application|end_use)' THEN
      ARRAY['function_use']::text[]
    WHEN lower(facet_key) ~ '(package|packing|container|net|weight|volume|size|capacity|dimension|diameter|thickness|length|width|cross_section|strength)' THEN
      ARRAY['packaging_quantity']::text[]
    ELSE
      ARRAY['product_identity']::text[]
  END;

UPDATE kg.kg_edges
SET
  use_scopes = CASE
    WHEN type = 'duty_treatment' THEN
      ARRAY['duty','landed_cost','audit']::text[]
    WHEN type = 'footnote' THEN
      ARRAY['declaration','audit']::text[]
    WHEN type IN ('hsen_section_general','hsen_general') THEN
      ARRAY['classification','audit']::text[]
    WHEN type = 'hsen_heading' THEN
      ARRAY['retrieval','classification','audit']::text[]
    WHEN id LIKE 'gir_%' OR type = 'classification_order' THEN
      ARRAY['classification','audit']::text[]
    WHEN type = 'exclusion' THEN
      ARRAY['retrieval','classification','audit']::text[]
    WHEN type IN ('inclusion','definition','discriminator') THEN
      ARRAY['retrieval','classification','qa','audit']::text[]
    WHEN type = 'rationale' OR id LIKE 'atar_%' OR lower(source) LIKE '%atar%' THEN
      ARRAY['retrieval','classification','audit']::text[]
    WHEN authority_tier <= 3 THEN
      ARRAY['classification','audit']::text[]
    ELSE
      ARRAY['audit']::text[]
  END,
  evidence_roles = CASE
    WHEN type = 'duty_treatment' THEN
      ARRAY['duty_rate_measure','landed_cost_component']::text[]
    WHEN type = 'footnote' THEN
      ARRAY['footnote','document_requirement']::text[]
    WHEN type = 'hsen_section_general' THEN
      ARRAY['interpretive_guidance']::text[]
    WHEN type = 'hsen_general' THEN
      ARRAY['interpretive_guidance']::text[]
    WHEN type = 'hsen_heading' THEN
      ARRAY['heading_guidance','interpretive_guidance']::text[]
    WHEN id LIKE 'gir_%' OR type = 'classification_order' THEN
      ARRAY['classification_order']::text[]
    WHEN type = 'exclusion' THEN
      ARRAY['legal_exclusion']::text[]
    WHEN type = 'inclusion' THEN
      ARRAY['legal_inclusion']::text[]
    WHEN type = 'definition' THEN
      ARRAY['legal_definition']::text[]
    WHEN type = 'discriminator' THEN
      ARRAY['product_identity','classification_rationale']::text[]
    WHEN type = 'rationale' OR id LIKE 'atar_%' OR lower(source) LIKE '%atar%' THEN
      ARRAY['classification_rationale']::text[]
    WHEN authority_tier <= 3 THEN
      ARRAY['interpretive_guidance']::text[]
    ELSE
      ARRAY['unknown']::text[]
  END;

ALTER TABLE kg.commodity_facets
  DROP CONSTRAINT IF EXISTS commodity_facets_use_scopes_allowed,
  ADD CONSTRAINT commodity_facets_use_scopes_allowed
    CHECK (
      cardinality(use_scopes) > 0
      AND use_scopes <@ ARRAY['retrieval','classification','qa','valuation','duty','landed_cost','declaration','audit']::text[]
    ),
  DROP CONSTRAINT IF EXISTS commodity_facets_evidence_roles_allowed,
  ADD CONSTRAINT commodity_facets_evidence_roles_allowed
    CHECK (
      cardinality(evidence_roles) > 0
      AND evidence_roles <@ ARRAY[
        'alias','product_identity','material_composition','form_presentation','function_use',
        'packaging_quantity','composition_threshold','additional_code','origin_or_region',
        'legal_definition','legal_inclusion','legal_exclusion','classification_order',
        'classification_rationale','interpretive_guidance','heading_guidance',
        'measure_condition','document_requirement','duty_rate_measure','valuation_input',
        'valuation_method','landed_cost_component','landed_cost_result','declaration_data',
        'footnote','index_text','unknown'
      ]::text[]
    );

ALTER TABLE kg.kg_edges
  DROP CONSTRAINT IF EXISTS kg_edges_use_scopes_allowed,
  ADD CONSTRAINT kg_edges_use_scopes_allowed
    CHECK (
      cardinality(use_scopes) > 0
      AND use_scopes <@ ARRAY['retrieval','classification','qa','valuation','duty','landed_cost','declaration','audit']::text[]
    ),
  DROP CONSTRAINT IF EXISTS kg_edges_evidence_roles_allowed,
  ADD CONSTRAINT kg_edges_evidence_roles_allowed
    CHECK (
      cardinality(evidence_roles) > 0
      AND evidence_roles <@ ARRAY[
        'alias','product_identity','material_composition','form_presentation','function_use',
        'packaging_quantity','composition_threshold','additional_code','origin_or_region',
        'legal_definition','legal_inclusion','legal_exclusion','classification_order',
        'classification_rationale','interpretive_guidance','heading_guidance',
        'measure_condition','document_requirement','duty_rate_measure','valuation_input',
        'valuation_method','landed_cost_component','landed_cost_result','declaration_data',
        'footnote','index_text','unknown'
      ]::text[]
    );
