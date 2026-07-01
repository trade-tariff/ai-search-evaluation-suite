from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- Classification -----------------------------------------------------

class CandidateCC(BaseModel):
    commodity_code: str
    code_dotted: str = ""
    description: str
    score: float = 0.0
    sources: list[str] = []
    in_slice: bool = False


class ClassifyStartRequest(BaseModel):
    query: str
    config: Optional[dict] = Field(default=None)


class LLMQuestion(BaseModel):
    """The AI Guided Search question - free-form text, multi-option."""
    question: str
    options: list[str]


class LLMAnswer(BaseModel):
    commodity_code: str
    confidence: str = Field(description="Best match | Also possible | From retrieval")
    description: str = ""
    leaf_adjusted: Optional[bool] = Field(
        default=None,
        description="True when the answer was auto-descended from a parent to the declarable leaf.",
    )


class ClassifyTurn(BaseModel):
    """One AI Guided Search turn: retrieval + LLM response (questions or answers)."""
    candidates: list[CandidateCC]
    fixed_candidates: list[dict] = Field(default_factory=list)
    mode: Literal["answers", "questions", "error", "no_candidates"]
    answers: list[LLMAnswer] = []
    question: Optional[LLMQuestion] = None
    kg_notes: list[dict] = []
    facet_enrichment: Optional[dict] = None
    augmentation_summary: dict = Field(
        default_factory=dict,
        description="How many candidates have facets, how many KG edges applied - for UI transparency",
    )
    error_message: Optional[str] = None
    qa_history: list[dict] = []
    retrieval_health: Optional[str] = Field(
        default=None,
        description="Data-source health for this turn: live | fixture | degraded | infra-error",
    )
    eliminate_trace: Optional[dict] = None
    survivors_all: list = Field(default_factory=list)


class ClassifyAnswerRequest(BaseModel):
    query: str
    qa_history: list[dict] = Field(
        default_factory=list,
        description="Prior turns: [{question, answer}, ...]",
    )
    config: Optional[dict] = Field(
        default=None,
        description="Augmentation config: {use_query_expansion, use_facets, kg_include: {...}}",
    )
    fixed_candidates: list[dict] = Field(
        default_factory=list,
        description="Frozen candidate set for eliminate strategy follow-up turns.",
    )


class CompareRequest(BaseModel):
    query: str
    qa_history: list[dict] = Field(default_factory=list)
    panels: list[dict] = Field(
        description="List of {label, config} - one per side-by-side panel",
    )


class ComparePanelResult(BaseModel):
    label: str
    config: dict
    turn: ClassifyTurn


class HydrationRequest(BaseModel):
    summarize: bool = Field(
        default=False,
        description="When true, call the configured provider model for an evidence summary.",
    )
    allow_spend: bool = Field(
        default=False,
        description="Per-request approval for provider-backed helper calls such as cheap question wording.",
    )
    model: Optional[str] = Field(
        default=None,
        description="OpenAI model id for provider-backed hydration summaries.",
    )
    sources: dict = Field(
        default_factory=dict,
        description="Evidence source toggles, e.g. {footnotes, measures, section_notes, chapter_notes, hsen, atar}. Missing keys default on.",
    )


class CandidateHydrationRequest(HydrationRequest):
    query: str = ""
    config: Optional[dict] = Field(default=None)
    question_mode: Literal[
        "facet_rules",
        "facet_rules_llm_wording",
        "llm_generated",
        "facets",
        "llm",
    ] = Field(
        default="facet_rules",
        description="How to build the Q&A hint: deterministic facet rules, facet rules plus LLM wording, or LLM-generated.",
    )
    qa_history: list[dict] = Field(default_factory=list)
    candidates: list[dict] = Field(
        default_factory=list,
        description="Already-retrieved candidate CCs to hydrate. Preferred over re-retrieval.",
    )
    candidate_limit: int = 500
    hydrate_limit: int = 0


# --- Valuation ----------------------------------------------------------

class ValuationRequest(BaseModel):
    invoice_value: float = Field(default=0.0, ge=0)
    invoice_currency: str = "GBP"
    fx_rate_to_gbp: float = Field(default=1.0, gt=0)
    freight_gbp: float = Field(default=0.0, ge=0)
    insurance_gbp: float = Field(default=0.0, ge=0)
    other_costs_gbp: float = Field(default=0.0, ge=0)
    method: Literal[
        "known_customs_value",
        "method_1_transaction_value",
        "method_2_identical_goods",
        "method_3_similar_goods",
        "method_4_deductive",
        "method_5_computed",
        "method_6_fallback",
    ] = "method_1_transaction_value"
    known_customs_value_gbp: Optional[float] = Field(default=None, ge=0)
    method_inputs: dict = Field(default_factory=dict)


class ValuationResult(BaseModel):
    customs_value_gbp: float
    breakdown: dict[str, float]
    method: str = "Method 1 (transaction value)"
    notes: list[str] = []
    method_code: str = "method_1_transaction_value"
    confidence: Literal["known", "calculated", "estimate"] = "calculated"


class ValuationGuideRequest(BaseModel):
    has_sale_for_export: bool = False
    has_usable_transaction_value: bool = False
    has_identical_goods_value: bool = False
    has_similar_goods_value: bool = False
    has_uk_resale_price: bool = False
    has_production_costs: bool = False
    try_computed_before_deductive: bool = False
    inputs: dict = Field(default_factory=dict)


class ValuationMethodChoice(BaseModel):
    method_code: str
    method: str
    reason: str


class ValuationGuideResult(BaseModel):
    choice: ValuationMethodChoice
    result: Optional[ValuationResult] = None
    required_inputs: list[str] = []
    notes: list[str] = []


# --- Duty (multi-step) --------------------------------------------------

class CommodityRequirements(BaseModel):
    """What the duty wizard needs to ask the trader, given the chosen commodity.

    Drives which sub-steps are shown in the frontend. Computed server-side
    from the commodity's fact sheet so the FE doesn't duplicate that logic.
    """
    needs_supplementary_units: bool
    supplementary_unit_type: Optional[str] = None
    needs_excise: bool
    excise_beverage_type: Optional[str] = None
    excise_category: Optional[Literal["alcohol", "tobacco", "fuel"]] = None
    excise_required_inputs: list[str] = Field(
        default_factory=list,
        description="ExciseInputs fields the wizard should collect, e.g. ['abv', 'volume_litres'] or ['sticks', 'retail_value_gbp']",
    )
    needs_meursing_code: bool = False
    additional_code_type: Optional[str] = None
    mfn_rate: float
    has_any_preference: bool
    default_vat_rate: float = 20.0


class ExciseInputs(BaseModel):
    abv: Optional[float] = Field(default=None, ge=0, le=100)
    volume_litres: Optional[float] = Field(default=None, gt=0)
    is_small_producer: bool = False
    is_draught: bool = False
    # Tobacco (chapter 24) inputs
    sticks: Optional[float] = Field(default=None, gt=0, description="Number of cigarettes (sticks)")
    retail_value_gbp: Optional[float] = Field(default=None, ge=0, description="Total UK retail selling price of the cigarettes")
    net_weight_kg: Optional[float] = Field(default=None, gt=0, description="Net tobacco weight for per-kg duty (cigars, hand-rolling, other)")


class MeursingInputs(BaseModel):
    starch_glucose_pct: Optional[float] = Field(default=None, ge=0, le=100)
    sucrose_invert_isoglucose_pct: Optional[float] = Field(default=None, ge=0, le=100)
    milk_fat_pct: Optional[float] = Field(default=None, ge=0, le=100)
    milk_protein_pct: Optional[float] = Field(default=None, ge=0, le=100)
    additional_code: Optional[str] = None


class DutyRequest(BaseModel):
    commodity_code: str
    country_of_origin: str
    customs_value_gbp: float = Field(ge=0)
    import_destination: Literal["GB", "XI"] = "GB"
    import_date: Optional[str] = None  # ISO yyyy-mm-dd; None = today
    quantity_units: Optional[float] = Field(default=None, ge=0)
    quantity_unit_type: Optional[str] = None
    has_proof_of_origin: bool = True
    excise_inputs: Optional[ExciseInputs] = None
    meursing_inputs: Optional[MeursingInputs] = None
    vat_rate: Optional[float] = Field(
        default=None, ge=0, le=100,
        description="Explicit VAT rate. None (unset) seeds from the commodity's type-305 VAT measure; 0 is a real zero rate.",
    )


class DutyInputInferenceRequest(BaseModel):
    commodity_code: str
    query: str = ""
    qa_history: list[dict] = Field(default_factory=list)
    customs_value_gbp: Optional[float] = None
    known_inputs: dict = Field(
        default_factory=dict,
        description="Fields already elicited earlier in the journey; these override LLM/text extraction.",
    )


class DutyInputInferenceResult(BaseModel):
    inferred: dict = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    skipped_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExciseBreakdown(BaseModel):
    band_id: str
    band_label: str
    base_rate_per_lpa_gbp: float
    effective_rate_per_lpa_gbp: float
    applied_reliefs: list[dict] = []
    pure_alcohol_litres: float
    volume_litres: float
    abv: float
    duty_gbp: float


class DutyResult(BaseModel):
    commodity_code: str
    commodity_description: str = ""
    country_of_origin: str
    country_name: str = ""
    import_destination: Literal["GB", "XI"] = "GB"
    import_date: Optional[str] = None
    customs_value_gbp: float = 0
    rate_applied: float = Field(description="Percentage or specific amount applied")
    rate_kind: Literal["ad_valorem", "specific", "free"] = "ad_valorem"
    rate_per_unit: Optional[str] = None
    rate_monetary_unit: Optional[str] = None
    rate_expression: str = Field(default="", description="Raw OTT duty expression text e.g. '26.00 GBP / hl'")
    rate_source: str = Field(description="MFN or preference group name")
    eligible_preferences: list[dict] = []
    customs_duty_gbp: float
    excise: Optional[ExciseBreakdown] = None
    excise_duty_gbp: float = 0
    excise_detail: Optional[dict] = Field(
        default=None,
        description="Non-alcohol excise breakdown (tobacco/fuel): {category, label, components, notes, rates_as_of, duty_gbp}",
    )
    meursing: Optional[dict] = None
    vat_rate: float = 20.0
    notes: list[str] = []
    warnings: list[dict] = Field(
        default_factory=list,
        description="Educational warnings: {kind: trade_remedy|quota|suspension_applied|prohibition|excise_missing|other, message, rate_range}",
    )
    low_value_regime: bool = Field(
        default=False,
        description="True when the GBP 135 low-value regime applies (no customs duty; supply VAT instead of import VAT).",
    )
    needs_more_detail: Optional[dict] = Field(
        default=None,
        description="Set when the code is not a declarable leaf: {message, children: [{commodity_code, description}]}. No totals are computed.",
    )
    measures_inspected: list[dict] = Field(
        default_factory=list,
        description="Raw OTT measures considered, for transparency in the UI",
    )


class DutyExplainRequest(BaseModel):
    duty_result: DutyResult


class DutyExplainResponse(BaseModel):
    text: str


# --- Landed -------------------------------------------------------------

class LandedRequest(BaseModel):
    customs_value_gbp: float = Field(ge=0)
    customs_duty_gbp: float = Field(ge=0)
    excise_duty_gbp: float = Field(default=0, ge=0)
    vat_rate: float = Field(default=20.0, ge=0, le=100)
    additional_charges_gbp: float = Field(
        default=0, ge=0,
        description="Post-arrival charges (broker, UK haulage after first destination) - outside the import VAT base",
    )
    incidental_costs_to_uk_gbp: float = Field(
        default=0, ge=0,
        description="Incidental costs up to the first UK destination (port handling, onward freight) - included in the import VAT base",
    )
    use_postponed_vat: bool = Field(
        default=False,
        description="Postponed VAT accounting: import VAT accounted on the VAT return, no cash at the border",
    )
    low_value_import: Optional[bool] = Field(
        default=None,
        description="GBP 135 regime override. None = infer from customs_value_gbp <= 135 and no excise duty.",
    )


class LandedResult(BaseModel):
    vat_taxable_amount_gbp: float
    vat_gbp: float
    total_landed_cost_gbp: float
    breakdown: dict[str, float]
    notes: list[str] = []
    vat_treatment: Literal["import_vat", "supply_vat", "postponed"] = "import_vat"
    cash_at_border_gbp: float = 0
    total_tax_liability_gbp: float = 0


# --- Declaration --------------------------------------------------------

class DeclarationRequest(BaseModel):
    commodity_code: str
    description_of_goods: str
    country_of_origin: str
    import_date: Optional[str] = None
    customs_value_gbp: float = Field(ge=0)
    quantity_units: Optional[float] = Field(default=None, ge=0)
    quantity_unit_type: Optional[str] = None
    net_mass_kg: Optional[float] = Field(default=None, ge=0)
    duty_gbp: float = Field(ge=0)
    excise_gbp: float = Field(default=0, ge=0)
    vat_gbp: float = Field(ge=0)
    has_proof_of_origin: bool = True
    preference_claimed: Optional[str] = None
    valuation_method: Optional[str] = None
    additional_codes: list[dict] = Field(default_factory=list)
    # Optional journey-state for the audit trail (codex's review).
    original_query: Optional[str] = None
    qa_history: Optional[list[dict]] = None
    rejected_candidates: Optional[list[dict]] = None  # [{code, description, reason}]


class DeclarationResult(BaseModel):
    cds_box_values: dict[str, str]
    required_document_codes: list[dict]
    summary: dict[str, float]
    next_steps: list[str]
    audit_summary: Optional[dict] = None  # query, Q&A, chosen, rejected, measure IDs, source IDs


class FilingIntentRequest(BaseModel):
    declaration: DeclarationResult
    contact_email: Optional[str] = None


class DeclarationDownloadRequest(BaseModel):
    declaration: DeclarationResult
    journey_state: dict = Field(default_factory=dict)


class FilingIntentResult(BaseModel):
    status: Literal["not_submitted", "ready_for_broker"]
    reference: str
    message: str
    next_steps: list[str]
