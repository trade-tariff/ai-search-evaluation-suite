from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ProviderType(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelConfig(BaseModel):
    id: str
    name: str
    provider: ProviderType
    model_id: str
    is_baseline: bool = False  # legacy single-baseline (fallback if no panel)
    is_panel: bool = False  # consensus panel member
    enabled: bool = True
    category: str = "tier1"  # "tier1" = reputable Western providers, "tier2" = others
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    reasoning_effort: Optional[str] = None  # for OpenAI thinking models
    thinking_budget: Optional[int] = None  # for Anthropic extended thinking
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


class ApiKeys(BaseModel):
    openai: Optional[str] = None
    anthropic: Optional[str] = None
    google: Optional[str] = None
    groq: Optional[str] = None
    deepseek: Optional[str] = None
    mistral: Optional[str] = None
    xai: Optional[str] = None
    openrouter: Optional[str] = None
    cerebras: Optional[str] = None
    sambanova: Optional[str] = None


class JudgeConfig(BaseModel):
    enabled: bool = True
    model: str = "gpt-5-nano"
    reasoning_effort: str = "low"
    temperature: float = 0.0
    max_response_length: int = 30000
    input_cost_per_million: float = 0.05
    output_cost_per_million: float = 0.40
    system_prompt: str = ""  # empty = use built-in default


class SimulatorConfig(BaseModel):
    """Trader simulator with per-prompt fact store.

    One LLM call per question. The simulator sees the per-prompt fact store
    alongside the current question and is asked to (a) coin a snake_case slot
    label, (b) pick an option consistent with any existing fact in the store,
    or (c) commit a fresh choice if the slot is new. It NEVER abstains.

    The same simulator model is used for every candidate under test in a run,
    which gives the apples-to-apples property even when models word their
    questions differently. gpt-5-nano low is the default: good enough at slot
    canonicalisation, cheap enough to call per question.
    """
    enabled: bool = True
    model: str = "gpt-5-nano"
    reasoning_effort: str = "low"
    temperature: float = 0.0
    input_cost_per_million: float = 0.05
    output_cost_per_million: float = 0.40


class ScoringWeights(BaseModel):
    """Composite verdict weights. All weights are normalised to sum=1 when the
    verdict is computed, so users can enter any positive numbers. Zero weight
    excludes the dimension from the composite (still shown in the summary
    table for reference).

    10 dimensions, three buckets:

    Deterministic accuracy (computed from code-agreement vs reference):
      - top1_match            exact top-1 code match
      - heading_match         first 4 digits agree
      - chapter_match         first 2 digits agree
      - top5_overlap          Jaccard of top-5 codes

    Deterministic quality (computed from run data, zero LLM cost):
      - schema_valid          JSON schema compliance
      - rounds_efficiency     fewer Q&A rounds = better
      - question_efficiency   new_slots_set / total_questions (novelty)

    Operational (computed from run timings/cost):
      - speed                 reference-relative latency
      - cost                  inverted cost-per-classification

    LLM-derived (judge call, 2 dimensions):
      - fact_consistency      does final code respect committed facts?
      - question_quality      phrasing / option coverage / discriminativeness
                              (deterministic question_efficiency complements
                              this - one measures redundancy, this measures
                              nuance)

    Legacy judge dimensions (classification_accuracy / overall /
    structured_output) are removed - their deterministic equivalents above
    replace them.
    """
    # Deterministic accuracy (6) - ranked-list aware. Production AI search
    # returns a ranked list with confidence labels, so these reflect that.
    # Ground truth (2) - scored against the prompt's gold_code. When every
    # compared model has gold-evaluated prompts, the verdict uses these
    # INSTEAD of the reference-agreement bucket below: gold is the single
    # most valuable signal and must outrank resemblance to the reference.
    gold_top1: float = 0.35
    gold_hierarchical: float = 0.15

    top1_match: float = 0.15              # strict: exact top-1 match
    mean_reciprocal_rank: float = 0.12    # continuous: 1/rank of ref's top
    top3_hit: float = 0.08                # softer: ref's top in candidate top-3
    heading_match: float = 0.08           # first 4 digits agree
    top5_overlap: float = 0.04            # Jaccard of top-5 sets
    chapter_match: float = 0.03           # first 2 digits agree
    # Deterministic quality (3)
    schema_valid: float = 0.03
    rounds_efficiency: float = 0.04
    question_efficiency: float = 0.04
    # Operational (2)
    speed: float = 0.10
    cost: float = 0.07
    # LLM (2) - only dimensions where semantic understanding beats deterministic
    fact_consistency: float = 0.15
    question_quality: float = 0.07


class ReferenceConfig(BaseModel):
    """Pinned 'gold reference' that always runs per prompt regardless of
    which candidates the user picks. Candidates are judged against it.

    Three modes, all using compute_consensus under the hood:
      - "single":     one model, one pass. Point estimate.
      - "multi_pass": one model, N passes. Reduces same-model variance;
                      compute_consensus majority-votes on the final code.
      - "panel":      multiple models, one pass each. Reduces model bias;
                      same consensus logic. panel_agreement (avg pairwise
                      cosine sim of outputs) becomes the 'reference
                      uncertainty' signal.

    Because the fact store makes every model classify the same hypothetical
    product, all three modes should converge on well-defined prompts.
    Divergence surfaces genuinely ambiguous queries.
    """
    mode: str = "single"  # "single" | "multi_pass" | "panel"
    # Used in single and multi_pass modes
    model_id: str = "gpt-5-nano"
    # Used only in multi_pass mode; number of times to run model_id
    passes: int = 1
    # Used only in panel mode; list of ModelConfig.id values
    panel_model_ids: list[str] = ["gpt-5-nano", "gpt-5-mini"]


class AppConfig(BaseModel):
    api_keys: ApiKeys = ApiKeys()
    models: list[ModelConfig] = []
    judge_config: JudgeConfig = JudgeConfig()
    simulator_config: SimulatorConfig = SimulatorConfig()
    reference_config: ReferenceConfig = ReferenceConfig()
    scoring_weights: ScoringWeights = ScoringWeights()
    # User's preferred model selection - auto-loaded on app start so picks
    # survive reloads. Persisted via the "Save selection as default" button.
    default_selected_model_ids: list[str] = ["gpt-5-mini"]


class PromptInfo(BaseModel):
    index: int
    raw_query: str
    result_count: int
    selected: bool = True
    # Optional ground-truth commodity code. When present, every model
    # (including the reference) is scored against it as a separate axis.
    # See EvaluationResult.gold_* fields and ModelSummary.gold_* aggregates.
    gold_code: Optional[str] = None
    # Slice 2: optional fact-sheet + oracle text (typically populated from
    # ATaR rulings). When set:
    #   - gold_facts pre-seed the FactStore at run start (so candidate Q&A
    #     gets consistent, user-approved answers without burning LLM calls).
    #   - oracle_text is passed to the simulator as the authoritative product
    #     description; it answers questions the seeded facts don't cover.
    # has_oracle_text + gold_facts_count are surfaced flags for the UI; the
    # heavy fields live in search_contexts.json and are loaded on-demand.
    has_oracle_text: bool = False
    gold_facts_count: int = 0
    source: Optional[str] = None  # e.g. "atar:600003124"


class QARound(BaseModel):
    """One round of the Q&A loop."""
    round_number: int
    response_text: str
    response_type: str  # "answers", "questions", "error", "unknown"
    questions_asked: list[dict] = []  # questions the model asked
    answers_given: list[dict] = []  # answers picked by the simulator
    # Per-question simulator telemetry; one entry per question in questions_asked.
    # Fields: {"question": str, "chosen": str, "slot": str, "reasoning": str,
    #          "consistent_with_prior": bool, "from_store": bool,
    #          "cost": float, "latency_ms": float}
    simulator_trace: list[dict] = []
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost: float = 0.0
    simulator_cost: float = 0.0
    simulator_latency_ms: float = 0.0


class CompletionResult(BaseModel):
    model_id: str
    prompt_index: int
    response_text: str  # final response text
    response_type: str = "unknown"  # final response type
    rounds: list[QARound] = []
    total_rounds: int = 1
    total_latency_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    # Per-round backwards compat
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost: float = 0.0
    # Simulator cost is kept separate so the main cost figures remain comparable
    # to pre-simulator runs. Summed across all rounds.
    total_simulator_cost: float = 0.0
    total_simulator_latency_ms: float = 0.0
    # How many answers this model received from the fact store (i.e. some other
    # model had already committed the slot for this prompt).
    simulator_store_hits: int = 0
    error: Optional[str] = None


class EvaluationResult(BaseModel):
    model_id: str
    prompt_index: int
    # ── Deterministic code-agreement signals vs reference ─────────────
    # All Optional: in gold mode there is no reference/consensus, so every
    # reference-agreement field is None ("not evaluated", distinct from 0).
    cosine_similarity: Optional[float]
    code_match_score: Optional[float]  # legacy weighted aggregate (kept for back-compat)
    top1_match: Optional[bool]                  # exact top-1 code match
    top3_hit: Optional[bool] = False            # reference's top-1 appears in candidate's top-3
    top5_overlap: Optional[float]               # Jaccard of top-5 lists
    mean_reciprocal_rank: Optional[float] = 0.0 # 1/rank of ref's top-1 in candidate list
    heading_match: Optional[bool] = False       # first 4 digits agree
    chapter_match: Optional[bool] = False       # first 2 digits agree
    hierarchical_score: Optional[float] = 0.0   # 0-1 graded by deepest common prefix
    # ── Deterministic output-quality signals ─────────────────────────
    schema_valid: float = 0.0         # 0/0.3/0.5/1 - JSON shape compliance
    total_questions: int = 0          # how many clarifying questions this candidate asked
    new_slots_set: int = 0            # how many of those questions set a new slot (non-recall)
    question_efficiency: float = 0.0  # new_slots_set / total_questions (1.0 if 0 questions)
    rounds_efficiency: float = 0.0    # 1 - (rounds-1)/(max_rounds-1); 1.0 = one round
    # ── Ground-truth agreement (optional, None when prompt has no gold_code) ─
    # Scored against the prompt's gold_code if set; independent of the reference.
    # A model can disagree with the reference but agree with gold - that's the
    # single most valuable signal when a gold set is available.
    gold_code: Optional[str] = None
    gold_top1_match: Optional[bool] = None
    gold_heading_match: Optional[bool] = None
    gold_chapter_match: Optional[bool] = None
    gold_hierarchical_score: Optional[float] = None
    # ── Composite ─────────────────────────────────────────────────────
    delta_score: Optional[float]
    # Consensus panel agreement for this prompt (0-1, None if single baseline)
    panel_agreement: Optional[float] = None
    # LLM-as-Judge scores (0-10 scale, None if judge not run or API error)
    judge_score: Optional[float] = None  # overall quality 0-10
    judge_classification_accuracy: Optional[float] = None
    judge_fact_consistency: Optional[float] = None
    judge_question_quality: Optional[float] = None
    judge_structured_output: Optional[float] = None
    judge_reasoning: Optional[str] = None  # brief explanation
    judge_cost: float = 0.0  # cost of the judge call itself
    # True when the judge call hit an API error - exclude these from averages
    judge_error: bool = False
    # Total across all Q&A rounds. baseline_*/speed_factor are None in gold
    # mode (no reference to compare against).
    total_latency_ms: float
    baseline_total_latency_ms: Optional[float]
    speed_factor: Optional[float]
    total_cost: float
    baseline_total_cost: Optional[float]
    total_rounds: int
    baseline_total_rounds: Optional[int]


class ModelSummary(BaseModel):
    model_id: str
    model_name: str
    # Reference-comparison aggregates are Optional: in gold mode there is no
    # reference/consensus, so they are None ("not evaluated"), never 0.0.
    avg_cosine_similarity: Optional[float]
    avg_code_match_score: Optional[float]
    avg_delta_score: Optional[float]
    avg_total_latency_ms: float
    avg_speed_factor: Optional[float]
    total_cost: float
    avg_cost_per_classification: float
    top1_accuracy: Optional[float]
    avg_top5_overlap: Optional[float]
    avg_rounds: float
    # Deterministic aggregates (0-1 unless noted; computed per-run).
    # Reference-based ones are None in gold mode, like the fields above.
    heading_match_rate: Optional[float] = 0.0
    chapter_match_rate: Optional[float] = 0.0
    top3_hit_rate: Optional[float] = 0.0
    avg_mean_reciprocal_rank: Optional[float] = 0.0
    avg_hierarchical_score: Optional[float] = 0.0
    avg_schema_valid: float = 0.0
    avg_question_efficiency: float = 0.0
    avg_rounds_efficiency: float = 0.0
    # LLM-as-Judge averages (None-safe; excludes API errors)
    avg_judge_score: Optional[float] = None
    avg_judge_classification_accuracy: Optional[float] = None
    avg_judge_fact_consistency: Optional[float] = None
    avg_judge_question_quality: Optional[float] = None
    avg_judge_structured_output: Optional[float] = None
    judge_scored_count: int = 0  # how many evals had a valid judge score
    judge_error_count: int = 0   # how many hit API errors
    total_judge_cost: float = 0.0
    # Simulator aggregates
    total_simulator_cost: float = 0.0
    avg_simulator_store_hit_rate: float = 0.0
    # Gold-truth aggregates: only populated when at least one prompt in the
    # run had a gold_code set. Rates are computed over the subset of prompts
    # that had a gold code. Rate = matches / gold_evaluated_count.
    gold_evaluated_count: int = 0
    gold_top1_rate: Optional[float] = None
    gold_heading_rate: Optional[float] = None
    gold_chapter_rate: Optional[float] = None
    avg_gold_hierarchical_score: Optional[float] = None
    # Completions that finished without committing to any commodity code -
    # typically the model used every round asking questions and never
    # answered. Scored the same as a wrong answer everywhere else, so it is
    # counted separately to keep the round cap's cost visible.
    no_answer_count: int = 0
    no_answer_rate: Optional[float] = None
    # Top codes that are not 10 digits - malformed output rather than a wrong
    # classification. Scored identically to a genuine miss everywhere else, so
    # counted here to keep the two distinguishable.
    malformed_code_count: int = 0
    malformed_codes: list[str] = []


class BenchmarkRun(BaseModel):
    id: str
    timestamp: str
    status: str = "pending"
    progress: float = 0.0
    baseline_model_id: Optional[str] = None  # legacy single-baseline
    opensearch_limit: int = 80
    # True when the run skipped the reference panel + consensus + judge and
    # scored candidates against gold codes only.
    gold_mode: bool = False
    # Consensus panel
    panel_model_ids: list[str] = []  # models that formed the panel
    panel_results: list[CompletionResult] = []  # raw results from each panel model
    consensus_results: list[CompletionResult] = []  # synthetic consensus per prompt
    # Legacy baseline (populated from consensus for backward compat)
    baseline_results: list[CompletionResult] = []
    model_results: list[CompletionResult] = []
    evaluations: list[EvaluationResult] = []
    summaries: list[ModelSummary] = []
    prompt_indices: list[int] = []
    model_ids: list[str] = []
    # Per-prompt simulator fact store snapshots (slots -> answer commitments).
    # Shape: { "<prompt_index>": [ {slot, answer, source_question,
    # source_model, source_round}, ... ] }
    fact_store: dict[str, list[dict]] = {}
    # Run-level list of issues: transient retries, task errors. Each entry:
    # {kind: "retry"|"error", source: str, model_id?: str, prompt_index?: int,
    #  message: str, attempt?: int, timestamp: str}
    issues: list[dict] = []
    # Per-prompt: is the gold code present in that prompt's retrieved
    # candidates? False means no model can score a hit there, because the
    # prompt forbids answering outside the results. Shape:
    # { "<prompt_index>": true|false }
    gold_retrievable: dict[str, bool] = {}
    # Per-prompt OTT section tag, derived from the reference's top commodity
    # code after consensus is computed. Shape:
    # { "<prompt_index>": {"number": int, "roman": str, "title": str} }
    # Prompts where the reference failed (no valid code) have no entry.
    prompt_sections: dict[str, dict] = {}


class BenchmarkRequest(BaseModel):
    prompt_indices: list[int]
    model_ids: list[str]
    opensearch_limit: int = 80  # how many of the 80 OS results to include in the prompt
    allow_spend: bool = False
    # Gold mode: skip the reference panel, consensus and LLM judge; score
    # candidates against gold codes only. None = auto (enabled when every
    # selected prompt has a gold code).
    gold_mode: Optional[bool] = None


class SSEEvent(BaseModel):
    event: str
    data: dict
