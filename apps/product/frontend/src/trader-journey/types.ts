// Mirrors backend/schemas.py - keep in sync.

export interface CandidateCC {
  commodity_code: string;
  code_dotted: string;
  description: string;
  score: number;
  sources: string[];
  in_slice: boolean;
}

// Standing final option appended to every question's options. On selection,
// send it back verbatim as the answer - the backend keeps the candidates and
// re-asks on a different facet.
export const NONE_OF_THESE_OPTION = "None of these / not sure";

export interface LLMQuestion {
  question: string;
  // Always ends with the standing NONE_OF_THESE_OPTION entry.
  options: string[];
}

// Server-side confidence enum - the backend sends exactly these values.
export type AnswerConfidence = "Best match" | "Also possible" | "From retrieval";

export interface LLMAnswer {
  commodity_code: string;
  confidence: string; // one of AnswerConfidence
  description: string;
  // True when the answer was auto-descended from a parent code to its
  // declarable leaf child.
  leaf_adjusted?: boolean | null;
}

export interface KGNote {
  id: string;
  title: string;
  body: string;
  source: string;
}

// Data-source health for a classify turn - render a banner when not "live".
export type RetrievalHealth = "live" | "fixture" | "degraded" | "infra-error";

export interface ClassifyTurn {
  candidates: CandidateCC[];
  // Frozen retrieval pool - returned on every turn; echo back on answer calls.
  fixed_candidates: Record<string, unknown>[];
  mode: "answers" | "questions" | "error" | "no_candidates";
  answers: LLMAnswer[];
  question: LLMQuestion | null;
  kg_notes: KGNote[];
  facet_enrichment: {
    code: string;
    facets: Record<string, string>;
    common_terms: string[];
    self_text: string;
    kg_edges: string[];
  } | null;
  augmentation_summary: {
    candidates_with_facets?: number;
    total_candidates?: number;
    kg_edges_applied?: number;
    candidate_selection?: {
      mode?: "llm" | "deterministic";
      model?: string;
      reason?: string;
    };
    question_cap?: number;
    question_cap_coerced?: boolean;
    frozen_pool_size?: number;
    rescue?: Record<string, unknown> | null;
    leaf_adjustment?: Record<string, unknown> | null;
    high_confidence?: boolean;
  };
  error_message: string | null;
  qa_history: { question: string; answer: string }[];
  retrieval_health?: RetrievalHealth | null;
}

export interface HydrationEvidence {
  kind: string;
  id: string;
  title: string;
  body: string;
  body_truncated: boolean;
  source: string;
  authority_tier?: number | null;
  scope?: string;
  url?: string | null;
  provenance?: Record<string, unknown>;
}

export interface CommodityHydration {
  ok: boolean;
  commodity_code: string;
  code_dotted: string;
  commodity: {
    code: string;
    code_dotted?: string;
    description?: string;
    [key: string]: unknown;
  };
  model_requested?: string | null;
  sources_requested?: Partial<HydrationSources>;
  candidate_guardrail: string;
  coverage: {
    counts_by_kind: Record<string, number>;
    has_atar?: boolean;
    has_hsen?: boolean;
    has_legal_notes?: boolean;
    has_footnotes?: boolean;
    has_facets?: boolean;
  };
  summary: {
    mode: string;
    counts_by_kind: Record<string, number>;
    bullets: string[];
    llm?: {
      enabled: boolean;
      model?: string;
      text?: string;
      reason?: string;
      input_tokens?: number;
      output_tokens?: number;
    };
  };
  evidence: HydrationEvidence[];
}

export interface CandidateHydrationRun {
  query: string;
  summarize: boolean;
  model_requested?: string | null;
  candidate_count: number;
  hydrate_limit: number;
  cache_write: boolean;
  retrieval_guardrail: string;
  candidates: CandidateCC[];
  hydrated: { candidate: CandidateCC; hydration: CommodityHydration }[];
  coverage_totals: Record<string, number>;
  question_hint?: LLMQuestion & { source?: string };
}

export interface HydrationSources {
  facets: boolean;
  footnotes: boolean;
  measures: boolean;
  section_notes: boolean;
  chapter_notes: boolean;
  hsen: boolean;
  atar: boolean;
  girs: boolean;
}

export interface ValuationResult {
  customs_value_gbp: number;
  breakdown: Record<string, number>;
  method: string;
  notes: string[];
  method_code: string;
  confidence: "known" | "calculated" | "estimate";
}

export interface ValuationGuideResult {
  choice: {
    method_code: string;
    method: string;
    reason: string;
  };
  result: ValuationResult | null;
  required_inputs: string[];
  notes: string[];
}

export interface ExciseBreakdown {
  band_id: string;
  band_label: string;
  base_rate_per_lpa_gbp: number;
  effective_rate_per_lpa_gbp: number;
  applied_reliefs: { name: string; discount_pct: number; note: string }[];
  pure_alcohol_litres: number;
  volume_litres: number;
  abv: number;
  duty_gbp: number;
}

export type ExciseCategory = "alcohol" | "tobacco" | "fuel";

// Mirrors backend ExciseInputs - all fields optional; send what the
// commodity's excise_required_inputs asks for.
export interface ExciseInputs {
  abv?: number | null;
  volume_litres?: number | null;
  is_small_producer?: boolean;
  is_draught?: boolean;
  // Tobacco: number of cigarettes.
  sticks?: number | null;
  // Tobacco: total UK retail selling price of the cigarettes.
  retail_value_gbp?: number | null;
  // Tobacco (cigars/hand-rolling/other): net weight for per-kg duty.
  net_weight_kg?: number | null;
}

export interface MeursingInputs {
  starch_glucose_pct?: number | null;
  sucrose_invert_isoglucose_pct?: number | null;
  milk_fat_pct?: number | null;
  milk_protein_pct?: number | null;
  additional_code?: string | null;
}

export interface MeursingResult {
  additional_code: string | null;
  code_type: string;
  complete: boolean;
  lookup_path?: string;
  lookup_url?: string;
  component_percentages?: Record<string, number | null>;
  bands?: Record<string, { value: string; label: string }>;
  note?: string;
}

export type DutyWarningKind =
  | "trade_remedy"
  | "quota"
  | "suspension_applied"
  | "prohibition"
  | "excise_missing"
  | "other";

// Educational annotate-and-warn layer - never blocks the calculation.
export interface DutyWarning {
  kind: DutyWarningKind;
  message: string;
  rate_range?: string | null;
}

// Non-alcohol excise breakdown (tobacco/fuel).
export interface ExciseDetail {
  category: ExciseCategory;
  label: string;
  components: { label: string; amount_gbp: number }[];
  notes: string[];
  rates_as_of: string;
  duty_gbp: number;
}

// Set when the chosen code is not a declarable leaf - the backend returns
// HTTP 200 with zero totals and lists the child codes to pick from.
export interface NeedsMoreDetail {
  message: string;
  children: { commodity_code: string; description: string }[];
}

export interface DutyResult {
  commodity_code: string;
  commodity_description: string;
  country_of_origin: string;
  country_name: string;
  import_destination: "GB" | "XI";
  import_date: string | null;
  customs_value_gbp: number;
  rate_applied: number;
  rate_kind: "ad_valorem" | "specific" | "free";
  rate_per_unit: string | null;
  rate_monetary_unit: string | null;
  rate_expression: string;
  rate_source: string;
  eligible_preferences: {
    group: string;
    rate: number | null;
    rate_kind?: string;
    rate_expression?: string;
    measure_id?: string;
    source?: string;
  }[];
  customs_duty_gbp: number;
  excise: ExciseBreakdown | null;
  excise_duty_gbp: number;
  excise_detail?: ExciseDetail | null;
  meursing: MeursingResult | null;
  vat_rate: number;
  notes: string[];
  warnings?: DutyWarning[];
  // True when the GBP 135 low-value regime applies (no customs duty;
  // supply VAT instead of import VAT).
  low_value_regime?: boolean;
  needs_more_detail?: NeedsMoreDetail | null;
  measures_inspected: {
    measure_id: string;
    measure_type_id: string;
    measure_type_description: string;
    geographical_area: string;
    duty_expression: string;
  }[];
}

export interface CommodityRequirements {
  needs_supplementary_units: boolean;
  supplementary_unit_type: string | null;
  needs_excise: boolean;
  excise_beverage_type: string | null;
  excise_category?: ExciseCategory | null;
  // ExciseInputs fields the wizard should collect,
  // e.g. ["abv", "volume_litres"] or ["sticks", "retail_value_gbp"].
  excise_required_inputs?: string[];
  needs_meursing_code: boolean;
  additional_code_type: string | null;
  mfn_rate: number;
  has_any_preference: boolean;
  default_vat_rate: number;
}

export interface DutyInputInferenceResult {
  inferred: Record<string, unknown>;
  sources: Record<string, string>;
  skipped_questions: string[];
  warnings: string[];
}

export type VatTreatment = "import_vat" | "supply_vat" | "postponed";

export interface LandedResult {
  vat_taxable_amount_gbp: number;
  vat_gbp: number;
  total_landed_cost_gbp: number;
  breakdown: Record<string, number>;
  notes: string[];
  vat_treatment?: VatTreatment;
  // VAT actually paid at the border (0 under postponed VAT accounting).
  cash_at_border_gbp?: number;
  // Duty + excise + VAT, regardless of when or how the VAT is paid.
  total_tax_liability_gbp?: number;
}

export interface DeclarationDocument {
  code: string;
  description: string;
  source?: string;
  attached_at?: string;
  inherited?: boolean;
}

export interface AuditSummary {
  original_query?: string;
  qa_history: { question: string; answer?: string }[];
  chosen_code: string;
  chosen_description: string;
  rejected_candidates: { code: string; description: string; reason: string }[];
  scenario: {
    country_of_origin: string;
    preference_claimed: string | null;
    customs_value_gbp: number;
  };
  measure_ids: number[];
  duty_measure_ids: number[];
  document_codes: string[];
  document_codes_by_source: Record<string, string[]>;
  computed_totals: {
    duty_gbp: number;
    excise_gbp: number;
    vat_gbp: number;
    total_taxes_gbp: number;
    total_landed_gbp: number;
  };
}

export interface DeclarationResult {
  cds_box_values: Record<string, string>;
  required_document_codes: DeclarationDocument[];
  summary: Record<string, number>;
  next_steps: string[];
  audit_summary?: AuditSummary;
}

export interface FilingIntentResult {
  status: "not_submitted" | "ready_for_broker";
  reference: string;
  message: string;
  next_steps: string[];
}

export interface Country {
  code: string;
  name: string;
  groups: string[];
}

export interface JourneyExample {
  id: string;
  label: string;
  query: string;
  expected_code: string;
  expected_code_dotted: string;
  description: string;
  source?: string;
  source_detail?: string;
  persona?: string;
  fact_count?: number;
  facet_count?: number;
  seed: {
    quantity_units?: number;
    quantity_unit_type?: string;
    excise_volume_litres?: number;
    abv?: number;
    description_of_goods?: string;
    invoice_value?: number;
    invoice_currency?: string;
    fx_rate_to_gbp?: number;
    freight_gbp?: number;
    insurance_gbp?: number;
    other_costs_gbp?: number;
    net_mass_kg?: number;
    meursing_inputs?: MeursingInputs;
  };
}

export interface JourneyPersona {
  id: string;
  label: string;
}

export type QaProcessMode = "local_rules" | "legacy_eliminate" | "ott_staging" | "ott_staging_kg";

// Shared journey state held by App and threaded through stages.
export interface JourneyState {
  // Classify
  query: string;
  qaHistory: { question: string; answer: string }[];
  lastClassifyTurn: ClassifyTurn | null;
  finalCommodity: { commodity_code: string; description: string } | null;
  classifyConfidence: string | null;
  fixedCandidates: Record<string, unknown>[];
  selectedExample: JourneyExample | null;
  classifyConfig: Record<string, unknown> | null;
  qaProcessMode: QaProcessMode;
  candidateSelectionUseModel: boolean;
  candidateSelectionModel: string;
  questionWordingUseModel: boolean;
  questionWordingModel: string;
  hydrationUseModel: boolean;
  hydrationModel: string;
  hydrationSources: HydrationSources;

  // Value
  knowsCustomsValue: boolean | null;
  invoiceValue: number | null;
  invoiceCurrency: string;
  fxRateToGbp: number;
  freightGbp: number;
  insuranceGbp: number;
  otherCostsGbp: number;
  // Incoterms question: does the invoice price already include freight and
  // insurance to the UK? null = not asked yet.
  invoiceIncludesFreight: boolean | null;
  customsValueGbp: number | null;
  valuationResult: ValuationResult | null;
  valuationGuideResult: ValuationGuideResult | null;
  valuationMethodCode: string | null;

  // Duty
  importDestination: "GB" | "XI";
  importDate: string | null;
  countryOfOrigin: string;
  hasProofOfOrigin: boolean;
  quantityUnits: number | null;
  quantityUnitType: string | null;
  abv: number | null;
  isSmallProducer: boolean;
  isDraught: boolean;
  exciseVolumeLitres: number | null;
  meursingInputs: MeursingInputs | null;
  vatRate: number;
  netMassKg: number | null;
  descriptionOfGoods: string;
  dutyResult: DutyResult | null;
  dutyExplainerText: string | null;
  dutyInference: DutyInputInferenceResult | null;

  // Landed
  additionalChargesGbp: number;
  // Incidental costs to the first UK destination - inside the import VAT base.
  incidentalCostsToUkGbp: number;
  usePostponedVat: boolean;
  landedResult: LandedResult | null;

  // Declare
  declarationResult: DeclarationResult | null;
  filingIntent: FilingIntentResult | null;
}

export const initialJourneyState: JourneyState = {
  query: "",
  qaHistory: [],
  lastClassifyTurn: null,
  finalCommodity: null,
  classifyConfidence: null,
  fixedCandidates: [],
  selectedExample: null,
  classifyConfig: null,
  qaProcessMode: "ott_staging_kg",
  candidateSelectionUseModel: true,
  candidateSelectionModel: "gpt-5-nano",
  questionWordingUseModel: true,
  questionWordingModel: "gpt-5-nano",
  hydrationUseModel: true,
  hydrationModel: "gpt-5-nano",
  hydrationSources: {
    facets: true,
    footnotes: true,
    measures: true,
    section_notes: true,
    chapter_notes: true,
    hsen: true,
    atar: true,
    girs: true,
  },
  knowsCustomsValue: null,
  invoiceValue: null,
  invoiceCurrency: "GBP",
  fxRateToGbp: 1.0,
  freightGbp: 0,
  insuranceGbp: 0,
  otherCostsGbp: 0,
  invoiceIncludesFreight: null,
  customsValueGbp: null,
  valuationResult: null,
  valuationGuideResult: null,
  valuationMethodCode: null,
  importDestination: "GB",
  importDate: null,
  countryOfOrigin: "",
  hasProofOfOrigin: false,
  quantityUnits: null,
  quantityUnitType: null,
  abv: null,
  isSmallProducer: false,
  isDraught: false,
  exciseVolumeLitres: null,
  meursingInputs: null,
  vatRate: 20,
  netMassKg: null,
  descriptionOfGoods: "",
  dutyResult: null,
  dutyExplainerText: null,
  dutyInference: null,
  additionalChargesGbp: 0,
  incidentalCostsToUkGbp: 0,
  usePostponedVat: false,
  landedResult: null,
  declarationResult: null,
  filingIntent: null,
};
