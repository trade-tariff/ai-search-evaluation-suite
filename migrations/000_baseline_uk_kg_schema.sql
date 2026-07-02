--
-- PostgreSQL database dump
--

\restrict aN8F3dBehoGlQAb3SB6Gwmc0G5MW7zHDdaAMT5YXiI66JzhqiB6OcCI5Z5sLBjy

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: kg; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA kg;


ALTER SCHEMA kg OWNER TO postgres;

--
-- Name: uk; Type: SCHEMA; Schema: -; Owner: postgres
--

CREATE SCHEMA uk;


ALTER SCHEMA uk OWNER TO postgres;

--
-- Name: SCHEMA uk; Type: COMMENT; Schema: -; Owner: postgres
--

COMMENT ON SCHEMA uk IS 'standard public schema';


--
-- Name: fn_audit_trigger(); Type: FUNCTION; Schema: kg; Owner: postgres
--

CREATE FUNCTION kg.fn_audit_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;


ALTER FUNCTION kg.fn_audit_trigger() OWNER TO postgres;

--
-- Name: fn_mark_edge_embedding_stale(); Type: FUNCTION; Schema: kg; Owner: postgres
--

CREATE FUNCTION kg.fn_mark_edge_embedding_stale() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.title IS DISTINCT FROM OLD.title
       OR NEW.body IS DISTINCT FROM OLD.body THEN
        NEW.embedding := NULL;
        NEW.embedding_stale := true;
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION kg.fn_mark_edge_embedding_stale() OWNER TO postgres;

--
-- Name: fn_mark_facet_embedding_stale(); Type: FUNCTION; Schema: kg; Owner: postgres
--

CREATE FUNCTION kg.fn_mark_facet_embedding_stale() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.facet_key IS DISTINCT FROM OLD.facet_key
       OR NEW.facet_value IS DISTINCT FROM OLD.facet_value
       OR NEW.evidence IS DISTINCT FROM OLD.evidence THEN
        NEW.embedding := NULL;
        NEW.embedding_stale := true;
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION kg.fn_mark_facet_embedding_stale() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: audit_log; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.audit_log (
    id bigint NOT NULL,
    table_name text NOT NULL,
    row_id text NOT NULL,
    action text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    actor text NOT NULL,
    reason text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT audit_log_action_check CHECK ((action = ANY (ARRAY['create'::text, 'update'::text, 'delete'::text, 'verify'::text, 'supersede'::text])))
);


ALTER TABLE kg.audit_log OWNER TO postgres;

--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.audit_log_id_seq OWNER TO postgres;

--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.audit_log_id_seq OWNED BY kg.audit_log.id;


--
-- Name: classify_runs; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.classify_runs (
    id bigint NOT NULL,
    started_at timestamp without time zone DEFAULT now() NOT NULL,
    run_label text NOT NULL,
    config_json jsonb NOT NULL,
    strategy text NOT NULL,
    prompt_mode text NOT NULL,
    augmentation text NOT NULL,
    model text NOT NULL,
    candidate_limit integer NOT NULL,
    gold_id bigint NOT NULL,
    source_id text NOT NULL,
    persona text NOT NULL,
    query text NOT NULL,
    expected_code text NOT NULL,
    rounds integer NOT NULL,
    final_mode text NOT NULL,
    final_top1 text,
    final_set text[],
    gold_in_final_set boolean,
    gold_in_top1 boolean,
    gold_in_top5 boolean,
    gold_rank integer,
    survivor_set_size integer,
    classify_calls integer,
    simulator_calls integer,
    simulator_failed boolean DEFAULT false,
    est_cost_usd numeric(10,5),
    latency_seconds numeric(8,2),
    trace_json jsonb
);


ALTER TABLE kg.classify_runs OWNER TO postgres;

--
-- Name: classify_runs_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.classify_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.classify_runs_id_seq OWNER TO postgres;

--
-- Name: classify_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.classify_runs_id_seq OWNED BY kg.classify_runs.id;


--
-- Name: commodity_facets; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.commodity_facets (
    id bigint NOT NULL,
    commodity_code text NOT NULL,
    facet_key text NOT NULL,
    facet_value text NOT NULL,
    source text NOT NULL,
    confidence numeric(3,2) DEFAULT 1.0,
    evidence text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    authority_tier integer DEFAULT 6,
    provenance jsonb,
    updated_at timestamp without time zone,
    superseded_by bigint,
    embedding public.vector(1536),
    embedding_stale boolean DEFAULT true NOT NULL,
    use_scopes text[] DEFAULT ARRAY['audit'::text] NOT NULL,
    evidence_roles text[] DEFAULT ARRAY['unknown'::text] NOT NULL,
    CONSTRAINT commodity_facets_authority_tier_check CHECK (((authority_tier >= 1) AND (authority_tier <= 8))),
    CONSTRAINT commodity_facets_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT commodity_facets_evidence_roles_allowed CHECK (((cardinality(evidence_roles) > 0) AND (evidence_roles <@ ARRAY['alias'::text, 'product_identity'::text, 'material_composition'::text, 'form_presentation'::text, 'function_use'::text, 'packaging_quantity'::text, 'composition_threshold'::text, 'additional_code'::text, 'origin_or_region'::text, 'legal_definition'::text, 'legal_inclusion'::text, 'legal_exclusion'::text, 'classification_order'::text, 'classification_rationale'::text, 'interpretive_guidance'::text, 'heading_guidance'::text, 'measure_condition'::text, 'document_requirement'::text, 'duty_rate_measure'::text, 'valuation_input'::text, 'valuation_method'::text, 'declaration_data'::text, 'footnote'::text, 'index_text'::text, 'unknown'::text]))),
    CONSTRAINT commodity_facets_use_scopes_allowed CHECK (((cardinality(use_scopes) > 0) AND (use_scopes <@ ARRAY['retrieval'::text, 'classification'::text, 'qa'::text, 'valuation'::text, 'duty'::text, 'declaration'::text, 'audit'::text])))
);


ALTER TABLE kg.commodity_facets OWNER TO postgres;

--
-- Name: commodity_facets_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.commodity_facets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.commodity_facets_id_seq OWNER TO postgres;

--
-- Name: commodity_facets_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.commodity_facets_id_seq OWNED BY kg.commodity_facets.id;


--
-- Name: commodity_fact_model_eval; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.commodity_fact_model_eval (
    id bigint NOT NULL,
    run_id text NOT NULL,
    prompt_version text NOT NULL,
    model text NOT NULL,
    commodity_code text NOT NULL,
    source_bundle_hash text NOT NULL,
    source_bundle jsonb NOT NULL,
    raw_response jsonb,
    facts jsonb NOT NULL,
    quality jsonb NOT NULL,
    prompt_tokens integer DEFAULT 0 NOT NULL,
    completion_tokens integer DEFAULT 0 NOT NULL,
    cost_usd numeric DEFAULT 0 NOT NULL,
    duration_seconds double precision DEFAULT 0 NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.commodity_fact_model_eval OWNER TO postgres;

--
-- Name: commodity_fact_model_eval_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.commodity_fact_model_eval_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.commodity_fact_model_eval_id_seq OWNER TO postgres;

--
-- Name: commodity_fact_model_eval_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.commodity_fact_model_eval_id_seq OWNED BY kg.commodity_fact_model_eval.id;


--
-- Name: commodity_fact_sheets; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.commodity_fact_sheets (
    commodity_code text NOT NULL,
    fact_sheet_version text NOT NULL,
    fact_sheet jsonb NOT NULL,
    label_count integer DEFAULT 0 NOT NULL,
    signal_count integer DEFAULT 0 NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    construction jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text DEFAULT 'hydrated_qna_fact_sheet'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.commodity_fact_sheets OWNER TO postgres;

--
-- Name: commodity_hydration_cache; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.commodity_hydration_cache (
    commodity_code text NOT NULL,
    cache_version text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.commodity_hydration_cache OWNER TO postgres;

--
-- Name: commodity_llm_facts; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.commodity_llm_facts (
    id bigint NOT NULL,
    commodity_code text NOT NULL,
    scope text NOT NULL,
    fact_key text NOT NULL,
    fact_value text NOT NULL,
    label text NOT NULL,
    source text NOT NULL,
    evidence text NOT NULL,
    confidence double precision DEFAULT 0 NOT NULL,
    qna_usefulness double precision DEFAULT 0 NOT NULL,
    question_hint text DEFAULT ''::text NOT NULL,
    model text NOT NULL,
    prompt_version text NOT NULL,
    run_id text NOT NULL,
    source_bundle_hash text,
    raw_fact jsonb NOT NULL,
    quality jsonb,
    status text DEFAULT 'active'::text NOT NULL,
    provenance text DEFAULT 'kg.commodity_fact_model_eval'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.commodity_llm_facts OWNER TO postgres;

--
-- Name: commodity_llm_facts_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.commodity_llm_facts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.commodity_llm_facts_id_seq OWNER TO postgres;

--
-- Name: commodity_llm_facts_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.commodity_llm_facts_id_seq OWNED BY kg.commodity_llm_facts.id;


--
-- Name: composite_search_text; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.composite_search_text (
    goods_nomenclature_item_id text NOT NULL,
    composite_text text NOT NULL,
    composite_embedding public.vector(1536),
    stale boolean DEFAULT true NOT NULL
);


ALTER TABLE kg.composite_search_text OWNER TO postgres;

--
-- Name: contrastive_residual_text; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.contrastive_residual_text (
    goods_nomenclature_item_id text NOT NULL,
    contrastive_text text NOT NULL,
    contrastive_embedding public.vector(1536),
    stale boolean DEFAULT true NOT NULL
);


ALTER TABLE kg.contrastive_residual_text OWNER TO postgres;

--
-- Name: e2e_eval_results; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.e2e_eval_results (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    gold_id integer NOT NULL,
    source_id text NOT NULL,
    persona text NOT NULL,
    query text NOT NULL,
    expected_code text NOT NULL,
    retrieval_run_label text NOT NULL,
    question_mode text NOT NULL,
    answerer text NOT NULL,
    initial_rank integer,
    initial_top_codes text[] DEFAULT ARRAY[]::text[] NOT NULL,
    post_qa_rank integer,
    post_qa_top_codes text[] DEFAULT ARRAY[]::text[] NOT NULL,
    gold_in_retrieval boolean DEFAULT false NOT NULL,
    gold_kept boolean DEFAULT false NOT NULL,
    gold_top1_after_qa boolean DEFAULT false NOT NULL,
    rounds integer DEFAULT 0 NOT NULL,
    active_count integer DEFAULT 0 NOT NULL,
    out_count integer DEFAULT 0 NOT NULL,
    cache_hit_count integer DEFAULT 0 NOT NULL,
    cache_write_count integer DEFAULT 0 NOT NULL,
    provider_calls_used integer DEFAULT 0 NOT NULL,
    latency_seconds numeric,
    question_trace jsonb DEFAULT '[]'::jsonb NOT NULL,
    final_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.e2e_eval_results OWNER TO postgres;

--
-- Name: e2e_eval_results_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.e2e_eval_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.e2e_eval_results_id_seq OWNER TO postgres;

--
-- Name: e2e_eval_results_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.e2e_eval_results_id_seq OWNED BY kg.e2e_eval_results.id;


--
-- Name: e2e_eval_runs; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.e2e_eval_runs (
    id bigint NOT NULL,
    run_label text NOT NULL,
    retrieval_run_label text NOT NULL,
    question_mode text NOT NULL,
    answerer text NOT NULL,
    question_model text,
    simulator_model text,
    pair_limit integer NOT NULL,
    persona_count integer NOT NULL,
    input_count integer DEFAULT 0 NOT NULL,
    retrieval_limit integer NOT NULL,
    hydrate_limit integer NOT NULL,
    max_rounds integer NOT NULL,
    allow_spend boolean DEFAULT false NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    n_inputs integer,
    initial_gold_in_retrieval integer,
    gold_kept integer,
    gold_top1_after_qa integer,
    avg_initial_rank numeric,
    avg_post_qa_rank numeric,
    avg_rounds numeric,
    avg_active_count numeric,
    provider_calls_used integer DEFAULT 0 NOT NULL,
    errors integer DEFAULT 0 NOT NULL
);


ALTER TABLE kg.e2e_eval_runs OWNER TO postgres;

--
-- Name: e2e_eval_runs_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.e2e_eval_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.e2e_eval_runs_id_seq OWNER TO postgres;

--
-- Name: e2e_eval_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.e2e_eval_runs_id_seq OWNED BY kg.e2e_eval_runs.id;


--
-- Name: kg_edges; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.kg_edges (
    id text NOT NULL,
    type text NOT NULL,
    scope text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    source text NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    authority_tier integer DEFAULT 3,
    provenance jsonb,
    updated_at timestamp without time zone,
    superseded_by text,
    embedding public.vector(1536),
    embedding_stale boolean DEFAULT true NOT NULL,
    use_scopes text[] DEFAULT ARRAY['audit'::text] NOT NULL,
    evidence_roles text[] DEFAULT ARRAY['unknown'::text] NOT NULL,
    CONSTRAINT kg_edges_authority_tier_check CHECK (((authority_tier >= 1) AND (authority_tier <= 8))),
    CONSTRAINT kg_edges_evidence_roles_allowed CHECK (((cardinality(evidence_roles) > 0) AND (evidence_roles <@ ARRAY['alias'::text, 'product_identity'::text, 'material_composition'::text, 'form_presentation'::text, 'function_use'::text, 'packaging_quantity'::text, 'composition_threshold'::text, 'additional_code'::text, 'origin_or_region'::text, 'legal_definition'::text, 'legal_inclusion'::text, 'legal_exclusion'::text, 'classification_order'::text, 'classification_rationale'::text, 'interpretive_guidance'::text, 'heading_guidance'::text, 'measure_condition'::text, 'document_requirement'::text, 'duty_rate_measure'::text, 'valuation_input'::text, 'valuation_method'::text, 'declaration_data'::text, 'footnote'::text, 'index_text'::text, 'unknown'::text]))),
    CONSTRAINT kg_edges_use_scopes_allowed CHECK (((cardinality(use_scopes) > 0) AND (use_scopes <@ ARRAY['retrieval'::text, 'classification'::text, 'qa'::text, 'valuation'::text, 'duty'::text, 'declaration'::text, 'audit'::text])))
);


ALTER TABLE kg.kg_edges OWNER TO postgres;

--
-- Name: edge_tier_summary; Type: VIEW; Schema: kg; Owner: postgres
--

CREATE VIEW kg.edge_tier_summary AS
 SELECT authority_tier,
    type,
    count(*) AS count
   FROM kg.kg_edges
  GROUP BY authority_tier, type
  ORDER BY authority_tier, type;


ALTER VIEW kg.edge_tier_summary OWNER TO postgres;

--
-- Name: eval_gold; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.eval_gold (
    id bigint NOT NULL,
    source_type text NOT NULL,
    source_id text,
    persona text,
    query text NOT NULL,
    expected_code text NOT NULL,
    expected_description text,
    notes text,
    generator text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    active boolean DEFAULT true NOT NULL
);


ALTER TABLE kg.eval_gold OWNER TO postgres;

--
-- Name: eval_gold_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.eval_gold_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.eval_gold_id_seq OWNER TO postgres;

--
-- Name: eval_gold_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.eval_gold_id_seq OWNED BY kg.eval_gold.id;


--
-- Name: eval_run_results; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.eval_run_results (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    gold_id bigint NOT NULL,
    expected_code text NOT NULL,
    top_codes text[] NOT NULL,
    top_sources jsonb,
    rank_of_expected integer,
    rank_subheading integer,
    rank_heading integer
);


ALTER TABLE kg.eval_run_results OWNER TO postgres;

--
-- Name: eval_run_results_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.eval_run_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.eval_run_results_id_seq OWNER TO postgres;

--
-- Name: eval_run_results_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.eval_run_results_id_seq OWNED BY kg.eval_run_results.id;


--
-- Name: eval_runs; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.eval_runs (
    id bigint NOT NULL,
    run_label text NOT NULL,
    config_json jsonb NOT NULL,
    started_at timestamp without time zone DEFAULT now() NOT NULL,
    finished_at timestamp without time zone,
    n_queries integer,
    recall_at_1 numeric(5,4),
    recall_at_5 numeric(5,4),
    recall_at_10 numeric(5,4),
    recall_at_5_subheading numeric(5,4),
    recall_at_5_heading numeric(5,4),
    mrr numeric(5,4),
    notes text,
    curve_json jsonb,
    retrieval_limit integer
);


ALTER TABLE kg.eval_runs OWNER TO postgres;

--
-- Name: eval_runs_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.eval_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.eval_runs_id_seq OWNER TO postgres;

--
-- Name: eval_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.eval_runs_id_seq OWNED BY kg.eval_runs.id;


--
-- Name: evidence_label_definitions; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.evidence_label_definitions (
    label_kind text NOT NULL,
    key text NOT NULL,
    label text NOT NULL,
    description text NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT evidence_label_definitions_label_kind_check CHECK ((label_kind = ANY (ARRAY['use_scope'::text, 'evidence_role'::text])))
);


ALTER TABLE kg.evidence_label_definitions OWNER TO postgres;

--
-- Name: exp6_qa_runs; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.exp6_qa_runs (
    id bigint NOT NULL,
    started_at timestamp without time zone DEFAULT now() NOT NULL,
    gold_id bigint NOT NULL,
    source_id text NOT NULL,
    persona text NOT NULL,
    query text NOT NULL,
    expected_code text NOT NULL,
    rounds integer NOT NULL,
    final_mode text NOT NULL,
    final_top1 text,
    final_top5 text[],
    gold_in_top1 boolean,
    gold_in_top5 boolean,
    classify_calls integer,
    simulator_calls integer,
    simulator_failed boolean DEFAULT false,
    latency_seconds numeric(8,2),
    trace_json jsonb
);


ALTER TABLE kg.exp6_qa_runs OWNER TO postgres;

--
-- Name: exp6_qa_runs_id_seq; Type: SEQUENCE; Schema: kg; Owner: postgres
--

CREATE SEQUENCE kg.exp6_qa_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE kg.exp6_qa_runs_id_seq OWNER TO postgres;

--
-- Name: exp6_qa_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: kg; Owner: postgres
--

ALTER SEQUENCE kg.exp6_qa_runs_id_seq OWNED BY kg.exp6_qa_runs.id;


--
-- Name: facet_definitions; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.facet_definitions (
    key text NOT NULL,
    label text NOT NULL,
    short_label text,
    value_set jsonb DEFAULT '[]'::jsonb NOT NULL,
    applies_to_chapters text[] DEFAULT ARRAY[]::text[],
    rank integer DEFAULT 99 NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.facet_definitions OWNER TO postgres;

--
-- Name: facet_tier_summary; Type: VIEW; Schema: kg; Owner: postgres
--

CREATE VIEW kg.facet_tier_summary AS
 SELECT authority_tier,
    source,
    count(*) AS count
   FROM kg.commodity_facets
  GROUP BY authority_tier, source
  ORDER BY authority_tier, source;


ALTER VIEW kg.facet_tier_summary OWNER TO postgres;

--
-- Name: kg_edge_commodities; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.kg_edge_commodities (
    edge_id text NOT NULL,
    commodity_code text NOT NULL
);


ALTER TABLE kg.kg_edge_commodities OWNER TO postgres;

--
-- Name: measure_retrieval_docs; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.measure_retrieval_docs (
    commodity_code text NOT NULL,
    search_text text NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.measure_retrieval_docs OWNER TO postgres;

--
-- Name: query_descriptiveness; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.query_descriptiveness (
    gold_id bigint NOT NULL,
    source_id text,
    persona text,
    n_words integer,
    llm_spec numeric(4,1)
);


ALTER TABLE kg.query_descriptiveness OWNER TO postgres;

--
-- Name: query_embedding_cache; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.query_embedding_cache (
    cache_key text NOT NULL,
    model text NOT NULL,
    query text NOT NULL,
    embedding_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.query_embedding_cache OWNER TO postgres;

--
-- Name: query_qpp; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.query_qpp (
    gold_id bigint NOT NULL,
    persona text,
    source_id text,
    query_len integer,
    avg_idf numeric,
    max_idf numeric,
    avg_ictf numeric,
    scs numeric,
    nn_density numeric,
    input_quality numeric
);


ALTER TABLE kg.query_qpp OWNER TO postgres;

--
-- Name: retrieval_matrix_rows; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.retrieval_matrix_rows (
    run_label text NOT NULL,
    run_id text,
    started_at timestamp with time zone,
    ott_baseline boolean DEFAULT false NOT NULL,
    code_macro_recall_at_100 double precision,
    persona_recall jsonb DEFAULT '{}'::jsonb NOT NULL,
    config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    metric_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text DEFAULT 'db'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE kg.retrieval_matrix_rows OWNER TO postgres;

--
-- Name: triage_cache; Type: TABLE; Schema: kg; Owner: postgres
--

CREATE TABLE kg.triage_cache (
    query_norm text NOT NULL,
    rewrite text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE kg.triage_cache OWNER TO postgres;

--
-- Name: additional_code_description_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.additional_code_description_periods_oplog (
    additional_code_description_period_sid integer,
    additional_code_sid integer,
    additional_code_type_id character varying(1),
    additional_code character varying(3),
    validity_start_date timestamp without time zone,
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.additional_code_description_periods_oplog OWNER TO postgres;

--
-- Name: additional_code_description_periods; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.additional_code_description_periods AS
 SELECT additional_code_description_period_sid,
    additional_code_sid,
    additional_code_type_id,
    additional_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.additional_code_description_periods_oplog additional_code_description_periods1
  WHERE ((oid IN ( SELECT max(additional_code_description_periods2.oid) AS max
           FROM uk.additional_code_description_periods_oplog additional_code_description_periods2
          WHERE ((additional_code_description_periods1.additional_code_description_period_sid = additional_code_description_periods2.additional_code_description_period_sid) AND (additional_code_description_periods1.additional_code_sid = additional_code_description_periods2.additional_code_sid) AND ((additional_code_description_periods1.additional_code_type_id)::text = (additional_code_description_periods2.additional_code_type_id)::text)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.additional_code_description_periods OWNER TO postgres;

--
-- Name: additional_code_description_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.additional_code_description_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.additional_code_description_periods_oid_seq OWNER TO postgres;

--
-- Name: additional_code_description_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.additional_code_description_periods_oid_seq OWNED BY uk.additional_code_description_periods_oplog.oid;


--
-- Name: additional_code_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.additional_code_descriptions_oplog (
    additional_code_description_period_sid integer,
    language_id character varying(5),
    additional_code_sid integer,
    additional_code_type_id character varying(1),
    additional_code character varying(3),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.additional_code_descriptions_oplog OWNER TO postgres;

--
-- Name: additional_code_descriptions; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.additional_code_descriptions AS
 SELECT additional_code_description_period_sid,
    language_id,
    additional_code_sid,
    additional_code_type_id,
    additional_code,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.additional_code_descriptions_oplog additional_code_descriptions1
  WHERE ((oid IN ( SELECT max(additional_code_descriptions2.oid) AS max
           FROM uk.additional_code_descriptions_oplog additional_code_descriptions2
          WHERE ((additional_code_descriptions1.additional_code_description_period_sid = additional_code_descriptions2.additional_code_description_period_sid) AND (additional_code_descriptions1.additional_code_sid = additional_code_descriptions2.additional_code_sid)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.additional_code_descriptions OWNER TO postgres;

--
-- Name: additional_code_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.additional_code_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.additional_code_descriptions_oid_seq OWNER TO postgres;

--
-- Name: additional_code_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.additional_code_descriptions_oid_seq OWNED BY uk.additional_code_descriptions_oplog.oid;


--
-- Name: additional_code_type_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.additional_code_type_descriptions_oplog (
    additional_code_type_id character varying(1),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.additional_code_type_descriptions_oplog OWNER TO postgres;

--
-- Name: additional_code_type_descriptions; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.additional_code_type_descriptions AS
 SELECT additional_code_type_id,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.additional_code_type_descriptions_oplog additional_code_type_descriptions1
  WHERE ((oid IN ( SELECT max(additional_code_type_descriptions2.oid) AS max
           FROM uk.additional_code_type_descriptions_oplog additional_code_type_descriptions2
          WHERE (((additional_code_type_descriptions1.additional_code_type_id)::text = (additional_code_type_descriptions2.additional_code_type_id)::text) AND ((additional_code_type_descriptions1.language_id)::text = (additional_code_type_descriptions2.language_id)::text)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.additional_code_type_descriptions OWNER TO postgres;

--
-- Name: additional_code_type_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.additional_code_type_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.additional_code_type_descriptions_oid_seq OWNER TO postgres;

--
-- Name: additional_code_type_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.additional_code_type_descriptions_oid_seq OWNED BY uk.additional_code_type_descriptions_oplog.oid;


--
-- Name: additional_code_type_measure_types_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.additional_code_type_measure_types_oplog (
    measure_type_id character varying(6),
    additional_code_type_id character varying(1),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.additional_code_type_measure_types_oplog OWNER TO postgres;

--
-- Name: additional_code_type_measure_types; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.additional_code_type_measure_types AS
 SELECT measure_type_id,
    additional_code_type_id,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.additional_code_type_measure_types_oplog additional_code_type_measure_types1
  WHERE ((oid IN ( SELECT max(additional_code_type_measure_types2.oid) AS max
           FROM uk.additional_code_type_measure_types_oplog additional_code_type_measure_types2
          WHERE (((additional_code_type_measure_types1.measure_type_id)::text = (additional_code_type_measure_types2.measure_type_id)::text) AND ((additional_code_type_measure_types1.additional_code_type_id)::text = (additional_code_type_measure_types2.additional_code_type_id)::text)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.additional_code_type_measure_types OWNER TO postgres;

--
-- Name: additional_code_type_measure_types_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.additional_code_type_measure_types_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.additional_code_type_measure_types_oid_seq OWNER TO postgres;

--
-- Name: additional_code_type_measure_types_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.additional_code_type_measure_types_oid_seq OWNED BY uk.additional_code_type_measure_types_oplog.oid;


--
-- Name: additional_code_types_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.additional_code_types_oplog (
    additional_code_type_id character varying(1),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    application_code character varying(255),
    meursing_table_plan_id character varying(2),
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.additional_code_types_oplog OWNER TO postgres;

--
-- Name: additional_code_types; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.additional_code_types AS
 SELECT additional_code_type_id,
    validity_start_date,
    validity_end_date,
    application_code,
    meursing_table_plan_id,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.additional_code_types_oplog additional_code_types1
  WHERE ((oid IN ( SELECT max(additional_code_types2.oid) AS max
           FROM uk.additional_code_types_oplog additional_code_types2
          WHERE ((additional_code_types1.additional_code_type_id)::text = (additional_code_types2.additional_code_type_id)::text))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.additional_code_types OWNER TO postgres;

--
-- Name: additional_code_types_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.additional_code_types_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.additional_code_types_oid_seq OWNER TO postgres;

--
-- Name: additional_code_types_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.additional_code_types_oid_seq OWNED BY uk.additional_code_types_oplog.oid;


--
-- Name: additional_codes_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.additional_codes_oplog (
    additional_code_sid integer,
    additional_code_type_id character varying(1),
    additional_code character varying(3),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.additional_codes_oplog OWNER TO postgres;

--
-- Name: additional_codes; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.additional_codes AS
 SELECT additional_code_sid,
    additional_code_type_id,
    additional_code,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.additional_codes_oplog additional_codes1
  WHERE ((oid IN ( SELECT max(additional_codes2.oid) AS max
           FROM uk.additional_codes_oplog additional_codes2
          WHERE (additional_codes1.additional_code_sid = additional_codes2.additional_code_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.additional_codes OWNER TO postgres;

--
-- Name: additional_codes_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.additional_codes_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.additional_codes_oid_seq OWNER TO postgres;

--
-- Name: additional_codes_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.additional_codes_oid_seq OWNED BY uk.additional_codes_oplog.oid;


--
-- Name: admin_configurations; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.admin_configurations (
    id integer NOT NULL,
    name text NOT NULL,
    value jsonb DEFAULT '""'::jsonb NOT NULL,
    config_type character varying(50) NOT NULL,
    area character varying(50) DEFAULT 'classification'::character varying NOT NULL,
    description text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


ALTER TABLE uk.admin_configurations OWNER TO postgres;

--
-- Name: admin_configurations_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.admin_configurations ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.admin_configurations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: appendix_5as; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.appendix_5as (
    certificate_type_code text NOT NULL,
    certificate_code text NOT NULL,
    cds_guidance text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE uk.appendix_5as OWNER TO postgres;

--
-- Name: applies; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.applies (
    id integer NOT NULL,
    enqueued_at timestamp without time zone,
    whodunnit text
);


ALTER TABLE uk.applies OWNER TO postgres;

--
-- Name: applies_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.applies ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.applies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: audits; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.audits (
    id integer NOT NULL,
    auditable_id integer NOT NULL,
    auditable_type text NOT NULL,
    action text NOT NULL,
    changes json NOT NULL,
    version integer NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.audits OWNER TO postgres;

--
-- Name: audits_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.audits_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.audits_id_seq OWNER TO postgres;

--
-- Name: audits_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.audits_id_seq OWNED BY uk.audits.id;


--
-- Name: quota_associations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_associations_oplog (
    main_quota_definition_sid integer,
    sub_quota_definition_sid integer,
    relation_type character varying(255),
    coefficient numeric(16,5),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_associations_oplog OWNER TO postgres;

--
-- Name: quota_associations; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_associations AS
 SELECT main_quota_definition_sid,
    sub_quota_definition_sid,
    relation_type,
    coefficient,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_associations_oplog quota_associations1
  WHERE ((oid IN ( SELECT max(quota_associations2.oid) AS max
           FROM uk.quota_associations_oplog quota_associations2
          WHERE ((quota_associations1.main_quota_definition_sid = quota_associations2.main_quota_definition_sid) AND (quota_associations1.sub_quota_definition_sid = quota_associations2.sub_quota_definition_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_associations OWNER TO postgres;

--
-- Name: quota_definitions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_definitions_oplog (
    quota_definition_sid integer,
    quota_order_number_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    quota_order_number_sid integer,
    volume numeric(15,3),
    initial_volume numeric(15,3),
    measurement_unit_code character varying(3),
    maximum_precision integer,
    critical_state character varying(255),
    critical_threshold integer,
    monetary_unit_code character varying(255),
    measurement_unit_qualifier_code character varying(1),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_definitions_oplog OWNER TO postgres;

--
-- Name: quota_definitions; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_definitions AS
 SELECT quota_definition_sid,
    quota_order_number_id,
    validity_start_date,
    validity_end_date,
    quota_order_number_sid,
    volume,
    initial_volume,
    measurement_unit_code,
    maximum_precision,
    critical_state,
    critical_threshold,
    monetary_unit_code,
    measurement_unit_qualifier_code,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_definitions_oplog quota_definitions1
  WHERE ((oid IN ( SELECT max(quota_definitions2.oid) AS max
           FROM uk.quota_definitions_oplog quota_definitions2
          WHERE (quota_definitions1.quota_definition_sid = quota_definitions2.quota_definition_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_definitions OWNER TO postgres;

--
-- Name: quota_order_number_origins_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_order_number_origins_oplog (
    quota_order_number_origin_sid integer,
    quota_order_number_sid integer,
    geographical_area_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    geographical_area_sid integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_order_number_origins_oplog OWNER TO postgres;

--
-- Name: quota_order_number_origins; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_order_number_origins AS
 SELECT quota_order_number_origin_sid,
    quota_order_number_sid,
    geographical_area_id,
    validity_start_date,
    validity_end_date,
    geographical_area_sid,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_order_number_origins_oplog quota_order_number_origins1
  WHERE ((oid IN ( SELECT max(quota_order_number_origins2.oid) AS max
           FROM uk.quota_order_number_origins_oplog quota_order_number_origins2
          WHERE (quota_order_number_origins1.quota_order_number_origin_sid = quota_order_number_origins2.quota_order_number_origin_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_order_number_origins OWNER TO postgres;

--
-- Name: quota_order_numbers_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_order_numbers_oplog (
    quota_order_number_sid integer,
    quota_order_number_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_order_numbers_oplog OWNER TO postgres;

--
-- Name: quota_order_numbers; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_order_numbers AS
 SELECT quota_order_number_sid,
    quota_order_number_id,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_order_numbers_oplog quota_order_numbers1
  WHERE ((oid IN ( SELECT max(quota_order_numbers2.oid) AS max
           FROM uk.quota_order_numbers_oplog quota_order_numbers2
          WHERE (quota_order_numbers1.quota_order_number_sid = quota_order_numbers2.quota_order_number_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_order_numbers OWNER TO postgres;

--
-- Name: bad_quota_associations; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.bad_quota_associations AS
 SELECT qd_main.quota_order_number_id AS main_quota_order_number_id,
    qd_main.validity_start_date,
    qd_main.validity_end_date,
    qono_main.geographical_area_id AS main_origin,
    qd_sub.quota_order_number_id AS sub_quota_order_number_id,
    qono_sub.geographical_area_id AS sub_origin,
        CASE
            WHEN (qa.main_quota_definition_sid = qa.sub_quota_definition_sid) THEN 'self'::text
            ELSE 'other'::text
        END AS linkage,
    qa.relation_type,
    qa.coefficient
   FROM ((((((uk.quota_associations qa
     JOIN uk.quota_definitions qd_main ON ((qa.main_quota_definition_sid = qd_main.quota_definition_sid)))
     JOIN uk.quota_definitions qd_sub ON ((qa.sub_quota_definition_sid = qd_sub.quota_definition_sid)))
     JOIN uk.quota_order_numbers qon_main ON ((qon_main.quota_order_number_sid = qd_main.quota_order_number_sid)))
     JOIN uk.quota_order_numbers qon_sub ON ((qon_sub.quota_order_number_sid = qd_sub.quota_order_number_sid)))
     JOIN uk.quota_order_number_origins qono_main ON ((qon_main.quota_order_number_sid = qono_main.quota_order_number_sid)))
     JOIN uk.quota_order_number_origins qono_sub ON ((qon_sub.quota_order_number_sid = qono_sub.quota_order_number_sid)))
  WHERE (qd_main.validity_start_date >= '2021-01-01 00:00:00'::timestamp without time zone)
  ORDER BY qd_main.quota_order_number_id, qd_sub.quota_order_number_id, qd_main.validity_start_date;


ALTER VIEW uk.bad_quota_associations OWNER TO postgres;

--
-- Name: base_regulations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.base_regulations_oplog (
    base_regulation_role integer,
    base_regulation_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    community_code integer,
    regulation_group_id character varying(255),
    replacement_indicator integer,
    stopped_flag boolean,
    information_text text,
    approved_flag boolean,
    published_date date,
    officialjournal_number character varying(255),
    officialjournal_page integer,
    effective_end_date timestamp without time zone,
    antidumping_regulation_role integer,
    related_antidumping_regulation_id character varying(255),
    complete_abrogation_regulation_role integer,
    complete_abrogation_regulation_id character varying(255),
    explicit_abrogation_regulation_role integer,
    explicit_abrogation_regulation_id character varying(255),
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.base_regulations_oplog OWNER TO postgres;

--
-- Name: base_regulations; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.base_regulations AS
 SELECT base_regulation_role,
    base_regulation_id,
    validity_start_date,
    validity_end_date,
    community_code,
    regulation_group_id,
    replacement_indicator,
    stopped_flag,
    information_text,
    approved_flag,
    published_date,
    officialjournal_number,
    officialjournal_page,
    effective_end_date,
    antidumping_regulation_role,
    related_antidumping_regulation_id,
    complete_abrogation_regulation_role,
    complete_abrogation_regulation_id,
    explicit_abrogation_regulation_role,
    explicit_abrogation_regulation_id,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.base_regulations_oplog base_regulations1
  WHERE ((oid IN ( SELECT max(base_regulations2.oid) AS max
           FROM uk.base_regulations_oplog base_regulations2
          WHERE (((base_regulations1.base_regulation_id)::text = (base_regulations2.base_regulation_id)::text) AND (base_regulations1.base_regulation_role = base_regulations2.base_regulation_role)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.base_regulations OWNER TO postgres;

--
-- Name: base_regulations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.base_regulations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.base_regulations_oid_seq OWNER TO postgres;

--
-- Name: base_regulations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.base_regulations_oid_seq OWNED BY uk.base_regulations_oplog.oid;


--
-- Name: cds_update_notifications; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.cds_update_notifications (
    id integer NOT NULL,
    filename text,
    enqueued_at timestamp without time zone,
    whodunnit text
);


ALTER TABLE uk.cds_update_notifications OWNER TO postgres;

--
-- Name: cds_update_notifications_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.cds_update_notifications ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.cds_update_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: certificate_description_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.certificate_description_periods_oplog (
    certificate_description_period_sid integer,
    certificate_type_code character varying(1),
    certificate_code character varying(3),
    validity_start_date timestamp without time zone,
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.certificate_description_periods_oplog OWNER TO postgres;

--
-- Name: certificate_description_periods; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.certificate_description_periods AS
 SELECT certificate_description_period_sid,
    certificate_type_code,
    certificate_code,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.certificate_description_periods_oplog certificate_description_periods1
  WHERE ((oid IN ( SELECT max(certificate_description_periods2.oid) AS max
           FROM uk.certificate_description_periods_oplog certificate_description_periods2
          WHERE (certificate_description_periods1.certificate_description_period_sid = certificate_description_periods2.certificate_description_period_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.certificate_description_periods OWNER TO postgres;

--
-- Name: certificate_description_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.certificate_description_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.certificate_description_periods_oid_seq OWNER TO postgres;

--
-- Name: certificate_description_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.certificate_description_periods_oid_seq OWNED BY uk.certificate_description_periods_oplog.oid;


--
-- Name: certificate_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.certificate_descriptions_oplog (
    certificate_description_period_sid integer,
    language_id character varying(5),
    certificate_type_code character varying(1),
    certificate_code character varying(3),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.certificate_descriptions_oplog OWNER TO postgres;

--
-- Name: certificate_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.certificate_descriptions AS
 SELECT certificate_description_period_sid,
    language_id,
    certificate_type_code,
    certificate_code,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.certificate_descriptions_oplog certificate_descriptions1
  WHERE ((oid IN ( SELECT max(certificate_descriptions2.oid) AS max
           FROM uk.certificate_descriptions_oplog certificate_descriptions2
          WHERE (certificate_descriptions1.certificate_description_period_sid = certificate_descriptions2.certificate_description_period_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.certificate_descriptions OWNER TO postgres;

--
-- Name: certificate_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.certificate_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.certificate_descriptions_oid_seq OWNER TO postgres;

--
-- Name: certificate_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.certificate_descriptions_oid_seq OWNED BY uk.certificate_descriptions_oplog.oid;


--
-- Name: certificate_type_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.certificate_type_descriptions_oplog (
    certificate_type_code character varying(1),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.certificate_type_descriptions_oplog OWNER TO postgres;

--
-- Name: certificate_type_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.certificate_type_descriptions AS
 SELECT certificate_type_code,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.certificate_type_descriptions_oplog certificate_type_descriptions1
  WHERE ((oid IN ( SELECT max(certificate_type_descriptions2.oid) AS max
           FROM uk.certificate_type_descriptions_oplog certificate_type_descriptions2
          WHERE ((certificate_type_descriptions1.certificate_type_code)::text = (certificate_type_descriptions2.certificate_type_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.certificate_type_descriptions OWNER TO postgres;

--
-- Name: certificate_type_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.certificate_type_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.certificate_type_descriptions_oid_seq OWNER TO postgres;

--
-- Name: certificate_type_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.certificate_type_descriptions_oid_seq OWNED BY uk.certificate_type_descriptions_oplog.oid;


--
-- Name: certificate_types_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.certificate_types_oplog (
    certificate_type_code character varying(1),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.certificate_types_oplog OWNER TO postgres;

--
-- Name: certificate_types; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.certificate_types AS
 SELECT certificate_type_code,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.certificate_types_oplog certificate_types1
  WHERE ((oid IN ( SELECT max(certificate_types2.oid) AS max
           FROM uk.certificate_types_oplog certificate_types2
          WHERE ((certificate_types1.certificate_type_code)::text = (certificate_types2.certificate_type_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.certificate_types OWNER TO postgres;

--
-- Name: certificate_types_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.certificate_types_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.certificate_types_oid_seq OWNER TO postgres;

--
-- Name: certificate_types_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.certificate_types_oid_seq OWNED BY uk.certificate_types_oplog.oid;


--
-- Name: certificates_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.certificates_oplog (
    certificate_type_code character varying(1),
    certificate_code character varying(3),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    national_abbrev text,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.certificates_oplog OWNER TO postgres;

--
-- Name: certificates; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.certificates AS
 SELECT certificate_type_code,
    certificate_code,
    validity_start_date,
    validity_end_date,
    "national",
    national_abbrev,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.certificates_oplog certificates1
  WHERE ((oid IN ( SELECT max(certificates2.oid) AS max
           FROM uk.certificates_oplog certificates2
          WHERE (((certificates1.certificate_code)::text = (certificates2.certificate_code)::text) AND ((certificates1.certificate_type_code)::text = (certificates2.certificate_type_code)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.certificates OWNER TO postgres;

--
-- Name: certificates_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.certificates_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.certificates_oid_seq OWNER TO postgres;

--
-- Name: certificates_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.certificates_oid_seq OWNED BY uk.certificates_oplog.oid;


--
-- Name: changes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.changes (
    id integer NOT NULL,
    goods_nomenclature_item_id text,
    goods_nomenclature_sid integer,
    productline_suffix character varying(255),
    end_line boolean,
    change_type character varying(255),
    change_date date
);


ALTER TABLE uk.changes OWNER TO postgres;

--
-- Name: changes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.changes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.changes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: chapter_notes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.chapter_notes (
    id integer NOT NULL,
    section_id integer,
    chapter_id character varying(2),
    content text
);


ALTER TABLE uk.chapter_notes OWNER TO postgres;

--
-- Name: chapter_notes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.chapter_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.chapter_notes_id_seq OWNER TO postgres;

--
-- Name: chapter_notes_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.chapter_notes_id_seq OWNED BY uk.chapter_notes.id;


--
-- Name: chapters_guides; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.chapters_guides (
    id integer NOT NULL,
    goods_nomenclature_sid integer,
    guide_id integer
);


ALTER TABLE uk.chapters_guides OWNER TO postgres;

--
-- Name: chapters_guides_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.chapters_guides_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.chapters_guides_id_seq OWNER TO postgres;

--
-- Name: chapters_guides_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.chapters_guides_id_seq OWNED BY uk.chapters_guides.id;


--
-- Name: chapters_sections; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.chapters_sections (
    goods_nomenclature_sid integer,
    section_id integer
);


ALTER TABLE uk.chapters_sections OWNER TO postgres;

--
-- Name: chemical_names; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.chemical_names (
    id integer NOT NULL,
    chemical_id integer,
    name text
);


ALTER TABLE uk.chemical_names OWNER TO postgres;

--
-- Name: chemical_names_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.chemical_names ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.chemical_names_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: chemicals; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.chemicals (
    id integer NOT NULL,
    cas text
);


ALTER TABLE uk.chemicals OWNER TO postgres;

--
-- Name: chemicals_goods_nomenclatures; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.chemicals_goods_nomenclatures (
    chemical_id integer NOT NULL,
    goods_nomenclature_sid integer NOT NULL
);


ALTER TABLE uk.chemicals_goods_nomenclatures OWNER TO postgres;

--
-- Name: chemicals_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.chemicals ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.chemicals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: clear_caches; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.clear_caches (
    id integer NOT NULL,
    user_id integer,
    enqueued_at timestamp without time zone
);


ALTER TABLE uk.clear_caches OWNER TO postgres;

--
-- Name: clear_caches_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.clear_caches ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.clear_caches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: complete_abrogation_regulations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.complete_abrogation_regulations_oplog (
    complete_abrogation_regulation_role integer,
    complete_abrogation_regulation_id character varying(255),
    published_date date,
    officialjournal_number character varying(255),
    officialjournal_page integer,
    replacement_indicator integer,
    information_text text,
    approved_flag boolean,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.complete_abrogation_regulations_oplog OWNER TO postgres;

--
-- Name: complete_abrogation_regulations; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.complete_abrogation_regulations AS
 SELECT complete_abrogation_regulation_role,
    complete_abrogation_regulation_id,
    published_date,
    officialjournal_number,
    officialjournal_page,
    replacement_indicator,
    information_text,
    approved_flag,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.complete_abrogation_regulations_oplog complete_abrogation_regulations1
  WHERE ((oid IN ( SELECT max(complete_abrogation_regulations2.oid) AS max
           FROM uk.complete_abrogation_regulations_oplog complete_abrogation_regulations2
          WHERE (((complete_abrogation_regulations1.complete_abrogation_regulation_id)::text = (complete_abrogation_regulations2.complete_abrogation_regulation_id)::text) AND (complete_abrogation_regulations1.complete_abrogation_regulation_role = complete_abrogation_regulations2.complete_abrogation_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.complete_abrogation_regulations OWNER TO postgres;

--
-- Name: complete_abrogation_regulations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.complete_abrogation_regulations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.complete_abrogation_regulations_oid_seq OWNER TO postgres;

--
-- Name: complete_abrogation_regulations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.complete_abrogation_regulations_oid_seq OWNED BY uk.complete_abrogation_regulations_oplog.oid;


--
-- Name: customs_tariff_chapter_notes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.customs_tariff_chapter_notes (
    id integer NOT NULL,
    customs_tariff_update_version text NOT NULL,
    chapter_id character varying(2) NOT NULL,
    content text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    validity_start_date date,
    validity_end_date date
);


ALTER TABLE uk.customs_tariff_chapter_notes OWNER TO postgres;

--
-- Name: customs_tariff_chapter_notes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.customs_tariff_chapter_notes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.customs_tariff_chapter_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: customs_tariff_general_rules; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.customs_tariff_general_rules (
    id integer NOT NULL,
    customs_tariff_update_version text NOT NULL,
    rule_label character varying(10) NOT NULL,
    content text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    validity_start_date date,
    validity_end_date date
);


ALTER TABLE uk.customs_tariff_general_rules OWNER TO postgres;

--
-- Name: customs_tariff_general_rules_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.customs_tariff_general_rules ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.customs_tariff_general_rules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: customs_tariff_section_notes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.customs_tariff_section_notes (
    id integer NOT NULL,
    customs_tariff_update_version text NOT NULL,
    section_id integer NOT NULL,
    content text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    validity_start_date date,
    validity_end_date date
);


ALTER TABLE uk.customs_tariff_section_notes OWNER TO postgres;

--
-- Name: customs_tariff_section_notes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.customs_tariff_section_notes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.customs_tariff_section_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: customs_tariff_updates; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.customs_tariff_updates (
    version text NOT NULL,
    validity_start_date date NOT NULL,
    validity_end_date date,
    status text DEFAULT 'pending'::text NOT NULL,
    source_url text,
    s3_path text,
    file_checksum text,
    document_created_on date,
    import_error text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.customs_tariff_updates OWNER TO postgres;

--
-- Name: data_migrations; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.data_migrations (
    filename text NOT NULL
);


ALTER TABLE uk.data_migrations OWNER TO postgres;

--
-- Name: description_intercepts; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.description_intercepts (
    id integer NOT NULL,
    term text NOT NULL,
    sources text[] DEFAULT '{}'::text[] NOT NULL,
    message text,
    excluded boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    guidance_level text,
    guidance_location text,
    escalate_to_webchat boolean DEFAULT false NOT NULL,
    filter_prefixes text[],
    aliases text[] DEFAULT '{}'::text[] NOT NULL,
    message_header text,
    CONSTRAINT description_intercepts_term_not_alias CHECK ((NOT (term = ANY (COALESCE(aliases, ARRAY[]::text[])))))
);


ALTER TABLE uk.description_intercepts OWNER TO postgres;

--
-- Name: description_intercepts_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.description_intercepts ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.description_intercepts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: differences_logs; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.differences_logs (
    id integer NOT NULL,
    date date NOT NULL,
    key text NOT NULL,
    value text NOT NULL
);


ALTER TABLE uk.differences_logs OWNER TO postgres;

--
-- Name: differences_logs_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.differences_logs ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.differences_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: downloads; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.downloads (
    id integer NOT NULL,
    enqueued_at timestamp without time zone,
    whodunnit text
);


ALTER TABLE uk.downloads OWNER TO postgres;

--
-- Name: downloads_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.downloads ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.downloads_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: duty_expression_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.duty_expression_descriptions_oplog (
    duty_expression_id character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.duty_expression_descriptions_oplog OWNER TO postgres;

--
-- Name: duty_expression_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.duty_expression_descriptions AS
 SELECT duty_expression_id,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.duty_expression_descriptions_oplog duty_expression_descriptions1
  WHERE ((oid IN ( SELECT max(duty_expression_descriptions2.oid) AS max
           FROM uk.duty_expression_descriptions_oplog duty_expression_descriptions2
          WHERE ((duty_expression_descriptions1.duty_expression_id)::text = (duty_expression_descriptions2.duty_expression_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.duty_expression_descriptions OWNER TO postgres;

--
-- Name: duty_expression_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.duty_expression_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.duty_expression_descriptions_oid_seq OWNER TO postgres;

--
-- Name: duty_expression_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.duty_expression_descriptions_oid_seq OWNED BY uk.duty_expression_descriptions_oplog.oid;


--
-- Name: duty_expressions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.duty_expressions_oplog (
    duty_expression_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    duty_amount_applicability_code integer,
    measurement_unit_applicability_code integer,
    monetary_unit_applicability_code integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.duty_expressions_oplog OWNER TO postgres;

--
-- Name: duty_expressions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.duty_expressions AS
 SELECT duty_expression_id,
    validity_start_date,
    validity_end_date,
    duty_amount_applicability_code,
    measurement_unit_applicability_code,
    monetary_unit_applicability_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.duty_expressions_oplog duty_expressions1
  WHERE ((oid IN ( SELECT max(duty_expressions2.oid) AS max
           FROM uk.duty_expressions_oplog duty_expressions2
          WHERE ((duty_expressions1.duty_expression_id)::text = (duty_expressions2.duty_expression_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.duty_expressions OWNER TO postgres;

--
-- Name: duty_expressions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.duty_expressions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.duty_expressions_oid_seq OWNER TO postgres;

--
-- Name: duty_expressions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.duty_expressions_oid_seq OWNED BY uk.duty_expressions_oplog.oid;


--
-- Name: exchange_rate_countries; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.exchange_rate_countries (
    currency_code character varying(10),
    country character varying(200),
    country_code character varying(10) NOT NULL,
    active boolean
);


ALTER TABLE uk.exchange_rate_countries OWNER TO postgres;

--
-- Name: exchange_rate_countries_currencies; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.exchange_rate_countries_currencies (
    id integer NOT NULL,
    country_code text NOT NULL,
    country_description text NOT NULL,
    currency_code text NOT NULL,
    currency_description text NOT NULL,
    validity_start_date date NOT NULL,
    validity_end_date date,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.exchange_rate_countries_currencies OWNER TO postgres;

--
-- Name: exchange_rate_countries_currencies_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.exchange_rate_countries_currencies ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.exchange_rate_countries_currencies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: exchange_rate_currencies; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.exchange_rate_currencies (
    currency_code character varying(10) NOT NULL,
    currency_description character varying(200),
    spot_rate_required boolean DEFAULT false
);


ALTER TABLE uk.exchange_rate_currencies OWNER TO postgres;

--
-- Name: exchange_rate_currency_rates; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.exchange_rate_currency_rates (
    id integer NOT NULL,
    currency_code character varying(10) NOT NULL,
    validity_start_date date,
    validity_end_date date,
    rate double precision,
    rate_type character varying(10)
);


ALTER TABLE uk.exchange_rate_currency_rates OWNER TO postgres;

--
-- Name: exchange_rate_currency_rates_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.exchange_rate_currency_rates ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.exchange_rate_currency_rates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: exchange_rate_files; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.exchange_rate_files (
    id integer NOT NULL,
    period_year integer NOT NULL,
    period_month integer NOT NULL,
    format character varying(20),
    file_size integer,
    publication_date date,
    type text NOT NULL
);


ALTER TABLE uk.exchange_rate_files OWNER TO postgres;

--
-- Name: exchange_rate_files_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.exchange_rate_files ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.exchange_rate_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: explicit_abrogation_regulations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.explicit_abrogation_regulations_oplog (
    explicit_abrogation_regulation_role integer,
    explicit_abrogation_regulation_id character varying(8),
    published_date date,
    officialjournal_number character varying(255),
    officialjournal_page integer,
    replacement_indicator integer,
    abrogation_date date,
    information_text text,
    approved_flag boolean,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.explicit_abrogation_regulations_oplog OWNER TO postgres;

--
-- Name: explicit_abrogation_regulations; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.explicit_abrogation_regulations AS
 SELECT explicit_abrogation_regulation_role,
    explicit_abrogation_regulation_id,
    published_date,
    officialjournal_number,
    officialjournal_page,
    replacement_indicator,
    abrogation_date,
    information_text,
    approved_flag,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.explicit_abrogation_regulations_oplog explicit_abrogation_regulations1
  WHERE ((oid IN ( SELECT max(explicit_abrogation_regulations2.oid) AS max
           FROM uk.explicit_abrogation_regulations_oplog explicit_abrogation_regulations2
          WHERE (((explicit_abrogation_regulations1.explicit_abrogation_regulation_id)::text = (explicit_abrogation_regulations2.explicit_abrogation_regulation_id)::text) AND (explicit_abrogation_regulations1.explicit_abrogation_regulation_role = explicit_abrogation_regulations2.explicit_abrogation_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.explicit_abrogation_regulations OWNER TO postgres;

--
-- Name: explicit_abrogation_regulations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.explicit_abrogation_regulations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.explicit_abrogation_regulations_oid_seq OWNER TO postgres;

--
-- Name: explicit_abrogation_regulations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.explicit_abrogation_regulations_oid_seq OWNED BY uk.explicit_abrogation_regulations_oplog.oid;


--
-- Name: export_refund_nomenclature_description_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.export_refund_nomenclature_description_periods_oplog (
    export_refund_nomenclature_description_period_sid integer,
    export_refund_nomenclature_sid integer,
    validity_start_date timestamp without time zone,
    goods_nomenclature_item_id character varying(10),
    additional_code_type text,
    export_refund_code character varying(255),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.export_refund_nomenclature_description_periods_oplog OWNER TO postgres;

--
-- Name: export_refund_nomenclature_description_periods; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.export_refund_nomenclature_description_periods AS
 SELECT export_refund_nomenclature_description_period_sid,
    export_refund_nomenclature_sid,
    validity_start_date,
    goods_nomenclature_item_id,
    additional_code_type,
    export_refund_code,
    productline_suffix,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.export_refund_nomenclature_description_periods_oplog export_refund_nomenclature_description_periods1
  WHERE ((oid IN ( SELECT max(export_refund_nomenclature_description_periods2.oid) AS max
           FROM uk.export_refund_nomenclature_description_periods_oplog export_refund_nomenclature_description_periods2
          WHERE ((export_refund_nomenclature_description_periods1.export_refund_nomenclature_sid = export_refund_nomenclature_description_periods2.export_refund_nomenclature_sid) AND (export_refund_nomenclature_description_periods1.export_refund_nomenclature_description_period_sid = export_refund_nomenclature_description_periods2.export_refund_nomenclature_description_period_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.export_refund_nomenclature_description_periods OWNER TO postgres;

--
-- Name: export_refund_nomenclature_description_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.export_refund_nomenclature_description_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.export_refund_nomenclature_description_periods_oid_seq OWNER TO postgres;

--
-- Name: export_refund_nomenclature_description_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.export_refund_nomenclature_description_periods_oid_seq OWNED BY uk.export_refund_nomenclature_description_periods_oplog.oid;


--
-- Name: export_refund_nomenclature_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.export_refund_nomenclature_descriptions_oplog (
    export_refund_nomenclature_description_period_sid integer,
    language_id character varying(5),
    export_refund_nomenclature_sid integer,
    goods_nomenclature_item_id character varying(10),
    additional_code_type text,
    export_refund_code character varying(255),
    productline_suffix character varying(2),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.export_refund_nomenclature_descriptions_oplog OWNER TO postgres;

--
-- Name: export_refund_nomenclature_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.export_refund_nomenclature_descriptions AS
 SELECT export_refund_nomenclature_description_period_sid,
    language_id,
    export_refund_nomenclature_sid,
    goods_nomenclature_item_id,
    additional_code_type,
    export_refund_code,
    productline_suffix,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.export_refund_nomenclature_descriptions_oplog export_refund_nomenclature_descriptions1
  WHERE ((oid IN ( SELECT max(export_refund_nomenclature_descriptions2.oid) AS max
           FROM uk.export_refund_nomenclature_descriptions_oplog export_refund_nomenclature_descriptions2
          WHERE (export_refund_nomenclature_descriptions1.export_refund_nomenclature_description_period_sid = export_refund_nomenclature_descriptions2.export_refund_nomenclature_description_period_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.export_refund_nomenclature_descriptions OWNER TO postgres;

--
-- Name: export_refund_nomenclature_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.export_refund_nomenclature_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.export_refund_nomenclature_descriptions_oid_seq OWNER TO postgres;

--
-- Name: export_refund_nomenclature_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.export_refund_nomenclature_descriptions_oid_seq OWNED BY uk.export_refund_nomenclature_descriptions_oplog.oid;


--
-- Name: export_refund_nomenclature_indents_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.export_refund_nomenclature_indents_oplog (
    export_refund_nomenclature_indents_sid integer,
    export_refund_nomenclature_sid integer,
    validity_start_date timestamp without time zone,
    number_export_refund_nomenclature_indents integer,
    goods_nomenclature_item_id character varying(10),
    additional_code_type text,
    export_refund_code character varying(255),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.export_refund_nomenclature_indents_oplog OWNER TO postgres;

--
-- Name: export_refund_nomenclature_indents; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.export_refund_nomenclature_indents AS
 SELECT export_refund_nomenclature_indents_sid,
    export_refund_nomenclature_sid,
    validity_start_date,
    number_export_refund_nomenclature_indents,
    goods_nomenclature_item_id,
    additional_code_type,
    export_refund_code,
    productline_suffix,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.export_refund_nomenclature_indents_oplog export_refund_nomenclature_indents1
  WHERE ((oid IN ( SELECT max(export_refund_nomenclature_indents2.oid) AS max
           FROM uk.export_refund_nomenclature_indents_oplog export_refund_nomenclature_indents2
          WHERE (export_refund_nomenclature_indents1.export_refund_nomenclature_indents_sid = export_refund_nomenclature_indents2.export_refund_nomenclature_indents_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.export_refund_nomenclature_indents OWNER TO postgres;

--
-- Name: export_refund_nomenclature_indents_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.export_refund_nomenclature_indents_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.export_refund_nomenclature_indents_oid_seq OWNER TO postgres;

--
-- Name: export_refund_nomenclature_indents_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.export_refund_nomenclature_indents_oid_seq OWNED BY uk.export_refund_nomenclature_indents_oplog.oid;


--
-- Name: export_refund_nomenclatures_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.export_refund_nomenclatures_oplog (
    export_refund_nomenclature_sid integer,
    goods_nomenclature_item_id character varying(10),
    additional_code_type character varying(1),
    export_refund_code character varying(3),
    productline_suffix character varying(2),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    goods_nomenclature_sid integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.export_refund_nomenclatures_oplog OWNER TO postgres;

--
-- Name: export_refund_nomenclatures; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.export_refund_nomenclatures AS
 SELECT export_refund_nomenclature_sid,
    goods_nomenclature_item_id,
    additional_code_type,
    export_refund_code,
    productline_suffix,
    validity_start_date,
    validity_end_date,
    goods_nomenclature_sid,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.export_refund_nomenclatures_oplog export_refund_nomenclatures1
  WHERE ((oid IN ( SELECT max(export_refund_nomenclatures2.oid) AS max
           FROM uk.export_refund_nomenclatures_oplog export_refund_nomenclatures2
          WHERE (export_refund_nomenclatures1.export_refund_nomenclature_sid = export_refund_nomenclatures2.export_refund_nomenclature_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.export_refund_nomenclatures OWNER TO postgres;

--
-- Name: export_refund_nomenclatures_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.export_refund_nomenclatures_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.export_refund_nomenclatures_oid_seq OWNER TO postgres;

--
-- Name: export_refund_nomenclatures_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.export_refund_nomenclatures_oid_seq OWNED BY uk.export_refund_nomenclatures_oplog.oid;


--
-- Name: footnote_association_additional_codes_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_association_additional_codes_oplog (
    additional_code_sid integer,
    footnote_type_id character varying(3),
    footnote_id character varying(5),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    additional_code_type_id text,
    additional_code character varying(3),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_association_additional_codes_oplog OWNER TO postgres;

--
-- Name: footnote_association_additional_codes; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_association_additional_codes AS
 SELECT additional_code_sid,
    footnote_type_id,
    footnote_id,
    validity_start_date,
    validity_end_date,
    additional_code_type_id,
    additional_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_association_additional_codes_oplog footnote_association_additional_codes1
  WHERE ((oid IN ( SELECT max(footnote_association_additional_codes2.oid) AS max
           FROM uk.footnote_association_additional_codes_oplog footnote_association_additional_codes2
          WHERE (((footnote_association_additional_codes1.footnote_id)::text = (footnote_association_additional_codes2.footnote_id)::text) AND ((footnote_association_additional_codes1.footnote_type_id)::text = (footnote_association_additional_codes2.footnote_type_id)::text) AND (footnote_association_additional_codes1.additional_code_sid = footnote_association_additional_codes2.additional_code_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_association_additional_codes OWNER TO postgres;

--
-- Name: footnote_association_additional_codes_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_association_additional_codes_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_association_additional_codes_oid_seq OWNER TO postgres;

--
-- Name: footnote_association_additional_codes_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_association_additional_codes_oid_seq OWNED BY uk.footnote_association_additional_codes_oplog.oid;


--
-- Name: footnote_association_erns_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_association_erns_oplog (
    export_refund_nomenclature_sid integer,
    footnote_type character varying(3),
    footnote_id character varying(5),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    goods_nomenclature_item_id character varying(10),
    additional_code_type text,
    export_refund_code character varying(255),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_association_erns_oplog OWNER TO postgres;

--
-- Name: footnote_association_erns; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_association_erns AS
 SELECT export_refund_nomenclature_sid,
    footnote_type,
    footnote_id,
    validity_start_date,
    validity_end_date,
    goods_nomenclature_item_id,
    additional_code_type,
    export_refund_code,
    productline_suffix,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_association_erns_oplog footnote_association_erns1
  WHERE ((oid IN ( SELECT max(footnote_association_erns2.oid) AS max
           FROM uk.footnote_association_erns_oplog footnote_association_erns2
          WHERE ((footnote_association_erns1.export_refund_nomenclature_sid = footnote_association_erns2.export_refund_nomenclature_sid) AND ((footnote_association_erns1.footnote_id)::text = (footnote_association_erns2.footnote_id)::text) AND ((footnote_association_erns1.footnote_type)::text = (footnote_association_erns2.footnote_type)::text) AND (footnote_association_erns1.validity_start_date = footnote_association_erns2.validity_start_date)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_association_erns OWNER TO postgres;

--
-- Name: footnote_association_erns_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_association_erns_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_association_erns_oid_seq OWNER TO postgres;

--
-- Name: footnote_association_erns_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_association_erns_oid_seq OWNED BY uk.footnote_association_erns_oplog.oid;


--
-- Name: footnote_association_goods_nomenclatures_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_association_goods_nomenclatures_oplog (
    goods_nomenclature_sid integer,
    footnote_type character varying(3),
    footnote_id character varying(5),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_association_goods_nomenclatures_oplog OWNER TO postgres;

--
-- Name: footnote_association_goods_nomenclatures; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_association_goods_nomenclatures AS
 SELECT goods_nomenclature_sid,
    footnote_type,
    footnote_id,
    validity_start_date,
    validity_end_date,
    goods_nomenclature_item_id,
    productline_suffix,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_association_goods_nomenclatures_oplog footnote_association_goods_nomenclatures1
  WHERE ((oid IN ( SELECT max(footnote_association_goods_nomenclatures2.oid) AS max
           FROM uk.footnote_association_goods_nomenclatures_oplog footnote_association_goods_nomenclatures2
          WHERE (((footnote_association_goods_nomenclatures1.footnote_id)::text = (footnote_association_goods_nomenclatures2.footnote_id)::text) AND ((footnote_association_goods_nomenclatures1.footnote_type)::text = (footnote_association_goods_nomenclatures2.footnote_type)::text) AND (footnote_association_goods_nomenclatures1.goods_nomenclature_sid = footnote_association_goods_nomenclatures2.goods_nomenclature_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_association_goods_nomenclatures OWNER TO postgres;

--
-- Name: footnote_association_goods_nomenclatures_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_association_goods_nomenclatures_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_association_goods_nomenclatures_oid_seq OWNER TO postgres;

--
-- Name: footnote_association_goods_nomenclatures_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_association_goods_nomenclatures_oid_seq OWNED BY uk.footnote_association_goods_nomenclatures_oplog.oid;


--
-- Name: footnote_association_measures_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_association_measures_oplog (
    measure_sid integer,
    footnote_type_id character varying(3),
    footnote_id character varying(5),
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_association_measures_oplog OWNER TO postgres;

--
-- Name: footnote_association_measures; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_association_measures AS
 SELECT measure_sid,
    footnote_type_id,
    footnote_id,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_association_measures_oplog footnote_association_measures1
  WHERE ((oid IN ( SELECT max(footnote_association_measures2.oid) AS max
           FROM uk.footnote_association_measures_oplog footnote_association_measures2
          WHERE ((footnote_association_measures1.measure_sid = footnote_association_measures2.measure_sid) AND ((footnote_association_measures1.footnote_id)::text = (footnote_association_measures2.footnote_id)::text) AND ((footnote_association_measures1.footnote_type_id)::text = (footnote_association_measures2.footnote_type_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_association_measures OWNER TO postgres;

--
-- Name: footnote_association_measures_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_association_measures_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_association_measures_oid_seq OWNER TO postgres;

--
-- Name: footnote_association_measures_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_association_measures_oid_seq OWNED BY uk.footnote_association_measures_oplog.oid;


--
-- Name: footnote_association_meursing_headings_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_association_meursing_headings_oplog (
    meursing_table_plan_id character varying(2),
    meursing_heading_number character varying(255),
    row_column_code integer,
    footnote_type character varying(3),
    footnote_id character varying(5),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_association_meursing_headings_oplog OWNER TO postgres;

--
-- Name: footnote_association_meursing_headings; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_association_meursing_headings AS
 SELECT meursing_table_plan_id,
    meursing_heading_number,
    row_column_code,
    footnote_type,
    footnote_id,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_association_meursing_headings_oplog footnote_association_meursing_headings1
  WHERE ((oid IN ( SELECT max(footnote_association_meursing_headings2.oid) AS max
           FROM uk.footnote_association_meursing_headings_oplog footnote_association_meursing_headings2
          WHERE (((footnote_association_meursing_headings1.footnote_id)::text = (footnote_association_meursing_headings2.footnote_id)::text) AND ((footnote_association_meursing_headings1.meursing_table_plan_id)::text = (footnote_association_meursing_headings2.meursing_table_plan_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_association_meursing_headings OWNER TO postgres;

--
-- Name: footnote_association_meursing_headings_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_association_meursing_headings_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_association_meursing_headings_oid_seq OWNER TO postgres;

--
-- Name: footnote_association_meursing_headings_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_association_meursing_headings_oid_seq OWNED BY uk.footnote_association_meursing_headings_oplog.oid;


--
-- Name: footnote_description_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_description_periods_oplog (
    footnote_description_period_sid integer,
    footnote_type_id character varying(3),
    footnote_id character varying(5),
    validity_start_date timestamp without time zone,
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_description_periods_oplog OWNER TO postgres;

--
-- Name: footnote_description_periods; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.footnote_description_periods AS
 SELECT footnote_description_period_sid,
    footnote_type_id,
    footnote_id,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_description_periods_oplog footnote_description_periods1
  WHERE ((oid IN ( SELECT max(footnote_description_periods2.oid) AS max
           FROM uk.footnote_description_periods_oplog footnote_description_periods2
          WHERE (((footnote_description_periods1.footnote_id)::text = (footnote_description_periods2.footnote_id)::text) AND ((footnote_description_periods1.footnote_type_id)::text = (footnote_description_periods2.footnote_type_id)::text) AND (footnote_description_periods1.footnote_description_period_sid = footnote_description_periods2.footnote_description_period_sid)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.footnote_description_periods OWNER TO postgres;

--
-- Name: footnote_description_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_description_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_description_periods_oid_seq OWNER TO postgres;

--
-- Name: footnote_description_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_description_periods_oid_seq OWNED BY uk.footnote_description_periods_oplog.oid;


--
-- Name: footnote_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_descriptions_oplog (
    footnote_description_period_sid integer,
    footnote_type_id character varying(3),
    footnote_id character varying(5),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_descriptions_oplog OWNER TO postgres;

--
-- Name: footnote_descriptions; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.footnote_descriptions AS
 SELECT footnote_description_period_sid,
    footnote_type_id,
    footnote_id,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_descriptions_oplog footnote_descriptions1
  WHERE ((oid IN ( SELECT max(footnote_descriptions2.oid) AS max
           FROM uk.footnote_descriptions_oplog footnote_descriptions2
          WHERE ((footnote_descriptions1.footnote_description_period_sid = footnote_descriptions2.footnote_description_period_sid) AND ((footnote_descriptions1.footnote_id)::text = (footnote_descriptions2.footnote_id)::text) AND ((footnote_descriptions1.footnote_type_id)::text = (footnote_descriptions2.footnote_type_id)::text)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.footnote_descriptions OWNER TO postgres;

--
-- Name: footnote_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_descriptions_oid_seq OWNER TO postgres;

--
-- Name: footnote_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_descriptions_oid_seq OWNED BY uk.footnote_descriptions_oplog.oid;


--
-- Name: footnote_type_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_type_descriptions_oplog (
    footnote_type_id character varying(3),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_type_descriptions_oplog OWNER TO postgres;

--
-- Name: footnote_type_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_type_descriptions AS
 SELECT footnote_type_id,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_type_descriptions_oplog footnote_type_descriptions1
  WHERE ((oid IN ( SELECT max(footnote_type_descriptions2.oid) AS max
           FROM uk.footnote_type_descriptions_oplog footnote_type_descriptions2
          WHERE ((footnote_type_descriptions1.footnote_type_id)::text = (footnote_type_descriptions2.footnote_type_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_type_descriptions OWNER TO postgres;

--
-- Name: footnote_type_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_type_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_type_descriptions_oid_seq OWNER TO postgres;

--
-- Name: footnote_type_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_type_descriptions_oid_seq OWNED BY uk.footnote_type_descriptions_oplog.oid;


--
-- Name: footnote_types_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnote_types_oplog (
    footnote_type_id character varying(3),
    application_code integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnote_types_oplog OWNER TO postgres;

--
-- Name: footnote_types; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.footnote_types AS
 SELECT footnote_type_id,
    application_code,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnote_types_oplog footnote_types1
  WHERE ((oid IN ( SELECT max(footnote_types2.oid) AS max
           FROM uk.footnote_types_oplog footnote_types2
          WHERE ((footnote_types1.footnote_type_id)::text = (footnote_types2.footnote_type_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.footnote_types OWNER TO postgres;

--
-- Name: footnote_types_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnote_types_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnote_types_oid_seq OWNER TO postgres;

--
-- Name: footnote_types_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnote_types_oid_seq OWNED BY uk.footnote_types_oplog.oid;


--
-- Name: footnotes_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.footnotes_oplog (
    footnote_id character varying(5),
    footnote_type_id character varying(3),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.footnotes_oplog OWNER TO postgres;

--
-- Name: footnotes; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.footnotes AS
 SELECT footnote_id,
    footnote_type_id,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.footnotes_oplog footnotes1
  WHERE ((oid IN ( SELECT max(footnotes2.oid) AS max
           FROM uk.footnotes_oplog footnotes2
          WHERE (((footnotes1.footnote_type_id)::text = (footnotes2.footnote_type_id)::text) AND ((footnotes1.footnote_id)::text = (footnotes2.footnote_id)::text)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.footnotes OWNER TO postgres;

--
-- Name: footnotes_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.footnotes_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.footnotes_oid_seq OWNER TO postgres;

--
-- Name: footnotes_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.footnotes_oid_seq OWNED BY uk.footnotes_oplog.oid;


--
-- Name: forum_links; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.forum_links (
    id integer NOT NULL,
    url text,
    goods_nomenclature_sid integer,
    created_at timestamp without time zone
);


ALTER TABLE uk.forum_links OWNER TO postgres;

--
-- Name: forum_links_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.forum_links ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.forum_links_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: fts_regulation_actions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.fts_regulation_actions_oplog (
    fts_regulation_role integer,
    fts_regulation_id character varying(8),
    stopped_regulation_role integer,
    stopped_regulation_id character varying(8),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.fts_regulation_actions_oplog OWNER TO postgres;

--
-- Name: fts_regulation_actions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.fts_regulation_actions AS
 SELECT fts_regulation_role,
    fts_regulation_id,
    stopped_regulation_role,
    stopped_regulation_id,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.fts_regulation_actions_oplog fts_regulation_actions1
  WHERE ((oid IN ( SELECT max(fts_regulation_actions2.oid) AS max
           FROM uk.fts_regulation_actions_oplog fts_regulation_actions2
          WHERE (((fts_regulation_actions1.fts_regulation_id)::text = (fts_regulation_actions2.fts_regulation_id)::text) AND (fts_regulation_actions1.fts_regulation_role = fts_regulation_actions2.fts_regulation_role) AND ((fts_regulation_actions1.stopped_regulation_id)::text = (fts_regulation_actions2.stopped_regulation_id)::text) AND (fts_regulation_actions1.stopped_regulation_role = fts_regulation_actions2.stopped_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.fts_regulation_actions OWNER TO postgres;

--
-- Name: fts_regulation_actions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.fts_regulation_actions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.fts_regulation_actions_oid_seq OWNER TO postgres;

--
-- Name: fts_regulation_actions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.fts_regulation_actions_oid_seq OWNED BY uk.fts_regulation_actions_oplog.oid;


--
-- Name: full_chemicals; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.full_chemicals (
    cus text NOT NULL,
    goods_nomenclature_sid integer NOT NULL,
    cn_code text,
    cas_rn text,
    ec_number text,
    un_number text,
    nomen text,
    name text,
    goods_nomenclature_item_id text,
    producline_suffix text,
    updated_at timestamp without time zone,
    created_at timestamp without time zone
);


ALTER TABLE uk.full_chemicals OWNER TO postgres;

--
-- Name: full_temporary_stop_regulations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.full_temporary_stop_regulations_oplog (
    full_temporary_stop_regulation_role integer,
    full_temporary_stop_regulation_id character varying(8),
    published_date date,
    officialjournal_number character varying(255),
    officialjournal_page integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    effective_enddate date,
    explicit_abrogation_regulation_role integer,
    explicit_abrogation_regulation_id character varying(8),
    replacement_indicator integer,
    information_text text,
    approved_flag boolean,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    complete_abrogation_regulation_role integer,
    complete_abrogation_regulation_id character varying(8),
    filename text
);


ALTER TABLE uk.full_temporary_stop_regulations_oplog OWNER TO postgres;

--
-- Name: full_temporary_stop_regulations; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.full_temporary_stop_regulations AS
 SELECT full_temporary_stop_regulation_role,
    full_temporary_stop_regulation_id,
    published_date,
    officialjournal_number,
    officialjournal_page,
    validity_start_date,
    validity_end_date,
    effective_enddate,
    explicit_abrogation_regulation_role,
    explicit_abrogation_regulation_id,
    replacement_indicator,
    information_text,
    approved_flag,
    oid,
    operation,
    operation_date,
    complete_abrogation_regulation_role,
    complete_abrogation_regulation_id,
    filename
   FROM uk.full_temporary_stop_regulations_oplog full_temporary_stop_regulations1
  WHERE ((oid IN ( SELECT max(full_temporary_stop_regulations2.oid) AS max
           FROM uk.full_temporary_stop_regulations_oplog full_temporary_stop_regulations2
          WHERE (((full_temporary_stop_regulations1.full_temporary_stop_regulation_id)::text = (full_temporary_stop_regulations2.full_temporary_stop_regulation_id)::text) AND (full_temporary_stop_regulations1.full_temporary_stop_regulation_role = full_temporary_stop_regulations2.full_temporary_stop_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.full_temporary_stop_regulations OWNER TO postgres;

--
-- Name: full_temporary_stop_regulations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.full_temporary_stop_regulations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.full_temporary_stop_regulations_oid_seq OWNER TO postgres;

--
-- Name: full_temporary_stop_regulations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.full_temporary_stop_regulations_oid_seq OWNED BY uk.full_temporary_stop_regulations_oplog.oid;


--
-- Name: geographical_area_description_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.geographical_area_description_periods_oplog (
    geographical_area_description_period_sid integer,
    geographical_area_sid integer,
    validity_start_date timestamp without time zone,
    geographical_area_id character varying(255),
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.geographical_area_description_periods_oplog OWNER TO postgres;

--
-- Name: geographical_area_description_periods; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.geographical_area_description_periods AS
 SELECT geographical_area_description_period_sid,
    geographical_area_sid,
    validity_start_date,
    geographical_area_id,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.geographical_area_description_periods_oplog geographical_area_description_periods1
  WHERE ((oid IN ( SELECT max(geographical_area_description_periods2.oid) AS max
           FROM uk.geographical_area_description_periods_oplog geographical_area_description_periods2
          WHERE ((geographical_area_description_periods1.geographical_area_description_period_sid = geographical_area_description_periods2.geographical_area_description_period_sid) AND (geographical_area_description_periods1.geographical_area_sid = geographical_area_description_periods2.geographical_area_sid)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.geographical_area_description_periods OWNER TO postgres;

--
-- Name: geographical_area_description_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.geographical_area_description_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.geographical_area_description_periods_oid_seq OWNER TO postgres;

--
-- Name: geographical_area_description_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.geographical_area_description_periods_oid_seq OWNED BY uk.geographical_area_description_periods_oplog.oid;


--
-- Name: geographical_area_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.geographical_area_descriptions_oplog (
    geographical_area_description_period_sid integer,
    language_id character varying(5),
    geographical_area_sid integer,
    geographical_area_id character varying(255),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.geographical_area_descriptions_oplog OWNER TO postgres;

--
-- Name: geographical_area_descriptions; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.geographical_area_descriptions AS
 SELECT geographical_area_description_period_sid,
    language_id,
    geographical_area_sid,
    geographical_area_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.geographical_area_descriptions_oplog geographical_area_descriptions1
  WHERE ((oid IN ( SELECT max(geographical_area_descriptions2.oid) AS max
           FROM uk.geographical_area_descriptions_oplog geographical_area_descriptions2
          WHERE ((geographical_area_descriptions1.geographical_area_description_period_sid = geographical_area_descriptions2.geographical_area_description_period_sid) AND (geographical_area_descriptions1.geographical_area_sid = geographical_area_descriptions2.geographical_area_sid)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.geographical_area_descriptions OWNER TO postgres;

--
-- Name: geographical_area_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.geographical_area_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.geographical_area_descriptions_oid_seq OWNER TO postgres;

--
-- Name: geographical_area_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.geographical_area_descriptions_oid_seq OWNED BY uk.geographical_area_descriptions_oplog.oid;


--
-- Name: geographical_area_memberships_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.geographical_area_memberships_oplog (
    geographical_area_sid integer,
    geographical_area_group_sid integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text,
    hjid integer,
    geographical_area_hjid integer,
    geographical_area_group_hjid integer
);


ALTER TABLE uk.geographical_area_memberships_oplog OWNER TO postgres;

--
-- Name: geographical_area_memberships; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.geographical_area_memberships AS
 SELECT geographical_area_sid,
    geographical_area_group_sid,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename,
    hjid,
    geographical_area_hjid,
    geographical_area_group_hjid
   FROM uk.geographical_area_memberships_oplog memberships
  WHERE ((oid IN ( SELECT max(memberships2.oid) AS max
           FROM uk.geographical_area_memberships_oplog memberships2
          WHERE ((memberships.geographical_area_sid = memberships2.geographical_area_sid) AND (memberships.geographical_area_group_sid = memberships2.geographical_area_group_sid) AND (memberships.validity_start_date = memberships2.validity_start_date)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.geographical_area_memberships OWNER TO postgres;

--
-- Name: geographical_area_memberships_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.geographical_area_memberships_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.geographical_area_memberships_oid_seq OWNER TO postgres;

--
-- Name: geographical_area_memberships_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.geographical_area_memberships_oid_seq OWNED BY uk.geographical_area_memberships_oplog.oid;


--
-- Name: geographical_areas_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.geographical_areas_oplog (
    geographical_area_sid integer,
    parent_geographical_area_group_sid integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    geographical_code character varying(255),
    geographical_area_id character varying(255),
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text,
    hjid integer
);


ALTER TABLE uk.geographical_areas_oplog OWNER TO postgres;

--
-- Name: geographical_areas; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.geographical_areas AS
 SELECT geographical_area_sid,
    parent_geographical_area_group_sid,
    validity_start_date,
    validity_end_date,
    geographical_code,
    geographical_area_id,
    "national",
    oid,
    operation,
    operation_date,
    filename,
    hjid
   FROM uk.geographical_areas_oplog geographical_areas1
  WHERE ((oid IN ( SELECT max(geographical_areas2.oid) AS max
           FROM uk.geographical_areas_oplog geographical_areas2
          WHERE (geographical_areas1.geographical_area_sid = geographical_areas2.geographical_area_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.geographical_areas OWNER TO postgres;

--
-- Name: geographical_areas_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.geographical_areas_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.geographical_areas_oid_seq OWNER TO postgres;

--
-- Name: geographical_areas_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.geographical_areas_oid_seq OWNED BY uk.geographical_areas_oplog.oid;


--
-- Name: goods_nomenclature_description_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_description_periods_oplog (
    goods_nomenclature_description_period_sid integer,
    goods_nomenclature_sid integer,
    validity_start_date timestamp without time zone,
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_description_periods_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_description_periods; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_description_periods AS
 SELECT goods_nomenclature_description_period_sid,
    goods_nomenclature_sid,
    validity_start_date,
    goods_nomenclature_item_id,
    productline_suffix,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_description_periods_oplog goods_nomenclature_description_periods1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_description_periods2.oid) AS max
           FROM uk.goods_nomenclature_description_periods_oplog goods_nomenclature_description_periods2
          WHERE (goods_nomenclature_description_periods1.goods_nomenclature_description_period_sid = goods_nomenclature_description_periods2.goods_nomenclature_description_period_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_description_periods OWNER TO postgres;

--
-- Name: goods_nomenclature_description_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_description_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_description_periods_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_description_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_description_periods_oid_seq OWNED BY uk.goods_nomenclature_description_periods_oplog.oid;


--
-- Name: goods_nomenclature_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_descriptions_oplog (
    goods_nomenclature_description_period_sid integer,
    language_id character varying(5),
    goods_nomenclature_sid integer,
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_descriptions_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_descriptions AS
 SELECT goods_nomenclature_description_period_sid,
    language_id,
    goods_nomenclature_sid,
    goods_nomenclature_item_id,
    productline_suffix,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_descriptions_oplog goods_nomenclature_descriptions1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_descriptions2.oid) AS max
           FROM uk.goods_nomenclature_descriptions_oplog goods_nomenclature_descriptions2
          WHERE ((goods_nomenclature_descriptions1.goods_nomenclature_sid = goods_nomenclature_descriptions2.goods_nomenclature_sid) AND (goods_nomenclature_descriptions1.goods_nomenclature_description_period_sid = goods_nomenclature_descriptions2.goods_nomenclature_description_period_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_descriptions OWNER TO postgres;

--
-- Name: goods_nomenclature_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_descriptions_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_descriptions_oid_seq OWNED BY uk.goods_nomenclature_descriptions_oplog.oid;


--
-- Name: goods_nomenclature_group_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_group_descriptions_oplog (
    goods_nomenclature_group_type character varying(1),
    goods_nomenclature_group_id character varying(6),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_group_descriptions_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_group_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_group_descriptions AS
 SELECT goods_nomenclature_group_type,
    goods_nomenclature_group_id,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_group_descriptions_oplog goods_nomenclature_group_descriptions1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_group_descriptions2.oid) AS max
           FROM uk.goods_nomenclature_group_descriptions_oplog goods_nomenclature_group_descriptions2
          WHERE (((goods_nomenclature_group_descriptions1.goods_nomenclature_group_id)::text = (goods_nomenclature_group_descriptions2.goods_nomenclature_group_id)::text) AND ((goods_nomenclature_group_descriptions1.goods_nomenclature_group_type)::text = (goods_nomenclature_group_descriptions2.goods_nomenclature_group_type)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_group_descriptions OWNER TO postgres;

--
-- Name: goods_nomenclature_group_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_group_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_group_descriptions_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_group_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_group_descriptions_oid_seq OWNED BY uk.goods_nomenclature_group_descriptions_oplog.oid;


--
-- Name: goods_nomenclature_groups_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_groups_oplog (
    goods_nomenclature_group_type character varying(1),
    goods_nomenclature_group_id character varying(6),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    nomenclature_group_facility_code integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_groups_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_groups; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_groups AS
 SELECT goods_nomenclature_group_type,
    goods_nomenclature_group_id,
    validity_start_date,
    validity_end_date,
    nomenclature_group_facility_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_groups_oplog goods_nomenclature_groups1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_groups2.oid) AS max
           FROM uk.goods_nomenclature_groups_oplog goods_nomenclature_groups2
          WHERE (((goods_nomenclature_groups1.goods_nomenclature_group_id)::text = (goods_nomenclature_groups2.goods_nomenclature_group_id)::text) AND ((goods_nomenclature_groups1.goods_nomenclature_group_type)::text = (goods_nomenclature_groups2.goods_nomenclature_group_type)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_groups OWNER TO postgres;

--
-- Name: goods_nomenclature_groups_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_groups_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_groups_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_groups_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_groups_oid_seq OWNED BY uk.goods_nomenclature_groups_oplog.oid;


--
-- Name: goods_nomenclature_indents_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_indents_oplog (
    goods_nomenclature_indent_sid integer,
    goods_nomenclature_sid integer,
    validity_start_date timestamp without time zone,
    number_indents integer,
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_indents_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_indents; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_indents AS
 SELECT goods_nomenclature_indent_sid,
    goods_nomenclature_sid,
    validity_start_date,
    number_indents,
    goods_nomenclature_item_id,
    productline_suffix,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_indents_oplog goods_nomenclature_indents1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_indents2.oid) AS max
           FROM uk.goods_nomenclature_indents_oplog goods_nomenclature_indents2
          WHERE (goods_nomenclature_indents1.goods_nomenclature_indent_sid = goods_nomenclature_indents2.goods_nomenclature_indent_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_indents OWNER TO postgres;

--
-- Name: goods_nomenclature_indents_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_indents_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_indents_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_indents_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_indents_oid_seq OWNED BY uk.goods_nomenclature_indents_oplog.oid;


--
-- Name: goods_nomenclature_intercepts; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_intercepts (
    goods_nomenclature_sid bigint NOT NULL,
    message text,
    excluded boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


ALTER TABLE uk.goods_nomenclature_intercepts OWNER TO postgres;

--
-- Name: goods_nomenclature_labels; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_labels (
    goods_nomenclature_sid integer NOT NULL,
    goods_nomenclature_type character varying(50) NOT NULL,
    goods_nomenclature_item_id character varying(10) NOT NULL,
    producline_suffix character varying(2) NOT NULL,
    labels jsonb DEFAULT '{}'::jsonb NOT NULL,
    stale boolean DEFAULT false NOT NULL,
    manually_edited boolean DEFAULT false NOT NULL,
    context_hash character varying(64),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    description text,
    original_description text,
    synonyms text[] DEFAULT '{}'::text[],
    colloquial_terms text[] DEFAULT '{}'::text[],
    known_brands text[] DEFAULT '{}'::text[],
    description_score double precision,
    synonym_scores double precision[] DEFAULT '{}'::double precision[],
    colloquial_term_scores double precision[] DEFAULT '{}'::double precision[],
    needs_review boolean DEFAULT false NOT NULL,
    approved boolean DEFAULT false NOT NULL,
    expired boolean DEFAULT false NOT NULL
);


ALTER TABLE uk.goods_nomenclature_labels OWNER TO postgres;

--
-- Name: goods_nomenclature_origins_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_origins_oplog (
    goods_nomenclature_sid integer,
    derived_goods_nomenclature_item_id character varying(10),
    derived_productline_suffix character varying(2),
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_origins_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_origins; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_origins AS
 SELECT goods_nomenclature_sid,
    derived_goods_nomenclature_item_id,
    derived_productline_suffix,
    goods_nomenclature_item_id,
    productline_suffix,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_origins_oplog goods_nomenclature_origins1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_origins2.oid) AS max
           FROM uk.goods_nomenclature_origins_oplog goods_nomenclature_origins2
          WHERE ((goods_nomenclature_origins1.goods_nomenclature_sid = goods_nomenclature_origins2.goods_nomenclature_sid) AND ((goods_nomenclature_origins1.derived_goods_nomenclature_item_id)::text = (goods_nomenclature_origins2.derived_goods_nomenclature_item_id)::text) AND ((goods_nomenclature_origins1.derived_productline_suffix)::text = (goods_nomenclature_origins2.derived_productline_suffix)::text) AND ((goods_nomenclature_origins1.goods_nomenclature_item_id)::text = (goods_nomenclature_origins2.goods_nomenclature_item_id)::text) AND ((goods_nomenclature_origins1.productline_suffix)::text = (goods_nomenclature_origins2.productline_suffix)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_origins OWNER TO postgres;

--
-- Name: goods_nomenclature_origins_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_origins_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_origins_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_origins_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_origins_oid_seq OWNED BY uk.goods_nomenclature_origins_oplog.oid;


--
-- Name: goods_nomenclature_self_texts; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_self_texts (
    goods_nomenclature_sid integer NOT NULL,
    goods_nomenclature_item_id character varying(10) NOT NULL,
    self_text text NOT NULL,
    generation_type text NOT NULL,
    input_context jsonb NOT NULL,
    context_hash character varying(64) NOT NULL,
    needs_review boolean DEFAULT false,
    manually_edited boolean DEFAULT false,
    stale boolean DEFAULT false,
    generated_at timestamp without time zone NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    embedding public.vector(1536),
    eu_self_text text,
    eu_embedding public.vector(1536),
    similarity_score double precision,
    coherence_score double precision,
    search_embedding public.vector(1536),
    search_text text,
    search_embedding_stale boolean DEFAULT false NOT NULL,
    approved boolean DEFAULT false NOT NULL,
    expired boolean DEFAULT false NOT NULL
);


ALTER TABLE uk.goods_nomenclature_self_texts OWNER TO postgres;

--
-- Name: goods_nomenclature_successors_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_successors_oplog (
    goods_nomenclature_sid integer,
    absorbed_goods_nomenclature_item_id character varying(10),
    absorbed_productline_suffix character varying(2),
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.goods_nomenclature_successors_oplog OWNER TO postgres;

--
-- Name: goods_nomenclature_successors; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.goods_nomenclature_successors AS
 SELECT goods_nomenclature_sid,
    absorbed_goods_nomenclature_item_id,
    absorbed_productline_suffix,
    goods_nomenclature_item_id,
    productline_suffix,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.goods_nomenclature_successors_oplog goods_nomenclature_successors1
  WHERE ((oid IN ( SELECT max(goods_nomenclature_successors2.oid) AS max
           FROM uk.goods_nomenclature_successors_oplog goods_nomenclature_successors2
          WHERE ((goods_nomenclature_successors1.goods_nomenclature_sid = goods_nomenclature_successors2.goods_nomenclature_sid) AND ((goods_nomenclature_successors1.absorbed_goods_nomenclature_item_id)::text = (goods_nomenclature_successors2.absorbed_goods_nomenclature_item_id)::text) AND ((goods_nomenclature_successors1.absorbed_productline_suffix)::text = (goods_nomenclature_successors2.absorbed_productline_suffix)::text) AND ((goods_nomenclature_successors1.goods_nomenclature_item_id)::text = (goods_nomenclature_successors2.goods_nomenclature_item_id)::text) AND ((goods_nomenclature_successors1.productline_suffix)::text = (goods_nomenclature_successors2.productline_suffix)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.goods_nomenclature_successors OWNER TO postgres;

--
-- Name: goods_nomenclature_successors_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclature_successors_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclature_successors_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclature_successors_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclature_successors_oid_seq OWNED BY uk.goods_nomenclature_successors_oplog.oid;


--
-- Name: goods_nomenclature_tree_node_overrides; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclature_tree_node_overrides (
    id integer NOT NULL,
    goods_nomenclature_indent_sid integer NOT NULL,
    depth integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone
);


ALTER TABLE uk.goods_nomenclature_tree_node_overrides OWNER TO postgres;

--
-- Name: goods_nomenclature_tree_node_overrides_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.goods_nomenclature_tree_node_overrides ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.goods_nomenclature_tree_node_overrides_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: goods_nomenclatures_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.goods_nomenclatures_oplog (
    goods_nomenclature_sid integer,
    goods_nomenclature_item_id character varying(10),
    producline_suffix character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    statistical_indicator integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text,
    path integer[]
);


ALTER TABLE uk.goods_nomenclatures_oplog OWNER TO postgres;

--
-- Name: goods_nomenclatures; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.goods_nomenclatures AS
 SELECT goods_nomenclature_sid,
    goods_nomenclature_item_id,
    producline_suffix,
    validity_start_date,
    validity_end_date,
    statistical_indicator,
    oid,
    operation,
    operation_date,
    filename,
    path,
        CASE
            WHEN ((goods_nomenclature_item_id)::text ~~ '__00000000'::text) THEN NULL::text
            ELSE "left"((goods_nomenclature_item_id)::text, 4)
        END AS heading_short_code,
    "left"((goods_nomenclature_item_id)::text, 2) AS chapter_short_code
   FROM uk.goods_nomenclatures_oplog goods_nomenclatures1
  WHERE ((oid IN ( SELECT max(goods_nomenclatures2.oid) AS max
           FROM uk.goods_nomenclatures_oplog goods_nomenclatures2
          WHERE (goods_nomenclatures1.goods_nomenclature_sid = goods_nomenclatures2.goods_nomenclature_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.goods_nomenclatures OWNER TO postgres;

--
-- Name: goods_nomenclature_tree_nodes; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.goods_nomenclature_tree_nodes AS
 SELECT indents.goods_nomenclature_indent_sid,
    indents.goods_nomenclature_sid,
    indents.number_indents,
    indents.goods_nomenclature_item_id,
    indents.productline_suffix,
    (concat(indents.goods_nomenclature_item_id, indents.productline_suffix))::bigint AS "position",
    indents.validity_start_date,
    COALESCE(indents.validity_end_date, (min(replacement_indents.validity_start_date) - '00:00:01'::interval), nomenclatures.validity_end_date) AS validity_end_date,
    indents.oid,
    COALESCE(overrides.depth, ((indents.number_indents + 2) - ((((indents.goods_nomenclature_item_id)::text ~~ '%00000000'::text) AND (indents.number_indents = 0)))::integer)) AS depth
   FROM (((uk.goods_nomenclature_indents indents
     JOIN uk.goods_nomenclatures nomenclatures ON ((indents.goods_nomenclature_sid = nomenclatures.goods_nomenclature_sid)))
     LEFT JOIN uk.goods_nomenclature_indents replacement_indents ON (((indents.goods_nomenclature_sid = replacement_indents.goods_nomenclature_sid) AND (indents.validity_start_date < replacement_indents.validity_start_date) AND (indents.validity_end_date IS NULL))))
     LEFT JOIN uk.goods_nomenclature_tree_node_overrides overrides ON (((indents.goods_nomenclature_indent_sid = overrides.goods_nomenclature_indent_sid) AND (indents.operation_date < COALESCE(overrides.updated_at, overrides.created_at)))))
  GROUP BY indents.goods_nomenclature_indent_sid, indents.goods_nomenclature_sid, indents.number_indents, indents.goods_nomenclature_item_id, indents.productline_suffix, indents.validity_start_date, indents.validity_end_date, nomenclatures.validity_end_date, indents.oid, overrides.depth
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.goods_nomenclature_tree_nodes OWNER TO postgres;

--
-- Name: goods_nomenclatures_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.goods_nomenclatures_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.goods_nomenclatures_oid_seq OWNER TO postgres;

--
-- Name: goods_nomenclatures_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.goods_nomenclatures_oid_seq OWNED BY uk.goods_nomenclatures_oplog.oid;


--
-- Name: green_lanes_category_assessments; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_category_assessments (
    id integer NOT NULL,
    measure_type_id character varying(6) NOT NULL,
    regulation_id character varying(255),
    regulation_role integer,
    theme_id integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    CONSTRAINT regulation_id_requires_role CHECK (((regulation_id IS NULL) = (regulation_role IS NULL)))
);


ALTER TABLE uk.green_lanes_category_assessments OWNER TO postgres;

--
-- Name: green_lanes_category_assessments_exemptions; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_category_assessments_exemptions (
    category_assessment_id integer NOT NULL,
    exemption_id integer NOT NULL
);


ALTER TABLE uk.green_lanes_category_assessments_exemptions OWNER TO postgres;

--
-- Name: green_lanes_category_assessments_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_category_assessments ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_category_assessments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_exempting_additional_code_overrides; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_exempting_additional_code_overrides (
    id integer NOT NULL,
    additional_code_type_id text NOT NULL,
    additional_code text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.green_lanes_exempting_additional_code_overrides OWNER TO postgres;

--
-- Name: green_lanes_exempting_additional_code_overrides_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_exempting_additional_code_overrides ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_exempting_additional_code_overrides_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_exempting_certificate_overrides; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_exempting_certificate_overrides (
    id integer NOT NULL,
    certificate_type_code text NOT NULL,
    certificate_code text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.green_lanes_exempting_certificate_overrides OWNER TO postgres;

--
-- Name: green_lanes_exempting_certificate_overrides_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_exempting_certificate_overrides ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_exempting_certificate_overrides_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_exemptions; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_exemptions (
    id integer NOT NULL,
    code text NOT NULL,
    description text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.green_lanes_exemptions OWNER TO postgres;

--
-- Name: green_lanes_exemptions_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_exemptions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_exemptions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_faq_feedback; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_faq_feedback (
    id integer NOT NULL,
    session_id text NOT NULL,
    category_id integer NOT NULL,
    question_id integer NOT NULL,
    useful boolean NOT NULL,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE uk.green_lanes_faq_feedback OWNER TO postgres;

--
-- Name: green_lanes_faq_feedback_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_faq_feedback ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_faq_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_identified_measure_type_category_assessments; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_identified_measure_type_category_assessments (
    id integer NOT NULL,
    measure_type_id character varying(6) NOT NULL,
    theme_id integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.green_lanes_identified_measure_type_category_assessments OWNER TO postgres;

--
-- Name: green_lanes_identified_measure_type_category_assessments_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_identified_measure_type_category_assessments ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_identified_measure_type_category_assessments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_measures; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_measures (
    id integer NOT NULL,
    category_assessment_id integer NOT NULL,
    goods_nomenclature_item_id character varying(10) NOT NULL,
    productline_suffix character varying(2) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.green_lanes_measures OWNER TO postgres;

--
-- Name: green_lanes_measures_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_measures ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_measures_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_themes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_themes (
    id integer NOT NULL,
    section integer NOT NULL,
    subsection integer NOT NULL,
    theme character varying(255) NOT NULL,
    description text NOT NULL,
    category integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.green_lanes_themes OWNER TO postgres;

--
-- Name: green_lanes_themes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_themes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_themes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: green_lanes_update_notifications; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.green_lanes_update_notifications (
    id integer NOT NULL,
    measure_type_id character varying(6) NOT NULL,
    regulation_id character varying(255),
    regulation_role integer,
    status integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    theme_id integer
);


ALTER TABLE uk.green_lanes_update_notifications OWNER TO postgres;

--
-- Name: green_lanes_update_notifications_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.green_lanes_update_notifications ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.green_lanes_update_notifications_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guides; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.guides (
    id integer NOT NULL,
    title text,
    url text,
    strapline character varying(255),
    image character varying(255)
);


ALTER TABLE uk.guides OWNER TO postgres;

--
-- Name: guides_goods_nomenclatures; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.guides_goods_nomenclatures (
    id integer NOT NULL,
    guide_id integer NOT NULL,
    goods_nomenclature_sid integer NOT NULL
);


ALTER TABLE uk.guides_goods_nomenclatures OWNER TO postgres;

--
-- Name: guides_goods_nomenclatures_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.guides_goods_nomenclatures ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.guides_goods_nomenclatures_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guides_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.guides_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.guides_id_seq OWNER TO postgres;

--
-- Name: guides_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.guides_id_seq OWNED BY uk.guides.id;


--
-- Name: hidden_goods_nomenclatures; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.hidden_goods_nomenclatures (
    goods_nomenclature_item_id text,
    created_at timestamp without time zone
);


ALTER TABLE uk.hidden_goods_nomenclatures OWNER TO postgres;

--
-- Name: language_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.language_descriptions_oplog (
    language_code_id character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.language_descriptions_oplog OWNER TO postgres;

--
-- Name: language_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.language_descriptions AS
 SELECT language_code_id,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.language_descriptions_oplog language_descriptions1
  WHERE ((oid IN ( SELECT max(language_descriptions2.oid) AS max
           FROM uk.language_descriptions_oplog language_descriptions2
          WHERE (((language_descriptions1.language_id)::text = (language_descriptions2.language_id)::text) AND ((language_descriptions1.language_code_id)::text = (language_descriptions2.language_code_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.language_descriptions OWNER TO postgres;

--
-- Name: language_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.language_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.language_descriptions_oid_seq OWNER TO postgres;

--
-- Name: language_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.language_descriptions_oid_seq OWNED BY uk.language_descriptions_oplog.oid;


--
-- Name: languages_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.languages_oplog (
    language_id character varying(5),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.languages_oplog OWNER TO postgres;

--
-- Name: languages; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.languages AS
 SELECT language_id,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.languages_oplog languages1
  WHERE ((oid IN ( SELECT max(languages2.oid) AS max
           FROM uk.languages_oplog languages2
          WHERE ((languages1.language_id)::text = (languages2.language_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.languages OWNER TO postgres;

--
-- Name: languages_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.languages_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.languages_oid_seq OWNER TO postgres;

--
-- Name: languages_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.languages_oid_seq OWNED BY uk.languages_oplog.oid;


--
-- Name: measure_action_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_action_descriptions_oplog (
    action_code character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_action_descriptions_oplog OWNER TO postgres;

--
-- Name: measure_action_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_action_descriptions AS
 SELECT action_code,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_action_descriptions_oplog measure_action_descriptions1
  WHERE ((oid IN ( SELECT max(measure_action_descriptions2.oid) AS max
           FROM uk.measure_action_descriptions_oplog measure_action_descriptions2
          WHERE ((measure_action_descriptions1.action_code)::text = (measure_action_descriptions2.action_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_action_descriptions OWNER TO postgres;

--
-- Name: measure_action_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_action_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_action_descriptions_oid_seq OWNER TO postgres;

--
-- Name: measure_action_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_action_descriptions_oid_seq OWNED BY uk.measure_action_descriptions_oplog.oid;


--
-- Name: measure_actions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_actions_oplog (
    action_code character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_actions_oplog OWNER TO postgres;

--
-- Name: measure_actions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_actions AS
 SELECT action_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_actions_oplog measure_actions1
  WHERE ((oid IN ( SELECT max(measure_actions2.oid) AS max
           FROM uk.measure_actions_oplog measure_actions2
          WHERE ((measure_actions1.action_code)::text = (measure_actions2.action_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_actions OWNER TO postgres;

--
-- Name: measure_actions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_actions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_actions_oid_seq OWNER TO postgres;

--
-- Name: measure_actions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_actions_oid_seq OWNED BY uk.measure_actions_oplog.oid;


--
-- Name: measure_components_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_components_oplog (
    measure_sid integer,
    duty_expression_id character varying(255),
    duty_amount double precision,
    monetary_unit_code character varying(255),
    measurement_unit_code character varying(3),
    measurement_unit_qualifier_code character varying(1),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_components_oplog OWNER TO postgres;

--
-- Name: measure_components; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.measure_components AS
 SELECT measure_sid,
    duty_expression_id,
    duty_amount,
    monetary_unit_code,
    measurement_unit_code,
    measurement_unit_qualifier_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_components_oplog measure_components1
  WHERE ((oid IN ( SELECT max(measure_components2.oid) AS max
           FROM uk.measure_components_oplog measure_components2
          WHERE ((measure_components1.measure_sid = measure_components2.measure_sid) AND ((measure_components1.duty_expression_id)::text = (measure_components2.duty_expression_id)::text)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.measure_components OWNER TO postgres;

--
-- Name: measure_components_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_components_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_components_oid_seq OWNER TO postgres;

--
-- Name: measure_components_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_components_oid_seq OWNED BY uk.measure_components_oplog.oid;


--
-- Name: measure_condition_code_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_condition_code_descriptions_oplog (
    condition_code character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_condition_code_descriptions_oplog OWNER TO postgres;

--
-- Name: measure_condition_code_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_condition_code_descriptions AS
 SELECT condition_code,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_condition_code_descriptions_oplog measure_condition_code_descriptions1
  WHERE ((oid IN ( SELECT max(measure_condition_code_descriptions2.oid) AS max
           FROM uk.measure_condition_code_descriptions_oplog measure_condition_code_descriptions2
          WHERE ((measure_condition_code_descriptions1.condition_code)::text = (measure_condition_code_descriptions2.condition_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_condition_code_descriptions OWNER TO postgres;

--
-- Name: measure_condition_code_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_condition_code_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_condition_code_descriptions_oid_seq OWNER TO postgres;

--
-- Name: measure_condition_code_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_condition_code_descriptions_oid_seq OWNED BY uk.measure_condition_code_descriptions_oplog.oid;


--
-- Name: measure_condition_codes_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_condition_codes_oplog (
    condition_code character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_condition_codes_oplog OWNER TO postgres;

--
-- Name: measure_condition_codes; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_condition_codes AS
 SELECT condition_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_condition_codes_oplog measure_condition_codes1
  WHERE ((oid IN ( SELECT max(measure_condition_codes2.oid) AS max
           FROM uk.measure_condition_codes_oplog measure_condition_codes2
          WHERE ((measure_condition_codes1.condition_code)::text = (measure_condition_codes2.condition_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_condition_codes OWNER TO postgres;

--
-- Name: measure_condition_codes_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_condition_codes_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_condition_codes_oid_seq OWNER TO postgres;

--
-- Name: measure_condition_codes_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_condition_codes_oid_seq OWNED BY uk.measure_condition_codes_oplog.oid;


--
-- Name: measure_condition_components_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_condition_components_oplog (
    measure_condition_sid integer,
    duty_expression_id character varying(255),
    duty_amount double precision,
    monetary_unit_code character varying(255),
    measurement_unit_code character varying(3),
    measurement_unit_qualifier_code character varying(1),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_condition_components_oplog OWNER TO postgres;

--
-- Name: measure_condition_components; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_condition_components AS
 SELECT measure_condition_sid,
    duty_expression_id,
    duty_amount,
    monetary_unit_code,
    measurement_unit_code,
    measurement_unit_qualifier_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_condition_components_oplog measure_condition_components1
  WHERE ((oid IN ( SELECT max(measure_condition_components2.oid) AS max
           FROM uk.measure_condition_components_oplog measure_condition_components2
          WHERE ((measure_condition_components1.measure_condition_sid = measure_condition_components2.measure_condition_sid) AND ((measure_condition_components1.duty_expression_id)::text = (measure_condition_components2.duty_expression_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_condition_components OWNER TO postgres;

--
-- Name: measure_condition_components_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_condition_components_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_condition_components_oid_seq OWNER TO postgres;

--
-- Name: measure_condition_components_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_condition_components_oid_seq OWNED BY uk.measure_condition_components_oplog.oid;


--
-- Name: measure_conditions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_conditions_oplog (
    measure_condition_sid integer,
    measure_sid integer,
    condition_code character varying(255),
    component_sequence_number integer,
    condition_duty_amount double precision,
    condition_monetary_unit_code character varying(255),
    condition_measurement_unit_code character varying(3),
    condition_measurement_unit_qualifier_code character varying(1),
    action_code character varying(255),
    certificate_type_code character varying(1),
    certificate_code character varying(3),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_conditions_oplog OWNER TO postgres;

--
-- Name: measure_conditions; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.measure_conditions AS
 SELECT measure_condition_sid,
    measure_sid,
    condition_code,
    component_sequence_number,
    condition_duty_amount,
    condition_monetary_unit_code,
    condition_measurement_unit_code,
    condition_measurement_unit_qualifier_code,
    action_code,
    certificate_type_code,
    certificate_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_conditions_oplog measure_conditions1
  WHERE ((oid IN ( SELECT max(measure_conditions2.oid) AS max
           FROM uk.measure_conditions_oplog measure_conditions2
          WHERE (measure_conditions1.measure_condition_sid = measure_conditions2.measure_condition_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.measure_conditions OWNER TO postgres;

--
-- Name: measure_conditions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_conditions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_conditions_oid_seq OWNER TO postgres;

--
-- Name: measure_conditions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_conditions_oid_seq OWNED BY uk.measure_conditions_oplog.oid;


--
-- Name: measure_excluded_geographical_areas_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_excluded_geographical_areas_oplog (
    measure_sid integer,
    excluded_geographical_area character varying(255),
    geographical_area_sid integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_excluded_geographical_areas_oplog OWNER TO postgres;

--
-- Name: measure_excluded_geographical_areas; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.measure_excluded_geographical_areas AS
 SELECT measure_sid,
    excluded_geographical_area,
    geographical_area_sid,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_excluded_geographical_areas_oplog geographical_areas
  WHERE ((oid IN ( SELECT max(geographical_areas2.oid) AS max
           FROM uk.measure_excluded_geographical_areas_oplog geographical_areas2
          WHERE ((geographical_areas.measure_sid = geographical_areas2.measure_sid) AND (geographical_areas.geographical_area_sid = geographical_areas2.geographical_area_sid)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.measure_excluded_geographical_areas OWNER TO postgres;

--
-- Name: measure_excluded_geographical_areas_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_excluded_geographical_areas_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_excluded_geographical_areas_oid_seq OWNER TO postgres;

--
-- Name: measure_excluded_geographical_areas_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_excluded_geographical_areas_oid_seq OWNED BY uk.measure_excluded_geographical_areas_oplog.oid;


--
-- Name: measure_partial_temporary_stops_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_partial_temporary_stops_oplog (
    measure_sid integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    partial_temporary_stop_regulation_id character varying(255),
    partial_temporary_stop_regulation_officialjournal_number character varying(255),
    partial_temporary_stop_regulation_officialjournal_page integer,
    abrogation_regulation_id character varying(255),
    abrogation_regulation_officialjournal_number character varying(255),
    abrogation_regulation_officialjournal_page integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_partial_temporary_stops_oplog OWNER TO postgres;

--
-- Name: measure_partial_temporary_stops; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_partial_temporary_stops AS
 SELECT measure_sid,
    validity_start_date,
    validity_end_date,
    partial_temporary_stop_regulation_id,
    partial_temporary_stop_regulation_officialjournal_number,
    partial_temporary_stop_regulation_officialjournal_page,
    abrogation_regulation_id,
    abrogation_regulation_officialjournal_number,
    abrogation_regulation_officialjournal_page,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_partial_temporary_stops_oplog measure_partial_temporary_stops1
  WHERE ((oid IN ( SELECT max(measure_partial_temporary_stops2.oid) AS max
           FROM uk.measure_partial_temporary_stops_oplog measure_partial_temporary_stops2
          WHERE ((measure_partial_temporary_stops1.measure_sid = measure_partial_temporary_stops2.measure_sid) AND ((measure_partial_temporary_stops1.partial_temporary_stop_regulation_id)::text = (measure_partial_temporary_stops2.partial_temporary_stop_regulation_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_partial_temporary_stops OWNER TO postgres;

--
-- Name: measure_partial_temporary_stops_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_partial_temporary_stops_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_partial_temporary_stops_oid_seq OWNER TO postgres;

--
-- Name: measure_partial_temporary_stops_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_partial_temporary_stops_oid_seq OWNED BY uk.measure_partial_temporary_stops_oplog.oid;


--
-- Name: measure_type_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_type_descriptions_oplog (
    measure_type_id character varying(6),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_type_descriptions_oplog OWNER TO postgres;

--
-- Name: measure_type_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_type_descriptions AS
 SELECT measure_type_id,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_type_descriptions_oplog measure_type_descriptions1
  WHERE ((oid IN ( SELECT max(measure_type_descriptions2.oid) AS max
           FROM uk.measure_type_descriptions_oplog measure_type_descriptions2
          WHERE ((measure_type_descriptions1.measure_type_id)::text = (measure_type_descriptions2.measure_type_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_type_descriptions OWNER TO postgres;

--
-- Name: measure_type_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_type_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_type_descriptions_oid_seq OWNER TO postgres;

--
-- Name: measure_type_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_type_descriptions_oid_seq OWNED BY uk.measure_type_descriptions_oplog.oid;


--
-- Name: measure_type_series_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_type_series_oplog (
    measure_type_series_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    measure_type_combination integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_type_series_oplog OWNER TO postgres;

--
-- Name: measure_type_series; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_type_series AS
 SELECT measure_type_series_id,
    validity_start_date,
    validity_end_date,
    measure_type_combination,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_type_series_oplog measure_type_series1
  WHERE ((oid IN ( SELECT max(measure_type_series2.oid) AS max
           FROM uk.measure_type_series_oplog measure_type_series2
          WHERE ((measure_type_series1.measure_type_series_id)::text = (measure_type_series2.measure_type_series_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_type_series OWNER TO postgres;

--
-- Name: measure_type_series_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_type_series_descriptions_oplog (
    measure_type_series_id character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_type_series_descriptions_oplog OWNER TO postgres;

--
-- Name: measure_type_series_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_type_series_descriptions AS
 SELECT measure_type_series_id,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_type_series_descriptions_oplog measure_type_series_descriptions1
  WHERE ((oid IN ( SELECT max(measure_type_series_descriptions2.oid) AS max
           FROM uk.measure_type_series_descriptions_oplog measure_type_series_descriptions2
          WHERE ((measure_type_series_descriptions1.measure_type_series_id)::text = (measure_type_series_descriptions2.measure_type_series_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_type_series_descriptions OWNER TO postgres;

--
-- Name: measure_type_series_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_type_series_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_type_series_descriptions_oid_seq OWNER TO postgres;

--
-- Name: measure_type_series_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_type_series_descriptions_oid_seq OWNED BY uk.measure_type_series_descriptions_oplog.oid;


--
-- Name: measure_type_series_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_type_series_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_type_series_oid_seq OWNER TO postgres;

--
-- Name: measure_type_series_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_type_series_oid_seq OWNED BY uk.measure_type_series_oplog.oid;


--
-- Name: measure_types_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measure_types_oplog (
    measure_type_id character varying(6),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    trade_movement_code integer,
    priority_code integer,
    measure_component_applicable_code integer,
    origin_dest_code integer,
    order_number_capture_code integer,
    measure_explosion_level integer,
    measure_type_series_id character varying(255),
    created_at timestamp without time zone,
    "national" boolean,
    measure_type_acronym character varying(3),
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measure_types_oplog OWNER TO postgres;

--
-- Name: measure_types; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measure_types AS
 SELECT measure_type_id,
    validity_start_date,
    validity_end_date,
    trade_movement_code,
    priority_code,
    measure_component_applicable_code,
    origin_dest_code,
    order_number_capture_code,
    measure_explosion_level,
    measure_type_series_id,
    "national",
    measure_type_acronym,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measure_types_oplog measure_types1
  WHERE ((oid IN ( SELECT max(measure_types2.oid) AS max
           FROM uk.measure_types_oplog measure_types2
          WHERE ((measure_types1.measure_type_id)::text = (measure_types2.measure_type_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measure_types OWNER TO postgres;

--
-- Name: measure_types_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measure_types_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measure_types_oid_seq OWNER TO postgres;

--
-- Name: measure_types_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measure_types_oid_seq OWNED BY uk.measure_types_oplog.oid;


--
-- Name: measurement_unit_abbreviations; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measurement_unit_abbreviations (
    id integer NOT NULL,
    abbreviation text,
    measurement_unit_code text,
    measurement_unit_qualifier text
);


ALTER TABLE uk.measurement_unit_abbreviations OWNER TO postgres;

--
-- Name: measurement_unit_abbreviations_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measurement_unit_abbreviations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measurement_unit_abbreviations_id_seq OWNER TO postgres;

--
-- Name: measurement_unit_abbreviations_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measurement_unit_abbreviations_id_seq OWNED BY uk.measurement_unit_abbreviations.id;


--
-- Name: measurement_unit_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measurement_unit_descriptions_oplog (
    measurement_unit_code character varying(3),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measurement_unit_descriptions_oplog OWNER TO postgres;

--
-- Name: measurement_unit_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measurement_unit_descriptions AS
 SELECT measurement_unit_code,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measurement_unit_descriptions_oplog measurement_unit_descriptions1
  WHERE ((oid IN ( SELECT max(measurement_unit_descriptions2.oid) AS max
           FROM uk.measurement_unit_descriptions_oplog measurement_unit_descriptions2
          WHERE ((measurement_unit_descriptions1.measurement_unit_code)::text = (measurement_unit_descriptions2.measurement_unit_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measurement_unit_descriptions OWNER TO postgres;

--
-- Name: measurement_unit_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measurement_unit_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measurement_unit_descriptions_oid_seq OWNER TO postgres;

--
-- Name: measurement_unit_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measurement_unit_descriptions_oid_seq OWNED BY uk.measurement_unit_descriptions_oplog.oid;


--
-- Name: measurement_unit_qualifier_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measurement_unit_qualifier_descriptions_oplog (
    measurement_unit_qualifier_code character varying(1),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measurement_unit_qualifier_descriptions_oplog OWNER TO postgres;

--
-- Name: measurement_unit_qualifier_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measurement_unit_qualifier_descriptions AS
 SELECT measurement_unit_qualifier_code,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measurement_unit_qualifier_descriptions_oplog measurement_unit_qualifier_descriptions1
  WHERE ((oid IN ( SELECT max(measurement_unit_qualifier_descriptions2.oid) AS max
           FROM uk.measurement_unit_qualifier_descriptions_oplog measurement_unit_qualifier_descriptions2
          WHERE ((measurement_unit_qualifier_descriptions1.measurement_unit_qualifier_code)::text = (measurement_unit_qualifier_descriptions2.measurement_unit_qualifier_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measurement_unit_qualifier_descriptions OWNER TO postgres;

--
-- Name: measurement_unit_qualifier_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measurement_unit_qualifier_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measurement_unit_qualifier_descriptions_oid_seq OWNER TO postgres;

--
-- Name: measurement_unit_qualifier_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measurement_unit_qualifier_descriptions_oid_seq OWNED BY uk.measurement_unit_qualifier_descriptions_oplog.oid;


--
-- Name: measurement_unit_qualifiers_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measurement_unit_qualifiers_oplog (
    measurement_unit_qualifier_code character varying(1),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measurement_unit_qualifiers_oplog OWNER TO postgres;

--
-- Name: measurement_unit_qualifiers; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measurement_unit_qualifiers AS
 SELECT measurement_unit_qualifier_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measurement_unit_qualifiers_oplog measurement_unit_qualifiers1
  WHERE ((oid IN ( SELECT max(measurement_unit_qualifiers2.oid) AS max
           FROM uk.measurement_unit_qualifiers_oplog measurement_unit_qualifiers2
          WHERE ((measurement_unit_qualifiers1.measurement_unit_qualifier_code)::text = (measurement_unit_qualifiers2.measurement_unit_qualifier_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measurement_unit_qualifiers OWNER TO postgres;

--
-- Name: measurement_unit_qualifiers_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measurement_unit_qualifiers_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measurement_unit_qualifiers_oid_seq OWNER TO postgres;

--
-- Name: measurement_unit_qualifiers_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measurement_unit_qualifiers_oid_seq OWNED BY uk.measurement_unit_qualifiers_oplog.oid;


--
-- Name: measurement_units_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measurement_units_oplog (
    measurement_unit_code character varying(3),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measurement_units_oplog OWNER TO postgres;

--
-- Name: measurement_units; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measurement_units AS
 SELECT measurement_unit_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measurement_units_oplog measurement_units1
  WHERE ((oid IN ( SELECT max(measurement_units2.oid) AS max
           FROM uk.measurement_units_oplog measurement_units2
          WHERE ((measurement_units1.measurement_unit_code)::text = (measurement_units2.measurement_unit_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measurement_units OWNER TO postgres;

--
-- Name: measurement_units_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measurement_units_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measurement_units_oid_seq OWNER TO postgres;

--
-- Name: measurement_units_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measurement_units_oid_seq OWNED BY uk.measurement_units_oplog.oid;


--
-- Name: measurements_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measurements_oplog (
    measurement_unit_code character varying(3),
    measurement_unit_qualifier_code character varying(1),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measurements_oplog OWNER TO postgres;

--
-- Name: measurements; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.measurements AS
 SELECT measurement_unit_code,
    measurement_unit_qualifier_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measurements_oplog measurements1
  WHERE ((oid IN ( SELECT max(measurements2.oid) AS max
           FROM uk.measurements_oplog measurements2
          WHERE (((measurements1.measurement_unit_code)::text = (measurements2.measurement_unit_code)::text) AND ((measurements1.measurement_unit_qualifier_code)::text = (measurements2.measurement_unit_qualifier_code)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.measurements OWNER TO postgres;

--
-- Name: measurements_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measurements_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measurements_oid_seq OWNER TO postgres;

--
-- Name: measurements_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measurements_oid_seq OWNED BY uk.measurements_oplog.oid;


--
-- Name: measures_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.measures_oplog (
    measure_sid integer,
    measure_type_id character varying(6),
    geographical_area_id character varying(255),
    goods_nomenclature_item_id character varying(10),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    measure_generating_regulation_role integer,
    measure_generating_regulation_id character varying(255),
    justification_regulation_role integer,
    justification_regulation_id character varying(255),
    stopped_flag boolean,
    geographical_area_sid integer,
    goods_nomenclature_sid integer,
    ordernumber character varying(255),
    additional_code_type_id text,
    additional_code_id character varying(3),
    additional_code_sid integer,
    reduction_indicator integer,
    export_refund_nomenclature_sid integer,
    created_at timestamp without time zone,
    "national" boolean,
    tariff_measure_number character varying(10),
    invalidated_by integer,
    invalidated_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.measures_oplog OWNER TO postgres;

--
-- Name: measures; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.measures AS
 SELECT measure_sid,
    measure_type_id,
    geographical_area_id,
    goods_nomenclature_item_id,
    validity_start_date,
    validity_end_date,
    measure_generating_regulation_role,
    measure_generating_regulation_id,
    justification_regulation_role,
    justification_regulation_id,
    stopped_flag,
    geographical_area_sid,
    goods_nomenclature_sid,
    ordernumber,
    additional_code_type_id,
    additional_code_id,
    additional_code_sid,
    reduction_indicator,
    export_refund_nomenclature_sid,
    "national",
    tariff_measure_number,
    invalidated_by,
    invalidated_at,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.measures_oplog measures1
  WHERE ((oid IN ( SELECT max(measures2.oid) AS max
           FROM uk.measures_oplog measures2
          WHERE (measures1.measure_sid = measures2.measure_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.measures OWNER TO postgres;

--
-- Name: measures_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.measures_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.measures_oid_seq OWNER TO postgres;

--
-- Name: measures_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.measures_oid_seq OWNED BY uk.measures_oplog.oid;


--
-- Name: meursing_additional_codes_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.meursing_additional_codes_oplog (
    meursing_additional_code_sid integer,
    additional_code character varying(3),
    validity_start_date timestamp without time zone,
    created_at timestamp without time zone,
    validity_end_date timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.meursing_additional_codes_oplog OWNER TO postgres;

--
-- Name: meursing_additional_codes; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.meursing_additional_codes AS
 SELECT meursing_additional_code_sid,
    additional_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.meursing_additional_codes_oplog meursing_additional_codes1
  WHERE ((oid IN ( SELECT max(meursing_additional_codes2.oid) AS max
           FROM uk.meursing_additional_codes_oplog meursing_additional_codes2
          WHERE (meursing_additional_codes1.meursing_additional_code_sid = meursing_additional_codes2.meursing_additional_code_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.meursing_additional_codes OWNER TO postgres;

--
-- Name: meursing_additional_codes_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.meursing_additional_codes_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.meursing_additional_codes_oid_seq OWNER TO postgres;

--
-- Name: meursing_additional_codes_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.meursing_additional_codes_oid_seq OWNED BY uk.meursing_additional_codes_oplog.oid;


--
-- Name: meursing_heading_texts_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.meursing_heading_texts_oplog (
    meursing_table_plan_id character varying(2),
    meursing_heading_number integer,
    row_column_code integer,
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.meursing_heading_texts_oplog OWNER TO postgres;

--
-- Name: meursing_heading_texts; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.meursing_heading_texts AS
 SELECT meursing_table_plan_id,
    meursing_heading_number,
    row_column_code,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.meursing_heading_texts_oplog meursing_heading_texts1
  WHERE ((oid IN ( SELECT max(meursing_heading_texts2.oid) AS max
           FROM uk.meursing_heading_texts_oplog meursing_heading_texts2
          WHERE (((meursing_heading_texts1.meursing_table_plan_id)::text = (meursing_heading_texts2.meursing_table_plan_id)::text) AND (meursing_heading_texts1.meursing_heading_number = meursing_heading_texts2.meursing_heading_number) AND (meursing_heading_texts1.row_column_code = meursing_heading_texts2.row_column_code)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.meursing_heading_texts OWNER TO postgres;

--
-- Name: meursing_heading_texts_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.meursing_heading_texts_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.meursing_heading_texts_oid_seq OWNER TO postgres;

--
-- Name: meursing_heading_texts_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.meursing_heading_texts_oid_seq OWNED BY uk.meursing_heading_texts_oplog.oid;


--
-- Name: meursing_headings_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.meursing_headings_oplog (
    meursing_table_plan_id character varying(2),
    meursing_heading_number text,
    row_column_code integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.meursing_headings_oplog OWNER TO postgres;

--
-- Name: meursing_headings; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.meursing_headings AS
 SELECT meursing_table_plan_id,
    meursing_heading_number,
    row_column_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.meursing_headings_oplog meursing_headings1
  WHERE ((oid IN ( SELECT max(meursing_headings2.oid) AS max
           FROM uk.meursing_headings_oplog meursing_headings2
          WHERE (((meursing_headings1.meursing_table_plan_id)::text = (meursing_headings2.meursing_table_plan_id)::text) AND (meursing_headings1.meursing_heading_number = meursing_headings2.meursing_heading_number) AND (meursing_headings1.row_column_code = meursing_headings2.row_column_code)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.meursing_headings OWNER TO postgres;

--
-- Name: meursing_headings_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.meursing_headings_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.meursing_headings_oid_seq OWNER TO postgres;

--
-- Name: meursing_headings_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.meursing_headings_oid_seq OWNED BY uk.meursing_headings_oplog.oid;


--
-- Name: meursing_subheadings_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.meursing_subheadings_oplog (
    meursing_table_plan_id character varying(2),
    meursing_heading_number integer,
    row_column_code integer,
    subheading_sequence_number integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.meursing_subheadings_oplog OWNER TO postgres;

--
-- Name: meursing_subheadings; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.meursing_subheadings AS
 SELECT meursing_table_plan_id,
    meursing_heading_number,
    row_column_code,
    subheading_sequence_number,
    validity_start_date,
    validity_end_date,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.meursing_subheadings_oplog meursing_subheadings1
  WHERE ((oid IN ( SELECT max(meursing_subheadings2.oid) AS max
           FROM uk.meursing_subheadings_oplog meursing_subheadings2
          WHERE (((meursing_subheadings1.meursing_table_plan_id)::text = (meursing_subheadings2.meursing_table_plan_id)::text) AND (meursing_subheadings1.meursing_heading_number = meursing_subheadings2.meursing_heading_number) AND (meursing_subheadings1.row_column_code = meursing_subheadings2.row_column_code) AND (meursing_subheadings1.subheading_sequence_number = meursing_subheadings2.subheading_sequence_number)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.meursing_subheadings OWNER TO postgres;

--
-- Name: meursing_subheadings_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.meursing_subheadings_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.meursing_subheadings_oid_seq OWNER TO postgres;

--
-- Name: meursing_subheadings_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.meursing_subheadings_oid_seq OWNED BY uk.meursing_subheadings_oplog.oid;


--
-- Name: meursing_table_cell_components_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.meursing_table_cell_components_oplog (
    meursing_additional_code_sid integer,
    meursing_table_plan_id character varying(2),
    heading_number integer,
    row_column_code integer,
    subheading_sequence_number integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    additional_code character varying(3),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.meursing_table_cell_components_oplog OWNER TO postgres;

--
-- Name: meursing_table_cell_components; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.meursing_table_cell_components AS
 SELECT meursing_additional_code_sid,
    meursing_table_plan_id,
    heading_number,
    row_column_code,
    subheading_sequence_number,
    validity_start_date,
    validity_end_date,
    additional_code,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.meursing_table_cell_components_oplog meursing_table_cell_components1
  WHERE ((oid IN ( SELECT max(meursing_table_cell_components2.oid) AS max
           FROM uk.meursing_table_cell_components_oplog meursing_table_cell_components2
          WHERE (((meursing_table_cell_components1.meursing_table_plan_id)::text = (meursing_table_cell_components2.meursing_table_plan_id)::text) AND (meursing_table_cell_components1.heading_number = meursing_table_cell_components2.heading_number) AND (meursing_table_cell_components1.row_column_code = meursing_table_cell_components2.row_column_code) AND (meursing_table_cell_components1.meursing_additional_code_sid = meursing_table_cell_components2.meursing_additional_code_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.meursing_table_cell_components OWNER TO postgres;

--
-- Name: meursing_table_cell_components_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.meursing_table_cell_components_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.meursing_table_cell_components_oid_seq OWNER TO postgres;

--
-- Name: meursing_table_cell_components_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.meursing_table_cell_components_oid_seq OWNED BY uk.meursing_table_cell_components_oplog.oid;


--
-- Name: meursing_table_plans_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.meursing_table_plans_oplog (
    meursing_table_plan_id character varying(2),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.meursing_table_plans_oplog OWNER TO postgres;

--
-- Name: meursing_table_plans; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.meursing_table_plans AS
 SELECT meursing_table_plan_id,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.meursing_table_plans_oplog meursing_table_plans1
  WHERE ((oid IN ( SELECT max(meursing_table_plans2.oid) AS max
           FROM uk.meursing_table_plans_oplog meursing_table_plans2
          WHERE ((meursing_table_plans1.meursing_table_plan_id)::text = (meursing_table_plans2.meursing_table_plan_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.meursing_table_plans OWNER TO postgres;

--
-- Name: meursing_table_plans_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.meursing_table_plans_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.meursing_table_plans_oid_seq OWNER TO postgres;

--
-- Name: meursing_table_plans_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.meursing_table_plans_oid_seq OWNED BY uk.meursing_table_plans_oplog.oid;


--
-- Name: modification_regulations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.modification_regulations_oplog (
    modification_regulation_role integer,
    modification_regulation_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    published_date date,
    officialjournal_number character varying(255),
    officialjournal_page integer,
    base_regulation_role integer,
    base_regulation_id character varying(255),
    replacement_indicator integer,
    stopped_flag boolean,
    information_text text,
    approved_flag boolean,
    explicit_abrogation_regulation_role integer,
    explicit_abrogation_regulation_id character varying(8),
    effective_end_date timestamp without time zone,
    complete_abrogation_regulation_role integer,
    complete_abrogation_regulation_id character varying(8),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.modification_regulations_oplog OWNER TO postgres;

--
-- Name: modification_regulations; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.modification_regulations AS
 SELECT modification_regulation_role,
    modification_regulation_id,
    validity_start_date,
    validity_end_date,
    published_date,
    officialjournal_number,
    officialjournal_page,
    base_regulation_role,
    base_regulation_id,
    replacement_indicator,
    stopped_flag,
    information_text,
    approved_flag,
    explicit_abrogation_regulation_role,
    explicit_abrogation_regulation_id,
    effective_end_date,
    complete_abrogation_regulation_role,
    complete_abrogation_regulation_id,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.modification_regulations_oplog modification_regulations1
  WHERE ((oid IN ( SELECT max(modification_regulations2.oid) AS max
           FROM uk.modification_regulations_oplog modification_regulations2
          WHERE (((modification_regulations1.modification_regulation_id)::text = (modification_regulations2.modification_regulation_id)::text) AND (modification_regulations1.modification_regulation_role = modification_regulations2.modification_regulation_role)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.modification_regulations OWNER TO postgres;

--
-- Name: modification_regulations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.modification_regulations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.modification_regulations_oid_seq OWNER TO postgres;

--
-- Name: modification_regulations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.modification_regulations_oid_seq OWNED BY uk.modification_regulations_oplog.oid;


--
-- Name: monetary_exchange_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.monetary_exchange_periods_oplog (
    monetary_exchange_period_sid integer,
    parent_monetary_unit_code character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.monetary_exchange_periods_oplog OWNER TO postgres;

--
-- Name: monetary_exchange_periods; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.monetary_exchange_periods AS
 SELECT monetary_exchange_period_sid,
    parent_monetary_unit_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.monetary_exchange_periods_oplog monetary_exchange_periods1
  WHERE ((oid IN ( SELECT max(monetary_exchange_periods2.oid) AS max
           FROM uk.monetary_exchange_periods_oplog monetary_exchange_periods2
          WHERE ((monetary_exchange_periods1.monetary_exchange_period_sid = monetary_exchange_periods2.monetary_exchange_period_sid) AND ((monetary_exchange_periods1.parent_monetary_unit_code)::text = (monetary_exchange_periods2.parent_monetary_unit_code)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.monetary_exchange_periods OWNER TO postgres;

--
-- Name: monetary_exchange_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.monetary_exchange_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.monetary_exchange_periods_oid_seq OWNER TO postgres;

--
-- Name: monetary_exchange_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.monetary_exchange_periods_oid_seq OWNED BY uk.monetary_exchange_periods_oplog.oid;


--
-- Name: monetary_exchange_rates_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.monetary_exchange_rates_oplog (
    monetary_exchange_period_sid integer,
    child_monetary_unit_code character varying(255),
    exchange_rate numeric(16,8),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.monetary_exchange_rates_oplog OWNER TO postgres;

--
-- Name: monetary_exchange_rates; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.monetary_exchange_rates AS
 SELECT monetary_exchange_period_sid,
    child_monetary_unit_code,
    exchange_rate,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.monetary_exchange_rates_oplog monetary_exchange_rates1
  WHERE ((oid IN ( SELECT max(monetary_exchange_rates2.oid) AS max
           FROM uk.monetary_exchange_rates_oplog monetary_exchange_rates2
          WHERE ((monetary_exchange_rates1.monetary_exchange_period_sid = monetary_exchange_rates2.monetary_exchange_period_sid) AND ((monetary_exchange_rates1.child_monetary_unit_code)::text = (monetary_exchange_rates2.child_monetary_unit_code)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.monetary_exchange_rates OWNER TO postgres;

--
-- Name: monetary_exchange_rates_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.monetary_exchange_rates_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.monetary_exchange_rates_oid_seq OWNER TO postgres;

--
-- Name: monetary_exchange_rates_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.monetary_exchange_rates_oid_seq OWNED BY uk.monetary_exchange_rates_oplog.oid;


--
-- Name: monetary_unit_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.monetary_unit_descriptions_oplog (
    monetary_unit_code character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.monetary_unit_descriptions_oplog OWNER TO postgres;

--
-- Name: monetary_unit_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.monetary_unit_descriptions AS
 SELECT monetary_unit_code,
    language_id,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.monetary_unit_descriptions_oplog monetary_unit_descriptions1
  WHERE ((oid IN ( SELECT max(monetary_unit_descriptions2.oid) AS max
           FROM uk.monetary_unit_descriptions_oplog monetary_unit_descriptions2
          WHERE ((monetary_unit_descriptions1.monetary_unit_code)::text = (monetary_unit_descriptions2.monetary_unit_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.monetary_unit_descriptions OWNER TO postgres;

--
-- Name: monetary_unit_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.monetary_unit_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.monetary_unit_descriptions_oid_seq OWNER TO postgres;

--
-- Name: monetary_unit_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.monetary_unit_descriptions_oid_seq OWNED BY uk.monetary_unit_descriptions_oplog.oid;


--
-- Name: monetary_units_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.monetary_units_oplog (
    monetary_unit_code character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.monetary_units_oplog OWNER TO postgres;

--
-- Name: monetary_units; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.monetary_units AS
 SELECT monetary_unit_code,
    validity_start_date,
    validity_end_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.monetary_units_oplog monetary_units1
  WHERE ((oid IN ( SELECT max(monetary_units2.oid) AS max
           FROM uk.monetary_units_oplog monetary_units2
          WHERE ((monetary_units1.monetary_unit_code)::text = (monetary_units2.monetary_unit_code)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.monetary_units OWNER TO postgres;

--
-- Name: monetary_units_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.monetary_units_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.monetary_units_oid_seq OWNER TO postgres;

--
-- Name: monetary_units_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.monetary_units_oid_seq OWNED BY uk.monetary_units_oplog.oid;


--
-- Name: news_collections; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.news_collections (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    priority integer DEFAULT 0 NOT NULL,
    description text,
    slug character varying(255),
    published boolean DEFAULT true,
    subscribable boolean DEFAULT false
);


ALTER TABLE uk.news_collections OWNER TO postgres;

--
-- Name: news_collections_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.news_collections ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.news_collections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: news_collections_news_items; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.news_collections_news_items (
    collection_id integer NOT NULL,
    item_id integer NOT NULL
);


ALTER TABLE uk.news_collections_news_items OWNER TO postgres;

--
-- Name: news_items; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.news_items (
    id integer NOT NULL,
    title character varying(255) NOT NULL,
    content text NOT NULL,
    display_style integer NOT NULL,
    show_on_uk boolean NOT NULL,
    show_on_xi boolean NOT NULL,
    show_on_updates_page boolean NOT NULL,
    show_on_home_page boolean NOT NULL,
    start_date date NOT NULL,
    end_date date,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone,
    show_on_banner boolean DEFAULT false NOT NULL,
    precis text,
    slug character varying(255),
    imported_at timestamp without time zone,
    chapters text,
    notify_subscribers boolean DEFAULT false
);


ALTER TABLE uk.news_items OWNER TO postgres;

--
-- Name: news_items_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.news_items ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.news_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: nomenclature_group_memberships_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.nomenclature_group_memberships_oplog (
    goods_nomenclature_sid integer,
    goods_nomenclature_group_type character varying(1),
    goods_nomenclature_group_id character varying(6),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    goods_nomenclature_item_id character varying(10),
    productline_suffix character varying(2),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.nomenclature_group_memberships_oplog OWNER TO postgres;

--
-- Name: nomenclature_group_memberships; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.nomenclature_group_memberships AS
 SELECT goods_nomenclature_sid,
    goods_nomenclature_group_type,
    goods_nomenclature_group_id,
    validity_start_date,
    validity_end_date,
    goods_nomenclature_item_id,
    productline_suffix,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.nomenclature_group_memberships_oplog nomenclature_group_memberships1
  WHERE ((oid IN ( SELECT max(nomenclature_group_memberships2.oid) AS max
           FROM uk.nomenclature_group_memberships_oplog nomenclature_group_memberships2
          WHERE ((nomenclature_group_memberships1.goods_nomenclature_sid = nomenclature_group_memberships2.goods_nomenclature_sid) AND ((nomenclature_group_memberships1.goods_nomenclature_group_id)::text = (nomenclature_group_memberships2.goods_nomenclature_group_id)::text) AND ((nomenclature_group_memberships1.goods_nomenclature_group_type)::text = (nomenclature_group_memberships2.goods_nomenclature_group_type)::text) AND ((nomenclature_group_memberships1.goods_nomenclature_item_id)::text = (nomenclature_group_memberships2.goods_nomenclature_item_id)::text) AND (nomenclature_group_memberships1.validity_start_date = nomenclature_group_memberships2.validity_start_date)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.nomenclature_group_memberships OWNER TO postgres;

--
-- Name: nomenclature_group_memberships_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.nomenclature_group_memberships_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.nomenclature_group_memberships_oid_seq OWNER TO postgres;

--
-- Name: nomenclature_group_memberships_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.nomenclature_group_memberships_oid_seq OWNED BY uk.nomenclature_group_memberships_oplog.oid;


--
-- Name: prorogation_regulation_actions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.prorogation_regulation_actions_oplog (
    prorogation_regulation_role integer,
    prorogation_regulation_id character varying(8),
    prorogated_regulation_role integer,
    prorogated_regulation_id character varying(8),
    prorogated_date date,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.prorogation_regulation_actions_oplog OWNER TO postgres;

--
-- Name: prorogation_regulation_actions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.prorogation_regulation_actions AS
 SELECT prorogation_regulation_role,
    prorogation_regulation_id,
    prorogated_regulation_role,
    prorogated_regulation_id,
    prorogated_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.prorogation_regulation_actions_oplog prorogation_regulation_actions1
  WHERE ((oid IN ( SELECT max(prorogation_regulation_actions2.oid) AS max
           FROM uk.prorogation_regulation_actions_oplog prorogation_regulation_actions2
          WHERE (((prorogation_regulation_actions1.prorogation_regulation_id)::text = (prorogation_regulation_actions2.prorogation_regulation_id)::text) AND (prorogation_regulation_actions1.prorogation_regulation_role = prorogation_regulation_actions2.prorogation_regulation_role) AND ((prorogation_regulation_actions1.prorogated_regulation_id)::text = (prorogation_regulation_actions2.prorogated_regulation_id)::text) AND (prorogation_regulation_actions1.prorogated_regulation_role = prorogation_regulation_actions2.prorogated_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.prorogation_regulation_actions OWNER TO postgres;

--
-- Name: prorogation_regulation_actions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.prorogation_regulation_actions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.prorogation_regulation_actions_oid_seq OWNER TO postgres;

--
-- Name: prorogation_regulation_actions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.prorogation_regulation_actions_oid_seq OWNED BY uk.prorogation_regulation_actions_oplog.oid;


--
-- Name: prorogation_regulations_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.prorogation_regulations_oplog (
    prorogation_regulation_role integer,
    prorogation_regulation_id character varying(255),
    published_date date,
    officialjournal_number character varying(255),
    officialjournal_page integer,
    replacement_indicator integer,
    information_text text,
    approved_flag boolean,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.prorogation_regulations_oplog OWNER TO postgres;

--
-- Name: prorogation_regulations; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.prorogation_regulations AS
 SELECT prorogation_regulation_role,
    prorogation_regulation_id,
    published_date,
    officialjournal_number,
    officialjournal_page,
    replacement_indicator,
    information_text,
    approved_flag,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.prorogation_regulations_oplog prorogation_regulations1
  WHERE ((oid IN ( SELECT max(prorogation_regulations2.oid) AS max
           FROM uk.prorogation_regulations_oplog prorogation_regulations2
          WHERE (((prorogation_regulations1.prorogation_regulation_id)::text = (prorogation_regulations2.prorogation_regulation_id)::text) AND (prorogation_regulations1.prorogation_regulation_role = prorogation_regulations2.prorogation_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.prorogation_regulations OWNER TO postgres;

--
-- Name: prorogation_regulations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.prorogation_regulations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.prorogation_regulations_oid_seq OWNER TO postgres;

--
-- Name: prorogation_regulations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.prorogation_regulations_oid_seq OWNED BY uk.prorogation_regulations_oplog.oid;


--
-- Name: publication_sigles_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.publication_sigles_oplog (
    oid integer NOT NULL,
    code_type_id character varying(4),
    code character varying(10),
    publication_code character varying(1),
    publication_sigle character varying(20),
    validity_end_date timestamp without time zone,
    validity_start_date timestamp without time zone,
    created_at timestamp without time zone,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.publication_sigles_oplog OWNER TO postgres;

--
-- Name: publication_sigles; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.publication_sigles AS
 SELECT oid,
    code_type_id,
    code,
    publication_code,
    publication_sigle,
    validity_end_date,
    validity_start_date,
    operation,
    operation_date,
    filename
   FROM uk.publication_sigles_oplog publication_sigles1
  WHERE ((oid IN ( SELECT max(publication_sigles2.oid) AS max
           FROM uk.publication_sigles_oplog publication_sigles2
          WHERE (((publication_sigles1.code)::text = (publication_sigles2.code)::text) AND ((publication_sigles1.code_type_id)::text = (publication_sigles2.code_type_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.publication_sigles OWNER TO postgres;

--
-- Name: publication_sigles_oplog_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.publication_sigles_oplog_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.publication_sigles_oplog_oid_seq OWNER TO postgres;

--
-- Name: publication_sigles_oplog_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.publication_sigles_oplog_oid_seq OWNED BY uk.publication_sigles_oplog.oid;


--
-- Name: quota_associations_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_associations_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_associations_oid_seq OWNER TO postgres;

--
-- Name: quota_associations_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_associations_oid_seq OWNED BY uk.quota_associations_oplog.oid;


--
-- Name: quota_balance_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_balance_events_oplog (
    quota_definition_sid integer,
    occurrence_timestamp timestamp without time zone,
    last_import_date_in_allocation date,
    old_balance numeric(15,3),
    new_balance numeric(15,3),
    imported_amount numeric(15,3),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_balance_events_oplog OWNER TO postgres;

--
-- Name: quota_balance_events; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_balance_events AS
 SELECT quota_definition_sid,
    occurrence_timestamp,
    last_import_date_in_allocation,
    old_balance,
    new_balance,
    imported_amount,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_balance_events_oplog quota_balance_events1
  WHERE ((oid IN ( SELECT max(quota_balance_events2.oid) AS max
           FROM uk.quota_balance_events_oplog quota_balance_events2
          WHERE ((quota_balance_events1.quota_definition_sid = quota_balance_events2.quota_definition_sid) AND (quota_balance_events1.occurrence_timestamp = quota_balance_events2.occurrence_timestamp)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_balance_events OWNER TO postgres;

--
-- Name: quota_balance_events_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_balance_events_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_balance_events_oid_seq OWNER TO postgres;

--
-- Name: quota_balance_events_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_balance_events_oid_seq OWNED BY uk.quota_balance_events_oplog.oid;


--
-- Name: quota_blocking_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_blocking_periods_oplog (
    quota_blocking_period_sid integer,
    quota_definition_sid integer,
    blocking_start_date date,
    blocking_end_date date,
    blocking_period_type integer,
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_blocking_periods_oplog OWNER TO postgres;

--
-- Name: quota_blocking_periods; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_blocking_periods AS
 SELECT quota_blocking_period_sid,
    quota_definition_sid,
    blocking_start_date,
    blocking_end_date,
    blocking_period_type,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_blocking_periods_oplog quota_blocking_periods1
  WHERE ((oid IN ( SELECT max(quota_blocking_periods2.oid) AS max
           FROM uk.quota_blocking_periods_oplog quota_blocking_periods2
          WHERE (quota_blocking_periods1.quota_blocking_period_sid = quota_blocking_periods2.quota_blocking_period_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_blocking_periods OWNER TO postgres;

--
-- Name: quota_blocking_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_blocking_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_blocking_periods_oid_seq OWNER TO postgres;

--
-- Name: quota_blocking_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_blocking_periods_oid_seq OWNED BY uk.quota_blocking_periods_oplog.oid;


--
-- Name: quota_closed_and_transferred_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_closed_and_transferred_events_oplog (
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    quota_definition_sid integer NOT NULL,
    occurrence_timestamp timestamp without time zone NOT NULL,
    target_quota_definition_sid integer NOT NULL,
    closing_date date,
    transferred_amount numeric(15,3),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    filename text
);


ALTER TABLE uk.quota_closed_and_transferred_events_oplog OWNER TO postgres;

--
-- Name: quota_closed_and_transferred_events; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_closed_and_transferred_events AS
 SELECT oid,
    quota_definition_sid,
    target_quota_definition_sid,
    occurrence_timestamp,
    operation,
    operation_date,
    transferred_amount,
    closing_date,
    filename
   FROM uk.quota_closed_and_transferred_events_oplog quota_closed_and_transferred_events1
  WHERE ((oid IN ( SELECT max(quota_closed_and_transferred_events2.oid) AS max
           FROM uk.quota_closed_and_transferred_events_oplog quota_closed_and_transferred_events2
          WHERE ((quota_closed_and_transferred_events1.quota_definition_sid = quota_closed_and_transferred_events2.quota_definition_sid) AND (quota_closed_and_transferred_events1.occurrence_timestamp = quota_closed_and_transferred_events2.occurrence_timestamp)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_closed_and_transferred_events OWNER TO postgres;

--
-- Name: quota_closed_and_transferred_events_oplog_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.quota_closed_and_transferred_events_oplog ALTER COLUMN oid ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.quota_closed_and_transferred_events_oplog_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: quota_critical_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_critical_events_oplog (
    quota_definition_sid integer,
    occurrence_timestamp timestamp without time zone,
    critical_state character varying(255),
    critical_state_change_date date,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_critical_events_oplog OWNER TO postgres;

--
-- Name: quota_critical_events; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_critical_events AS
 SELECT quota_definition_sid,
    occurrence_timestamp,
    critical_state,
    critical_state_change_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_critical_events_oplog quota_critical_events1
  WHERE ((oid IN ( SELECT max(quota_critical_events2.oid) AS max
           FROM uk.quota_critical_events_oplog quota_critical_events2
          WHERE ((quota_critical_events1.quota_definition_sid = quota_critical_events2.quota_definition_sid) AND (quota_critical_events1.occurrence_timestamp = quota_critical_events2.occurrence_timestamp)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_critical_events OWNER TO postgres;

--
-- Name: quota_critical_events_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_critical_events_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_critical_events_oid_seq OWNER TO postgres;

--
-- Name: quota_critical_events_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_critical_events_oid_seq OWNED BY uk.quota_critical_events_oplog.oid;


--
-- Name: quota_definitions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_definitions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_definitions_oid_seq OWNER TO postgres;

--
-- Name: quota_definitions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_definitions_oid_seq OWNED BY uk.quota_definitions_oplog.oid;


--
-- Name: quota_exhaustion_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_exhaustion_events_oplog (
    quota_definition_sid integer,
    occurrence_timestamp timestamp without time zone,
    exhaustion_date date,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_exhaustion_events_oplog OWNER TO postgres;

--
-- Name: quota_exhaustion_events; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_exhaustion_events AS
 SELECT quota_definition_sid,
    occurrence_timestamp,
    exhaustion_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_exhaustion_events_oplog quota_exhaustion_events1
  WHERE ((oid IN ( SELECT max(quota_exhaustion_events2.oid) AS max
           FROM uk.quota_exhaustion_events_oplog quota_exhaustion_events2
          WHERE ((quota_exhaustion_events1.quota_definition_sid = quota_exhaustion_events2.quota_definition_sid) AND (quota_exhaustion_events1.occurrence_timestamp = quota_exhaustion_events2.occurrence_timestamp)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_exhaustion_events OWNER TO postgres;

--
-- Name: quota_exhaustion_events_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_exhaustion_events_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_exhaustion_events_oid_seq OWNER TO postgres;

--
-- Name: quota_exhaustion_events_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_exhaustion_events_oid_seq OWNED BY uk.quota_exhaustion_events_oplog.oid;


--
-- Name: quota_order_number_origin_exclusions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_order_number_origin_exclusions_oplog (
    quota_order_number_origin_sid integer,
    excluded_geographical_area_sid integer,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_order_number_origin_exclusions_oplog OWNER TO postgres;

--
-- Name: quota_order_number_origin_exclusions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_order_number_origin_exclusions AS
 SELECT quota_order_number_origin_sid,
    excluded_geographical_area_sid,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_order_number_origin_exclusions_oplog quota_order_number_origin_exclusions1
  WHERE ((oid IN ( SELECT max(quota_order_number_origin_exclusions2.oid) AS max
           FROM uk.quota_order_number_origin_exclusions_oplog quota_order_number_origin_exclusions2
          WHERE ((quota_order_number_origin_exclusions1.quota_order_number_origin_sid = quota_order_number_origin_exclusions2.quota_order_number_origin_sid) AND (quota_order_number_origin_exclusions1.excluded_geographical_area_sid = quota_order_number_origin_exclusions2.excluded_geographical_area_sid)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_order_number_origin_exclusions OWNER TO postgres;

--
-- Name: quota_order_number_origin_exclusions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_order_number_origin_exclusions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_order_number_origin_exclusions_oid_seq OWNER TO postgres;

--
-- Name: quota_order_number_origin_exclusions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_order_number_origin_exclusions_oid_seq OWNED BY uk.quota_order_number_origin_exclusions_oplog.oid;


--
-- Name: quota_order_number_origins_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_order_number_origins_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_order_number_origins_oid_seq OWNER TO postgres;

--
-- Name: quota_order_number_origins_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_order_number_origins_oid_seq OWNED BY uk.quota_order_number_origins_oplog.oid;


--
-- Name: quota_order_numbers_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_order_numbers_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_order_numbers_oid_seq OWNER TO postgres;

--
-- Name: quota_order_numbers_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_order_numbers_oid_seq OWNED BY uk.quota_order_numbers_oplog.oid;


--
-- Name: quota_reopening_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_reopening_events_oplog (
    quota_definition_sid integer,
    occurrence_timestamp timestamp without time zone,
    reopening_date date,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_reopening_events_oplog OWNER TO postgres;

--
-- Name: quota_reopening_events; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_reopening_events AS
 SELECT quota_definition_sid,
    occurrence_timestamp,
    reopening_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_reopening_events_oplog quota_reopening_events1
  WHERE ((oid IN ( SELECT max(quota_reopening_events2.oid) AS max
           FROM uk.quota_reopening_events_oplog quota_reopening_events2
          WHERE ((quota_reopening_events1.quota_definition_sid = quota_reopening_events2.quota_definition_sid) AND (quota_reopening_events1.occurrence_timestamp = quota_reopening_events2.occurrence_timestamp)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_reopening_events OWNER TO postgres;

--
-- Name: quota_reopening_events_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_reopening_events_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_reopening_events_oid_seq OWNER TO postgres;

--
-- Name: quota_reopening_events_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_reopening_events_oid_seq OWNED BY uk.quota_reopening_events_oplog.oid;


--
-- Name: quota_suspension_periods_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_suspension_periods_oplog (
    quota_suspension_period_sid integer,
    quota_definition_sid integer,
    suspension_start_date date,
    suspension_end_date date,
    description text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_suspension_periods_oplog OWNER TO postgres;

--
-- Name: quota_suspension_periods; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.quota_suspension_periods AS
 SELECT quota_suspension_period_sid,
    quota_definition_sid,
    suspension_start_date,
    suspension_end_date,
    description,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_suspension_periods_oplog quota_suspension_periods1
  WHERE ((oid IN ( SELECT max(quota_suspension_periods2.oid) AS max
           FROM uk.quota_suspension_periods_oplog quota_suspension_periods2
          WHERE (quota_suspension_periods1.quota_suspension_period_sid = quota_suspension_periods2.quota_suspension_period_sid))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.quota_suspension_periods OWNER TO postgres;

--
-- Name: quota_suspension_periods_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_suspension_periods_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_suspension_periods_oid_seq OWNER TO postgres;

--
-- Name: quota_suspension_periods_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_suspension_periods_oid_seq OWNED BY uk.quota_suspension_periods_oplog.oid;


--
-- Name: quota_unblocking_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_unblocking_events_oplog (
    quota_definition_sid integer,
    occurrence_timestamp timestamp without time zone,
    unblocking_date date,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_unblocking_events_oplog OWNER TO postgres;

--
-- Name: quota_unblocking_events; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_unblocking_events AS
 SELECT quota_definition_sid,
    occurrence_timestamp,
    unblocking_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_unblocking_events_oplog quota_unblocking_events1
  WHERE ((oid IN ( SELECT max(quota_unblocking_events2.oid) AS max
           FROM uk.quota_unblocking_events_oplog quota_unblocking_events2
          WHERE (quota_unblocking_events1.quota_definition_sid = quota_unblocking_events2.quota_definition_sid))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_unblocking_events OWNER TO postgres;

--
-- Name: quota_unblocking_events_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_unblocking_events_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_unblocking_events_oid_seq OWNER TO postgres;

--
-- Name: quota_unblocking_events_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_unblocking_events_oid_seq OWNED BY uk.quota_unblocking_events_oplog.oid;


--
-- Name: quota_unsuspension_events_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.quota_unsuspension_events_oplog (
    quota_definition_sid integer,
    occurrence_timestamp timestamp without time zone,
    unsuspension_date date,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.quota_unsuspension_events_oplog OWNER TO postgres;

--
-- Name: quota_unsuspension_events; Type: MATERIALIZED VIEW; Schema: uk; Owner: postgres
--

CREATE MATERIALIZED VIEW uk.quota_unsuspension_events AS
 SELECT quota_definition_sid,
    occurrence_timestamp,
    unsuspension_date,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.quota_unsuspension_events_oplog quota_unsuspension_events1
  WHERE ((oid IN ( SELECT max(quota_unsuspension_events2.oid) AS max
           FROM uk.quota_unsuspension_events_oplog quota_unsuspension_events2
          WHERE ((quota_unsuspension_events1.quota_definition_sid = quota_unsuspension_events2.quota_definition_sid) AND (quota_unsuspension_events1.occurrence_timestamp = quota_unsuspension_events2.occurrence_timestamp)))) AND ((operation)::text <> 'D'::text))
  WITH NO DATA;


ALTER MATERIALIZED VIEW uk.quota_unsuspension_events OWNER TO postgres;

--
-- Name: quota_unsuspension_events_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.quota_unsuspension_events_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.quota_unsuspension_events_oid_seq OWNER TO postgres;

--
-- Name: quota_unsuspension_events_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.quota_unsuspension_events_oid_seq OWNED BY uk.quota_unsuspension_events_oplog.oid;


--
-- Name: regulation_group_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.regulation_group_descriptions_oplog (
    regulation_group_id character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.regulation_group_descriptions_oplog OWNER TO postgres;

--
-- Name: regulation_group_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.regulation_group_descriptions AS
 SELECT regulation_group_id,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.regulation_group_descriptions_oplog regulation_group_descriptions1
  WHERE ((oid IN ( SELECT max(regulation_group_descriptions2.oid) AS max
           FROM uk.regulation_group_descriptions_oplog regulation_group_descriptions2
          WHERE ((regulation_group_descriptions1.regulation_group_id)::text = (regulation_group_descriptions2.regulation_group_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.regulation_group_descriptions OWNER TO postgres;

--
-- Name: regulation_group_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.regulation_group_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.regulation_group_descriptions_oid_seq OWNER TO postgres;

--
-- Name: regulation_group_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.regulation_group_descriptions_oid_seq OWNED BY uk.regulation_group_descriptions_oplog.oid;


--
-- Name: regulation_groups_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.regulation_groups_oplog (
    regulation_group_id character varying(255),
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.regulation_groups_oplog OWNER TO postgres;

--
-- Name: regulation_groups; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.regulation_groups AS
 SELECT regulation_group_id,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.regulation_groups_oplog regulation_groups1
  WHERE ((oid IN ( SELECT max(regulation_groups2.oid) AS max
           FROM uk.regulation_groups_oplog regulation_groups2
          WHERE ((regulation_groups1.regulation_group_id)::text = (regulation_groups2.regulation_group_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.regulation_groups OWNER TO postgres;

--
-- Name: regulation_groups_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.regulation_groups_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.regulation_groups_oid_seq OWNER TO postgres;

--
-- Name: regulation_groups_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.regulation_groups_oid_seq OWNED BY uk.regulation_groups_oplog.oid;


--
-- Name: regulation_replacements_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.regulation_replacements_oplog (
    geographical_area_id character varying(255),
    chapter_heading character varying(255),
    replacing_regulation_role integer,
    replacing_regulation_id character varying(255),
    replaced_regulation_role integer,
    replaced_regulation_id character varying(255),
    measure_type_id character varying(6),
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.regulation_replacements_oplog OWNER TO postgres;

--
-- Name: regulation_replacements; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.regulation_replacements AS
 SELECT geographical_area_id,
    chapter_heading,
    replacing_regulation_role,
    replacing_regulation_id,
    replaced_regulation_role,
    replaced_regulation_id,
    measure_type_id,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.regulation_replacements_oplog regulation_replacements1
  WHERE ((oid IN ( SELECT max(regulation_replacements2.oid) AS max
           FROM uk.regulation_replacements_oplog regulation_replacements2
          WHERE (((regulation_replacements1.replacing_regulation_id)::text = (regulation_replacements2.replacing_regulation_id)::text) AND (regulation_replacements1.replacing_regulation_role = regulation_replacements2.replacing_regulation_role) AND ((regulation_replacements1.replaced_regulation_id)::text = (regulation_replacements2.replaced_regulation_id)::text) AND (regulation_replacements1.replaced_regulation_role = regulation_replacements2.replaced_regulation_role)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.regulation_replacements OWNER TO postgres;

--
-- Name: regulation_replacements_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.regulation_replacements_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.regulation_replacements_oid_seq OWNER TO postgres;

--
-- Name: regulation_replacements_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.regulation_replacements_oid_seq OWNED BY uk.regulation_replacements_oplog.oid;


--
-- Name: regulation_role_type_descriptions_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.regulation_role_type_descriptions_oplog (
    regulation_role_type_id character varying(255),
    language_id character varying(5),
    description text,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.regulation_role_type_descriptions_oplog OWNER TO postgres;

--
-- Name: regulation_role_type_descriptions; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.regulation_role_type_descriptions AS
 SELECT regulation_role_type_id,
    language_id,
    description,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.regulation_role_type_descriptions_oplog regulation_role_type_descriptions1
  WHERE ((oid IN ( SELECT max(regulation_role_type_descriptions2.oid) AS max
           FROM uk.regulation_role_type_descriptions_oplog regulation_role_type_descriptions2
          WHERE ((regulation_role_type_descriptions1.regulation_role_type_id)::text = (regulation_role_type_descriptions2.regulation_role_type_id)::text))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.regulation_role_type_descriptions OWNER TO postgres;

--
-- Name: regulation_role_type_descriptions_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.regulation_role_type_descriptions_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.regulation_role_type_descriptions_oid_seq OWNER TO postgres;

--
-- Name: regulation_role_type_descriptions_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.regulation_role_type_descriptions_oid_seq OWNED BY uk.regulation_role_type_descriptions_oplog.oid;


--
-- Name: regulation_role_types_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.regulation_role_types_oplog (
    regulation_role_type_id integer,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    created_at timestamp without time zone,
    "national" boolean,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.regulation_role_types_oplog OWNER TO postgres;

--
-- Name: regulation_role_types; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.regulation_role_types AS
 SELECT regulation_role_type_id,
    validity_start_date,
    validity_end_date,
    "national",
    oid,
    operation,
    operation_date,
    filename
   FROM uk.regulation_role_types_oplog regulation_role_types1
  WHERE ((oid IN ( SELECT max(regulation_role_types2.oid) AS max
           FROM uk.regulation_role_types_oplog regulation_role_types2
          WHERE (regulation_role_types1.regulation_role_type_id = regulation_role_types2.regulation_role_type_id))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.regulation_role_types OWNER TO postgres;

--
-- Name: regulation_role_types_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.regulation_role_types_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.regulation_role_types_oid_seq OWNER TO postgres;

--
-- Name: regulation_role_types_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.regulation_role_types_oid_seq OWNED BY uk.regulation_role_types_oplog.oid;


--
-- Name: rollbacks; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.rollbacks (
    id integer NOT NULL,
    date date,
    enqueued_at timestamp without time zone,
    reason text,
    keep boolean,
    whodunnit text
);


ALTER TABLE uk.rollbacks OWNER TO postgres;

--
-- Name: rollbacks_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.rollbacks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.rollbacks_id_seq OWNER TO postgres;

--
-- Name: rollbacks_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.rollbacks_id_seq OWNED BY uk.rollbacks.id;


--
-- Name: schema_migrations; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.schema_migrations (
    filename text NOT NULL
);


ALTER TABLE uk.schema_migrations OWNER TO postgres;

--
-- Name: search_references; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.search_references (
    id integer NOT NULL,
    title text,
    referenced_class character varying(10),
    productline_suffix text DEFAULT '80'::text NOT NULL,
    goods_nomenclature_sid integer,
    goods_nomenclature_item_id text
);


ALTER TABLE uk.search_references OWNER TO postgres;

--
-- Name: search_references_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.search_references_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.search_references_id_seq OWNER TO postgres;

--
-- Name: search_references_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.search_references_id_seq OWNED BY uk.search_references.id;


--
-- Name: search_suggestions; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.search_suggestions (
    id text NOT NULL,
    value text NOT NULL,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    type text NOT NULL,
    priority integer,
    goods_nomenclature_sid integer,
    goods_nomenclature_class text,
    declarable boolean DEFAULT false NOT NULL
);


ALTER TABLE uk.search_suggestions OWNER TO postgres;

--
-- Name: section_notes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.section_notes (
    id integer NOT NULL,
    section_id integer,
    content text
);


ALTER TABLE uk.section_notes OWNER TO postgres;

--
-- Name: section_notes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.section_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.section_notes_id_seq OWNER TO postgres;

--
-- Name: section_notes_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.section_notes_id_seq OWNED BY uk.section_notes.id;


--
-- Name: sections; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.sections (
    id integer NOT NULL,
    "position" integer,
    numeral character varying(255),
    title character varying(500),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone
);


ALTER TABLE uk.sections OWNER TO postgres;

--
-- Name: sections_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.sections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.sections_id_seq OWNER TO postgres;

--
-- Name: sections_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.sections_id_seq OWNED BY uk.sections.id;


--
-- Name: simplified_procedural_codes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.simplified_procedural_codes (
    simplified_procedural_code text NOT NULL,
    goods_nomenclature_item_id text NOT NULL,
    goods_nomenclature_label text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE uk.simplified_procedural_codes OWNER TO postgres;

--
-- Name: simplified_procedural_code_measures; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.simplified_procedural_code_measures AS
 SELECT simplified_procedural_codes.simplified_procedural_code,
    measures.validity_start_date,
    measures.validity_end_date,
    string_agg(DISTINCT simplified_procedural_codes.goods_nomenclature_item_id, ', '::text) AS goods_nomenclature_item_ids,
    max(measure_components.duty_amount) AS duty_amount,
    max((measure_components.monetary_unit_code)::text) AS monetary_unit_code,
    max((measure_components.measurement_unit_code)::text) AS measurement_unit_code,
    max((measure_components.measurement_unit_qualifier_code)::text) AS measurement_unit_qualifier_code,
    max(simplified_procedural_codes.goods_nomenclature_label) AS goods_nomenclature_label
   FROM ((uk.measures
     JOIN uk.measure_components ON ((measures.measure_sid = measure_components.measure_sid)))
     RIGHT JOIN uk.simplified_procedural_codes ON ((((measures.goods_nomenclature_item_id)::text = simplified_procedural_codes.goods_nomenclature_item_id) AND ((measures.measure_type_id)::text = '488'::text) AND (measures.validity_end_date > '2021-01-01 00:00:00'::timestamp without time zone) AND ((measures.geographical_area_id)::text = '1011'::text))))
  GROUP BY simplified_procedural_codes.simplified_procedural_code, measures.validity_start_date, measures.validity_end_date;


ALTER VIEW uk.simplified_procedural_code_measures OWNER TO postgres;

--
-- Name: tariff_changes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.tariff_changes (
    id integer NOT NULL,
    type text NOT NULL,
    object_sid integer NOT NULL,
    goods_nomenclature_sid integer NOT NULL,
    goods_nomenclature_item_id character varying(10) NOT NULL,
    action text NOT NULL,
    operation_date date NOT NULL,
    date_of_effect date NOT NULL,
    validity_start_date timestamp without time zone,
    validity_end_date timestamp without time zone,
    updated_at timestamp without time zone,
    created_at timestamp without time zone,
    metadata jsonb DEFAULT '{}'::jsonb
);


ALTER TABLE uk.tariff_changes OWNER TO postgres;

--
-- Name: tariff_changes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.tariff_changes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.tariff_changes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: tariff_update_presence_errors; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.tariff_update_presence_errors (
    id integer NOT NULL,
    tariff_update_filename text NOT NULL,
    model_name text NOT NULL,
    details jsonb
);


ALTER TABLE uk.tariff_update_presence_errors OWNER TO postgres;

--
-- Name: tariff_update_presence_errors_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.tariff_update_presence_errors_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.tariff_update_presence_errors_id_seq OWNER TO postgres;

--
-- Name: tariff_update_presence_errors_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.tariff_update_presence_errors_id_seq OWNED BY uk.tariff_update_presence_errors.id;


--
-- Name: tariff_update_state_changes; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.tariff_update_state_changes (
    id integer NOT NULL,
    tariff_update_filename text NOT NULL,
    from_state character varying(1),
    to_state character varying(1) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.tariff_update_state_changes OWNER TO postgres;

--
-- Name: tariff_update_state_changes_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.tariff_update_state_changes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.tariff_update_state_changes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: tariff_updates; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.tariff_updates (
    filename character varying(255) NOT NULL,
    update_type character varying(50),
    state character varying(1),
    issue_date date,
    updated_at timestamp without time zone,
    created_at timestamp without time zone,
    filesize integer,
    applied_at timestamp without time zone,
    last_error text,
    last_error_at timestamp without time zone,
    exception_backtrace text,
    exception_queries text,
    exception_class text,
    inserts text,
    report_sent boolean DEFAULT false NOT NULL
);


ALTER TABLE uk.tariff_updates OWNER TO postgres;

--
-- Name: transmission_comments_oplog; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.transmission_comments_oplog (
    comment_sid integer,
    language_id character varying(5),
    comment_text text,
    created_at timestamp without time zone,
    oid integer NOT NULL,
    operation character varying(1) DEFAULT 'C'::character varying,
    operation_date date,
    filename text
);


ALTER TABLE uk.transmission_comments_oplog OWNER TO postgres;

--
-- Name: transmission_comments; Type: VIEW; Schema: uk; Owner: postgres
--

CREATE VIEW uk.transmission_comments AS
 SELECT comment_sid,
    language_id,
    comment_text,
    oid,
    operation,
    operation_date,
    filename
   FROM uk.transmission_comments_oplog transmission_comments1
  WHERE ((oid IN ( SELECT max(transmission_comments2.oid) AS max
           FROM uk.transmission_comments_oplog transmission_comments2
          WHERE ((transmission_comments1.comment_sid = transmission_comments2.comment_sid) AND ((transmission_comments1.language_id)::text = (transmission_comments2.language_id)::text)))) AND ((operation)::text <> 'D'::text));


ALTER VIEW uk.transmission_comments OWNER TO postgres;

--
-- Name: transmission_comments_oid_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.transmission_comments_oid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.transmission_comments_oid_seq OWNER TO postgres;

--
-- Name: transmission_comments_oid_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.transmission_comments_oid_seq OWNED BY uk.transmission_comments_oplog.oid;


--
-- Name: users; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.users (
    id integer NOT NULL,
    uid text,
    name text,
    email text,
    version integer,
    permissions text,
    remotely_signed_out boolean,
    updated_at timestamp without time zone,
    created_at timestamp without time zone,
    organisation_slug text,
    disabled boolean DEFAULT false,
    organisation_content_id text
);


ALTER TABLE uk.users OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

CREATE SEQUENCE uk.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE uk.users_id_seq OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: uk; Owner: postgres
--

ALTER SEQUENCE uk.users_id_seq OWNED BY uk.users.id;


--
-- Name: versions; Type: TABLE; Schema: uk; Owner: postgres
--

CREATE TABLE uk.versions (
    id integer NOT NULL,
    item_type text NOT NULL,
    item_id text NOT NULL,
    event text NOT NULL,
    object jsonb NOT NULL,
    whodunnit text,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE uk.versions OWNER TO postgres;

--
-- Name: versions_id_seq; Type: SEQUENCE; Schema: uk; Owner: postgres
--

ALTER TABLE uk.versions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME uk.versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: audit_log id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.audit_log ALTER COLUMN id SET DEFAULT nextval('kg.audit_log_id_seq'::regclass);


--
-- Name: classify_runs id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.classify_runs ALTER COLUMN id SET DEFAULT nextval('kg.classify_runs_id_seq'::regclass);


--
-- Name: commodity_facets id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_facets ALTER COLUMN id SET DEFAULT nextval('kg.commodity_facets_id_seq'::regclass);


--
-- Name: commodity_fact_model_eval id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_fact_model_eval ALTER COLUMN id SET DEFAULT nextval('kg.commodity_fact_model_eval_id_seq'::regclass);


--
-- Name: commodity_llm_facts id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_llm_facts ALTER COLUMN id SET DEFAULT nextval('kg.commodity_llm_facts_id_seq'::regclass);


--
-- Name: e2e_eval_results id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.e2e_eval_results ALTER COLUMN id SET DEFAULT nextval('kg.e2e_eval_results_id_seq'::regclass);


--
-- Name: e2e_eval_runs id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.e2e_eval_runs ALTER COLUMN id SET DEFAULT nextval('kg.e2e_eval_runs_id_seq'::regclass);


--
-- Name: eval_gold id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_gold ALTER COLUMN id SET DEFAULT nextval('kg.eval_gold_id_seq'::regclass);


--
-- Name: eval_run_results id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_run_results ALTER COLUMN id SET DEFAULT nextval('kg.eval_run_results_id_seq'::regclass);


--
-- Name: eval_runs id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_runs ALTER COLUMN id SET DEFAULT nextval('kg.eval_runs_id_seq'::regclass);


--
-- Name: exp6_qa_runs id; Type: DEFAULT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.exp6_qa_runs ALTER COLUMN id SET DEFAULT nextval('kg.exp6_qa_runs_id_seq'::regclass);


--
-- Name: additional_code_description_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_description_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.additional_code_description_periods_oid_seq'::regclass);


--
-- Name: additional_code_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.additional_code_descriptions_oid_seq'::regclass);


--
-- Name: additional_code_type_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_type_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.additional_code_type_descriptions_oid_seq'::regclass);


--
-- Name: additional_code_type_measure_types_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_type_measure_types_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.additional_code_type_measure_types_oid_seq'::regclass);


--
-- Name: additional_code_types_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_types_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.additional_code_types_oid_seq'::regclass);


--
-- Name: additional_codes_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_codes_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.additional_codes_oid_seq'::regclass);


--
-- Name: audits id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.audits ALTER COLUMN id SET DEFAULT nextval('uk.audits_id_seq'::regclass);


--
-- Name: base_regulations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.base_regulations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.base_regulations_oid_seq'::regclass);


--
-- Name: certificate_description_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_description_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.certificate_description_periods_oid_seq'::regclass);


--
-- Name: certificate_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.certificate_descriptions_oid_seq'::regclass);


--
-- Name: certificate_type_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_type_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.certificate_type_descriptions_oid_seq'::regclass);


--
-- Name: certificate_types_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_types_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.certificate_types_oid_seq'::regclass);


--
-- Name: certificates_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificates_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.certificates_oid_seq'::regclass);


--
-- Name: chapter_notes id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chapter_notes ALTER COLUMN id SET DEFAULT nextval('uk.chapter_notes_id_seq'::regclass);


--
-- Name: chapters_guides id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chapters_guides ALTER COLUMN id SET DEFAULT nextval('uk.chapters_guides_id_seq'::regclass);


--
-- Name: complete_abrogation_regulations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.complete_abrogation_regulations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.complete_abrogation_regulations_oid_seq'::regclass);


--
-- Name: duty_expression_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.duty_expression_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.duty_expression_descriptions_oid_seq'::regclass);


--
-- Name: duty_expressions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.duty_expressions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.duty_expressions_oid_seq'::regclass);


--
-- Name: explicit_abrogation_regulations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.explicit_abrogation_regulations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.explicit_abrogation_regulations_oid_seq'::regclass);


--
-- Name: export_refund_nomenclature_description_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclature_description_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.export_refund_nomenclature_description_periods_oid_seq'::regclass);


--
-- Name: export_refund_nomenclature_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclature_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.export_refund_nomenclature_descriptions_oid_seq'::regclass);


--
-- Name: export_refund_nomenclature_indents_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclature_indents_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.export_refund_nomenclature_indents_oid_seq'::regclass);


--
-- Name: export_refund_nomenclatures_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclatures_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.export_refund_nomenclatures_oid_seq'::regclass);


--
-- Name: footnote_association_additional_codes_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_additional_codes_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_association_additional_codes_oid_seq'::regclass);


--
-- Name: footnote_association_erns_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_erns_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_association_erns_oid_seq'::regclass);


--
-- Name: footnote_association_goods_nomenclatures_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_goods_nomenclatures_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_association_goods_nomenclatures_oid_seq'::regclass);


--
-- Name: footnote_association_measures_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_measures_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_association_measures_oid_seq'::regclass);


--
-- Name: footnote_association_meursing_headings_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_meursing_headings_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_association_meursing_headings_oid_seq'::regclass);


--
-- Name: footnote_description_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_description_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_description_periods_oid_seq'::regclass);


--
-- Name: footnote_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_descriptions_oid_seq'::regclass);


--
-- Name: footnote_type_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_type_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_type_descriptions_oid_seq'::regclass);


--
-- Name: footnote_types_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_types_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnote_types_oid_seq'::regclass);


--
-- Name: footnotes_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnotes_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.footnotes_oid_seq'::regclass);


--
-- Name: fts_regulation_actions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.fts_regulation_actions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.fts_regulation_actions_oid_seq'::regclass);


--
-- Name: full_temporary_stop_regulations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.full_temporary_stop_regulations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.full_temporary_stop_regulations_oid_seq'::regclass);


--
-- Name: geographical_area_description_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_area_description_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.geographical_area_description_periods_oid_seq'::regclass);


--
-- Name: geographical_area_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_area_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.geographical_area_descriptions_oid_seq'::regclass);


--
-- Name: geographical_area_memberships_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_area_memberships_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.geographical_area_memberships_oid_seq'::regclass);


--
-- Name: geographical_areas_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_areas_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.geographical_areas_oid_seq'::regclass);


--
-- Name: goods_nomenclature_description_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_description_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_description_periods_oid_seq'::regclass);


--
-- Name: goods_nomenclature_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_descriptions_oid_seq'::regclass);


--
-- Name: goods_nomenclature_group_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_group_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_group_descriptions_oid_seq'::regclass);


--
-- Name: goods_nomenclature_groups_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_groups_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_groups_oid_seq'::regclass);


--
-- Name: goods_nomenclature_indents_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_indents_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_indents_oid_seq'::regclass);


--
-- Name: goods_nomenclature_origins_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_origins_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_origins_oid_seq'::regclass);


--
-- Name: goods_nomenclature_successors_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_successors_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclature_successors_oid_seq'::regclass);


--
-- Name: goods_nomenclatures_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclatures_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.goods_nomenclatures_oid_seq'::regclass);


--
-- Name: guides id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.guides ALTER COLUMN id SET DEFAULT nextval('uk.guides_id_seq'::regclass);


--
-- Name: language_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.language_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.language_descriptions_oid_seq'::regclass);


--
-- Name: languages_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.languages_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.languages_oid_seq'::regclass);


--
-- Name: measure_action_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_action_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_action_descriptions_oid_seq'::regclass);


--
-- Name: measure_actions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_actions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_actions_oid_seq'::regclass);


--
-- Name: measure_components_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_components_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_components_oid_seq'::regclass);


--
-- Name: measure_condition_code_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_condition_code_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_condition_code_descriptions_oid_seq'::regclass);


--
-- Name: measure_condition_codes_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_condition_codes_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_condition_codes_oid_seq'::regclass);


--
-- Name: measure_condition_components_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_condition_components_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_condition_components_oid_seq'::regclass);


--
-- Name: measure_conditions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_conditions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_conditions_oid_seq'::regclass);


--
-- Name: measure_excluded_geographical_areas_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_excluded_geographical_areas_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_excluded_geographical_areas_oid_seq'::regclass);


--
-- Name: measure_partial_temporary_stops_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_partial_temporary_stops_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_partial_temporary_stops_oid_seq'::regclass);


--
-- Name: measure_type_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_type_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_type_descriptions_oid_seq'::regclass);


--
-- Name: measure_type_series_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_type_series_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_type_series_descriptions_oid_seq'::regclass);


--
-- Name: measure_type_series_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_type_series_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_type_series_oid_seq'::regclass);


--
-- Name: measure_types_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_types_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measure_types_oid_seq'::regclass);


--
-- Name: measurement_unit_abbreviations id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_abbreviations ALTER COLUMN id SET DEFAULT nextval('uk.measurement_unit_abbreviations_id_seq'::regclass);


--
-- Name: measurement_unit_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measurement_unit_descriptions_oid_seq'::regclass);


--
-- Name: measurement_unit_qualifier_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_qualifier_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measurement_unit_qualifier_descriptions_oid_seq'::regclass);


--
-- Name: measurement_unit_qualifiers_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_qualifiers_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measurement_unit_qualifiers_oid_seq'::regclass);


--
-- Name: measurement_units_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_units_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measurement_units_oid_seq'::regclass);


--
-- Name: measurements_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurements_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measurements_oid_seq'::regclass);


--
-- Name: measures_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measures_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.measures_oid_seq'::regclass);


--
-- Name: meursing_additional_codes_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_additional_codes_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.meursing_additional_codes_oid_seq'::regclass);


--
-- Name: meursing_heading_texts_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_heading_texts_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.meursing_heading_texts_oid_seq'::regclass);


--
-- Name: meursing_headings_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_headings_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.meursing_headings_oid_seq'::regclass);


--
-- Name: meursing_subheadings_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_subheadings_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.meursing_subheadings_oid_seq'::regclass);


--
-- Name: meursing_table_cell_components_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_table_cell_components_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.meursing_table_cell_components_oid_seq'::regclass);


--
-- Name: meursing_table_plans_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_table_plans_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.meursing_table_plans_oid_seq'::regclass);


--
-- Name: modification_regulations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.modification_regulations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.modification_regulations_oid_seq'::regclass);


--
-- Name: monetary_exchange_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_exchange_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.monetary_exchange_periods_oid_seq'::regclass);


--
-- Name: monetary_exchange_rates_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_exchange_rates_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.monetary_exchange_rates_oid_seq'::regclass);


--
-- Name: monetary_unit_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_unit_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.monetary_unit_descriptions_oid_seq'::regclass);


--
-- Name: monetary_units_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_units_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.monetary_units_oid_seq'::regclass);


--
-- Name: nomenclature_group_memberships_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.nomenclature_group_memberships_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.nomenclature_group_memberships_oid_seq'::regclass);


--
-- Name: prorogation_regulation_actions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.prorogation_regulation_actions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.prorogation_regulation_actions_oid_seq'::regclass);


--
-- Name: prorogation_regulations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.prorogation_regulations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.prorogation_regulations_oid_seq'::regclass);


--
-- Name: publication_sigles_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.publication_sigles_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.publication_sigles_oplog_oid_seq'::regclass);


--
-- Name: quota_associations_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_associations_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_associations_oid_seq'::regclass);


--
-- Name: quota_balance_events_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_balance_events_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_balance_events_oid_seq'::regclass);


--
-- Name: quota_blocking_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_blocking_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_blocking_periods_oid_seq'::regclass);


--
-- Name: quota_critical_events_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_critical_events_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_critical_events_oid_seq'::regclass);


--
-- Name: quota_definitions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_definitions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_definitions_oid_seq'::regclass);


--
-- Name: quota_exhaustion_events_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_exhaustion_events_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_exhaustion_events_oid_seq'::regclass);


--
-- Name: quota_order_number_origin_exclusions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_order_number_origin_exclusions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_order_number_origin_exclusions_oid_seq'::regclass);


--
-- Name: quota_order_number_origins_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_order_number_origins_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_order_number_origins_oid_seq'::regclass);


--
-- Name: quota_order_numbers_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_order_numbers_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_order_numbers_oid_seq'::regclass);


--
-- Name: quota_reopening_events_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_reopening_events_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_reopening_events_oid_seq'::regclass);


--
-- Name: quota_suspension_periods_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_suspension_periods_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_suspension_periods_oid_seq'::regclass);


--
-- Name: quota_unblocking_events_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_unblocking_events_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_unblocking_events_oid_seq'::regclass);


--
-- Name: quota_unsuspension_events_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_unsuspension_events_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.quota_unsuspension_events_oid_seq'::regclass);


--
-- Name: regulation_group_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_group_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.regulation_group_descriptions_oid_seq'::regclass);


--
-- Name: regulation_groups_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_groups_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.regulation_groups_oid_seq'::regclass);


--
-- Name: regulation_replacements_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_replacements_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.regulation_replacements_oid_seq'::regclass);


--
-- Name: regulation_role_type_descriptions_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_role_type_descriptions_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.regulation_role_type_descriptions_oid_seq'::regclass);


--
-- Name: regulation_role_types_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_role_types_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.regulation_role_types_oid_seq'::regclass);


--
-- Name: rollbacks id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.rollbacks ALTER COLUMN id SET DEFAULT nextval('uk.rollbacks_id_seq'::regclass);


--
-- Name: search_references id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.search_references ALTER COLUMN id SET DEFAULT nextval('uk.search_references_id_seq'::regclass);


--
-- Name: section_notes id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.section_notes ALTER COLUMN id SET DEFAULT nextval('uk.section_notes_id_seq'::regclass);


--
-- Name: sections id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.sections ALTER COLUMN id SET DEFAULT nextval('uk.sections_id_seq'::regclass);


--
-- Name: tariff_update_presence_errors id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.tariff_update_presence_errors ALTER COLUMN id SET DEFAULT nextval('uk.tariff_update_presence_errors_id_seq'::regclass);


--
-- Name: transmission_comments_oplog oid; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.transmission_comments_oplog ALTER COLUMN oid SET DEFAULT nextval('uk.transmission_comments_oid_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.users ALTER COLUMN id SET DEFAULT nextval('uk.users_id_seq'::regclass);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: classify_runs classify_runs_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.classify_runs
    ADD CONSTRAINT classify_runs_pkey PRIMARY KEY (id);


--
-- Name: commodity_facets commodity_facets_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_facets
    ADD CONSTRAINT commodity_facets_pkey PRIMARY KEY (id);


--
-- Name: commodity_facets commodity_facets_unique; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_facets
    ADD CONSTRAINT commodity_facets_unique UNIQUE (commodity_code, facet_key, facet_value, source);


--
-- Name: commodity_fact_model_eval commodity_fact_model_eval_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_fact_model_eval
    ADD CONSTRAINT commodity_fact_model_eval_pkey PRIMARY KEY (id);


--
-- Name: commodity_fact_model_eval commodity_fact_model_eval_run_id_prompt_version_model_commo_key; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_fact_model_eval
    ADD CONSTRAINT commodity_fact_model_eval_run_id_prompt_version_model_commo_key UNIQUE (run_id, prompt_version, model, commodity_code);


--
-- Name: commodity_fact_sheets commodity_fact_sheets_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_fact_sheets
    ADD CONSTRAINT commodity_fact_sheets_pkey PRIMARY KEY (commodity_code);


--
-- Name: commodity_hydration_cache commodity_hydration_cache_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_hydration_cache
    ADD CONSTRAINT commodity_hydration_cache_pkey PRIMARY KEY (commodity_code);


--
-- Name: commodity_llm_facts commodity_llm_facts_commodity_code_run_id_model_prompt_vers_key; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_llm_facts
    ADD CONSTRAINT commodity_llm_facts_commodity_code_run_id_model_prompt_vers_key UNIQUE (commodity_code, run_id, model, prompt_version, scope, fact_key, fact_value, label);


--
-- Name: commodity_llm_facts commodity_llm_facts_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_llm_facts
    ADD CONSTRAINT commodity_llm_facts_pkey PRIMARY KEY (id);


--
-- Name: composite_search_text composite_search_text_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.composite_search_text
    ADD CONSTRAINT composite_search_text_pkey PRIMARY KEY (goods_nomenclature_item_id);


--
-- Name: contrastive_residual_text contrastive_residual_text_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.contrastive_residual_text
    ADD CONSTRAINT contrastive_residual_text_pkey PRIMARY KEY (goods_nomenclature_item_id);


--
-- Name: e2e_eval_results e2e_eval_results_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.e2e_eval_results
    ADD CONSTRAINT e2e_eval_results_pkey PRIMARY KEY (id);


--
-- Name: e2e_eval_runs e2e_eval_runs_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.e2e_eval_runs
    ADD CONSTRAINT e2e_eval_runs_pkey PRIMARY KEY (id);


--
-- Name: eval_gold eval_gold_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_gold
    ADD CONSTRAINT eval_gold_pkey PRIMARY KEY (id);


--
-- Name: eval_gold eval_gold_source_type_source_id_persona_query_key; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_gold
    ADD CONSTRAINT eval_gold_source_type_source_id_persona_query_key UNIQUE (source_type, source_id, persona, query);


--
-- Name: eval_run_results eval_run_results_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_run_results
    ADD CONSTRAINT eval_run_results_pkey PRIMARY KEY (id);


--
-- Name: eval_runs eval_runs_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_runs
    ADD CONSTRAINT eval_runs_pkey PRIMARY KEY (id);


--
-- Name: evidence_label_definitions evidence_label_definitions_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.evidence_label_definitions
    ADD CONSTRAINT evidence_label_definitions_pkey PRIMARY KEY (label_kind, key);


--
-- Name: exp6_qa_runs exp6_qa_runs_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.exp6_qa_runs
    ADD CONSTRAINT exp6_qa_runs_pkey PRIMARY KEY (id);


--
-- Name: facet_definitions facet_definitions_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.facet_definitions
    ADD CONSTRAINT facet_definitions_pkey PRIMARY KEY (key);


--
-- Name: kg_edge_commodities kg_edge_commodities_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.kg_edge_commodities
    ADD CONSTRAINT kg_edge_commodities_pkey PRIMARY KEY (edge_id, commodity_code);


--
-- Name: kg_edges kg_edges_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.kg_edges
    ADD CONSTRAINT kg_edges_pkey PRIMARY KEY (id);


--
-- Name: measure_retrieval_docs measure_retrieval_docs_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.measure_retrieval_docs
    ADD CONSTRAINT measure_retrieval_docs_pkey PRIMARY KEY (commodity_code);


--
-- Name: query_descriptiveness query_descriptiveness_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.query_descriptiveness
    ADD CONSTRAINT query_descriptiveness_pkey PRIMARY KEY (gold_id);


--
-- Name: query_embedding_cache query_embedding_cache_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.query_embedding_cache
    ADD CONSTRAINT query_embedding_cache_pkey PRIMARY KEY (cache_key);


--
-- Name: query_qpp query_qpp_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.query_qpp
    ADD CONSTRAINT query_qpp_pkey PRIMARY KEY (gold_id);


--
-- Name: retrieval_matrix_rows retrieval_matrix_rows_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.retrieval_matrix_rows
    ADD CONSTRAINT retrieval_matrix_rows_pkey PRIMARY KEY (run_label);


--
-- Name: triage_cache triage_cache_pkey; Type: CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.triage_cache
    ADD CONSTRAINT triage_cache_pkey PRIMARY KEY (query_norm);


--
-- Name: additional_code_description_periods_oplog additional_code_description_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_description_periods_oplog
    ADD CONSTRAINT additional_code_description_periods_pkey PRIMARY KEY (oid);


--
-- Name: additional_code_descriptions_oplog additional_code_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_descriptions_oplog
    ADD CONSTRAINT additional_code_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: additional_code_type_descriptions_oplog additional_code_type_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_type_descriptions_oplog
    ADD CONSTRAINT additional_code_type_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: additional_code_type_measure_types_oplog additional_code_type_measure_types_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_type_measure_types_oplog
    ADD CONSTRAINT additional_code_type_measure_types_pkey PRIMARY KEY (oid);


--
-- Name: additional_code_types_oplog additional_code_types_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_code_types_oplog
    ADD CONSTRAINT additional_code_types_pkey PRIMARY KEY (oid);


--
-- Name: additional_codes_oplog additional_codes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.additional_codes_oplog
    ADD CONSTRAINT additional_codes_pkey PRIMARY KEY (oid);


--
-- Name: admin_configurations admin_configurations_name_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.admin_configurations
    ADD CONSTRAINT admin_configurations_name_key UNIQUE (name);


--
-- Name: admin_configurations admin_configurations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.admin_configurations
    ADD CONSTRAINT admin_configurations_pkey PRIMARY KEY (id);


--
-- Name: appendix_5as appendix_5as_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.appendix_5as
    ADD CONSTRAINT appendix_5as_pkey PRIMARY KEY (certificate_type_code, certificate_code);


--
-- Name: applies applies_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.applies
    ADD CONSTRAINT applies_pkey PRIMARY KEY (id);


--
-- Name: audits audits_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.audits
    ADD CONSTRAINT audits_pkey PRIMARY KEY (id);


--
-- Name: base_regulations_oplog base_regulations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.base_regulations_oplog
    ADD CONSTRAINT base_regulations_pkey PRIMARY KEY (oid);


--
-- Name: cds_update_notifications cds_update_notifications_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.cds_update_notifications
    ADD CONSTRAINT cds_update_notifications_pkey PRIMARY KEY (id);


--
-- Name: certificate_description_periods_oplog certificate_description_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_description_periods_oplog
    ADD CONSTRAINT certificate_description_periods_pkey PRIMARY KEY (oid);


--
-- Name: certificate_descriptions_oplog certificate_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_descriptions_oplog
    ADD CONSTRAINT certificate_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: certificate_type_descriptions_oplog certificate_type_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_type_descriptions_oplog
    ADD CONSTRAINT certificate_type_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: certificate_types_oplog certificate_types_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificate_types_oplog
    ADD CONSTRAINT certificate_types_pkey PRIMARY KEY (oid);


--
-- Name: certificates_oplog certificates_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.certificates_oplog
    ADD CONSTRAINT certificates_pkey PRIMARY KEY (oid);


--
-- Name: changes changes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.changes
    ADD CONSTRAINT changes_pkey PRIMARY KEY (id);


--
-- Name: changes changes_upsert_unique; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.changes
    ADD CONSTRAINT changes_upsert_unique UNIQUE (goods_nomenclature_sid, change_date);


--
-- Name: chapter_notes chapter_notes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chapter_notes
    ADD CONSTRAINT chapter_notes_pkey PRIMARY KEY (id);


--
-- Name: chapters_guides chapters_guides_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chapters_guides
    ADD CONSTRAINT chapters_guides_pkey PRIMARY KEY (id);


--
-- Name: chemical_names chemical_names_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chemical_names
    ADD CONSTRAINT chemical_names_pkey PRIMARY KEY (id);


--
-- Name: chemicals chemicals_cas_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chemicals
    ADD CONSTRAINT chemicals_cas_key UNIQUE (cas);


--
-- Name: chemicals_goods_nomenclatures chemicals_goods_nomenclatures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chemicals_goods_nomenclatures
    ADD CONSTRAINT chemicals_goods_nomenclatures_pkey PRIMARY KEY (chemical_id, goods_nomenclature_sid);


--
-- Name: chemicals chemicals_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chemicals
    ADD CONSTRAINT chemicals_pkey PRIMARY KEY (id);


--
-- Name: clear_caches clear_caches_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.clear_caches
    ADD CONSTRAINT clear_caches_pkey PRIMARY KEY (id);


--
-- Name: complete_abrogation_regulations_oplog complete_abrogation_regulations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.complete_abrogation_regulations_oplog
    ADD CONSTRAINT complete_abrogation_regulations_pkey PRIMARY KEY (oid);


--
-- Name: customs_tariff_chapter_notes customs_tariff_chapter_notes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.customs_tariff_chapter_notes
    ADD CONSTRAINT customs_tariff_chapter_notes_pkey PRIMARY KEY (id);


--
-- Name: customs_tariff_general_rules customs_tariff_general_rules_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.customs_tariff_general_rules
    ADD CONSTRAINT customs_tariff_general_rules_pkey PRIMARY KEY (id);


--
-- Name: customs_tariff_section_notes customs_tariff_section_notes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.customs_tariff_section_notes
    ADD CONSTRAINT customs_tariff_section_notes_pkey PRIMARY KEY (id);


--
-- Name: customs_tariff_updates customs_tariff_updates_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.customs_tariff_updates
    ADD CONSTRAINT customs_tariff_updates_pkey PRIMARY KEY (version);


--
-- Name: data_migrations data_migrations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.data_migrations
    ADD CONSTRAINT data_migrations_pkey PRIMARY KEY (filename);


--
-- Name: description_intercepts description_intercepts_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.description_intercepts
    ADD CONSTRAINT description_intercepts_pkey PRIMARY KEY (id);


--
-- Name: description_intercepts description_intercepts_term_unique; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.description_intercepts
    ADD CONSTRAINT description_intercepts_term_unique UNIQUE (term);


--
-- Name: differences_logs differences_logs_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.differences_logs
    ADD CONSTRAINT differences_logs_pkey PRIMARY KEY (id);


--
-- Name: downloads downloads_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.downloads
    ADD CONSTRAINT downloads_pkey PRIMARY KEY (id);


--
-- Name: duty_expression_descriptions_oplog duty_expression_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.duty_expression_descriptions_oplog
    ADD CONSTRAINT duty_expression_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: duty_expressions_oplog duty_expressions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.duty_expressions_oplog
    ADD CONSTRAINT duty_expressions_pkey PRIMARY KEY (oid);


--
-- Name: exchange_rate_countries_currencies exchange_rate_countries_currencies_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.exchange_rate_countries_currencies
    ADD CONSTRAINT exchange_rate_countries_currencies_pkey PRIMARY KEY (id);


--
-- Name: exchange_rate_countries exchange_rate_countries_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.exchange_rate_countries
    ADD CONSTRAINT exchange_rate_countries_pkey PRIMARY KEY (country_code);


--
-- Name: exchange_rate_currencies exchange_rate_currencies_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.exchange_rate_currencies
    ADD CONSTRAINT exchange_rate_currencies_pkey PRIMARY KEY (currency_code);


--
-- Name: exchange_rate_currency_rates exchange_rate_currency_rates_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.exchange_rate_currency_rates
    ADD CONSTRAINT exchange_rate_currency_rates_pkey PRIMARY KEY (id);


--
-- Name: exchange_rate_files exchange_rate_files_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.exchange_rate_files
    ADD CONSTRAINT exchange_rate_files_pkey PRIMARY KEY (id);


--
-- Name: explicit_abrogation_regulations_oplog explicit_abrogation_regulations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.explicit_abrogation_regulations_oplog
    ADD CONSTRAINT explicit_abrogation_regulations_pkey PRIMARY KEY (oid);


--
-- Name: export_refund_nomenclature_description_periods_oplog export_refund_nomenclature_description_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclature_description_periods_oplog
    ADD CONSTRAINT export_refund_nomenclature_description_periods_pkey PRIMARY KEY (oid);


--
-- Name: export_refund_nomenclature_descriptions_oplog export_refund_nomenclature_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclature_descriptions_oplog
    ADD CONSTRAINT export_refund_nomenclature_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: export_refund_nomenclature_indents_oplog export_refund_nomenclature_indents_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclature_indents_oplog
    ADD CONSTRAINT export_refund_nomenclature_indents_pkey PRIMARY KEY (oid);


--
-- Name: export_refund_nomenclatures_oplog export_refund_nomenclatures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.export_refund_nomenclatures_oplog
    ADD CONSTRAINT export_refund_nomenclatures_pkey PRIMARY KEY (oid);


--
-- Name: footnote_association_additional_codes_oplog footnote_association_additional_codes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_additional_codes_oplog
    ADD CONSTRAINT footnote_association_additional_codes_pkey PRIMARY KEY (oid);


--
-- Name: footnote_association_erns_oplog footnote_association_erns_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_erns_oplog
    ADD CONSTRAINT footnote_association_erns_pkey PRIMARY KEY (oid);


--
-- Name: footnote_association_goods_nomenclatures_oplog footnote_association_goods_nomenclatures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_goods_nomenclatures_oplog
    ADD CONSTRAINT footnote_association_goods_nomenclatures_pkey PRIMARY KEY (oid);


--
-- Name: footnote_association_measures_oplog footnote_association_measures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_measures_oplog
    ADD CONSTRAINT footnote_association_measures_pkey PRIMARY KEY (oid);


--
-- Name: footnote_association_meursing_headings_oplog footnote_association_meursing_headings_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_association_meursing_headings_oplog
    ADD CONSTRAINT footnote_association_meursing_headings_pkey PRIMARY KEY (oid);


--
-- Name: footnote_description_periods_oplog footnote_description_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_description_periods_oplog
    ADD CONSTRAINT footnote_description_periods_pkey PRIMARY KEY (oid);


--
-- Name: footnote_descriptions_oplog footnote_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_descriptions_oplog
    ADD CONSTRAINT footnote_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: footnote_type_descriptions_oplog footnote_type_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_type_descriptions_oplog
    ADD CONSTRAINT footnote_type_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: footnote_types_oplog footnote_types_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnote_types_oplog
    ADD CONSTRAINT footnote_types_pkey PRIMARY KEY (oid);


--
-- Name: footnotes_oplog footnotes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.footnotes_oplog
    ADD CONSTRAINT footnotes_pkey PRIMARY KEY (oid);


--
-- Name: forum_links forum_links_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.forum_links
    ADD CONSTRAINT forum_links_pkey PRIMARY KEY (id);


--
-- Name: fts_regulation_actions_oplog fts_regulation_actions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.fts_regulation_actions_oplog
    ADD CONSTRAINT fts_regulation_actions_pkey PRIMARY KEY (oid);


--
-- Name: full_chemicals full_chemicals_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.full_chemicals
    ADD CONSTRAINT full_chemicals_pkey PRIMARY KEY (cus, goods_nomenclature_sid);


--
-- Name: full_temporary_stop_regulations_oplog full_temporary_stop_regulations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.full_temporary_stop_regulations_oplog
    ADD CONSTRAINT full_temporary_stop_regulations_pkey PRIMARY KEY (oid);


--
-- Name: geographical_area_description_periods_oplog geographical_area_description_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_area_description_periods_oplog
    ADD CONSTRAINT geographical_area_description_periods_pkey PRIMARY KEY (oid);


--
-- Name: geographical_area_descriptions_oplog geographical_area_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_area_descriptions_oplog
    ADD CONSTRAINT geographical_area_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: geographical_area_memberships_oplog geographical_area_memberships_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_area_memberships_oplog
    ADD CONSTRAINT geographical_area_memberships_pkey PRIMARY KEY (oid);


--
-- Name: geographical_areas_oplog geographical_areas_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.geographical_areas_oplog
    ADD CONSTRAINT geographical_areas_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_description_periods_oplog goods_nomenclature_description_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_description_periods_oplog
    ADD CONSTRAINT goods_nomenclature_description_periods_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_descriptions_oplog goods_nomenclature_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_descriptions_oplog
    ADD CONSTRAINT goods_nomenclature_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_group_descriptions_oplog goods_nomenclature_group_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_group_descriptions_oplog
    ADD CONSTRAINT goods_nomenclature_group_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_groups_oplog goods_nomenclature_groups_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_groups_oplog
    ADD CONSTRAINT goods_nomenclature_groups_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_indents_oplog goods_nomenclature_indents_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_indents_oplog
    ADD CONSTRAINT goods_nomenclature_indents_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_intercepts goods_nomenclature_intercepts_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_intercepts
    ADD CONSTRAINT goods_nomenclature_intercepts_pkey PRIMARY KEY (goods_nomenclature_sid);


--
-- Name: goods_nomenclature_labels goods_nomenclature_labels_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_labels
    ADD CONSTRAINT goods_nomenclature_labels_pkey PRIMARY KEY (goods_nomenclature_sid);


--
-- Name: goods_nomenclature_origins_oplog goods_nomenclature_origins_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_origins_oplog
    ADD CONSTRAINT goods_nomenclature_origins_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_self_texts goods_nomenclature_self_texts_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_self_texts
    ADD CONSTRAINT goods_nomenclature_self_texts_pkey PRIMARY KEY (goods_nomenclature_sid);


--
-- Name: goods_nomenclature_successors_oplog goods_nomenclature_successors_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_successors_oplog
    ADD CONSTRAINT goods_nomenclature_successors_pkey PRIMARY KEY (oid);


--
-- Name: goods_nomenclature_tree_node_overrides goods_nomenclature_tree_node_overrides_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclature_tree_node_overrides
    ADD CONSTRAINT goods_nomenclature_tree_node_overrides_pkey PRIMARY KEY (id);


--
-- Name: goods_nomenclatures_oplog goods_nomenclatures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.goods_nomenclatures_oplog
    ADD CONSTRAINT goods_nomenclatures_pkey PRIMARY KEY (oid);


--
-- Name: green_lanes_category_assessments_exemptions green_lanes_category_assessments_exemptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_category_assessments_exemptions
    ADD CONSTRAINT green_lanes_category_assessments_exemptions_pkey PRIMARY KEY (category_assessment_id, exemption_id);


--
-- Name: green_lanes_category_assessments green_lanes_category_assessments_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_category_assessments
    ADD CONSTRAINT green_lanes_category_assessments_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_exempting_additional_code_overrides green_lanes_exempting_additional_code_overrides_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_exempting_additional_code_overrides
    ADD CONSTRAINT green_lanes_exempting_additional_code_overrides_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_exempting_certificate_overrides green_lanes_exempting_certificate_overrides_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_exempting_certificate_overrides
    ADD CONSTRAINT green_lanes_exempting_certificate_overrides_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_exemptions green_lanes_exemptions_code_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_exemptions
    ADD CONSTRAINT green_lanes_exemptions_code_key UNIQUE (code);


--
-- Name: green_lanes_exemptions green_lanes_exemptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_exemptions
    ADD CONSTRAINT green_lanes_exemptions_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_faq_feedback green_lanes_faq_feedback_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_faq_feedback
    ADD CONSTRAINT green_lanes_faq_feedback_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_identified_measure_type_category_assessments green_lanes_identified_measure_type_categor_measure_type_id_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_identified_measure_type_category_assessments
    ADD CONSTRAINT green_lanes_identified_measure_type_categor_measure_type_id_key UNIQUE (measure_type_id);


--
-- Name: green_lanes_identified_measure_type_category_assessments green_lanes_identified_measure_type_category_assessments_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_identified_measure_type_category_assessments
    ADD CONSTRAINT green_lanes_identified_measure_type_category_assessments_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_measures green_lanes_measures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_measures
    ADD CONSTRAINT green_lanes_measures_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_themes green_lanes_themes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_themes
    ADD CONSTRAINT green_lanes_themes_pkey PRIMARY KEY (id);


--
-- Name: green_lanes_update_notifications green_lanes_update_notifications_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_update_notifications
    ADD CONSTRAINT green_lanes_update_notifications_pkey PRIMARY KEY (id);


--
-- Name: guides_goods_nomenclatures guides_goods_nomenclatures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.guides_goods_nomenclatures
    ADD CONSTRAINT guides_goods_nomenclatures_pkey PRIMARY KEY (id);


--
-- Name: guides guides_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.guides
    ADD CONSTRAINT guides_pkey PRIMARY KEY (id);


--
-- Name: language_descriptions_oplog language_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.language_descriptions_oplog
    ADD CONSTRAINT language_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: languages_oplog languages_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.languages_oplog
    ADD CONSTRAINT languages_pkey PRIMARY KEY (oid);


--
-- Name: measure_action_descriptions_oplog measure_action_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_action_descriptions_oplog
    ADD CONSTRAINT measure_action_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: measure_actions_oplog measure_actions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_actions_oplog
    ADD CONSTRAINT measure_actions_pkey PRIMARY KEY (oid);


--
-- Name: measure_components_oplog measure_components_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_components_oplog
    ADD CONSTRAINT measure_components_pkey PRIMARY KEY (oid);


--
-- Name: measure_condition_code_descriptions_oplog measure_condition_code_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_condition_code_descriptions_oplog
    ADD CONSTRAINT measure_condition_code_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: measure_condition_codes_oplog measure_condition_codes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_condition_codes_oplog
    ADD CONSTRAINT measure_condition_codes_pkey PRIMARY KEY (oid);


--
-- Name: measure_condition_components_oplog measure_condition_components_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_condition_components_oplog
    ADD CONSTRAINT measure_condition_components_pkey PRIMARY KEY (oid);


--
-- Name: measure_conditions_oplog measure_conditions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_conditions_oplog
    ADD CONSTRAINT measure_conditions_pkey PRIMARY KEY (oid);


--
-- Name: measure_excluded_geographical_areas_oplog measure_excluded_geographical_areas_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_excluded_geographical_areas_oplog
    ADD CONSTRAINT measure_excluded_geographical_areas_pkey PRIMARY KEY (oid);


--
-- Name: measure_partial_temporary_stops_oplog measure_partial_temporary_stops_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_partial_temporary_stops_oplog
    ADD CONSTRAINT measure_partial_temporary_stops_pkey PRIMARY KEY (oid);


--
-- Name: measure_type_descriptions_oplog measure_type_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_type_descriptions_oplog
    ADD CONSTRAINT measure_type_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: measure_type_series_descriptions_oplog measure_type_series_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_type_series_descriptions_oplog
    ADD CONSTRAINT measure_type_series_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: measure_type_series_oplog measure_type_series_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_type_series_oplog
    ADD CONSTRAINT measure_type_series_pkey PRIMARY KEY (oid);


--
-- Name: measure_types_oplog measure_types_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measure_types_oplog
    ADD CONSTRAINT measure_types_pkey PRIMARY KEY (oid);


--
-- Name: measurement_unit_abbreviations measurement_unit_abbreviations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_abbreviations
    ADD CONSTRAINT measurement_unit_abbreviations_pkey PRIMARY KEY (id);


--
-- Name: measurement_unit_descriptions_oplog measurement_unit_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_descriptions_oplog
    ADD CONSTRAINT measurement_unit_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: measurement_unit_qualifier_descriptions_oplog measurement_unit_qualifier_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_qualifier_descriptions_oplog
    ADD CONSTRAINT measurement_unit_qualifier_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: measurement_unit_qualifiers_oplog measurement_unit_qualifiers_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_unit_qualifiers_oplog
    ADD CONSTRAINT measurement_unit_qualifiers_pkey PRIMARY KEY (oid);


--
-- Name: measurement_units_oplog measurement_units_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurement_units_oplog
    ADD CONSTRAINT measurement_units_pkey PRIMARY KEY (oid);


--
-- Name: measurements_oplog measurements_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measurements_oplog
    ADD CONSTRAINT measurements_pkey PRIMARY KEY (oid);


--
-- Name: measures_oplog measures_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.measures_oplog
    ADD CONSTRAINT measures_pkey PRIMARY KEY (oid);


--
-- Name: meursing_additional_codes_oplog meursing_additional_codes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_additional_codes_oplog
    ADD CONSTRAINT meursing_additional_codes_pkey PRIMARY KEY (oid);


--
-- Name: meursing_heading_texts_oplog meursing_heading_texts_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_heading_texts_oplog
    ADD CONSTRAINT meursing_heading_texts_pkey PRIMARY KEY (oid);


--
-- Name: meursing_headings_oplog meursing_headings_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_headings_oplog
    ADD CONSTRAINT meursing_headings_pkey PRIMARY KEY (oid);


--
-- Name: meursing_subheadings_oplog meursing_subheadings_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_subheadings_oplog
    ADD CONSTRAINT meursing_subheadings_pkey PRIMARY KEY (oid);


--
-- Name: meursing_table_cell_components_oplog meursing_table_cell_components_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_table_cell_components_oplog
    ADD CONSTRAINT meursing_table_cell_components_pkey PRIMARY KEY (oid);


--
-- Name: meursing_table_plans_oplog meursing_table_plans_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.meursing_table_plans_oplog
    ADD CONSTRAINT meursing_table_plans_pkey PRIMARY KEY (oid);


--
-- Name: modification_regulations_oplog modification_regulations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.modification_regulations_oplog
    ADD CONSTRAINT modification_regulations_pkey PRIMARY KEY (oid);


--
-- Name: monetary_exchange_periods_oplog monetary_exchange_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_exchange_periods_oplog
    ADD CONSTRAINT monetary_exchange_periods_pkey PRIMARY KEY (oid);


--
-- Name: monetary_exchange_rates_oplog monetary_exchange_rates_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_exchange_rates_oplog
    ADD CONSTRAINT monetary_exchange_rates_pkey PRIMARY KEY (oid);


--
-- Name: monetary_unit_descriptions_oplog monetary_unit_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_unit_descriptions_oplog
    ADD CONSTRAINT monetary_unit_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: monetary_units_oplog monetary_units_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.monetary_units_oplog
    ADD CONSTRAINT monetary_units_pkey PRIMARY KEY (oid);


--
-- Name: news_collections news_collections_name_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_collections
    ADD CONSTRAINT news_collections_name_key UNIQUE (name);


--
-- Name: news_collections_news_items news_collections_news_items_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_collections_news_items
    ADD CONSTRAINT news_collections_news_items_pkey PRIMARY KEY (collection_id, item_id);


--
-- Name: news_collections news_collections_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_collections
    ADD CONSTRAINT news_collections_pkey PRIMARY KEY (id);


--
-- Name: news_collections news_collections_slug_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_collections
    ADD CONSTRAINT news_collections_slug_key UNIQUE (slug);


--
-- Name: news_items news_items_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_items
    ADD CONSTRAINT news_items_pkey PRIMARY KEY (id);


--
-- Name: news_items news_items_slug_key; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_items
    ADD CONSTRAINT news_items_slug_key UNIQUE (slug);


--
-- Name: nomenclature_group_memberships_oplog nomenclature_group_memberships_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.nomenclature_group_memberships_oplog
    ADD CONSTRAINT nomenclature_group_memberships_pkey PRIMARY KEY (oid);


--
-- Name: prorogation_regulation_actions_oplog prorogation_regulation_actions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.prorogation_regulation_actions_oplog
    ADD CONSTRAINT prorogation_regulation_actions_pkey PRIMARY KEY (oid);


--
-- Name: prorogation_regulations_oplog prorogation_regulations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.prorogation_regulations_oplog
    ADD CONSTRAINT prorogation_regulations_pkey PRIMARY KEY (oid);


--
-- Name: publication_sigles_oplog publication_sigles_oplog_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.publication_sigles_oplog
    ADD CONSTRAINT publication_sigles_oplog_pkey PRIMARY KEY (oid);


--
-- Name: quota_associations_oplog quota_associations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_associations_oplog
    ADD CONSTRAINT quota_associations_pkey PRIMARY KEY (oid);


--
-- Name: quota_balance_events_oplog quota_balance_events_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_balance_events_oplog
    ADD CONSTRAINT quota_balance_events_pkey PRIMARY KEY (oid);


--
-- Name: quota_blocking_periods_oplog quota_blocking_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_blocking_periods_oplog
    ADD CONSTRAINT quota_blocking_periods_pkey PRIMARY KEY (oid);


--
-- Name: quota_closed_and_transferred_events_oplog quota_closed_and_transferred_events_oplog_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_closed_and_transferred_events_oplog
    ADD CONSTRAINT quota_closed_and_transferred_events_oplog_pkey PRIMARY KEY (oid);


--
-- Name: quota_critical_events_oplog quota_critical_events_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_critical_events_oplog
    ADD CONSTRAINT quota_critical_events_pkey PRIMARY KEY (oid);


--
-- Name: quota_definitions_oplog quota_definitions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_definitions_oplog
    ADD CONSTRAINT quota_definitions_pkey PRIMARY KEY (oid);


--
-- Name: quota_exhaustion_events_oplog quota_exhaustion_events_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_exhaustion_events_oplog
    ADD CONSTRAINT quota_exhaustion_events_pkey PRIMARY KEY (oid);


--
-- Name: quota_order_number_origin_exclusions_oplog quota_order_number_origin_exclusions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_order_number_origin_exclusions_oplog
    ADD CONSTRAINT quota_order_number_origin_exclusions_pkey PRIMARY KEY (oid);


--
-- Name: quota_order_number_origins_oplog quota_order_number_origins_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_order_number_origins_oplog
    ADD CONSTRAINT quota_order_number_origins_pkey PRIMARY KEY (oid);


--
-- Name: quota_order_numbers_oplog quota_order_numbers_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_order_numbers_oplog
    ADD CONSTRAINT quota_order_numbers_pkey PRIMARY KEY (oid);


--
-- Name: quota_reopening_events_oplog quota_reopening_events_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_reopening_events_oplog
    ADD CONSTRAINT quota_reopening_events_pkey PRIMARY KEY (oid);


--
-- Name: quota_suspension_periods_oplog quota_suspension_periods_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_suspension_periods_oplog
    ADD CONSTRAINT quota_suspension_periods_pkey PRIMARY KEY (oid);


--
-- Name: quota_unblocking_events_oplog quota_unblocking_events_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_unblocking_events_oplog
    ADD CONSTRAINT quota_unblocking_events_pkey PRIMARY KEY (oid);


--
-- Name: quota_unsuspension_events_oplog quota_unsuspension_events_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.quota_unsuspension_events_oplog
    ADD CONSTRAINT quota_unsuspension_events_pkey PRIMARY KEY (oid);


--
-- Name: regulation_group_descriptions_oplog regulation_group_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_group_descriptions_oplog
    ADD CONSTRAINT regulation_group_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: regulation_groups_oplog regulation_groups_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_groups_oplog
    ADD CONSTRAINT regulation_groups_pkey PRIMARY KEY (oid);


--
-- Name: regulation_replacements_oplog regulation_replacements_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_replacements_oplog
    ADD CONSTRAINT regulation_replacements_pkey PRIMARY KEY (oid);


--
-- Name: regulation_role_type_descriptions_oplog regulation_role_type_descriptions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_role_type_descriptions_oplog
    ADD CONSTRAINT regulation_role_type_descriptions_pkey PRIMARY KEY (oid);


--
-- Name: regulation_role_types_oplog regulation_role_types_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.regulation_role_types_oplog
    ADD CONSTRAINT regulation_role_types_pkey PRIMARY KEY (oid);


--
-- Name: rollbacks rollbacks_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.rollbacks
    ADD CONSTRAINT rollbacks_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (filename);


--
-- Name: search_references search_references_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.search_references
    ADD CONSTRAINT search_references_pkey PRIMARY KEY (id);


--
-- Name: search_suggestions search_suggestions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.search_suggestions
    ADD CONSTRAINT search_suggestions_pkey PRIMARY KEY (id, type);


--
-- Name: section_notes section_notes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.section_notes
    ADD CONSTRAINT section_notes_pkey PRIMARY KEY (id);


--
-- Name: sections sections_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.sections
    ADD CONSTRAINT sections_pkey PRIMARY KEY (id);


--
-- Name: simplified_procedural_codes simplified_procedural_codes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.simplified_procedural_codes
    ADD CONSTRAINT simplified_procedural_codes_pkey PRIMARY KEY (simplified_procedural_code, goods_nomenclature_item_id);


--
-- Name: tariff_changes tariff_changes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.tariff_changes
    ADD CONSTRAINT tariff_changes_pkey PRIMARY KEY (id);


--
-- Name: tariff_update_presence_errors tariff_update_presence_errors_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.tariff_update_presence_errors
    ADD CONSTRAINT tariff_update_presence_errors_pkey PRIMARY KEY (id);


--
-- Name: tariff_update_state_changes tariff_update_state_changes_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.tariff_update_state_changes
    ADD CONSTRAINT tariff_update_state_changes_pkey PRIMARY KEY (id);


--
-- Name: tariff_updates tariff_updates_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.tariff_updates
    ADD CONSTRAINT tariff_updates_pkey PRIMARY KEY (filename);


--
-- Name: transmission_comments_oplog transmission_comments_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.transmission_comments_oplog
    ADD CONSTRAINT transmission_comments_pkey PRIMARY KEY (oid);


--
-- Name: green_lanes_faq_feedback unique_faq_feedback; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_faq_feedback
    ADD CONSTRAINT unique_faq_feedback UNIQUE (session_id, category_id, question_id);


--
-- Name: exchange_rate_files unique_key_for_files; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.exchange_rate_files
    ADD CONSTRAINT unique_key_for_files UNIQUE (period_year, period_month, format, type);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: versions versions_pkey; Type: CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.versions
    ADD CONSTRAINT versions_pkey PRIMARY KEY (id);


--
-- Name: audit_log_by_created; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX audit_log_by_created ON kg.audit_log USING btree (created_at DESC);


--
-- Name: audit_log_by_row; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX audit_log_by_row ON kg.audit_log USING btree (table_name, row_id);


--
-- Name: classify_runs_label_persona; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX classify_runs_label_persona ON kg.classify_runs USING btree (run_label, persona);


--
-- Name: commodity_facets_by_code; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_by_code ON kg.commodity_facets USING btree (commodity_code);


--
-- Name: commodity_facets_by_key; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_by_key ON kg.commodity_facets USING btree (facet_key);


--
-- Name: commodity_facets_by_tier; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_by_tier ON kg.commodity_facets USING btree (authority_tier);


--
-- Name: commodity_facets_embedding_hnsw; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_embedding_hnsw ON kg.commodity_facets USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: commodity_facets_embedding_stale; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_embedding_stale ON kg.commodity_facets USING btree (embedding_stale) WHERE (embedding_stale = true);


--
-- Name: commodity_facets_evidence_roles_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_evidence_roles_gin ON kg.commodity_facets USING gin (evidence_roles);


--
-- Name: commodity_facets_retrieval_fts_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_retrieval_fts_gin ON kg.commodity_facets USING gin (to_tsvector('english'::regconfig, ((((facet_key || ' '::text) || facet_value) || ' '::text) || COALESCE(evidence, ''::text))));


--
-- Name: commodity_facets_source_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_source_idx ON kg.commodity_facets USING btree (source);


--
-- Name: commodity_facets_use_scopes_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_facets_use_scopes_gin ON kg.commodity_facets USING gin (use_scopes);


--
-- Name: commodity_fact_model_eval_run_model_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_fact_model_eval_run_model_idx ON kg.commodity_fact_model_eval USING btree (run_id, model);


--
-- Name: commodity_fact_model_eval_run_prompt_model_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_fact_model_eval_run_prompt_model_idx ON kg.commodity_fact_model_eval USING btree (run_id, prompt_version, model);


--
-- Name: commodity_fact_sheets_updated_at_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_fact_sheets_updated_at_idx ON kg.commodity_fact_sheets USING btree (updated_at DESC);


--
-- Name: commodity_fact_sheets_version_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_fact_sheets_version_idx ON kg.commodity_fact_sheets USING btree (fact_sheet_version);


--
-- Name: commodity_hydration_cache_updated_at_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_hydration_cache_updated_at_idx ON kg.commodity_hydration_cache USING btree (updated_at DESC);


--
-- Name: commodity_llm_facts_code_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_llm_facts_code_idx ON kg.commodity_llm_facts USING btree (commodity_code);


--
-- Name: commodity_llm_facts_qna_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_llm_facts_qna_idx ON kg.commodity_llm_facts USING btree (commodity_code, qna_usefulness DESC) WHERE ((scope = 'qna'::text) AND (status = 'active'::text));


--
-- Name: commodity_llm_facts_scope_key_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX commodity_llm_facts_scope_key_idx ON kg.commodity_llm_facts USING btree (scope, fact_key);


--
-- Name: e2e_eval_results_mode_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX e2e_eval_results_mode_idx ON kg.e2e_eval_results USING btree (question_mode, answerer);


--
-- Name: e2e_eval_results_run_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX e2e_eval_results_run_idx ON kg.e2e_eval_results USING btree (run_id);


--
-- Name: eval_gold_by_code; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX eval_gold_by_code ON kg.eval_gold USING btree (expected_code);


--
-- Name: eval_gold_by_source; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX eval_gold_by_source ON kg.eval_gold USING btree (source_type, source_id);


--
-- Name: eval_run_results_run; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX eval_run_results_run ON kg.eval_run_results USING btree (run_id);


--
-- Name: kg_edge_commodities_by_code; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edge_commodities_by_code ON kg.kg_edge_commodities USING btree (commodity_code);


--
-- Name: kg_edges_by_scope; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_by_scope ON kg.kg_edges USING btree (scope);


--
-- Name: kg_edges_by_tier; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_by_tier ON kg.kg_edges USING btree (authority_tier);


--
-- Name: kg_edges_embedding_hnsw; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_embedding_hnsw ON kg.kg_edges USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: kg_edges_embedding_stale; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_embedding_stale ON kg.kg_edges USING btree (embedding_stale) WHERE (embedding_stale = true);


--
-- Name: kg_edges_evidence_roles_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_evidence_roles_gin ON kg.kg_edges USING gin (evidence_roles);


--
-- Name: kg_edges_retrieval_fts_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_retrieval_fts_gin ON kg.kg_edges USING gin (to_tsvector('english'::regconfig, ((title || ' '::text) || body)));


--
-- Name: kg_edges_source_type_idx; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_source_type_idx ON kg.kg_edges USING btree (source, type);


--
-- Name: kg_edges_use_scopes_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX kg_edges_use_scopes_gin ON kg.kg_edges USING gin (use_scopes);


--
-- Name: measure_retrieval_docs_fts_gin; Type: INDEX; Schema: kg; Owner: postgres
--

CREATE INDEX measure_retrieval_docs_fts_gin ON kg.measure_retrieval_docs USING gin (to_tsvector('english'::regconfig, search_text));


--
-- Name: abrogation_regulation_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX abrogation_regulation_id ON uk.measure_partial_temporary_stops_oplog USING btree (abrogation_regulation_id);


--
-- Name: acdo_addcoddesopl_nalodeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX acdo_addcoddesopl_nalodeonslog_operation_date ON uk.additional_code_descriptions_oplog USING btree (operation_date);


--
-- Name: acdpo_addcoddesperopl_nalodeionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX acdpo_addcoddesperopl_nalodeionodslog_operation_date ON uk.additional_code_description_periods_oplog USING btree (operation_date);


--
-- Name: aco_addcodopl_naldeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX aco_addcodopl_naldeslog_operation_date ON uk.additional_codes_oplog USING btree (operation_date);


--
-- Name: actdo_addcodtypdesopl_nalodeypeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX actdo_addcodtypdesopl_nalodeypeonslog_operation_date ON uk.additional_code_type_descriptions_oplog USING btree (operation_date);


--
-- Name: actmto_addcodtypmeatypopl_nalodeypeurepeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX actmto_addcodtypmeatypopl_nalodeypeurepeslog_operation_date ON uk.additional_code_type_measure_types_oplog USING btree (operation_date);


--
-- Name: acto_addcodtypopl_nalodepeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX acto_addcodtypopl_nalodepeslog_operation_date ON uk.additional_code_types_oplog USING btree (operation_date);


--
-- Name: adco_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_desc_pk ON uk.additional_code_descriptions_oplog USING btree (additional_code_description_period_sid, additional_code_type_id, additional_code_sid);


--
-- Name: adco_periods_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_periods_pk ON uk.additional_code_description_periods_oplog USING btree (additional_code_description_period_sid, additional_code_sid, additional_code_type_id);


--
-- Name: adco_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_pk ON uk.additional_codes_oplog USING btree (additional_code_sid);


--
-- Name: adco_type_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_type_desc_pk ON uk.additional_code_type_descriptions_oplog USING btree (additional_code_type_id);


--
-- Name: adco_type_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_type_id ON uk.additional_codes_oplog USING btree (additional_code_type_id);


--
-- Name: adco_type_measure_type_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_type_measure_type_pk ON uk.additional_code_type_measure_types_oplog USING btree (measure_type_id, additional_code_type_id);


--
-- Name: adco_types_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX adco_types_pk ON uk.additional_code_types_oplog USING btree (additional_code_type_id);


--
-- Name: add_code_desc_description_trgm_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX add_code_desc_description_trgm_idx ON uk.additional_code_descriptions_oplog USING gist (description public.gist_trgm_ops);


--
-- Name: additional_code_description_periods_additional_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_description_periods_additional_code ON uk.additional_code_description_periods USING btree (additional_code_description_period_sid, additional_code_sid, additional_code_type_id);


--
-- Name: additional_code_description_periods_additional_code_description; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_description_periods_additional_code_description ON uk.additional_code_description_periods USING btree (additional_code_description_period_sid);


--
-- Name: additional_code_description_periods_additional_code_type_id_ind; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_description_periods_additional_code_type_id_ind ON uk.additional_code_description_periods USING btree (additional_code_type_id);


--
-- Name: additional_code_description_periods_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX additional_code_description_periods_oid_index ON uk.additional_code_description_periods USING btree (oid);


--
-- Name: additional_code_description_periods_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_description_periods_operation_date_index ON uk.additional_code_description_periods USING btree (operation_date);


--
-- Name: additional_code_descriptions_additional_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_descriptions_additional_code ON uk.additional_code_descriptions USING btree (additional_code_description_period_sid, additional_code_type_id, additional_code_sid);


--
-- Name: additional_code_descriptions_additional_code_description_period; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_descriptions_additional_code_description_period ON uk.additional_code_descriptions USING btree (additional_code_description_period_sid);


--
-- Name: additional_code_descriptions_additional_code_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_descriptions_additional_code_sid_index ON uk.additional_code_descriptions USING btree (additional_code_sid);


--
-- Name: additional_code_descriptions_additional_code_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_descriptions_additional_code_type_id_index ON uk.additional_code_descriptions USING btree (additional_code_type_id);


--
-- Name: additional_code_descriptions_language_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_descriptions_language_id_index ON uk.additional_code_descriptions USING btree (language_id);


--
-- Name: additional_code_descriptions_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX additional_code_descriptions_oid_index ON uk.additional_code_descriptions USING btree (oid);


--
-- Name: additional_code_descriptions_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_descriptions_operation_date_index ON uk.additional_code_descriptions USING btree (operation_date);


--
-- Name: additional_code_type; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_type ON uk.footnote_association_additional_codes_oplog USING btree (additional_code_type_id);


--
-- Name: additional_code_type_descriptions_additional_code_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_type_descriptions_additional_code_type_id_index ON uk.additional_code_type_descriptions USING btree (additional_code_type_id);


--
-- Name: additional_code_type_descriptions_language_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_type_descriptions_language_id_index ON uk.additional_code_type_descriptions USING btree (language_id);


--
-- Name: additional_code_type_descriptions_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX additional_code_type_descriptions_oid_index ON uk.additional_code_type_descriptions USING btree (oid);


--
-- Name: additional_code_type_descriptions_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_type_descriptions_operation_date_index ON uk.additional_code_type_descriptions USING btree (operation_date);


--
-- Name: additional_code_type_measure_types_measure_type_id_additional_c; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_type_measure_types_measure_type_id_additional_c ON uk.additional_code_type_measure_types USING btree (measure_type_id, additional_code_type_id);


--
-- Name: additional_code_type_measure_types_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX additional_code_type_measure_types_oid_index ON uk.additional_code_type_measure_types USING btree (oid);


--
-- Name: additional_code_type_measure_types_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_type_measure_types_operation_date_index ON uk.additional_code_type_measure_types USING btree (operation_date);


--
-- Name: additional_code_types_additional_code_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_types_additional_code_type_id_index ON uk.additional_code_types USING btree (additional_code_type_id);


--
-- Name: additional_code_types_meursing_table_plan_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_types_meursing_table_plan_id_index ON uk.additional_code_types USING btree (meursing_table_plan_id);


--
-- Name: additional_code_types_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX additional_code_types_oid_index ON uk.additional_code_types USING btree (oid);


--
-- Name: additional_code_types_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_code_types_operation_date_index ON uk.additional_code_types USING btree (operation_date);


--
-- Name: additional_codes_additional_code_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_codes_additional_code_sid_index ON uk.additional_codes USING btree (additional_code_sid);


--
-- Name: additional_codes_additional_code_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_codes_additional_code_type_id_index ON uk.additional_codes USING btree (additional_code_type_id);


--
-- Name: additional_codes_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX additional_codes_oid_index ON uk.additional_codes USING btree (oid);


--
-- Name: additional_codes_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX additional_codes_operation_date_index ON uk.additional_codes USING btree (operation_date);


--
-- Name: antidumping_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX antidumping_regulation ON uk.base_regulations_oplog USING btree (antidumping_regulation_role, related_antidumping_regulation_id);


--
-- Name: base_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulation ON uk.modification_regulations_oplog USING btree (base_regulation_id, base_regulation_role);


--
-- Name: base_regulations_antidumping_regulation_role_related_antidumpin; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_antidumping_regulation_role_related_antidumpin ON uk.base_regulations USING btree (antidumping_regulation_role, related_antidumping_regulation_id);


--
-- Name: base_regulations_approved_composite_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_approved_composite_index ON uk.base_regulations USING btree (base_regulation_id, base_regulation_role) WHERE (approved_flag IS TRUE);


--
-- Name: base_regulations_approved_flag_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_approved_flag_index ON uk.base_regulations USING btree (approved_flag);


--
-- Name: base_regulations_base_regulation_id_base_regulation_role_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_base_regulation_id_base_regulation_role_index ON uk.base_regulations USING btree (base_regulation_id, base_regulation_role);


--
-- Name: base_regulations_complete_abrogation_regulation_role_complete_a; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_complete_abrogation_regulation_role_complete_a ON uk.base_regulations USING btree (complete_abrogation_regulation_role, complete_abrogation_regulation_id);


--
-- Name: base_regulations_effective_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_effective_end_date_index ON uk.base_regulations USING btree (effective_end_date);


--
-- Name: base_regulations_explicit_abrogation_regulation_role_explicit_a; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_explicit_abrogation_regulation_role_explicit_a ON uk.base_regulations USING btree (explicit_abrogation_regulation_role, explicit_abrogation_regulation_id);


--
-- Name: base_regulations_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX base_regulations_oid_index ON uk.base_regulations USING btree (oid);


--
-- Name: base_regulations_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_operation_date_index ON uk.base_regulations USING btree (operation_date);


--
-- Name: base_regulations_oplog_approved_flag_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_oplog_approved_flag_index ON uk.base_regulations_oplog USING btree (approved_flag);


--
-- Name: base_regulations_oplog_effective_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_oplog_effective_end_date_index ON uk.base_regulations_oplog USING btree (effective_end_date);


--
-- Name: base_regulations_oplog_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_oplog_validity_end_date_index ON uk.base_regulations_oplog USING btree (validity_end_date);


--
-- Name: base_regulations_oplog_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_oplog_validity_start_date_index ON uk.base_regulations_oplog USING btree (validity_start_date);


--
-- Name: base_regulations_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_pk ON uk.base_regulations_oplog USING btree (base_regulation_id, base_regulation_role);


--
-- Name: base_regulations_regulation_group_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_regulation_group_id_index ON uk.base_regulations USING btree (regulation_group_id);


--
-- Name: base_regulations_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_validity_end_date_index ON uk.base_regulations USING btree (validity_end_date);


--
-- Name: base_regulations_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX base_regulations_validity_start_date_index ON uk.base_regulations USING btree (validity_start_date);


--
-- Name: bro_basregopl_aseonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX bro_basregopl_aseonslog_operation_date ON uk.base_regulations_oplog USING btree (operation_date);


--
-- Name: caro_comabrregopl_eteiononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX caro_comabrregopl_eteiononslog_operation_date ON uk.complete_abrogation_regulations_oplog USING btree (operation_date);


--
-- Name: cdo_cerdesopl_ateonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cdo_cerdesopl_ateonslog_operation_date ON uk.certificate_descriptions_oplog USING btree (operation_date);


--
-- Name: cdpo_cerdesperopl_ateionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cdpo_cerdesperopl_ateionodslog_operation_date ON uk.certificate_description_periods_oplog USING btree (operation_date);


--
-- Name: cert_desc_certificate; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cert_desc_certificate ON uk.certificate_descriptions_oplog USING btree (certificate_code, certificate_type_code);


--
-- Name: cert_desc_period_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cert_desc_period_pk ON uk.certificate_description_periods_oplog USING btree (certificate_description_period_sid);


--
-- Name: cert_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cert_desc_pk ON uk.certificate_descriptions_oplog USING btree (certificate_description_period_sid);


--
-- Name: cert_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cert_pk ON uk.certificates_oplog USING btree (certificate_code, certificate_type_code, validity_start_date);


--
-- Name: cert_type_code_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cert_type_code_pk ON uk.certificate_type_descriptions_oplog USING btree (certificate_type_code);


--
-- Name: cert_types_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cert_types_pk ON uk.certificate_types_oplog USING btree (certificate_type_code, validity_start_date);


--
-- Name: certificate; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX certificate ON uk.certificate_description_periods_oplog USING btree (certificate_code, certificate_type_code);


--
-- Name: certificate_descriptions_description_trgm_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX certificate_descriptions_description_trgm_idx ON uk.certificate_descriptions_oplog USING gist (description public.gist_trgm_ops);


--
-- Name: chapter_notes_chapter_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX chapter_notes_chapter_id_index ON uk.chapter_notes USING btree (chapter_id);


--
-- Name: chapter_notes_section_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX chapter_notes_section_id_index ON uk.chapter_notes USING btree (section_id);


--
-- Name: chemical_names_name_chemical_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX chemical_names_name_chemical_id_index ON uk.chemical_names USING btree (name, chemical_id);


--
-- Name: chemicals_cas_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX chemicals_cas_index ON uk.chemicals USING btree (cas);


--
-- Name: chemicals_goods_nomenclatures_chemical_id_goods_nomenclature_si; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX chemicals_goods_nomenclatures_chemical_id_goods_nomenclature_si ON uk.chemicals_goods_nomenclatures USING btree (chemical_id, goods_nomenclature_sid);


--
-- Name: cmpl_abrg_reg_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cmpl_abrg_reg_pk ON uk.complete_abrogation_regulations_oplog USING btree (complete_abrogation_regulation_id, complete_abrogation_regulation_role);


--
-- Name: co_ceropl_teslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX co_ceropl_teslog_operation_date ON uk.certificates_oplog USING btree (operation_date);


--
-- Name: code_type_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX code_type_id ON uk.additional_code_description_periods_oplog USING btree (additional_code_type_id);


--
-- Name: complete_abrogation_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX complete_abrogation_regulation ON uk.base_regulations_oplog USING btree (complete_abrogation_regulation_role, complete_abrogation_regulation_id);


--
-- Name: condition_measurement_unit_qualifier_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX condition_measurement_unit_qualifier_code ON uk.measure_conditions_oplog USING btree (condition_measurement_unit_qualifier_code);


--
-- Name: ctdo_certypdesopl_ateypeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ctdo_certypdesopl_ateypeonslog_operation_date ON uk.certificate_type_descriptions_oplog USING btree (operation_date);


--
-- Name: cto_certypopl_atepeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX cto_certypopl_atepeslog_operation_date ON uk.certificate_types_oplog USING btree (operation_date);


--
-- Name: customs_tariff_chapter_notes_customs_tariff_update_version_chap; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX customs_tariff_chapter_notes_customs_tariff_update_version_chap ON uk.customs_tariff_chapter_notes USING btree (customs_tariff_update_version, chapter_id);


--
-- Name: customs_tariff_general_rules_customs_tariff_update_version_rule; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX customs_tariff_general_rules_customs_tariff_update_version_rule ON uk.customs_tariff_general_rules USING btree (customs_tariff_update_version, rule_label);


--
-- Name: customs_tariff_section_notes_customs_tariff_update_version_sect; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX customs_tariff_section_notes_customs_tariff_update_version_sect ON uk.customs_tariff_section_notes USING btree (customs_tariff_update_version, section_id);


--
-- Name: data_migrations_filename_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX data_migrations_filename_index ON uk.data_migrations USING btree (filename);


--
-- Name: dedo_dutexpdesopl_utyiononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX dedo_dutexpdesopl_utyiononslog_operation_date ON uk.duty_expression_descriptions_oplog USING btree (operation_date);


--
-- Name: deo_dutexpopl_utyonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX deo_dutexpopl_utyonslog_operation_date ON uk.duty_expressions_oplog USING btree (operation_date);


--
-- Name: description_intercepts_excluded_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX description_intercepts_excluded_index ON uk.description_intercepts USING btree (excluded);


--
-- Name: description_period_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX description_period_sid ON uk.additional_code_description_periods_oplog USING btree (additional_code_description_period_sid);


--
-- Name: duty_exp_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX duty_exp_desc_pk ON uk.duty_expression_descriptions_oplog USING btree (duty_expression_id);


--
-- Name: duty_exp_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX duty_exp_pk ON uk.duty_expressions_oplog USING btree (duty_expression_id, validity_start_date);


--
-- Name: earo_expabrregopl_citiononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX earo_expabrregopl_citiononslog_operation_date ON uk.explicit_abrogation_regulations_oplog USING btree (operation_date);


--
-- Name: erndo_exprefnomdesopl_ortundureonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX erndo_exprefnomdesopl_ortundureonslog_operation_date ON uk.export_refund_nomenclature_descriptions_oplog USING btree (operation_date);


--
-- Name: erndpo_exprefnomdesperopl_ortundureionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX erndpo_exprefnomdesperopl_ortundureionodslog_operation_date ON uk.export_refund_nomenclature_description_periods_oplog USING btree (operation_date);


--
-- Name: ernio_exprefnomindopl_ortundurentslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ernio_exprefnomindopl_ortundurentslog_operation_date ON uk.export_refund_nomenclature_indents_oplog USING btree (operation_date);


--
-- Name: erno_exprefnomopl_ortundreslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX erno_exprefnomopl_ortundreslog_operation_date ON uk.export_refund_nomenclatures_oplog USING btree (operation_date);


--
-- Name: exchange_rate_countries_country_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_countries_country_code_index ON uk.exchange_rate_countries USING btree (country_code);


--
-- Name: exchange_rate_countries_currencies_currency_code_country_code_v; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX exchange_rate_countries_currencies_currency_code_country_code_v ON uk.exchange_rate_countries_currencies USING btree (currency_code, country_code, validity_start_date, validity_end_date);


--
-- Name: exchange_rate_countries_currencies_currency_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_countries_currencies_currency_code_index ON uk.exchange_rate_countries_currencies USING btree (currency_code);


--
-- Name: exchange_rate_countries_currencies_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_countries_currencies_validity_end_date_index ON uk.exchange_rate_countries_currencies USING btree (validity_end_date);


--
-- Name: exchange_rate_countries_currencies_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_countries_currencies_validity_start_date_index ON uk.exchange_rate_countries_currencies USING btree (validity_start_date);


--
-- Name: exchange_rate_countries_currency_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_countries_currency_code_index ON uk.exchange_rate_countries USING btree (currency_code);


--
-- Name: exchange_rate_currencies_currency_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_currencies_currency_code_index ON uk.exchange_rate_currencies USING btree (currency_code);


--
-- Name: exchange_rate_currency_rates_currency_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_currency_rates_currency_code_index ON uk.exchange_rate_currency_rates USING btree (currency_code);


--
-- Name: exchange_rate_currency_rates_currency_code_validity_start_date_; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX exchange_rate_currency_rates_currency_code_validity_start_date_ ON uk.exchange_rate_currency_rates USING btree (currency_code, validity_start_date, validity_end_date, rate_type);


--
-- Name: exchange_rate_currency_rates_rate_type_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_currency_rates_rate_type_index ON uk.exchange_rate_currency_rates USING btree (rate_type);


--
-- Name: exchange_rate_currency_rates_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_currency_rates_validity_end_date_index ON uk.exchange_rate_currency_rates USING btree (validity_end_date);


--
-- Name: exchange_rate_currency_rates_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_currency_rates_validity_start_date_index ON uk.exchange_rate_currency_rates USING btree (validity_start_date);


--
-- Name: exchange_rate_files_period_month_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_files_period_month_index ON uk.exchange_rate_files USING btree (period_month);


--
-- Name: exchange_rate_files_period_year_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_files_period_year_index ON uk.exchange_rate_files USING btree (period_year);


--
-- Name: exchange_rate_files_type_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exchange_rate_files_type_index ON uk.exchange_rate_files USING btree (type);


--
-- Name: exp_abrg_reg_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exp_abrg_reg_pk ON uk.explicit_abrogation_regulations_oplog USING btree (explicit_abrogation_regulation_id, explicit_abrogation_regulation_role);


--
-- Name: exp_rfnd_desc_period_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exp_rfnd_desc_period_pk ON uk.export_refund_nomenclature_description_periods_oplog USING btree (export_refund_nomenclature_sid, export_refund_nomenclature_description_period_sid);


--
-- Name: exp_rfnd_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exp_rfnd_desc_pk ON uk.export_refund_nomenclature_descriptions_oplog USING btree (export_refund_nomenclature_description_period_sid);


--
-- Name: exp_rfnd_indent_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exp_rfnd_indent_pk ON uk.export_refund_nomenclature_indents_oplog USING btree (export_refund_nomenclature_indents_sid);


--
-- Name: exp_rfnd_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX exp_rfnd_pk ON uk.export_refund_nomenclatures_oplog USING btree (export_refund_nomenclature_sid);


--
-- Name: explicit_abrogation_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX explicit_abrogation_regulation ON uk.base_regulations_oplog USING btree (explicit_abrogation_regulation_role, explicit_abrogation_regulation_id);


--
-- Name: export_refund_nomenclature; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX export_refund_nomenclature ON uk.export_refund_nomenclature_descriptions_oplog USING btree (export_refund_nomenclature_sid);


--
-- Name: faaco_fooassaddcodopl_oteionnaldeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX faaco_fooassaddcodopl_oteionnaldeslog_operation_date ON uk.footnote_association_additional_codes_oplog USING btree (operation_date);


--
-- Name: faeo_fooassernopl_oteionrnslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX faeo_fooassernopl_oteionrnslog_operation_date ON uk.footnote_association_erns_oplog USING btree (operation_date);


--
-- Name: fagno_fooassgoonomopl_oteionodsreslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fagno_fooassgoonomopl_oteionodsreslog_operation_date ON uk.footnote_association_goods_nomenclatures_oplog USING btree (operation_date);


--
-- Name: famho_fooassmeuheaopl_oteioningngslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX famho_fooassmeuheaopl_oteioningngslog_operation_date ON uk.footnote_association_meursing_headings_oplog USING btree (operation_date);


--
-- Name: famo_fooassmeaopl_oteionreslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX famo_fooassmeaopl_oteionreslog_operation_date ON uk.footnote_association_measures_oplog USING btree (operation_date);


--
-- Name: fdo_foodesopl_oteonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fdo_foodesopl_oteonslog_operation_date ON uk.footnote_descriptions_oplog USING btree (operation_date);


--
-- Name: fdpo_foodesperopl_oteionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fdpo_foodesperopl_oteionodslog_operation_date ON uk.footnote_description_periods_oplog USING btree (operation_date);


--
-- Name: fo_fooopl_teslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fo_fooopl_teslog_operation_date ON uk.footnotes_oplog USING btree (operation_date);


--
-- Name: footnote_association_goods_nomenclatures_oplog_footnote_id_inde; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_association_goods_nomenclatures_oplog_footnote_id_inde ON uk.footnote_association_goods_nomenclatures_oplog USING btree (footnote_id);


--
-- Name: footnote_association_goods_nomenclatures_oplog_footnote_type_in; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_association_goods_nomenclatures_oplog_footnote_type_in ON uk.footnote_association_goods_nomenclatures_oplog USING btree (footnote_type);


--
-- Name: footnote_association_goods_nomenclatures_oplog_goods_nomenclatu; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_association_goods_nomenclatures_oplog_goods_nomenclatu ON uk.footnote_association_goods_nomenclatures_oplog USING btree (goods_nomenclature_sid);


--
-- Name: footnote_association_measures_oplog_footnote_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_association_measures_oplog_footnote_type_id_index ON uk.footnote_association_measures_oplog USING btree (footnote_type_id);


--
-- Name: footnote_description_periods_footnote_type_id_footnote_id_valid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_description_periods_footnote_type_id_footnote_id_valid ON uk.footnote_description_periods USING btree (footnote_type_id, footnote_id, validity_start_date);


--
-- Name: footnote_description_periods_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX footnote_description_periods_oid_index ON uk.footnote_description_periods USING btree (oid);


--
-- Name: footnote_descriptions_description_trgm_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_descriptions_description_trgm_idx ON uk.footnote_descriptions_oplog USING gist (description public.gist_trgm_ops);


--
-- Name: footnote_descriptions_footnote_description_period_sid_footnote_; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_descriptions_footnote_description_period_sid_footnote_ ON uk.footnote_descriptions USING btree (footnote_description_period_sid, footnote_type_id, footnote_id);


--
-- Name: footnote_descriptions_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX footnote_descriptions_oid_index ON uk.footnote_descriptions USING btree (oid);


--
-- Name: footnote_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnote_id ON uk.footnote_association_measures_oplog USING btree (footnote_id);


--
-- Name: footnotes_footnote_type_id_footnote_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnotes_footnote_type_id_footnote_id_index ON uk.footnotes USING btree (footnote_type_id, footnote_id);


--
-- Name: footnotes_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX footnotes_oid_index ON uk.footnotes USING btree (oid);


--
-- Name: footnotes_validity_start_date_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX footnotes_validity_start_date_validity_end_date_index ON uk.footnotes USING btree (validity_start_date, validity_end_date);


--
-- Name: forum_links_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX forum_links_goods_nomenclature_sid_index ON uk.forum_links USING btree (goods_nomenclature_sid);


--
-- Name: frao_ftsregactopl_ftsiononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX frao_ftsregactopl_ftsiononslog_operation_date ON uk.fts_regulation_actions_oplog USING btree (operation_date);


--
-- Name: ftdo_footypdesopl_oteypeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftdo_footypdesopl_oteypeonslog_operation_date ON uk.footnote_type_descriptions_oplog USING btree (operation_date);


--
-- Name: ftn_assoc_adco_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_assoc_adco_pk ON uk.footnote_association_additional_codes_oplog USING btree (footnote_id, footnote_type_id, additional_code_sid);


--
-- Name: ftn_assoc_ern_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_assoc_ern_pk ON uk.footnote_association_erns_oplog USING btree (export_refund_nomenclature_sid, footnote_id, footnote_type, validity_start_date);


--
-- Name: ftn_assoc_gono_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_assoc_gono_pk ON uk.footnote_association_goods_nomenclatures_oplog USING btree (footnote_id, footnote_type, goods_nomenclature_sid, validity_start_date);


--
-- Name: ftn_assoc_meurs_head_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_assoc_meurs_head_pk ON uk.footnote_association_meursing_headings_oplog USING btree (footnote_id, meursing_table_plan_id);


--
-- Name: ftn_desc; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_desc ON uk.footnote_descriptions_oplog USING btree (footnote_id, footnote_type_id, footnote_description_period_sid);


--
-- Name: ftn_desc_period; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_desc_period ON uk.footnote_description_periods_oplog USING btree (footnote_id, footnote_type_id, footnote_description_period_sid);


--
-- Name: ftn_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_pk ON uk.footnotes_oplog USING btree (footnote_id, footnote_type_id);


--
-- Name: ftn_type_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_type_desc_pk ON uk.footnote_type_descriptions_oplog USING btree (footnote_type_id);


--
-- Name: ftn_types_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftn_types_pk ON uk.footnote_types_oplog USING btree (footnote_type_id);


--
-- Name: fto_footypopl_otepeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fto_footypopl_otepeslog_operation_date ON uk.footnote_types_oplog USING btree (operation_date);


--
-- Name: fts_reg_act_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fts_reg_act_pk ON uk.fts_regulation_actions_oplog USING btree (fts_regulation_id, fts_regulation_role, stopped_regulation_id, stopped_regulation_role) INCLUDE (oid);


--
-- Name: fts_regulation_actions_stopped_regulation_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX fts_regulation_actions_stopped_regulation_id_index ON uk.fts_regulation_actions_oplog USING btree (stopped_regulation_id);


--
-- Name: ftsro_fultemstoregopl_ullarytoponslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ftsro_fultemstoregopl_ullarytoponslog_operation_date ON uk.full_temporary_stop_regulations_oplog USING btree (operation_date);


--
-- Name: full_chemicals_cas_rn_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX full_chemicals_cas_rn_idx ON uk.full_chemicals USING btree (cas_rn);


--
-- Name: full_chemicals_cus_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX full_chemicals_cus_idx ON uk.full_chemicals USING btree (cus);


--
-- Name: full_chemicals_goods_nomenclature_item_id_producline_suffix_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX full_chemicals_goods_nomenclature_item_id_producline_suffix_idx ON uk.full_chemicals USING btree (goods_nomenclature_item_id, producline_suffix);


--
-- Name: full_chemicals_goods_nomenclature_sid_idx; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX full_chemicals_goods_nomenclature_sid_idx ON uk.full_chemicals USING btree (goods_nomenclature_sid);


--
-- Name: full_temp_explicit_abrogation_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX full_temp_explicit_abrogation_regulation ON uk.full_temporary_stop_regulations_oplog USING btree (explicit_abrogation_regulation_role, explicit_abrogation_regulation_id);


--
-- Name: full_temp_stop_reg_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX full_temp_stop_reg_pk ON uk.full_temporary_stop_regulations_oplog USING btree (full_temporary_stop_regulation_id, full_temporary_stop_regulation_role) INCLUDE (oid);


--
-- Name: gado_geoaredesopl_calreaonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gado_geoaredesopl_calreaonslog_operation_date ON uk.geographical_area_descriptions_oplog USING btree (operation_date);


--
-- Name: gadpo_geoaredesperopl_calreaionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gadpo_geoaredesperopl_calreaionodslog_operation_date ON uk.geographical_area_description_periods_oplog USING btree (operation_date);


--
-- Name: gamo_geoarememopl_calreaipslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gamo_geoarememopl_calreaipslog_operation_date ON uk.geographical_area_memberships_oplog USING btree (operation_date);


--
-- Name: gao_geoareopl_caleaslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gao_geoareopl_caleaslog_operation_date ON uk.geographical_areas_oplog USING btree (operation_date);


--
-- Name: geo_area_member_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geo_area_member_pk ON uk.geographical_area_memberships_oplog USING btree (geographical_area_sid, geographical_area_group_sid, validity_start_date);


--
-- Name: geo_area_memberships_group_sid_validity_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geo_area_memberships_group_sid_validity_index ON uk.geographical_area_memberships USING btree (geographical_area_group_sid, validity_start_date, validity_end_date);


--
-- Name: geog_area_desc_period_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geog_area_desc_period_pk ON uk.geographical_area_description_periods_oplog USING btree (geographical_area_description_period_sid, geographical_area_sid);


--
-- Name: geog_area_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geog_area_desc_pk ON uk.geographical_area_descriptions_oplog USING btree (geographical_area_description_period_sid, geographical_area_sid);


--
-- Name: geog_area_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geog_area_pk ON uk.geographical_areas_oplog USING btree (geographical_area_id);


--
-- Name: geographical_area_description_periods_geographical_area_descrip; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_description_periods_geographical_area_descrip ON uk.geographical_area_description_periods USING btree (geographical_area_description_period_sid, geographical_area_sid);


--
-- Name: geographical_area_description_periods_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX geographical_area_description_periods_oid_index ON uk.geographical_area_description_periods USING btree (oid);


--
-- Name: geographical_area_description_periods_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_description_periods_operation_date_index ON uk.geographical_area_description_periods USING btree (operation_date);


--
-- Name: geographical_area_description_periods_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_description_periods_validity_end_date_index ON uk.geographical_area_description_periods USING btree (validity_end_date);


--
-- Name: geographical_area_description_periods_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_description_periods_validity_start_date_index ON uk.geographical_area_description_periods USING btree (validity_start_date);


--
-- Name: geographical_area_descriptions_geographical_area_description_pe; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_descriptions_geographical_area_description_pe ON uk.geographical_area_descriptions USING btree (geographical_area_description_period_sid, geographical_area_sid);


--
-- Name: geographical_area_descriptions_language_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_descriptions_language_id_index ON uk.geographical_area_descriptions USING btree (language_id);


--
-- Name: geographical_area_descriptions_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX geographical_area_descriptions_oid_index ON uk.geographical_area_descriptions USING btree (oid);


--
-- Name: geographical_area_descriptions_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_descriptions_operation_date_index ON uk.geographical_area_descriptions USING btree (operation_date);


--
-- Name: geographical_area_memberships_geographical_area_sid_geographica; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_memberships_geographical_area_sid_geographica ON uk.geographical_area_memberships USING btree (geographical_area_sid, geographical_area_group_sid, validity_start_date);


--
-- Name: geographical_area_memberships_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX geographical_area_memberships_oid_index ON uk.geographical_area_memberships USING btree (oid);


--
-- Name: geographical_area_memberships_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_area_memberships_operation_date_index ON uk.geographical_area_memberships USING btree (operation_date);


--
-- Name: geographical_areas_geographical_area_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_areas_geographical_area_id_index ON uk.geographical_areas USING btree (geographical_area_id);


--
-- Name: geographical_areas_geographical_area_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_areas_geographical_area_sid_index ON uk.geographical_areas USING btree (geographical_area_sid);


--
-- Name: geographical_areas_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX geographical_areas_oid_index ON uk.geographical_areas USING btree (oid);


--
-- Name: geographical_areas_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_areas_operation_date_index ON uk.geographical_areas USING btree (operation_date);


--
-- Name: geographical_areas_parent_geographical_area_group_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX geographical_areas_parent_geographical_area_group_sid_index ON uk.geographical_areas USING btree (parent_geographical_area_group_sid);


--
-- Name: gndo_goonomdesopl_odsureonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gndo_goonomdesopl_odsureonslog_operation_date ON uk.goods_nomenclature_descriptions_oplog USING btree (operation_date);


--
-- Name: gndpo_goonomdesperopl_odsureionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gndpo_goonomdesperopl_odsureionodslog_operation_date ON uk.goods_nomenclature_description_periods_oplog USING btree (operation_date);


--
-- Name: gngdo_goonomgrodesopl_odsureouponslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gngdo_goonomgrodesopl_odsureouponslog_operation_date ON uk.goods_nomenclature_group_descriptions_oplog USING btree (operation_date);


--
-- Name: gngo_goonomgroopl_odsureupslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gngo_goonomgroopl_odsureupslog_operation_date ON uk.goods_nomenclature_groups_oplog USING btree (operation_date);


--
-- Name: gnio_goonomindopl_odsurentslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gnio_goonomindopl_odsurentslog_operation_date ON uk.goods_nomenclature_indents_oplog USING btree (operation_date);


--
-- Name: gno_goonomopl_odsreslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gno_goonomopl_odsreslog_operation_date ON uk.goods_nomenclatures_oplog USING btree (operation_date);


--
-- Name: gnoo_goonomoriopl_odsureinslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gnoo_goonomoriopl_odsureinslog_operation_date ON uk.goods_nomenclature_origins_oplog USING btree (operation_date);


--
-- Name: gnso_goonomsucopl_odsureorslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gnso_goonomsucopl_odsureorslog_operation_date ON uk.goods_nomenclature_successors_oplog USING btree (operation_date);


--
-- Name: gono_desc_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_desc_oid_index ON uk.goods_nomenclature_descriptions_oplog USING btree (goods_nomenclature_sid, goods_nomenclature_description_period_sid, oid DESC);


--
-- Name: gono_desc_periods_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_desc_periods_oid_index ON uk.goods_nomenclature_description_periods_oplog USING btree (goods_nomenclature_description_period_sid, oid DESC);


--
-- Name: gono_desc_periods_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_desc_periods_pk ON uk.goods_nomenclature_description_periods_oplog USING btree (goods_nomenclature_sid, validity_start_date, validity_end_date);


--
-- Name: gono_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_desc_pk ON uk.goods_nomenclature_descriptions_oplog USING btree (goods_nomenclature_sid, goods_nomenclature_description_period_sid);


--
-- Name: gono_desc_primary_key; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_desc_primary_key ON uk.goods_nomenclature_description_periods_oplog USING btree (goods_nomenclature_description_period_sid);


--
-- Name: gono_grp_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_grp_desc_pk ON uk.goods_nomenclature_group_descriptions_oplog USING btree (goods_nomenclature_group_id, goods_nomenclature_group_type);


--
-- Name: gono_grp_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_grp_pk ON uk.goods_nomenclature_groups_oplog USING btree (goods_nomenclature_group_id, goods_nomenclature_group_type);


--
-- Name: gono_indent_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_indent_pk ON uk.goods_nomenclature_indents_oplog USING btree (goods_nomenclature_indent_sid);


--
-- Name: gono_origin_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_origin_pk ON uk.goods_nomenclature_origins_oplog USING btree (goods_nomenclature_sid, derived_goods_nomenclature_item_id, derived_productline_suffix, goods_nomenclature_item_id, productline_suffix);


--
-- Name: gono_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_pk ON uk.goods_nomenclatures_oplog USING btree (goods_nomenclature_sid);


--
-- Name: gono_succ_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX gono_succ_pk ON uk.goods_nomenclature_successors_oplog USING btree (goods_nomenclature_sid, absorbed_goods_nomenclature_item_id, absorbed_productline_suffix, goods_nomenclature_item_id, productline_suffix);


--
-- Name: goods_nomenclature_intercepts_excluded_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_intercepts_excluded_index ON uk.goods_nomenclature_intercepts USING btree (excluded);


--
-- Name: goods_nomenclature_labels_goods_nomenclature_item_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_labels_goods_nomenclature_item_id_index ON uk.goods_nomenclature_labels USING btree (goods_nomenclature_item_id);


--
-- Name: goods_nomenclature_labels_labels_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_labels_labels_index ON uk.goods_nomenclature_labels USING gin (labels);


--
-- Name: goods_nomenclature_self_texts_generation_type_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_self_texts_generation_type_index ON uk.goods_nomenclature_self_texts USING btree (generation_type);


--
-- Name: goods_nomenclature_self_texts_goods_nomenclature_item_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_self_texts_goods_nomenclature_item_id_index ON uk.goods_nomenclature_self_texts USING btree (goods_nomenclature_item_id);


--
-- Name: goods_nomenclature_self_texts_search_embedding_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_self_texts_search_embedding_index ON uk.goods_nomenclature_self_texts USING hnsw (search_embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: goods_nomenclature_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_sid ON uk.goods_nomenclature_indents_oplog USING btree (goods_nomenclature_sid);


--
-- Name: goods_nomenclature_tree_node_overrides_created_at_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_tree_node_overrides_created_at_index ON uk.goods_nomenclature_tree_node_overrides USING btree (created_at);


--
-- Name: goods_nomenclature_tree_node_overrides_goods_nomenclature_inden; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX goods_nomenclature_tree_node_overrides_goods_nomenclature_inden ON uk.goods_nomenclature_tree_node_overrides USING btree (goods_nomenclature_indent_sid);


--
-- Name: goods_nomenclature_tree_node_overrides_updated_at_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_tree_node_overrides_updated_at_index ON uk.goods_nomenclature_tree_node_overrides USING btree (updated_at);


--
-- Name: goods_nomenclature_tree_nodes_depth_position_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_tree_nodes_depth_position_index ON uk.goods_nomenclature_tree_nodes USING btree (depth, "position");


--
-- Name: goods_nomenclature_tree_nodes_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_tree_nodes_goods_nomenclature_sid_index ON uk.goods_nomenclature_tree_nodes USING btree (goods_nomenclature_sid);


--
-- Name: goods_nomenclature_tree_nodes_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX goods_nomenclature_tree_nodes_oid_index ON uk.goods_nomenclature_tree_nodes USING btree (oid);


--
-- Name: goods_nomenclature_validity_dates; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclature_validity_dates ON uk.goods_nomenclature_indents_oplog USING btree (validity_start_date, validity_end_date);


--
-- Name: goods_nomenclatures_goods_nomenclature_item_id_producline_suffi; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_goods_nomenclature_item_id_producline_suffi ON uk.goods_nomenclatures USING btree (goods_nomenclature_item_id, producline_suffix);


--
-- Name: goods_nomenclatures_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_goods_nomenclature_sid_index ON uk.goods_nomenclatures USING btree (goods_nomenclature_sid);


--
-- Name: goods_nomenclatures_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX goods_nomenclatures_oid_index ON uk.goods_nomenclatures USING btree (oid);


--
-- Name: goods_nomenclatures_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_operation_date_index ON uk.goods_nomenclatures USING btree (operation_date);


--
-- Name: goods_nomenclatures_oplog_path_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_oplog_path_index ON uk.goods_nomenclatures_oplog USING gin (path);


--
-- Name: goods_nomenclatures_oplog_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_oplog_validity_end_date_index ON uk.goods_nomenclatures_oplog USING btree (validity_end_date);


--
-- Name: goods_nomenclatures_oplog_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_oplog_validity_start_date_index ON uk.goods_nomenclatures_oplog USING btree (validity_start_date);


--
-- Name: goods_nomenclatures_path_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_path_index ON uk.goods_nomenclatures USING gin (path);


--
-- Name: goods_nomenclatures_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_validity_end_date_index ON uk.goods_nomenclatures USING btree (validity_end_date);


--
-- Name: goods_nomenclatures_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX goods_nomenclatures_validity_start_date_index ON uk.goods_nomenclatures USING btree (validity_start_date);


--
-- Name: green_lanes_category_assessments_exemptions_exemption_id_catego; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX green_lanes_category_assessments_exemptions_exemption_id_catego ON uk.green_lanes_category_assessments_exemptions USING btree (exemption_id, category_assessment_id);


--
-- Name: green_lanes_category_assessments_measure_type_id_regulation_id_; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX green_lanes_category_assessments_measure_type_id_regulation_id_ ON uk.green_lanes_category_assessments USING btree (measure_type_id, regulation_id, regulation_role, theme_id);


--
-- Name: green_lanes_category_assessments_updated_at_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX green_lanes_category_assessments_updated_at_index ON uk.green_lanes_category_assessments USING btree (updated_at);


--
-- Name: green_lanes_exempting_additional_code_overrides_additional_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX green_lanes_exempting_additional_code_overrides_additional_code ON uk.green_lanes_exempting_additional_code_overrides USING btree (additional_code_type_id, additional_code);


--
-- Name: green_lanes_exempting_certificate_overrides_certificate_code_ce; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX green_lanes_exempting_certificate_overrides_certificate_code_ce ON uk.green_lanes_exempting_certificate_overrides USING btree (certificate_code, certificate_type_code);


--
-- Name: green_lanes_identified_measure_type_category_assessments_measur; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX green_lanes_identified_measure_type_category_assessments_measur ON uk.green_lanes_identified_measure_type_category_assessments USING btree (measure_type_id);


--
-- Name: green_lanes_measures_category_assessment_id_goods_nomenclature_; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX green_lanes_measures_category_assessment_id_goods_nomenclature_ ON uk.green_lanes_measures USING btree (category_assessment_id, goods_nomenclature_item_id, productline_suffix);


--
-- Name: green_lanes_themes_section_subsection_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX green_lanes_themes_section_subsection_index ON uk.green_lanes_themes USING btree (section, subsection);


--
-- Name: guides_goods_nomenclatures_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX guides_goods_nomenclatures_goods_nomenclature_sid_index ON uk.guides_goods_nomenclatures USING btree (goods_nomenclature_sid);


--
-- Name: guides_goods_nomenclatures_guide_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX guides_goods_nomenclatures_guide_id_index ON uk.guides_goods_nomenclatures USING btree (guide_id);


--
-- Name: idx_ac_area; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_ac_area ON uk.admin_configurations USING btree (area);


--
-- Name: idx_ac_name; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX idx_ac_name ON uk.admin_configurations USING btree (name);


--
-- Name: idx_description_intercepts_aliases_gin; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_description_intercepts_aliases_gin ON uk.description_intercepts USING gin (aliases);


--
-- Name: idx_description_intercepts_sources_gin; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_description_intercepts_sources_gin ON uk.description_intercepts USING gin (sources);


--
-- Name: idx_labels_approved; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_labels_approved ON uk.goods_nomenclature_labels USING btree (approved) WHERE (approved = true);


--
-- Name: idx_labels_expired; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_labels_expired ON uk.goods_nomenclature_labels USING btree (expired) WHERE (expired = true);


--
-- Name: idx_labels_needs_review; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_labels_needs_review ON uk.goods_nomenclature_labels USING btree (needs_review) WHERE (needs_review = true);


--
-- Name: idx_labels_stale; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_labels_stale ON uk.goods_nomenclature_labels USING btree (stale) WHERE (stale = true);


--
-- Name: idx_search_suggestions_distinct; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_search_suggestions_distinct ON uk.search_suggestions USING btree (value, priority);


--
-- Name: idx_search_suggestions_value_trgm; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_search_suggestions_value_trgm ON uk.search_suggestions USING gin (value public.gin_trgm_ops);


--
-- Name: idx_self_texts_approved; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_self_texts_approved ON uk.goods_nomenclature_self_texts USING btree (approved) WHERE (approved = true);


--
-- Name: idx_self_texts_expired; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_self_texts_expired ON uk.goods_nomenclature_self_texts USING btree (expired) WHERE (expired = true);


--
-- Name: idx_self_texts_needs_review; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_self_texts_needs_review ON uk.goods_nomenclature_self_texts USING btree (needs_review) WHERE (needs_review = true);


--
-- Name: idx_self_texts_search_embedding_stale; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_self_texts_search_embedding_stale ON uk.goods_nomenclature_self_texts USING btree (search_embedding_stale) WHERE (search_embedding_stale = true);


--
-- Name: idx_self_texts_stale; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_self_texts_stale ON uk.goods_nomenclature_self_texts USING btree (stale) WHERE (stale = true);


--
-- Name: idx_tariff_changes_measure_metadata; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX idx_tariff_changes_measure_metadata ON uk.tariff_changes USING gin (((metadata -> 'measure'::text))) WHERE (type = 'Measure'::text);


--
-- Name: index_additional_code_type_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_additional_code_type_descriptions_on_language_id ON uk.additional_code_type_descriptions_oplog USING btree (language_id);


--
-- Name: index_additional_code_types_on_meursing_table_plan_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_additional_code_types_on_meursing_table_plan_id ON uk.additional_code_types_oplog USING btree (meursing_table_plan_id);


--
-- Name: index_base_regulations_on_regulation_group_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_base_regulations_on_regulation_group_id ON uk.base_regulations_oplog USING btree (regulation_group_id);


--
-- Name: index_certificate_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_certificate_descriptions_on_language_id ON uk.certificate_descriptions_oplog USING btree (language_id);


--
-- Name: index_certificate_type_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_certificate_type_descriptions_on_language_id ON uk.certificate_type_descriptions_oplog USING btree (language_id);


--
-- Name: index_chapters_sections_on_goods_nomenclature_sid_and_section_i; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX index_chapters_sections_on_goods_nomenclature_sid_and_section_i ON uk.chapters_sections USING btree (goods_nomenclature_sid, section_id);


--
-- Name: index_duty_expression_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_duty_expression_descriptions_on_language_id ON uk.duty_expression_descriptions_oplog USING btree (language_id);


--
-- Name: index_export_refund_nomenclature_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_export_refund_nomenclature_descriptions_on_language_id ON uk.export_refund_nomenclature_descriptions_oplog USING btree (language_id);


--
-- Name: index_export_refund_nomenclatures_on_goods_nomenclature_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_export_refund_nomenclatures_on_goods_nomenclature_sid ON uk.export_refund_nomenclatures_oplog USING btree (goods_nomenclature_sid);


--
-- Name: index_footnote_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_footnote_descriptions_on_language_id ON uk.footnote_descriptions_oplog USING btree (language_id);


--
-- Name: index_footnote_type_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_footnote_type_descriptions_on_language_id ON uk.footnote_type_descriptions_oplog USING btree (language_id);


--
-- Name: index_geographical_area_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_geographical_area_descriptions_on_language_id ON uk.geographical_area_descriptions_oplog USING btree (language_id);


--
-- Name: index_geographical_areas_on_parent_geographical_area_group_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_geographical_areas_on_parent_geographical_area_group_sid ON uk.geographical_areas_oplog USING btree (parent_geographical_area_group_sid);


--
-- Name: index_goods_nomenclature_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_goods_nomenclature_descriptions_on_language_id ON uk.goods_nomenclature_descriptions_oplog USING btree (language_id);


--
-- Name: index_goods_nomenclature_group_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_goods_nomenclature_group_descriptions_on_language_id ON uk.goods_nomenclature_group_descriptions_oplog USING btree (language_id);


--
-- Name: index_measure_components_on_measurement_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_components_on_measurement_unit_code ON uk.measure_components_oplog USING btree (measurement_unit_code);


--
-- Name: index_measure_components_on_measurement_unit_qualifier_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_components_on_measurement_unit_qualifier_code ON uk.measure_components_oplog USING btree (measurement_unit_qualifier_code);


--
-- Name: index_measure_components_on_monetary_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_components_on_monetary_unit_code ON uk.measure_components_oplog USING btree (monetary_unit_code);


--
-- Name: index_measure_condition_components_on_duty_expression_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_condition_components_on_duty_expression_id ON uk.measure_condition_components_oplog USING btree (duty_expression_id);


--
-- Name: index_measure_condition_components_on_measurement_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_condition_components_on_measurement_unit_code ON uk.measure_condition_components_oplog USING btree (measurement_unit_code);


--
-- Name: index_measure_condition_components_on_monetary_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_condition_components_on_monetary_unit_code ON uk.measure_condition_components_oplog USING btree (monetary_unit_code);


--
-- Name: index_measure_conditions_on_action_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_conditions_on_action_code ON uk.measure_conditions_oplog USING btree (action_code);


--
-- Name: index_measure_conditions_on_condition_measurement_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_conditions_on_condition_measurement_unit_code ON uk.measure_conditions_oplog USING btree (condition_measurement_unit_code);


--
-- Name: index_measure_conditions_on_condition_monetary_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_conditions_on_condition_monetary_unit_code ON uk.measure_conditions_oplog USING btree (condition_monetary_unit_code);


--
-- Name: index_measure_conditions_on_measure_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_conditions_on_measure_sid ON uk.measure_conditions_oplog USING btree (measure_sid);


--
-- Name: index_measure_type_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_type_descriptions_on_language_id ON uk.measure_type_descriptions_oplog USING btree (language_id);


--
-- Name: index_measure_type_series_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_type_series_descriptions_on_language_id ON uk.measure_type_series_descriptions_oplog USING btree (language_id);


--
-- Name: index_measure_types_on_measure_type_series_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measure_types_on_measure_type_series_id ON uk.measure_types_oplog USING btree (measure_type_series_id);


--
-- Name: index_measurement_unit_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measurement_unit_descriptions_on_language_id ON uk.measurement_unit_descriptions_oplog USING btree (language_id);


--
-- Name: index_measures_on_additional_code_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measures_on_additional_code_sid ON uk.measures_oplog USING btree (additional_code_sid);


--
-- Name: index_measures_on_geographical_area_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measures_on_geographical_area_sid ON uk.measures_oplog USING btree (geographical_area_sid);


--
-- Name: index_measures_on_goods_nomenclature_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measures_on_goods_nomenclature_sid ON uk.measures_oplog USING btree (goods_nomenclature_sid);


--
-- Name: index_measures_on_measure_type; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_measures_on_measure_type ON uk.measures_oplog USING btree (measure_type_id);


--
-- Name: index_monetary_unit_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_monetary_unit_descriptions_on_language_id ON uk.monetary_unit_descriptions_oplog USING btree (language_id);


--
-- Name: index_quota_definitions_on_measurement_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_quota_definitions_on_measurement_unit_code ON uk.quota_definitions_oplog USING btree (measurement_unit_code);


--
-- Name: index_quota_definitions_on_measurement_unit_qualifier_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_quota_definitions_on_measurement_unit_qualifier_code ON uk.quota_definitions_oplog USING btree (measurement_unit_qualifier_code);


--
-- Name: index_quota_definitions_on_monetary_unit_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_quota_definitions_on_monetary_unit_code ON uk.quota_definitions_oplog USING btree (monetary_unit_code);


--
-- Name: index_quota_definitions_on_quota_order_number_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_quota_definitions_on_quota_order_number_id ON uk.quota_definitions_oplog USING btree (quota_order_number_id);


--
-- Name: index_quota_order_number_origins_on_geographical_area_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_quota_order_number_origins_on_geographical_area_sid ON uk.quota_order_number_origins_oplog USING btree (geographical_area_sid);


--
-- Name: index_quota_suspension_periods_on_quota_definition_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_quota_suspension_periods_on_quota_definition_sid ON uk.quota_suspension_periods_oplog USING btree (quota_definition_sid);


--
-- Name: index_regulation_group_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_regulation_group_descriptions_on_language_id ON uk.regulation_group_descriptions_oplog USING btree (language_id);


--
-- Name: index_regulation_role_type_descriptions_on_language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX index_regulation_role_type_descriptions_on_language_id ON uk.regulation_role_type_descriptions_oplog USING btree (language_id);


--
-- Name: item_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX item_id ON uk.goods_nomenclatures_oplog USING btree (goods_nomenclature_item_id, producline_suffix);


--
-- Name: justification_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX justification_regulation ON uk.measures_oplog USING btree (justification_regulation_role, justification_regulation_id);


--
-- Name: lang_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX lang_desc_pk ON uk.language_descriptions_oplog USING btree (language_id, language_code_id);


--
-- Name: language_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX language_id ON uk.additional_code_descriptions_oplog USING btree (language_id);


--
-- Name: ldo_landesopl_ageonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ldo_landesopl_ageonslog_operation_date ON uk.language_descriptions_oplog USING btree (operation_date);


--
-- Name: lo_lanopl_geslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX lo_lanopl_geslog_operation_date ON uk.languages_oplog USING btree (operation_date);


--
-- Name: maco_meuaddcodopl_ingnaldeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX maco_meuaddcodopl_ingnaldeslog_operation_date ON uk.meursing_additional_codes_oplog USING btree (operation_date);


--
-- Name: mado_meaactdesopl_ureiononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mado_meaactdesopl_ureiononslog_operation_date ON uk.measure_action_descriptions_oplog USING btree (operation_date);


--
-- Name: mao_meaactopl_ureonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mao_meaactopl_ureonslog_operation_date ON uk.measure_actions_oplog USING btree (operation_date);


--
-- Name: mccdo_meaconcoddesopl_ureionodeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mccdo_meaconcoddesopl_ureionodeonslog_operation_date ON uk.measure_condition_code_descriptions_oplog USING btree (operation_date);


--
-- Name: mcco_meaconcodopl_ureiondeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mcco_meaconcodopl_ureiondeslog_operation_date ON uk.measure_condition_codes_oplog USING btree (operation_date);


--
-- Name: mcco_meaconcomopl_ureionntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mcco_meaconcomopl_ureionntslog_operation_date ON uk.measure_condition_components_oplog USING btree (operation_date);


--
-- Name: mco_meacomopl_urentslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mco_meacomopl_urentslog_operation_date ON uk.measure_components_oplog USING btree (operation_date);


--
-- Name: mco_meaconopl_ureonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mco_meaconopl_ureonslog_operation_date ON uk.measure_conditions_oplog USING btree (operation_date);


--
-- Name: meas_act_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_act_desc_pk ON uk.measure_action_descriptions_oplog USING btree (action_code);


--
-- Name: meas_act_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_act_pk ON uk.measure_actions_oplog USING btree (action_code, validity_start_date);


--
-- Name: meas_comp_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_comp_pk ON uk.measure_components_oplog USING btree (measure_sid, duty_expression_id);


--
-- Name: meas_cond_cd_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_cond_cd_desc_pk ON uk.measure_condition_code_descriptions_oplog USING btree (condition_code);


--
-- Name: meas_cond_cd_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_cond_cd_pk ON uk.measure_condition_codes_oplog USING btree (condition_code, validity_start_date);


--
-- Name: meas_cond_certificate; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_cond_certificate ON uk.measure_conditions_oplog USING btree (certificate_code, certificate_type_code);


--
-- Name: meas_cond_comp_cd; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_cond_comp_cd ON uk.measure_condition_components_oplog USING btree (measure_condition_sid, duty_expression_id);


--
-- Name: meas_cond_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_cond_pk ON uk.measure_conditions_oplog USING btree (measure_condition_sid);


--
-- Name: meas_excl_geog_area_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_excl_geog_area_pk ON uk.measure_excluded_geographical_areas_oplog USING btree (geographical_area_sid);


--
-- Name: meas_excl_geog_primary_key; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_excl_geog_primary_key ON uk.measure_excluded_geographical_areas_oplog USING btree (measure_sid, excluded_geographical_area, geographical_area_sid);


--
-- Name: meas_part_temp_stop_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_part_temp_stop_pk ON uk.measure_partial_temporary_stops_oplog USING btree (measure_sid, partial_temporary_stop_regulation_id);


--
-- Name: meas_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_pk ON uk.measures_oplog USING btree (measure_sid);


--
-- Name: meas_type_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_type_desc_pk ON uk.measure_type_descriptions_oplog USING btree (measure_type_id);


--
-- Name: meas_type_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_type_pk ON uk.measure_types_oplog USING btree (measure_type_id, validity_start_date);


--
-- Name: meas_type_series_desc; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_type_series_desc ON uk.measure_type_series_descriptions_oplog USING btree (measure_type_series_id);


--
-- Name: meas_type_series_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_type_series_pk ON uk.measure_type_series_oplog USING btree (measure_type_series_id);


--
-- Name: meas_unit_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_unit_desc_pk ON uk.measurement_unit_descriptions_oplog USING btree (measurement_unit_code);


--
-- Name: meas_unit_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_unit_pk ON uk.measurement_units_oplog USING btree (measurement_unit_code, validity_start_date);


--
-- Name: meas_unit_qual_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_unit_qual_desc_pk ON uk.measurement_unit_qualifier_descriptions_oplog USING btree (measurement_unit_qualifier_code);


--
-- Name: meas_unit_qual_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meas_unit_qual_pk ON uk.measurement_unit_qualifiers_oplog USING btree (measurement_unit_qualifier_code, validity_start_date);


--
-- Name: measrm_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measrm_pk ON uk.measurements_oplog USING btree (measurement_unit_code, measurement_unit_qualifier_code);


--
-- Name: measure_components_covering_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_components_covering_index ON uk.measure_components USING btree (measure_sid, duty_expression_id) INCLUDE (duty_amount, monetary_unit_code, measurement_unit_code, measurement_unit_qualifier_code, filename);


--
-- Name: measure_components_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX measure_components_oid_index ON uk.measure_components USING btree (oid);


--
-- Name: measure_conditions_measure_condition_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_conditions_measure_condition_sid_index ON uk.measure_conditions USING btree (measure_condition_sid);


--
-- Name: measure_conditions_measure_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_conditions_measure_sid_index ON uk.measure_conditions USING btree (measure_sid);


--
-- Name: measure_conditions_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX measure_conditions_oid_index ON uk.measure_conditions USING btree (oid);


--
-- Name: measure_conditions_oplog_certificate_type_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_conditions_oplog_certificate_type_code_index ON uk.measure_conditions_oplog USING btree (certificate_type_code);


--
-- Name: measure_excluded_geographical_areas_geographical_area_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_excluded_geographical_areas_geographical_area_sid_index ON uk.measure_excluded_geographical_areas USING btree (geographical_area_sid);


--
-- Name: measure_excluded_geographical_areas_measure_sid_excluded_geogra; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_excluded_geographical_areas_measure_sid_excluded_geogra ON uk.measure_excluded_geographical_areas USING btree (measure_sid, excluded_geographical_area, geographical_area_sid);


--
-- Name: measure_excluded_geographical_areas_measure_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_excluded_geographical_areas_measure_sid_index ON uk.measure_excluded_geographical_areas USING btree (measure_sid);


--
-- Name: measure_excluded_geographical_areas_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX measure_excluded_geographical_areas_oid_index ON uk.measure_excluded_geographical_areas USING btree (oid);


--
-- Name: measure_excluded_geographical_areas_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_excluded_geographical_areas_operation_date_index ON uk.measure_excluded_geographical_areas USING btree (operation_date);


--
-- Name: measure_generating_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_generating_regulation ON uk.measures_oplog USING btree (measure_generating_regulation_id);


--
-- Name: measure_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measure_sid ON uk.footnote_association_measures_oplog USING btree (measure_sid);


--
-- Name: measurement_unit_code_qualifier; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measurement_unit_code_qualifier ON uk.measurement_unit_abbreviations USING btree (measurement_unit_code, measurement_unit_qualifier);


--
-- Name: measurement_unit_qualifier_code; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measurement_unit_qualifier_code ON uk.measure_condition_components_oplog USING btree (measurement_unit_qualifier_code);


--
-- Name: measures_additional_code_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_additional_code_id_index ON uk.measures USING btree (additional_code_id);


--
-- Name: measures_additional_code_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_additional_code_sid_index ON uk.measures USING btree (additional_code_sid);


--
-- Name: measures_additional_code_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_additional_code_type_id_index ON uk.measures USING btree (additional_code_type_id);


--
-- Name: measures_export_refund_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_export_refund_nomenclature_sid_index ON uk.measures_oplog USING btree (export_refund_nomenclature_sid);


--
-- Name: measures_geographical_area_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_geographical_area_sid_index ON uk.measures USING btree (geographical_area_sid);


--
-- Name: measures_goods_nomenclature_item_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_goods_nomenclature_item_id_index ON uk.measures_oplog USING btree (goods_nomenclature_item_id);


--
-- Name: measures_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_goods_nomenclature_sid_index ON uk.measures USING btree (goods_nomenclature_sid);


--
-- Name: measures_goods_nomenclature_sid_validity_composite_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_goods_nomenclature_sid_validity_composite_index ON uk.measures USING btree (goods_nomenclature_sid, validity_start_date, validity_end_date);


--
-- Name: measures_justification_regulation_role_justification_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_justification_regulation_role_justification_regulation ON uk.measures USING btree (justification_regulation_role, justification_regulation_id);


--
-- Name: measures_measure_generating_regulation_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_measure_generating_regulation_id_index ON uk.measures USING btree (measure_generating_regulation_id);


--
-- Name: measures_measure_generating_regulation_role_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_measure_generating_regulation_role_index ON uk.measures USING btree (measure_generating_regulation_role);


--
-- Name: measures_measure_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_measure_sid_index ON uk.measures USING btree (measure_sid);


--
-- Name: measures_measure_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_measure_type_id_index ON uk.measures USING btree (measure_type_id);


--
-- Name: measures_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX measures_oid_index ON uk.measures USING btree (oid);


--
-- Name: measures_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_operation_date_index ON uk.measures USING btree (operation_date);


--
-- Name: measures_oplog_additional_code_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_oplog_additional_code_id_index ON uk.measures_oplog USING btree (additional_code_id);


--
-- Name: measures_oplog_additional_code_type_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_oplog_additional_code_type_id_index ON uk.measures_oplog USING btree (additional_code_type_id);


--
-- Name: measures_oplog_measure_generating_regulation_role_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_oplog_measure_generating_regulation_role_index ON uk.measures_oplog USING btree (measure_generating_regulation_role);


--
-- Name: measures_oplog_ordernumber_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_oplog_ordernumber_validity_start_date_index ON uk.measures_oplog USING btree (ordernumber, validity_start_date);


--
-- Name: measures_oplog_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_oplog_validity_end_date_index ON uk.measures_oplog USING btree (validity_end_date);


--
-- Name: measures_oplog_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_oplog_validity_start_date_index ON uk.measures_oplog USING btree (validity_start_date);


--
-- Name: measures_ordernumber_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_ordernumber_validity_start_date_index ON uk.measures USING btree (ordernumber, validity_start_date);


--
-- Name: measures_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_validity_end_date_index ON uk.measures USING btree (validity_end_date);


--
-- Name: measures_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_validity_start_date_index ON uk.measures USING btree (validity_start_date);


--
-- Name: measures_view_export_refund_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_view_export_refund_nomenclature_sid_index ON uk.measures USING btree (export_refund_nomenclature_sid);


--
-- Name: measures_view_goods_nomenclature_item_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX measures_view_goods_nomenclature_item_id_index ON uk.measures USING btree (goods_nomenclature_item_id);


--
-- Name: megao_meaexcgeoareopl_urededcaleaslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX megao_meaexcgeoareopl_urededcaleaslog_operation_date ON uk.measure_excluded_geographical_areas_oplog USING btree (operation_date);


--
-- Name: mepo_monexcperopl_aryngeodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mepo_monexcperopl_aryngeodslog_operation_date ON uk.monetary_exchange_periods_oplog USING btree (operation_date);


--
-- Name: mero_monexcratopl_aryngeteslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mero_monexcratopl_aryngeteslog_operation_date ON uk.monetary_exchange_rates_oplog USING btree (operation_date);


--
-- Name: meurs_adco_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meurs_adco_pk ON uk.meursing_additional_codes_oplog USING btree (meursing_additional_code_sid);


--
-- Name: meurs_head_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meurs_head_pk ON uk.meursing_headings_oplog USING btree (meursing_table_plan_id, meursing_heading_number, row_column_code);


--
-- Name: meurs_head_txt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meurs_head_txt_pk ON uk.meursing_heading_texts_oplog USING btree (meursing_table_plan_id, meursing_heading_number, row_column_code);


--
-- Name: meurs_subhead_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meurs_subhead_pk ON uk.meursing_subheadings_oplog USING btree (meursing_table_plan_id, meursing_heading_number, row_column_code, subheading_sequence_number);


--
-- Name: meurs_tbl_cell_comp_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meurs_tbl_cell_comp_pk ON uk.meursing_table_cell_components_oplog USING btree (meursing_table_plan_id, heading_number, row_column_code, meursing_additional_code_sid);


--
-- Name: meurs_tbl_plan_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX meurs_tbl_plan_pk ON uk.meursing_table_plans_oplog USING btree (meursing_table_plan_id);


--
-- Name: mho_meuheaopl_ingngslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mho_meuheaopl_ingngslog_operation_date ON uk.meursing_headings_oplog USING btree (operation_date);


--
-- Name: mhto_meuheatexopl_ingingxtslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mhto_meuheatexopl_ingingxtslog_operation_date ON uk.meursing_heading_texts_oplog USING btree (operation_date);


--
-- Name: mo_meaopl_ntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mo_meaopl_ntslog_operation_date ON uk.measurements_oplog USING btree (operation_date);


--
-- Name: mo_meaopl_reslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mo_meaopl_reslog_operation_date ON uk.measures_oplog USING btree (operation_date);


--
-- Name: mod_reg_complete_abrogation_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mod_reg_complete_abrogation_regulation ON uk.modification_regulations_oplog USING btree (complete_abrogation_regulation_id, complete_abrogation_regulation_role);


--
-- Name: mod_reg_explicit_abrogation_regulation; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mod_reg_explicit_abrogation_regulation ON uk.modification_regulations_oplog USING btree (explicit_abrogation_regulation_id, explicit_abrogation_regulation_role);


--
-- Name: mod_reg_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mod_reg_pk ON uk.modification_regulations_oplog USING btree (modification_regulation_id, modification_regulation_role);


--
-- Name: modification_regulations_approved_composite_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_approved_composite_index ON uk.modification_regulations USING btree (modification_regulation_id, modification_regulation_role) WHERE (approved_flag IS TRUE);


--
-- Name: modification_regulations_approved_flag_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_approved_flag_index ON uk.modification_regulations USING btree (approved_flag);


--
-- Name: modification_regulations_base_regulation_id_base_regulation_rol; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_base_regulation_id_base_regulation_rol ON uk.modification_regulations USING btree (base_regulation_id, base_regulation_role);


--
-- Name: modification_regulations_complete_abrogation_regulation_role_co; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_complete_abrogation_regulation_role_co ON uk.modification_regulations USING btree (complete_abrogation_regulation_role, complete_abrogation_regulation_id);


--
-- Name: modification_regulations_effective_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_effective_end_date_index ON uk.modification_regulations USING btree (effective_end_date);


--
-- Name: modification_regulations_explicit_abrogation_regulation_role_ex; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_explicit_abrogation_regulation_role_ex ON uk.modification_regulations USING btree (explicit_abrogation_regulation_role, explicit_abrogation_regulation_id);


--
-- Name: modification_regulations_modification_regulation_id_modificatio; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_modification_regulation_id_modificatio ON uk.modification_regulations USING btree (modification_regulation_id, modification_regulation_role);


--
-- Name: modification_regulations_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX modification_regulations_oid_index ON uk.modification_regulations USING btree (oid);


--
-- Name: modification_regulations_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_operation_date_index ON uk.modification_regulations USING btree (operation_date);


--
-- Name: modification_regulations_oplog_approved_flag_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_oplog_approved_flag_index ON uk.modification_regulations_oplog USING btree (approved_flag);


--
-- Name: modification_regulations_oplog_effective_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_oplog_effective_end_date_index ON uk.modification_regulations_oplog USING btree (effective_end_date);


--
-- Name: modification_regulations_oplog_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_oplog_validity_end_date_index ON uk.modification_regulations_oplog USING btree (validity_end_date);


--
-- Name: modification_regulations_oplog_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_oplog_validity_start_date_index ON uk.modification_regulations_oplog USING btree (validity_start_date);


--
-- Name: modification_regulations_validity_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_validity_end_date_index ON uk.modification_regulations USING btree (validity_end_date);


--
-- Name: modification_regulations_validity_start_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX modification_regulations_validity_start_date_index ON uk.modification_regulations USING btree (validity_start_date);


--
-- Name: mon_exch_period_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mon_exch_period_pk ON uk.monetary_exchange_periods_oplog USING btree (monetary_exchange_period_sid, parent_monetary_unit_code);


--
-- Name: mon_exch_rate_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mon_exch_rate_pk ON uk.monetary_exchange_rates_oplog USING btree (monetary_exchange_period_sid, child_monetary_unit_code);


--
-- Name: mon_unit_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mon_unit_desc_pk ON uk.monetary_unit_descriptions_oplog USING btree (monetary_unit_code);


--
-- Name: mon_unit_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mon_unit_pk ON uk.monetary_units_oplog USING btree (monetary_unit_code, validity_start_date);


--
-- Name: mptso_meapartemstoopl_ureialaryopslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mptso_meapartemstoopl_ureialaryopslog_operation_date ON uk.measure_partial_temporary_stops_oplog USING btree (operation_date);


--
-- Name: mro_modregopl_iononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mro_modregopl_iononslog_operation_date ON uk.modification_regulations_oplog USING btree (operation_date);


--
-- Name: mso_meusubopl_ingngslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mso_meusubopl_ingngslog_operation_date ON uk.meursing_subheadings_oplog USING btree (operation_date);


--
-- Name: mtcco_meutabcelcomopl_ingbleellntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mtcco_meutabcelcomopl_ingbleellntslog_operation_date ON uk.meursing_table_cell_components_oplog USING btree (operation_date);


--
-- Name: mtdo_meatypdesopl_ureypeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mtdo_meatypdesopl_ureypeonslog_operation_date ON uk.measure_type_descriptions_oplog USING btree (operation_date);


--
-- Name: mto_meatypopl_urepeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mto_meatypopl_urepeslog_operation_date ON uk.measure_types_oplog USING btree (operation_date);


--
-- Name: mtpo_meutabplaopl_ingbleanslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mtpo_meutabplaopl_ingbleanslog_operation_date ON uk.meursing_table_plans_oplog USING btree (operation_date);


--
-- Name: mtsdo_meatypserdesopl_ureypeiesonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mtsdo_meatypserdesopl_ureypeiesonslog_operation_date ON uk.measure_type_series_descriptions_oplog USING btree (operation_date);


--
-- Name: mtso_meatypseropl_ureypeieslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mtso_meatypseropl_ureypeieslog_operation_date ON uk.measure_type_series_oplog USING btree (operation_date);


--
-- Name: mudo_meaunidesopl_entnitonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mudo_meaunidesopl_entnitonslog_operation_date ON uk.measurement_unit_descriptions_oplog USING btree (operation_date);


--
-- Name: mudo_monunidesopl_arynitonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX mudo_monunidesopl_arynitonslog_operation_date ON uk.monetary_unit_descriptions_oplog USING btree (operation_date);


--
-- Name: muo_meauniopl_entitslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX muo_meauniopl_entitslog_operation_date ON uk.measurement_units_oplog USING btree (operation_date);


--
-- Name: muo_monuniopl_aryitslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX muo_monuniopl_aryitslog_operation_date ON uk.monetary_units_oplog USING btree (operation_date);


--
-- Name: muqdo_meauniquadesopl_entnitieronslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX muqdo_meauniquadesopl_entnitieronslog_operation_date ON uk.measurement_unit_qualifier_descriptions_oplog USING btree (operation_date);


--
-- Name: muqo_meauniquaopl_entniterslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX muqo_meauniquaopl_entniterslog_operation_date ON uk.measurement_unit_qualifiers_oplog USING btree (operation_date);


--
-- Name: news_collections_news_items_item_id_collection_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX news_collections_news_items_item_id_collection_id_index ON uk.news_collections_news_items USING btree (item_id, collection_id);


--
-- Name: news_items_show_on_uk_show_on_xi_show_on_updates_page_show_on_h; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX news_items_show_on_uk_show_on_xi_show_on_updates_page_show_on_h ON uk.news_items USING btree (show_on_uk, show_on_xi, show_on_updates_page, show_on_home_page, start_date, end_date);


--
-- Name: news_items_slug_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX news_items_slug_index ON uk.news_items USING btree (slug);


--
-- Name: news_items_updated_at_start_date_end_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX news_items_updated_at_start_date_end_date_index ON uk.news_items USING btree (updated_at, start_date, end_date);


--
-- Name: ngmo_nomgromemopl_ureoupipslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX ngmo_nomgromemopl_ureoupipslog_operation_date ON uk.nomenclature_group_memberships_oplog USING btree (operation_date);


--
-- Name: nom_grp_member_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX nom_grp_member_pk ON uk.nomenclature_group_memberships_oplog USING btree (goods_nomenclature_sid, goods_nomenclature_group_id, goods_nomenclature_group_type, goods_nomenclature_item_id, validity_start_date);


--
-- Name: period_sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX period_sid ON uk.additional_code_descriptions_oplog USING btree (additional_code_description_period_sid);


--
-- Name: prao_proregactopl_ioniononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX prao_proregactopl_ioniononslog_operation_date ON uk.prorogation_regulation_actions_oplog USING btree (operation_date);


--
-- Name: primary_key; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX primary_key ON uk.geographical_areas_oplog USING btree (geographical_area_sid);


--
-- Name: pro_proregopl_iononslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX pro_proregopl_iononslog_operation_date ON uk.prorogation_regulations_oplog USING btree (operation_date);


--
-- Name: prorog_reg_act_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX prorog_reg_act_pk ON uk.prorogation_regulation_actions_oplog USING btree (prorogation_regulation_id, prorogation_regulation_role, prorogated_regulation_id, prorogated_regulation_role);


--
-- Name: prorog_reg_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX prorog_reg_pk ON uk.prorogation_regulations_oplog USING btree (prorogation_regulation_id, prorogation_regulation_role);


--
-- Name: qao_quoassopl_otaonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qao_quoassopl_otaonslog_operation_date ON uk.quota_associations_oplog USING btree (operation_date);


--
-- Name: qbeo_quobaleveopl_otancentslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qbeo_quobaleveopl_otancentslog_operation_date ON uk.quota_balance_events_oplog USING btree (operation_date);


--
-- Name: qbpo_quobloperopl_otaingodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qbpo_quobloperopl_otaingodslog_operation_date ON uk.quota_blocking_periods_oplog USING btree (operation_date);


--
-- Name: qceo_quocrieveopl_otacalntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qceo_quocrieveopl_otacalntslog_operation_date ON uk.quota_critical_events_oplog USING btree (operation_date);


--
-- Name: qdo_quodefopl_otaonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qdo_quodefopl_otaonslog_operation_date ON uk.quota_definitions_oplog USING btree (operation_date);


--
-- Name: qeeo_quoexheveopl_otaionntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qeeo_quoexheveopl_otaionntslog_operation_date ON uk.quota_exhaustion_events_oplog USING btree (operation_date);


--
-- Name: qono_quoordnumopl_otadererslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qono_quoordnumopl_otadererslog_operation_date ON uk.quota_order_numbers_oplog USING btree (operation_date);


--
-- Name: qonoeo_quoordnumoriexcopl_otaderberginonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qonoeo_quoordnumoriexcopl_otaderberginonslog_operation_date ON uk.quota_order_number_origin_exclusions_oplog USING btree (operation_date);


--
-- Name: qonoo_quoordnumoriopl_otaderberinslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qonoo_quoordnumoriopl_otaderberinslog_operation_date ON uk.quota_order_number_origins_oplog USING btree (operation_date);


--
-- Name: qreo_quoreoeveopl_otaingntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qreo_quoreoeveopl_otaingntslog_operation_date ON uk.quota_reopening_events_oplog USING btree (operation_date);


--
-- Name: qspo_quosusperopl_otaionodslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX qspo_quosusperopl_otaionodslog_operation_date ON uk.quota_suspension_periods_oplog USING btree (operation_date);


--
-- Name: queo_quounbeveopl_otaingntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX queo_quounbeveopl_otaingntslog_operation_date ON uk.quota_unblocking_events_oplog USING btree (operation_date);


--
-- Name: queo_quounseveopl_otaionntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX queo_quounseveopl_otaionntslog_operation_date ON uk.quota_unsuspension_events_oplog USING btree (operation_date);


--
-- Name: quota_assoc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_assoc_pk ON uk.quota_associations_oplog USING btree (main_quota_definition_sid, sub_quota_definition_sid);


--
-- Name: quota_balance_events_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_balance_events_oid_index ON uk.quota_balance_events USING btree (oid);


--
-- Name: quota_balance_events_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_balance_events_operation_date_index ON uk.quota_balance_events USING btree (operation_date);


--
-- Name: quota_balance_events_quota_definition_sid, occurrence_timestamp; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX "quota_balance_events_quota_definition_sid, occurrence_timestamp" ON uk.quota_balance_events USING btree (quota_definition_sid, occurrence_timestamp, oid DESC);


--
-- Name: quota_balance_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_balance_evt_pk ON uk.quota_balance_events_oplog USING btree (quota_definition_sid, occurrence_timestamp, oid DESC);


--
-- Name: quota_block_period_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_block_period_pk ON uk.quota_blocking_periods_oplog USING btree (quota_blocking_period_sid);


--
-- Name: quota_closed_and_transferred_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_closed_and_transferred_evt_pk ON uk.quota_closed_and_transferred_events_oplog USING btree (quota_definition_sid, occurrence_timestamp, oid);


--
-- Name: quota_crit_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_crit_evt_pk ON uk.quota_critical_events_oplog USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_critical_events_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_critical_events_oid_index ON uk.quota_critical_events USING btree (oid);


--
-- Name: quota_critical_events_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_critical_events_operation_date_index ON uk.quota_critical_events USING btree (operation_date);


--
-- Name: quota_critical_events_quota_definition_sid_occurrence_timestamp; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_critical_events_quota_definition_sid_occurrence_timestamp ON uk.quota_critical_events USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_def_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_def_pk ON uk.quota_definitions_oplog USING btree (quota_definition_sid, oid DESC);


--
-- Name: quota_definitions_measurement_unit_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_definitions_measurement_unit_code_index ON uk.quota_definitions USING btree (measurement_unit_code);


--
-- Name: quota_definitions_measurement_unit_qualifier_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_definitions_measurement_unit_qualifier_code_index ON uk.quota_definitions USING btree (measurement_unit_qualifier_code);


--
-- Name: quota_definitions_monetary_unit_code_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_definitions_monetary_unit_code_index ON uk.quota_definitions USING btree (monetary_unit_code);


--
-- Name: quota_definitions_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_definitions_oid_index ON uk.quota_definitions USING btree (oid);


--
-- Name: quota_definitions_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_definitions_operation_date_index ON uk.quota_definitions USING btree (operation_date);


--
-- Name: quota_definitions_quota_definition_sid, oid DESC_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX "quota_definitions_quota_definition_sid, oid DESC_index" ON uk.quota_definitions USING btree (quota_definition_sid, oid DESC);


--
-- Name: quota_definitions_quota_order_number_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_definitions_quota_order_number_id_index ON uk.quota_definitions USING btree (quota_order_number_id);


--
-- Name: quota_exhaus_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_exhaus_evt_pk ON uk.quota_exhaustion_events_oplog USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_exhaustion_events_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_exhaustion_events_oid_index ON uk.quota_exhaustion_events USING btree (oid);


--
-- Name: quota_exhaustion_events_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_exhaustion_events_operation_date_index ON uk.quota_exhaustion_events USING btree (operation_date);


--
-- Name: quota_exhaustion_events_quota_definition_sid_occurrence_timesta; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_exhaustion_events_quota_definition_sid_occurrence_timesta ON uk.quota_exhaustion_events USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_ord_num_excl_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_ord_num_excl_pk ON uk.quota_order_number_origin_exclusions_oplog USING btree (quota_order_number_origin_sid, excluded_geographical_area_sid);


--
-- Name: quota_ord_num_orig_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_ord_num_orig_pk ON uk.quota_order_number_origins_oplog USING btree (quota_order_number_origin_sid);


--
-- Name: quota_ord_num_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_ord_num_pk ON uk.quota_order_numbers_oplog USING btree (quota_order_number_sid);


--
-- Name: quota_reopen_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_reopen_evt_pk ON uk.quota_reopening_events_oplog USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_reopening_events_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_reopening_events_oid_index ON uk.quota_reopening_events USING btree (oid);


--
-- Name: quota_reopening_events_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_reopening_events_operation_date_index ON uk.quota_reopening_events USING btree (operation_date);


--
-- Name: quota_reopening_events_quota_definition_sid_occurrence_timestam; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_reopening_events_quota_definition_sid_occurrence_timestam ON uk.quota_reopening_events USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_susp_period_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_susp_period_pk ON uk.quota_suspension_periods_oplog USING btree (quota_suspension_period_sid);


--
-- Name: quota_unblock_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_unblock_evt_pk ON uk.quota_unblocking_events_oplog USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_unblocking_events_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_unblocking_events_oid_index ON uk.quota_unblocking_events USING btree (oid);


--
-- Name: quota_unblocking_events_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_unblocking_events_operation_date_index ON uk.quota_unblocking_events USING btree (operation_date);


--
-- Name: quota_unblocking_events_quota_definition_sid_occurrence_timesta; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_unblocking_events_quota_definition_sid_occurrence_timesta ON uk.quota_unblocking_events USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_unsusp_evt_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_unsusp_evt_pk ON uk.quota_unsuspension_events_oplog USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: quota_unsuspension_events_oid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE UNIQUE INDEX quota_unsuspension_events_oid_index ON uk.quota_unsuspension_events USING btree (oid);


--
-- Name: quota_unsuspension_events_operation_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_unsuspension_events_operation_date_index ON uk.quota_unsuspension_events USING btree (operation_date);


--
-- Name: quota_unsuspension_events_quota_definition_sid_occurrence_times; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX quota_unsuspension_events_quota_definition_sid_occurrence_times ON uk.quota_unsuspension_events USING btree (quota_definition_sid, occurrence_timestamp);


--
-- Name: reg_grp_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX reg_grp_desc_pk ON uk.regulation_group_descriptions_oplog USING btree (regulation_group_id);


--
-- Name: reg_grp_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX reg_grp_pk ON uk.regulation_groups_oplog USING btree (regulation_group_id);


--
-- Name: reg_role_type_desc_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX reg_role_type_desc_pk ON uk.regulation_role_type_descriptions_oplog USING btree (regulation_role_type_id);


--
-- Name: reg_role_type_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX reg_role_type_pk ON uk.regulation_role_types_oplog USING btree (regulation_role_type_id);


--
-- Name: rgdo_reggrodesopl_ionouponslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX rgdo_reggrodesopl_ionouponslog_operation_date ON uk.regulation_group_descriptions_oplog USING btree (operation_date);


--
-- Name: rgo_reggroopl_ionupslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX rgo_reggroopl_ionupslog_operation_date ON uk.regulation_groups_oplog USING btree (operation_date);


--
-- Name: rr_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX rr_pk ON uk.regulation_replacements_oplog USING btree (replaced_regulation_role, replaced_regulation_id);


--
-- Name: rro_regrepopl_ionntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX rro_regrepopl_ionntslog_operation_date ON uk.regulation_replacements_oplog USING btree (operation_date);


--
-- Name: rrtdo_regroltypdesopl_ionoleypeonslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX rrtdo_regroltypdesopl_ionoleypeonslog_operation_date ON uk.regulation_role_type_descriptions_oplog USING btree (operation_date);


--
-- Name: rrto_regroltypopl_ionolepeslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX rrto_regroltypopl_ionolepeslog_operation_date ON uk.regulation_role_types_oplog USING btree (operation_date);


--
-- Name: search_references_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX search_references_goods_nomenclature_sid_index ON uk.search_references USING btree (goods_nomenclature_sid);


--
-- Name: search_suggestions_declarable_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX search_suggestions_declarable_index ON uk.search_suggestions USING btree (declarable);


--
-- Name: search_suggestions_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX search_suggestions_goods_nomenclature_sid_index ON uk.search_suggestions USING btree (goods_nomenclature_sid);


--
-- Name: search_suggestions_type_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX search_suggestions_type_index ON uk.search_suggestions USING btree (type);


--
-- Name: section_notes_section_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX section_notes_section_id_index ON uk.section_notes USING btree (section_id);


--
-- Name: sid; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX sid ON uk.additional_code_descriptions_oplog USING btree (additional_code_sid);


--
-- Name: tariff_changes_commodity_date_sid_action_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_changes_commodity_date_sid_action_index ON uk.tariff_changes USING btree (operation_date, goods_nomenclature_sid, action) WHERE (type = 'Commodity'::text);


--
-- Name: tariff_changes_commodity_description_date_sid_action_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_changes_commodity_description_date_sid_action_index ON uk.tariff_changes USING btree (operation_date, goods_nomenclature_sid, action) WHERE (type = 'GoodsNomenclatureDescription'::text);


--
-- Name: tariff_changes_goods_nomenclature_item_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_changes_goods_nomenclature_item_id_index ON uk.tariff_changes USING btree (goods_nomenclature_item_id);


--
-- Name: tariff_changes_measure_date_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_changes_measure_date_sid_index ON uk.tariff_changes USING btree (operation_date, goods_nomenclature_sid) WHERE (type = 'Measure'::text);


--
-- Name: tariff_changes_metadata_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_changes_metadata_index ON uk.tariff_changes USING gin (metadata);


--
-- Name: tariff_changes_operation_date_goods_nomenclature_sid_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_changes_operation_date_goods_nomenclature_sid_index ON uk.tariff_changes USING btree (operation_date, goods_nomenclature_sid);


--
-- Name: tariff_update_presence_errors_tariff_update_filename_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_update_presence_errors_tariff_update_filename_index ON uk.tariff_update_presence_errors USING btree (tariff_update_filename);


--
-- Name: tariff_update_state_changes_tariff_update_filename_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_update_state_changes_tariff_update_filename_index ON uk.tariff_update_state_changes USING btree (tariff_update_filename);


--
-- Name: tariff_updates_issue_date_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tariff_updates_issue_date_index ON uk.tariff_updates USING btree (issue_date);


--
-- Name: tco_tracomopl_ionntslog_operation_date; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX tco_tracomopl_ionntslog_operation_date ON uk.transmission_comments_oplog USING btree (operation_date);


--
-- Name: trans_comm_pk; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX trans_comm_pk ON uk.transmission_comments_oplog USING btree (comment_sid, language_id);


--
-- Name: type_id; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX type_id ON uk.additional_code_descriptions_oplog USING btree (additional_code_type_id);


--
-- Name: versions_created_at_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX versions_created_at_index ON uk.versions USING btree (created_at);


--
-- Name: versions_item_type_item_id_index; Type: INDEX; Schema: uk; Owner: postgres
--

CREATE INDEX versions_item_type_item_id_index ON uk.versions USING btree (item_type, item_id);


--
-- Name: commodity_facets audit_commodity_facets; Type: TRIGGER; Schema: kg; Owner: postgres
--

CREATE TRIGGER audit_commodity_facets AFTER INSERT OR DELETE OR UPDATE ON kg.commodity_facets FOR EACH ROW EXECUTE FUNCTION kg.fn_audit_trigger();


--
-- Name: kg_edges audit_kg_edges; Type: TRIGGER; Schema: kg; Owner: postgres
--

CREATE TRIGGER audit_kg_edges AFTER INSERT OR DELETE OR UPDATE ON kg.kg_edges FOR EACH ROW EXECUTE FUNCTION kg.fn_audit_trigger();


--
-- Name: kg_edges mark_edge_embedding_stale; Type: TRIGGER; Schema: kg; Owner: postgres
--

CREATE TRIGGER mark_edge_embedding_stale BEFORE UPDATE ON kg.kg_edges FOR EACH ROW EXECUTE FUNCTION kg.fn_mark_edge_embedding_stale();


--
-- Name: commodity_facets mark_facet_embedding_stale; Type: TRIGGER; Schema: kg; Owner: postgres
--

CREATE TRIGGER mark_facet_embedding_stale BEFORE UPDATE ON kg.commodity_facets FOR EACH ROW EXECUTE FUNCTION kg.fn_mark_facet_embedding_stale();


--
-- Name: commodity_facets commodity_facets_facet_key_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_facets
    ADD CONSTRAINT commodity_facets_facet_key_fkey FOREIGN KEY (facet_key) REFERENCES kg.facet_definitions(key) ON DELETE CASCADE;


--
-- Name: commodity_facets commodity_facets_superseded_by_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.commodity_facets
    ADD CONSTRAINT commodity_facets_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES kg.commodity_facets(id) ON DELETE SET NULL;


--
-- Name: e2e_eval_results e2e_eval_results_run_id_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.e2e_eval_results
    ADD CONSTRAINT e2e_eval_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES kg.e2e_eval_runs(id) ON DELETE CASCADE;


--
-- Name: eval_run_results eval_run_results_gold_id_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_run_results
    ADD CONSTRAINT eval_run_results_gold_id_fkey FOREIGN KEY (gold_id) REFERENCES kg.eval_gold(id);


--
-- Name: eval_run_results eval_run_results_run_id_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.eval_run_results
    ADD CONSTRAINT eval_run_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES kg.eval_runs(id) ON DELETE CASCADE;


--
-- Name: kg_edge_commodities kg_edge_commodities_edge_id_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.kg_edge_commodities
    ADD CONSTRAINT kg_edge_commodities_edge_id_fkey FOREIGN KEY (edge_id) REFERENCES kg.kg_edges(id) ON DELETE CASCADE;


--
-- Name: kg_edges kg_edges_superseded_by_fkey; Type: FK CONSTRAINT; Schema: kg; Owner: postgres
--

ALTER TABLE ONLY kg.kg_edges
    ADD CONSTRAINT kg_edges_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES kg.kg_edges(id) ON DELETE SET NULL;


--
-- Name: chemical_names chemical_names_chemical_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.chemical_names
    ADD CONSTRAINT chemical_names_chemical_id_fkey FOREIGN KEY (chemical_id) REFERENCES uk.chemicals(id);


--
-- Name: green_lanes_category_assessments_exemptions green_lanes_category_assessments_ex_category_assessment_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_category_assessments_exemptions
    ADD CONSTRAINT green_lanes_category_assessments_ex_category_assessment_id_fkey FOREIGN KEY (category_assessment_id) REFERENCES uk.green_lanes_category_assessments(id);


--
-- Name: green_lanes_category_assessments_exemptions green_lanes_category_assessments_exemptions_exemption_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_category_assessments_exemptions
    ADD CONSTRAINT green_lanes_category_assessments_exemptions_exemption_id_fkey FOREIGN KEY (exemption_id) REFERENCES uk.green_lanes_exemptions(id);


--
-- Name: green_lanes_category_assessments green_lanes_category_assessments_theme_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_category_assessments
    ADD CONSTRAINT green_lanes_category_assessments_theme_id_fkey FOREIGN KEY (theme_id) REFERENCES uk.green_lanes_themes(id);


--
-- Name: green_lanes_identified_measure_type_category_assessments green_lanes_identified_measure_type_category_asse_theme_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_identified_measure_type_category_assessments
    ADD CONSTRAINT green_lanes_identified_measure_type_category_asse_theme_id_fkey FOREIGN KEY (theme_id) REFERENCES uk.green_lanes_themes(id);


--
-- Name: green_lanes_measures green_lanes_measures_category_assessment_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_measures
    ADD CONSTRAINT green_lanes_measures_category_assessment_id_fkey FOREIGN KEY (category_assessment_id) REFERENCES uk.green_lanes_category_assessments(id);


--
-- Name: green_lanes_update_notifications green_lanes_update_notifications_theme_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.green_lanes_update_notifications
    ADD CONSTRAINT green_lanes_update_notifications_theme_id_fkey FOREIGN KEY (theme_id) REFERENCES uk.green_lanes_themes(id) ON DELETE SET NULL;


--
-- Name: news_collections_news_items news_collections_news_items_collection_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_collections_news_items
    ADD CONSTRAINT news_collections_news_items_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES uk.news_collections(id);


--
-- Name: news_collections_news_items news_collections_news_items_item_id_fkey; Type: FK CONSTRAINT; Schema: uk; Owner: postgres
--

ALTER TABLE ONLY uk.news_collections_news_items
    ADD CONSTRAINT news_collections_news_items_item_id_fkey FOREIGN KEY (item_id) REFERENCES uk.news_items(id);


--
-- PostgreSQL database dump complete
--

\unrestrict aN8F3dBehoGlQAb3SB6Gwmc0G5MW7zHDdaAMT5YXiI66JzhqiB6OcCI5Z5sLBjy

