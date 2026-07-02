export interface ModelConfig {
  id: string;
  name: string;
  provider: "openai" | "anthropic" | "google" | "openai_compatible";
  model_id: string;
  is_baseline: boolean;
  is_panel: boolean;
  enabled: boolean;
  category: "tier1_paid" | "tier1_free" | "tier2";
  base_url?: string;
  api_key_env?: string;
  reasoning_effort?: string;
  thinking_budget?: number;
  input_cost_per_million: number;
  output_cost_per_million: number;
}

export interface JudgeConfig {
  enabled: boolean;
  model: string;
  reasoning_effort: string;
  max_response_length: number;
  input_cost_per_million: number;
  output_cost_per_million: number;
  system_prompt: string;
}

export type ReferenceMode = "single" | "multi_pass" | "panel";

export interface ReferenceConfig {
  mode: ReferenceMode;
  model_id: string;
  passes: number;
  panel_model_ids: string[];
}

export interface SimulatorConfigT {
  enabled: boolean;
  model: string;
  reasoning_effort: string;
  temperature: number;
  input_cost_per_million: number;
  output_cost_per_million: number;
}

export interface ScoringWeights {
  // Deterministic accuracy (vs reference) - ranked-list aware
  top1_match: number;
  top3_hit: number;
  mean_reciprocal_rank: number;
  heading_match: number;
  chapter_match: number;
  top5_overlap: number;
  // Deterministic quality
  schema_valid: number;
  rounds_efficiency: number;
  question_efficiency: number;
  // Operational
  speed: number;
  cost: number;
  // LLM-derived
  fact_consistency: number;
  question_quality: number;
  // Ground truth - replaces the reference-agreement bucket in the verdict
  // whenever every compared model has gold-evaluated prompts
  gold_top1: number;
  gold_hierarchical: number;
}

export interface AppConfig {
  api_keys: Record<string, string | null>;
  api_keys_set: Record<string, boolean>;
  models: ModelConfig[];
  judge_config: JudgeConfig;
  reference_config?: ReferenceConfig;
  simulator_config?: SimulatorConfigT;
  scoring_weights?: ScoringWeights;
  default_selected_model_ids?: string[];
}

export interface PromptInfo {
  index: number;
  raw_query: string;
  result_count: number;
  selected?: boolean;
  // Optional ground-truth commodity code. When set, every model is scored
  // against it on a separate axis (gold_* fields on EvaluationResult and
  // ModelSummary).
  gold_code?: string | null;
  // Slice 2/3: presence flags for the optional fact sheet + oracle text
  // (typically populated from ATaR rulings via the AtarPanel approval flow).
  has_oracle_text?: boolean;
  gold_facts_count?: number;
  source?: string | null; // e.g. "atar:600003124"
}

export interface GoldFact {
  slot: string;
  answer: string;
  source_question?: string;
}

export interface PromptDetail {
  index: number;
  raw_query: string;
  processed_query: string;
  result_count: number;
  top_results: Array<{ commodity_code: string; description: string; score: number }>;
  gold_code?: string | null;
  oracle_text?: string | null;
  gold_facts?: GoldFact[];
  source?: string | null;
}

export interface SimulatorTraceEntry {
  question: string;
  chosen: string;
  slot: string;
  reasoning: string;
  consistent_with_prior: boolean;
  from_store: boolean;
  cost: number;
  latency_ms: number;
}

export interface FactEntry {
  slot: string;
  answer: string;
  source_question: string;
  source_model: string;
  source_round: number;
}

export interface QARound {
  round_number: number;
  response_text: string;
  response_type: string;
  questions_asked: Array<{ question: string; options: string[] }>;
  answers_given: Array<{ question: string; options?: string[]; answer: string }>;
  simulator_trace?: SimulatorTraceEntry[];
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  cost: number;
  simulator_cost?: number;
  simulator_latency_ms?: number;
}

export interface CompletionResult {
  model_id: string;
  prompt_index: number;
  response_text: string;
  response_type: string;
  rounds: QARound[];
  total_rounds: number;
  total_latency_ms: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost: number;
  total_simulator_cost?: number;
  total_simulator_latency_ms?: number;
  simulator_store_hits?: number;
  error?: string;
}

export interface EvaluationResult {
  model_id: string;
  prompt_index: number;
  cosine_similarity: number;
  code_match_score: number;
  top1_match: boolean;
  top3_hit?: boolean;
  top5_overlap: number;
  mean_reciprocal_rank?: number;
  // Deterministic fields (computed from run data, not from LLM judge)
  heading_match?: boolean;
  chapter_match?: boolean;
  hierarchical_score?: number;
  schema_valid?: number;
  total_questions?: number;
  new_slots_set?: number;
  question_efficiency?: number;
  rounds_efficiency?: number;
  // Ground-truth fields - only set when the prompt had a gold_code
  gold_code?: string | null;
  gold_top1_match?: boolean | null;
  gold_heading_match?: boolean | null;
  gold_chapter_match?: boolean | null;
  gold_hierarchical_score?: number | null;
  delta_score: number;
  panel_agreement?: number;
  // Legacy judge fields (kept for back-compat reading older runs); None in new runs
  judge_score?: number;
  judge_classification_accuracy?: number;
  judge_structured_output?: number;
  // Active judge fields
  judge_fact_consistency?: number;
  judge_question_quality?: number;
  judge_reasoning?: string;
  judge_cost: number;
  judge_error?: boolean;
  total_latency_ms: number;
  baseline_total_latency_ms: number;
  speed_factor: number | null;
  total_cost: number;
  baseline_total_cost: number;
  total_rounds: number;
  baseline_total_rounds: number;
}

export interface ModelSummary {
  model_id: string;
  model_name: string;
  // Reference-comparison aggregates: null in gold mode (no reference)
  avg_cosine_similarity: number | null;
  avg_code_match_score: number | null;
  avg_delta_score: number | null;
  avg_total_latency_ms: number;
  avg_speed_factor: number | null;
  total_cost: number;
  avg_cost_per_classification: number;
  top1_accuracy: number | null;
  avg_top5_overlap: number | null;
  avg_rounds: number;
  avg_judge_score?: number;
  avg_judge_classification_accuracy?: number;
  avg_judge_fact_consistency?: number;
  avg_judge_question_quality?: number;
  avg_judge_structured_output?: number;
  judge_scored_count?: number;
  judge_error_count?: number;
  total_judge_cost: number;
  total_simulator_cost?: number;
  avg_simulator_store_hit_rate?: number;
  // Deterministic aggregates (reference-based ones are null in gold mode)
  heading_match_rate?: number | null;
  chapter_match_rate?: number | null;
  top3_hit_rate?: number | null;
  avg_mean_reciprocal_rank?: number | null;
  avg_hierarchical_score?: number | null;
  avg_schema_valid?: number;
  avg_question_efficiency?: number;
  avg_rounds_efficiency?: number;
  // Gold-truth aggregates. None when no prompts in the run had a gold_code.
  gold_evaluated_count?: number;
  gold_top1_rate?: number | null;
  gold_heading_rate?: number | null;
  gold_chapter_rate?: number | null;
  avg_gold_hierarchical_score?: number | null;
}

export interface BenchmarkResults {
  id: string;
  timestamp: string;
  status: string;
  opensearch_limit?: number;
  baseline_model_id?: string;
  panel_model_ids: string[];
  panel_results: CompletionResult[];
  consensus_results: CompletionResult[];
  baseline_results: CompletionResult[];
  model_results: CompletionResult[];
  evaluations: EvaluationResult[];
  summaries: ModelSummary[];
  prompt_indices: number[];
  model_ids: string[];
  fact_store?: Record<string, FactEntry[]>;
  prompt_sections?: Record<string, SectionInfo>;
}

export interface SectionInfo {
  number: number;
  roman: string;
  title: string;
}

export interface SSEData {
  event: string;
  [key: string]: unknown;
}
