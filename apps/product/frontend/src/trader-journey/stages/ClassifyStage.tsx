import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { initialJourneyState, NONE_OF_THESE_OPTION, type AnswerConfidence, type CandidateHydrationRun, type ClassifyTurn, type CommodityHydration, type JourneyExample, type JourneyPersona, type JourneyState, type QaProcessMode, type RetrievalHealth } from "../types";

type HydrationProgress = {
  target: "candidates" | "selected";
  total: number;
  done: number;
  percent: number;
  label: string;
} | null;

// Compact shortlist rows from the streaming 'candidates_ready' milestone -
// shown inside the loading box while the question is still being prepared.
type CandidatePreview = {
  commodity_code: string;
  description: string;
  sources?: string[];
};

const QA_PROCESS_MODES: { id: QaProcessMode; label: string; hint: string }[] = [
  {
    id: "ott_staging_kg",
    label: "OTT staging + KG context",
    hint: "Staging rewrite plus labels/search refs/composite/facts/KG retrieval; shortlist facts and rules are injected as soft evidence.",
  },
  {
    id: "ott_staging",
    label: "OTT staging Q&A",
    hint: "Staging rewrite + labels/search refs/composite/semantic retrieval; provider decides ask vs answer without the extra KG prompt context.",
  },
  {
    id: "local_rules",
    label: "Local deterministic fallback",
    hint: "No provider spend. Uses local labels, facts and KG to force a first Q&A turn.",
  },
  {
    id: "legacy_eliminate",
    label: "Legacy eliminate",
    hint: "Fixed first shortlist with model ranking after Q&A. Kept for comparison only.",
  },
];

// Facet vocabulary values arrive snake_cased (e.g. "strap_thong"); display them
// in plain English but keep the raw value as the answer payload.
function prettyOption(option: string): string {
  const text = option.replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function seededCustomsValue(example: JourneyExample | null | undefined): number | null {
  const seed = example?.seed;
  if (!seed?.invoice_value) return null;
  const value =
    Number(seed.invoice_value) +
    Number(seed.freight_gbp ?? 0) +
    Number(seed.insurance_gbp ?? 0) +
    Number(seed.other_costs_gbp ?? 0);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function seededCustomsValueForCode(state: JourneyState, code: string): number | null {
  return state.selectedExample?.expected_code === code ? seededCustomsValue(state.selectedExample) : null;
}

interface Props {
  state: JourneyState;
  update: (patch: Partial<JourneyState>) => void;
  onNext: () => void;
}

function configForProcessMode(
  baseConfig: Record<string, unknown> | null,
  state: JourneyState,
): Record<string, unknown> {
  const cfg = { ...(baseConfig || {}) };
  const retrieval = { ...(((baseConfig || {}).retrieval || {}) as Record<string, unknown>) };
  const candidateModel = state.candidateSelectionModel || "gpt-5.5";
  const questionModel = state.questionWordingModel || candidateModel;
  const mode = state.qaProcessMode || "ott_staging_kg";

  if (mode === "local_rules") {
    return {
      ...cfg,
      qa_process_mode: "local_rules",
      strategy: "eliminate",
      use_query_expansion: false,
      use_llm_candidate_selection: false,
      use_llm_question_wording: false,
      retrieval: {
        ...retrieval,
        use_labels: true,
        use_curated: false,
        use_vector: false,
        use_composite: false,
        use_facts_leg: true,
        use_kg_context_leg: true,
        use_facts_vec_leg: true,
        use_kg_vec_leg: true,
      },
    };
  }

  if (mode === "legacy_eliminate") {
    return {
      ...cfg,
      qa_process_mode: "legacy_eliminate",
      strategy: "eliminate",
      use_query_expansion: false,
      use_llm_candidate_selection: true,
      candidate_selection_model: candidateModel,
      use_llm_question_wording: state.questionWordingUseModel,
      question_wording_model: questionModel,
      retrieval: {
        ...retrieval,
        use_labels: true,
      },
    };
  }

  if (mode === "ott_staging") {
    return {
      ...cfg,
      qa_process_mode: "ott_staging",
      strategy: "converge",
      prompt_mode: "baseline",
      first_turn_must_ask: true,
      use_query_expansion: true,
      query_expansion_model: "gpt-4.1-mini-2025-04-14",
      query_expansion_prompt_variant: "staging",
      use_kg_prompt_context: false,
      use_llm_candidate_selection: true,
      candidate_selection_model: candidateModel,
      use_entropy_picker: false,
      use_llm_question_wording: state.questionWordingUseModel,
      question_wording_model: questionModel,
      retrieval: {
        ...retrieval,
        use_labels: true,
        use_curated: true,
        use_vector: true,
        use_composite: true,
        use_facts_leg: false,
        use_kg_context_leg: false,
        use_facts_vec_leg: false,
        use_kg_vec_leg: false,
      },
    };
  }

  return {
    ...cfg,
    qa_process_mode: "ott_staging_kg",
    strategy: "eliminate",
    prompt_mode: "facet_soft_score",
    first_turn_must_ask: true,
    use_query_expansion: true,
    query_expansion_model: "gpt-4.1-mini-2025-04-14",
    query_expansion_prompt_variant: "staging",
    use_kg_prompt_context: true,
    use_llm_candidate_selection: true,
    candidate_selection_model: candidateModel,
    use_entropy_picker: false,
    use_llm_question_wording: state.questionWordingUseModel,
    question_wording_model: questionModel,
    retrieval: {
      ...retrieval,
      use_labels: true,
      use_curated: true,
      use_vector: true,
      use_composite: true,
      use_facts_leg: true,
      use_kg_context_leg: true,
      use_facts_vec_leg: true,
      use_kg_vec_leg: true,
      facts_cap: 0.5,
      kg_cap: 0.5,
      facts_vec_cap: 0.9,
      kg_vec_cap: 0.9,
    },
  };
}

export default function ClassifyStage({ state, update, onNext }: Props) {
  const [query, setQuery] = useState(state.query);
  const [turn, setTurn] = useState<ClassifyTurn | null>(null);
  const [examples, setExamples] = useState<JourneyExample[]>([]);
  const [exampleSource, setExampleSource] = useState<string>("");
  const [promptPersona, setPromptPersona] = useState<string>("emu_ordinary");
  const [promptPersonas, setPromptPersonas] = useState<JourneyPersona[]>([]);
  const [config, setConfig] = useState<Record<string, unknown> | null>(state.classifyConfig);
  const [loading, setLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState<string | null>(null);
  const [candidatePreview, setCandidatePreview] = useState<CandidatePreview[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hydratingTarget, setHydratingTarget] = useState<"candidates" | "selected" | null>(null);
  const [hydrationError, setHydrationError] = useState<string | null>(null);
  const [candidateHydration, setCandidateHydration] = useState<CandidateHydrationRun | null>(null);
  const [selectedHydration, setSelectedHydration] = useState<CommodityHydration | null>(null);
  const [hydrationProgress, setHydrationProgress] = useState<HydrationProgress>(null);
  const classifyRequestSeq = useRef(0);
  const hydrationRequestSeq = useRef(0);
  const hydrationProgressTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const queryRef = useRef(query);
  const turnRef = useRef<ClassifyTurn | null>(turn);

  const effectiveConfig = configForProcessMode(config, state);
  const retrievalConfig = ((config || {}).retrieval || {}) as Record<string, unknown>;
  const retrievalLimit = Number(retrievalConfig.limit ?? 80);

  useEffect(() => {
    queryRef.current = query;
  }, [query]);

  useEffect(() => {
    turnRef.current = turn;
  }, [turn]);

  useEffect(() => () => clearHydrationProgressTimer(), []);

  function clearHydrationProgressTimer() {
    if (hydrationProgressTimer.current) {
      clearInterval(hydrationProgressTimer.current);
      hydrationProgressTimer.current = null;
    }
  }

  function beginHydrationProgress(target: "candidates" | "selected", total: number, label: string) {
    clearHydrationProgressTimer();
    const safeTotal = Math.max(1, total);
    setHydrationProgress({ target, total: safeTotal, done: 0, percent: 3, label });
    hydrationProgressTimer.current = setInterval(() => {
      setHydrationProgress((prev) => {
        if (!prev || prev.target !== target) return prev;
        const nextPercent = Math.min(92, prev.percent + Math.max(1.2, 18 / safeTotal));
        return {
          ...prev,
          percent: nextPercent,
          done: Math.min(safeTotal - 1, Math.floor((nextPercent / 100) * safeTotal)),
          label: nextPercent > 65 ? "Preparing your next question..." : "Reading full tariff entries...",
        };
      });
    }, 380);
  }

  function finishHydrationProgress(done: number, total: number, label: string) {
    clearHydrationProgressTimer();
    const safeTotal = Math.max(1, total);
    setHydrationProgress({
      target: done <= 1 && safeTotal <= 1 ? "selected" : "candidates",
      total: safeTotal,
      done: Math.max(0, Math.min(done, safeTotal)),
      percent: 100,
      label,
    });
  }

  function clearClassificationForEditedQuery(nextQuery: string) {
    setQuery(nextQuery);
    if (
      nextQuery === state.query &&
      !turn &&
      !state.finalCommodity &&
      !state.lastClassifyTurn
    ) {
      return;
    }
    hydrationRequestSeq.current += 1;
    setTurn(null);
    setCandidatePreview([]);
    setCandidateHydration(null);
    setSelectedHydration(null);
    setHydrationError(null);
    setHydrationProgress(null);
    setHydratingTarget(null);
    clearHydrationProgressTimer();
    setLoadingMessage(null);
    update({
      query: nextQuery,
      selectedExample: null,
      qaHistory: [],
      lastClassifyTurn: null,
      finalCommodity: null,
      classifyConfidence: null,
      fixedCandidates: [],
      descriptionOfGoods: nextQuery,
      knowsCustomsValue: null,
      invoiceValue: null,
      invoiceCurrency: "GBP",
      fxRateToGbp: 1,
      freightGbp: 0,
      insuranceGbp: 0,
      otherCostsGbp: 0,
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
      exciseVolumeLitres: null,
      abv: null,
      isSmallProducer: false,
      isDraught: false,
      meursingInputs: null,
      vatRate: 20,
      netMassKg: null,
      dutyResult: null,
      dutyExplainerText: null,
      dutyInference: null,
      additionalChargesGbp: 0,
      landedResult: null,
      declarationResult: null,
      filingIntent: null,
    });
  }

  function updateRetrievalLimit(rawLimit: number) {
    const safeLimit = Math.max(20, Math.min(500, Math.round(rawLimit || 80)));
    const nextConfig = {
      ...(config || {}),
      retrieval: {
        ...retrievalConfig,
        limit: safeLimit,
      },
    };
    setConfig(nextConfig);
    update({ classifyConfig: nextConfig });
  }

  useEffect(() => {
    api.journeyExamples(promptPersona)
      .then((payload) => {
        setExamples(payload.examples);
        setExampleSource(payload.source || "");
        setPromptPersona(payload.persona || promptPersona);
        setPromptPersonas(payload.personas || []);
        setConfig(payload.config);
        update({ classifyConfig: payload.config });
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [promptPersona]);

  useEffect(() => {
    if (state.lastClassifyTurn && !turn) {
      setQuery(state.query);
      setTurn(state.lastClassifyTurn);
      return;
    }
    if (state.finalCommodity && !turn) {
      setQuery(state.query);
      setTurn(savedAnswerTurn(state));
    }
  }, [state.lastClassifyTurn, state.finalCommodity, state.classifyConfidence, state.query, state.qaHistory, turn]);

  function seedPatchForExample(example: JourneyExample): Partial<JourneyState> {
    const seed = example.seed || {};
    return {
      query: example.query,
      selectedExample: example,
      qaHistory: [],
      finalCommodity: null,
      classifyConfidence: null,
      fixedCandidates: [],
      knowsCustomsValue: null,
      invoiceValue: seed.invoice_value ?? null,
      invoiceCurrency: seed.invoice_currency ?? "GBP",
      fxRateToGbp: seed.fx_rate_to_gbp ?? 1,
      freightGbp: seed.freight_gbp ?? 0,
      insuranceGbp: seed.insurance_gbp ?? 0,
      otherCostsGbp: seed.other_costs_gbp ?? 0,
      customsValueGbp: seededCustomsValue(example),
      valuationResult: null,
      valuationGuideResult: null,
      valuationMethodCode: null,
      importDestination: "GB",
      importDate: null,
      countryOfOrigin: "",
      hasProofOfOrigin: false,
      quantityUnits: seed.quantity_units ?? null,
      quantityUnitType: seed.quantity_unit_type ?? null,
      exciseVolumeLitres: seed.excise_volume_litres ?? null,
      abv: seed.abv ?? null,
      isSmallProducer: false,
      isDraught: false,
      meursingInputs: seed.meursing_inputs ?? null,
      vatRate: 20,
      netMassKg: seed.net_mass_kg ?? null,
      descriptionOfGoods: seed.description_of_goods ?? example.description,
      dutyResult: null,
      dutyExplainerText: null,
      dutyInference: null,
      additionalChargesGbp: 0,
      landedResult: null,
      declarationResult: null,
      filingIntent: null,
    };
  }

  function savedAnswerTurn(saved: JourneyState): ClassifyTurn | null {
    if (!saved.finalCommodity) return null;
    return {
      candidates: [],
      fixed_candidates: saved.fixedCandidates,
      mode: "answers",
      answers: [
        {
          commodity_code: saved.finalCommodity.commodity_code,
          confidence: saved.classifyConfidence ?? "Saved",
          description: saved.finalCommodity.description,
        },
      ],
      question: null,
      kg_notes: [],
      facet_enrichment: null,
      augmentation_summary: {},
      error_message: null,
      qa_history: saved.qaHistory,
    };
  }

  function chooseExample(example: JourneyExample) {
    // Load the example into the input (and its context) but do NOT start the
    // classification - the trader reviews/edits, then clicks Start.
    setQuery(example.query);
    update(seedPatchForExample(example));
  }

  // Staged loading updates from the SSE classify endpoints. Stale-request
  // events are dropped so an abandoned stream cannot clobber a newer request.
  function onClassifyProgress(requestId: number, name: string, payload: unknown) {
    if (requestId !== classifyRequestSeq.current) return;
    if (name === "expansion_done") {
      setLoadingMessage("Understanding your description...");
    } else if (name === "retrieval_done") {
      const count = Number((payload as { count?: number } | null)?.count ?? 0);
      setLoadingMessage(`Searched the tariff - ${count} possible codes found...`);
    } else if (name === "candidates_ready") {
      const rows = ((payload as { candidates?: CandidatePreview[] } | null)?.candidates ?? [])
        .filter((c) => c && c.commodity_code);
      if (rows.length > 0) setCandidatePreview(rows);
    } else if (name === "llm_started") {
      setLoadingMessage("Preparing your question...");
    }
  }

  async function runClassification(nextQuery: string, patch: Partial<JourneyState> = {}, message = "Retrieving candidates and building the first Q&A turn...") {
    const trimmedQuery = nextQuery.trim();
    if (!trimmedQuery) return;
    const requestId = ++classifyRequestSeq.current;
    hydrationRequestSeq.current += 1;
    setLoading(true);
    setLoadingMessage(message);
    setError(null);
    setTurn(null);
    setCandidatePreview([]);
    setCandidateHydration(null);
    setSelectedHydration(null);
    setHydrationError(null);
    setHydrationProgress(null);
    setHydratingTarget(null);
    clearHydrationProgressTimer();
    try {
      let t: ClassifyTurn | null = null;
      try {
        t = await api.classifyStartStream(trimmedQuery, effectiveConfig, {
          onEvent: (name, payload) => onClassifyProgress(requestId, name, payload),
        });
      } catch (streamErr) {
        // Streaming unavailable (older backend, proxy buffering, parse
        // failure) - fall back to the non-streaming endpoint once. Say so:
        // a silent retry looks like a frozen screen for the whole re-run.
        console.warn("classify start stream failed, falling back:", streamErr);
        t = null;
      }
      if (!t) {
        if (requestId !== classifyRequestSeq.current) return;
        setLoadingMessage("Live progress unavailable - still searching (this can take a few minutes)...");
        t = await api.classifyStart(trimmedQuery, effectiveConfig);
      }
      if (requestId !== classifyRequestSeq.current) return;
      setQuery(trimmedQuery);
      setTurn(t);
      update({
        ...patch,
        query: trimmedQuery,
        qaHistory: [],
        lastClassifyTurn: t,
        finalCommodity: null,
        classifyConfidence: null,
        fixedCandidates: t.fixed_candidates || [],
        classifyConfig: effectiveConfig,
        descriptionOfGoods: patch.descriptionOfGoods ?? trimmedQuery,
        knowsCustomsValue: patch.knowsCustomsValue ?? null,
        invoiceValue: patch.invoiceValue ?? null,
        invoiceCurrency: patch.invoiceCurrency ?? "GBP",
        fxRateToGbp: patch.fxRateToGbp ?? 1,
        freightGbp: patch.freightGbp ?? 0,
        insuranceGbp: patch.insuranceGbp ?? 0,
        otherCostsGbp: patch.otherCostsGbp ?? 0,
        customsValueGbp: patch.customsValueGbp ?? null,
        valuationResult: null,
        valuationGuideResult: null,
        valuationMethodCode: null,
        importDestination: "GB",
        importDate: null,
        countryOfOrigin: patch.countryOfOrigin ?? "",
        hasProofOfOrigin: patch.hasProofOfOrigin ?? false,
        quantityUnits: patch.quantityUnits ?? null,
        quantityUnitType: patch.quantityUnitType ?? null,
        exciseVolumeLitres: patch.exciseVolumeLitres ?? null,
        abv: patch.abv ?? null,
        isSmallProducer: patch.isSmallProducer ?? false,
        isDraught: patch.isDraught ?? false,
        meursingInputs: patch.meursingInputs ?? null,
        vatRate: patch.vatRate ?? 20,
        netMassKg: patch.netMassKg ?? null,
        dutyResult: null,
        dutyExplainerText: null,
        dutyInference: null,
        additionalChargesGbp: patch.additionalChargesGbp ?? 0,
        landedResult: null,
        declarationResult: null,
        filingIntent: null,
      });
    } catch (err: any) {
      if (requestId !== classifyRequestSeq.current) return;
      setError(err.message ?? String(err));
    } finally {
      if (requestId === classifyRequestSeq.current) {
        setLoading(false);
        setLoadingMessage(null);
      }
    }
  }

  async function start(e: React.FormEvent) {
    e.preventDefault();
    await runClassification(query);
  }

  async function answer(qText: string, aText: string) {
    const newHistory = [...(turn?.qa_history ?? state.qaHistory), { question: qText, answer: aText }];
    const requestId = ++classifyRequestSeq.current;
    hydrationRequestSeq.current += 1;
    // We just superseded any in-flight hydration; its progress UI is ours to clear
    // or the simulated bar leaks forever (seen frozen at "92/100 codes").
    clearHydrationProgressTimer();
    setHydrationProgress(null);
    setHydratingTarget(null);
    setLoading(true);
    setLoadingMessage("Processing your answer and ranking the surviving candidate codes...");
    setError(null);
    setCandidatePreview([]);
    setCandidateHydration(null);
    setSelectedHydration(null);
    setHydrationError(null);
    const fixedCandidates = turn?.fixed_candidates || state.fixedCandidates;
    try {
      let t: ClassifyTurn | null = null;
      try {
        t = await api.classifyAnswerStream(query, newHistory, effectiveConfig, fixedCandidates, {
          onEvent: (name, payload) => onClassifyProgress(requestId, name, payload),
        });
      } catch (streamErr) {
        // Streaming unavailable - fall back to the non-streaming endpoint once.
        // Say so: the fallback re-runs the whole turn, and a silent retry
        // looks like a frozen screen for the duration.
        console.warn("classify answer stream failed, falling back:", streamErr);
        t = null;
      }
      if (!t) {
        if (requestId !== classifyRequestSeq.current) return;
        setLoadingMessage("Live progress unavailable - still ranking your answer (this can take a few minutes)...");
        t = await api.classifyAnswer(query, newHistory, effectiveConfig, fixedCandidates);
      }
      if (requestId !== classifyRequestSeq.current) return;
      setTurn(t);
      const patch: Partial<JourneyState> = {
        qaHistory: newHistory,
        lastClassifyTurn: t,
        fixedCandidates: t.fixed_candidates || turn?.fixed_candidates || state.fixedCandidates,
      };
      const topAnswer = t.mode === "answers" ? t.answers[0] : null;
      if (topAnswer) {
        patch.finalCommodity = {
          commodity_code: topAnswer.commodity_code,
          description: topAnswer.description,
        };
        patch.classifyConfidence = topAnswer.confidence;
        patch.descriptionOfGoods = state.descriptionOfGoods || query || topAnswer.description;
        patch.knowsCustomsValue = null;
        patch.customsValueGbp = state.customsValueGbp ?? seededCustomsValueForCode(state, topAnswer.commodity_code);
        patch.valuationResult = null;
        patch.valuationGuideResult = null;
        patch.valuationMethodCode = null;
        patch.importDestination = "GB";
        patch.importDate = null;
        patch.countryOfOrigin = "";
        patch.hasProofOfOrigin = false;
        patch.quantityUnits = state.quantityUnits;
        patch.quantityUnitType = state.quantityUnitType;
        patch.exciseVolumeLitres = state.exciseVolumeLitres;
        patch.abv = state.abv;
        patch.isSmallProducer = false;
        patch.isDraught = false;
        patch.meursingInputs = state.meursingInputs;
        patch.vatRate = 20;
        patch.netMassKg = state.netMassKg;
        patch.dutyInference = null;
        patch.dutyExplainerText = null;
        patch.dutyResult = null;
        patch.additionalChargesGbp = 0;
        patch.landedResult = null;
        patch.declarationResult = null;
        patch.filingIntent = null;
      }
      update(patch);
    } catch (err: any) {
      if (requestId !== classifyRequestSeq.current) return;
      setError(err.message ?? String(err));
    } finally {
      if (requestId === classifyRequestSeq.current) {
        setLoading(false);
        setLoadingMessage(null);
      }
    }
  }

  function pickCode(code: string, description: string, confidence: string) {
    hydrationRequestSeq.current += 1;
    setSelectedHydration(null);
    update({
      finalCommodity: { commodity_code: code, description },
      classifyConfidence: confidence,
      descriptionOfGoods: state.descriptionOfGoods || description,
      knowsCustomsValue: null,
      customsValueGbp: state.customsValueGbp ?? seededCustomsValueForCode(state, code),
      valuationResult: null,
      valuationGuideResult: null,
      valuationMethodCode: null,
      importDestination: "GB",
      importDate: null,
      countryOfOrigin: "",
      hasProofOfOrigin: false,
      quantityUnits: state.quantityUnits,
      quantityUnitType: state.quantityUnitType,
      exciseVolumeLitres: state.exciseVolumeLitres,
      abv: state.abv,
      isSmallProducer: false,
      isDraught: false,
      meursingInputs: state.meursingInputs,
      vatRate: 20,
      netMassKg: state.netMassKg,
      dutyInference: null,
      dutyExplainerText: null,
      dutyResult: null,
      additionalChargesGbp: 0,
      landedResult: null,
      declarationResult: null,
      filingIntent: null,
    });
  }

  function startOver() {
    classifyRequestSeq.current += 1;
    hydrationRequestSeq.current += 1;
    setTurn(null);
    setQuery("");
    setCandidatePreview([]);
    setCandidateHydration(null);
    setSelectedHydration(null);
    setHydrationError(null);
    setLoadingMessage(null);
    update(initialJourneyState);
  }

  const finalCode = state.finalCommodity?.commodity_code;
  const processMode = state.qaProcessMode || "ott_staging_kg";
  const processModeMeta = QA_PROCESS_MODES.find((mode) => mode.id === processMode) || QA_PROCESS_MODES[0];
  const questionCap = turn?.augmentation_summary?.question_cap ?? 7;
  const rescue = turn?.augmentation_summary?.rescue;
  const rescueTriggered = Boolean(rescue && rescue["triggered"]);

  async function hydrateCandidates() {
    if (!turn || turn.candidates.length === 0) return;
    const sourceTurn = turn;
    const sourceQuery = query;
    const requestId = ++hydrationRequestSeq.current;
    setHydratingTarget("candidates");
    setHydrationError(null);
    beginHydrationProgress("candidates", sourceTurn.candidates.length, "Reading full tariff entries...");
    try {
      const payload = await api.hydrateCandidates({
        query: sourceQuery,
        config: effectiveConfig,
        candidates: sourceTurn.candidates,
        candidate_limit: sourceTurn.candidates.length,
        hydrate_limit: 0,
        summarize: state.hydrationUseModel,
        allow_spend: true,
        model: state.hydrationModel,
        sources: state.hydrationSources,
      });
      if (requestId !== hydrationRequestSeq.current) return; // superseded: the newer request owns the progress UI
      if (queryRef.current !== sourceQuery || turnRef.current !== sourceTurn) {
        // Still the owner, but the question/turn moved on - clean up or the bar leaks forever.
        clearHydrationProgressTimer();
        setHydrationProgress(null);
        return;
      }
      setCandidateHydration(payload);
      finishHydrationProgress(
        payload.hydrated.length,
        payload.candidate_count,
        "Done. Your question has been refreshed."
      );
      if (payload.question_hint && sourceTurn.mode === "questions") {
        const nextTurn = {
          ...sourceTurn,
          question: {
            question: payload.question_hint.question,
            options: payload.question_hint.options,
          },
          augmentation_summary: {
            ...turn.augmentation_summary,
            hydrated_question_hint: {
              source: payload.question_hint.source || "hydrated_shortlist",
            },
          } as ClassifyTurn["augmentation_summary"],
        };
        setTurn(nextTurn);
        update({ lastClassifyTurn: nextTurn });
      }
    } catch (err: any) {
      if (requestId !== hydrationRequestSeq.current) return;
      setHydrationError(err.message ?? String(err));
      clearHydrationProgressTimer();
      setHydrationProgress((prev) => prev ? { ...prev, label: "Hydration failed before completion." } : prev);
    } finally {
      if (requestId === hydrationRequestSeq.current) {
        setHydratingTarget(null);
      }
    }
  }

  async function hydrateSelected() {
    if (!finalCode) return;
    const sourceCode = finalCode;
    const requestId = ++hydrationRequestSeq.current;
    setHydratingTarget("selected");
    setHydrationError(null);
    beginHydrationProgress("selected", 1, `Hydrating selected code ${sourceCode}...`);
    try {
      const payload = await api.hydrateCommodity(sourceCode, {
        summarize: state.hydrationUseModel,
        allow_spend: true,
        model: state.hydrationModel,
        sources: state.hydrationSources,
      });
      if (requestId !== hydrationRequestSeq.current) return; // superseded: the newer request owns the progress UI
      if (state.finalCommodity?.commodity_code !== sourceCode) {
        // Still the owner, but the context moved on - clean up or the bar leaks forever.
        clearHydrationProgressTimer();
        setHydrationProgress(null);
        return;
      }
      setSelectedHydration(payload);
      finishHydrationProgress(1, 1, `Selected code ${sourceCode} hydration complete.`);
    } catch (err: any) {
      if (requestId !== hydrationRequestSeq.current) return;
      setHydrationError(err.message ?? String(err));
      clearHydrationProgressTimer();
      setHydrationProgress((prev) => prev ? { ...prev, label: "Selected-code hydration failed." } : prev);
    } finally {
      if (requestId === hydrationRequestSeq.current) {
        setHydratingTarget(null);
      }
    }
  }

  const hasSideContext = Boolean(
    turn && (
      turn.qa_history.length > 0 ||
      turn.facet_enrichment ||
      turn.kg_notes.length > 0
    )
  );

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold mb-2">1. What are you importing?</h2>
        <p className="text-gray-400 mb-4">
          Describe what you're importing in a few words. We'll search the UK Trade Tariff and ask
          you a couple of short questions to find the right commodity code for your goods.
        </p>
        <p className="mb-4">
          <a href="https://www.gov.uk/trade-tariff" target="_blank" rel="noreferrer" className="text-sm text-blue-400 underline">
            Look up commodity codes on GOV.UK (opens in new tab)
          </a>
        </p>
        <form onSubmit={start} className="max-w-none space-y-3">
          <div className="space-y-1">
            <label className="tj-label" htmlFor="q">Describe your goods</label>
            <p className="tj-hint">
              For example: <em>flip flops</em>, <em>office chair</em>, <em>red wine</em>, <em>iPhone charger</em>, <em>rubber sole sneakers</em>
            </p>
          </div>
          <div className="flex flex-col gap-3 md:flex-row md:items-start">
            <input
              id="q"
              type="text"
              className="tj-input md:flex-1"
              value={query}
              onChange={(e) => clearClassificationForEditedQuery(e.target.value)}
              placeholder="What are you importing?"
              disabled={loading || hydratingTarget !== null}
              autoFocus
            />
            <button type="submit" className="tj-btn md:w-32" disabled={loading || hydratingTarget !== null || !query.trim()}>
              {loading ? "Searching..." : "Start"}
            </button>
            {turn && (
              <button type="button" className="tj-btn-secondary md:w-32" onClick={startOver} disabled={loading || hydratingTarget !== null}>
                Start over
              </button>
            )}
          </div>
        </form>
        {loading && (
          <div className="mt-3 max-w-4xl border-l-4 border-blue-500 bg-gray-900 p-3 text-sm text-gray-200">
            {loadingMessage || "Working..."}
            {candidatePreview.length > 0 && (
              <div className="mt-3 border-t border-gray-800 pt-3">
                <div className="text-xs font-bold uppercase tracking-widest text-gray-400">
                  Possible matches so far
                </div>
                <ul className="mt-2 space-y-1">
                  {candidatePreview.slice(0, 10).map((c) => (
                    <li key={c.commodity_code} className="flex justify-between gap-3 border-b border-gray-800 pb-1">
                      <span className="font-mono font-semibold text-blue-400">{c.commodity_code}</span>
                      <span className="flex-1 text-gray-100">{c.description}</span>
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-xs text-gray-400">
                  We're now working out which of these fits your goods best.
                </p>
              </div>
            )}
          </div>
        )}
        <details className="mt-4 max-w-none border border-gray-800 bg-gray-950/40">
          <summary className="cursor-pointer select-none px-3 py-2 text-xs font-bold uppercase tracking-widest text-gray-400">
            Advanced settings (demo)
          </summary>
          <div className="grid items-start gap-4 border-t border-gray-800 p-4 lg:grid-cols-[2fr_1.5fr_1fr]">
            <div className="min-w-0">
              <label className="tj-label block" htmlFor="qa-process-mode">Q&A process</label>
              <select
                id="qa-process-mode"
                className="tj-input w-full"
                value={processMode}
                disabled={loading || hydratingTarget !== null}
                onChange={(e) => update({ qaProcessMode: e.currentTarget.value as QaProcessMode })}
              >
                {QA_PROCESS_MODES.map((mode) => (
                  <option key={mode.id} value={mode.id}>{mode.label}</option>
                ))}
              </select>
              <span className="mt-1.5 block text-xs leading-relaxed text-gray-400">
                {processModeMeta.hint}
              </span>
            </div>
            <div className="min-w-0">
              <ModelSelector
                value={state.candidateSelectionModel || "gpt-5.5"}
                disabled={processMode === "local_rules" || loading || hydratingTarget !== null}
                onChange={(candidateSelectionModel) => update({ candidateSelectionModel })}
                label="AI model"
              />
              <span className="mt-1.5 block text-xs leading-relaxed text-gray-400">
                Ranks the codes and writes your questions. Nano is fastest; GPT-5.5 is the most thorough.
              </span>
            </div>
            <div className="min-w-0">
              <label className="tj-label block" htmlFor="candidate-limit">Retrieval pool</label>
              <input
                id="candidate-limit"
                type="number"
                className="tj-input w-full"
                value={Number.isFinite(retrievalLimit) ? retrievalLimit : 80}
                min={20}
                max={500}
                step={20}
                disabled={loading || hydratingTarget !== null}
                onChange={(e) => updateRetrievalLimit(Number(e.currentTarget.value))}
              />
              <span className="mt-1.5 block text-xs leading-relaxed text-gray-400">
                Frozen Q&A pool. The experiment winner used 500; keep 80 for demo latency unless recall is the question.
              </span>
            </div>
          </div>
        </details>
        {examples.length > 0 && (
          <div className="mt-4">
            <div className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div className="text-xs font-bold tracking-widest uppercase text-gray-400">
                Try an example
              </div>
              {exampleSource === "live_kg" && promptPersonas.length > 0 && (
                <label className="flex items-center gap-2 text-xs text-gray-300">
                  <span className="font-semibold uppercase tracking-wider text-gray-400">Prompt level</span>
	                  <select
                    className="tj-input h-9 min-h-0 w-44 py-1 text-xs"
                    value={promptPersona}
	                    onChange={(e) => {
	                      setPromptPersona(e.currentTarget.value);
	                      update({ selectedExample: null });
	                    }}
                    disabled={loading || hydratingTarget !== null}
                  >
                    {promptPersonas.map((persona) => (
                      <option key={persona.id} value={persona.id}>
                        {persona.label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {examples.map((example) => (
                <button
                  key={example.id}
                  type="button"
                  className={`tj-btn-secondary text-xs ${state.selectedExample?.id === example.id ? "border-emerald-500 text-emerald-300" : ""}`}
                  onClick={() => chooseExample(example)}
                  disabled={loading || hydratingTarget !== null}
                >
                  <span>{example.label}</span>
                  {(example.fact_count ?? example.facet_count) ? (
                    <span className="hidden ml-2 text-xs font-semibold text-gray-300">
                      target CC facts: {example.fact_count ?? example.facet_count}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="border-l-4 border-red-500 bg-gray-900 p-4">
          <strong>Sorry, something went wrong.</strong> Please try again.
          <details className="mt-2 text-xs text-gray-400">
            <summary className="cursor-pointer">Technical details</summary>
            {error}
          </details>
        </div>
      )}

      {turn && <RetrievalHealthBanner health={turn.retrieval_health} />}

      {turn && (
        <div className={`grid grid-cols-1 gap-6 ${hasSideContext ? "xl:grid-cols-5" : "xl:grid-cols-1"}`}>
          <div className={`${hasSideContext ? "xl:col-span-3" : ""} space-y-6`}>
            {turn.mode === "no_candidates" && (
              <div className="tj-card">
                <p>No tariff results matched that query. Try a different phrasing.</p>
                <p className="mt-2">
                  <a href="https://www.gov.uk/trade-tariff" target="_blank" rel="noreferrer" className="text-blue-400 underline">
                    Or search the Trade Tariff tool on GOV.UK (opens in new tab)
                  </a>
                </p>
              </div>
            )}

            {turn.mode === "error" && (
              <div className="border-l-4 border-red-500 bg-gray-900 p-4">
                <strong>The classifier couldn't answer:</strong> {turn.error_message}
                <br />
                Try starting over with a different description.
              </div>
            )}

            {turn.mode === "questions" && turn.question && turn.qa_history.length < questionCap && (
              <div className="tj-card">
                <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-baseline sm:justify-between">
                  <div>
                    <span className="text-xs font-bold tracking-widest uppercase text-emerald-400">
                      A quick question about your goods
                    </span>
                    <div className="mt-1 text-xs text-gray-400">
                      Question {turn.qa_history.length + 1} of up to {questionCap}
                    </div>
                    <div className="mt-1 text-xs text-gray-400">
                      {(turn.augmentation_summary as any)?.hydrated_question_hint ? "Based on detailed tariff entries" : "Based on UK tariff data"}
                    </div>
                  </div>
                  <span className="text-xs text-gray-400">{turn.candidates.length} candidate codes considered</span>
                </div>
                <RetrievalStackBadge summary={turn.augmentation_summary} />
                <AugmentationBadge summary={turn.augmentation_summary} />
                {loading && (
                  <div className="mb-4 border-l-4 border-blue-500 bg-gray-900 p-3 text-sm text-gray-200">
                    {loadingMessage || "Processing your answer and ranking the surviving candidate codes..."}
                  </div>
                )}
                <div className="mb-4 border border-gray-700 bg-gray-950 p-3 text-sm text-gray-300">
                  <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                    <div>
                      <strong className="text-gray-100">Want a better question?</strong>
                      <p className="mt-1 text-xs text-gray-400">
                        {loading
                          ? "Answer processing is in progress, so this is paused until the ranked results return."
                          : "Answer now, or let us read the full tariff entry for every possible code first."}
                      </p>
                    </div>
                    <button
                      type="button"
                      className="tj-btn-secondary whitespace-nowrap"
                      onClick={hydrateCandidates}
                      disabled={loading || hydratingTarget !== null || turn.candidates.length === 0}
                    >
                      {hydratingTarget === "candidates" ? "Getting more detail..." : `Get more detail on all ${turn.candidates.length} codes`}
                    </button>
                  </div>
                  {hydratingTarget === "candidates" && (
                    <HydrationProgressBar progress={hydrationProgress} compact />
                  )}
                  {hydrationError && (
                    <div className="mt-3 border-l-4 border-red-500 bg-gray-900 p-2 text-xs text-gray-200">
                      Hydration error: {hydrationError}
                    </div>
                  )}
                </div>
                <h3 className="text-xl mb-4">{turn.question.question}</h3>
                <div className="space-y-2">
                  {turn.question.options.filter((o, i, all) => all.indexOf(o) === i).map((o, i) => {
                    // Standing option: keep the raw text (sent back verbatim)
                    // and separate it visually from the real options.
                    if (o === NONE_OF_THESE_OPTION) {
                      return (
                        <div key={`${o}-${i}`} className="border-t border-gray-800 pt-3">
                          <button
                            onClick={() => answer(turn.question!.question, o)}
                            disabled={loading || hydratingTarget === "candidates"}
                            className="w-full text-left border-2 border-gray-700 bg-gray-900 px-4 py-3 text-gray-400 hover:bg-gray-800 focus:outline-none focus:ring-4 focus:ring-blue-500"
                          >
                            {o}
                          </button>
                        </div>
                      );
                    }
                    return (
                      <button
                        key={`${o}-${i}`}
                        onClick={() => answer(turn.question!.question, o)}
                        disabled={loading || hydratingTarget === "candidates"}
                        className="w-full text-left border-2 border-gray-600 bg-gray-900 px-4 py-3 hover:bg-gray-800 focus:outline-none focus:ring-4 focus:ring-blue-500"
                      >
                        <span className="font-semibold">{prettyOption(o)}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {(() => {
              // Transparency panel: prompt-engineering story for this turn.
              // Loosely typed - augmentation_summary.debug / eliminate_trace are
              // not on ClassifyTurn, so read through (turn as any) to keep tsc clean.
              const t = turn as any;
              const debug = t.augmentation_summary?.debug ?? {};
              const promptSystem: string = debug.prompt_system ?? "";
              const promptUser: string = debug.prompt_user ?? "";
              const sessionFacts = t.augmentation_summary?.session_facts;
              const elim = t.eliminate_trace ?? {};
              const survivors: any[] = Array.isArray(elim.survivors) ? elim.survivors : [];
              const ruledOut: any[] = Array.isArray(elim.ruled_out) ? elim.ruled_out : [];
              const frozenCount = t.augmentation_summary?.frozen_candidate_count;
              const survivorCount = t.augmentation_summary?.survivor_count;
              const ruledOutCount = t.augmentation_summary?.ruled_out_count;
              const hasPrompt = Boolean(promptSystem || promptUser);
              const hasState = (turn.qa_history?.length ?? 0) > 0 || (sessionFacts != null && Object.keys(sessionFacts || {}).length > 0);
              const hasShortlist = survivors.length > 0 || ruledOut.length > 0 || frozenCount != null;
              if (!hasPrompt && !hasState && !hasShortlist) return null;
              const sessionFactsText =
                sessionFacts == null
                  ? ""
                  : typeof sessionFacts === "string"
                    ? sessionFacts
                    : JSON.stringify(sessionFacts, null, 2);
              return (
                <details className="tj-card">
                  <summary className="cursor-pointer select-none text-xs font-bold uppercase tracking-widest text-gray-400">
                    What &amp; why (this turn)
                  </summary>
                  <p className="mt-1 text-xs text-gray-400">
                    How we steer the model: a frozen shortlist the LLM can only rule
                    out from (eliminate strategy), with KG-grounded clarifying questions.
                  </p>

                  <div className="mt-4">
                    <div className="text-xs font-bold uppercase tracking-widest text-emerald-400">The prompt</div>
                    <p className="mt-1 text-xs text-gray-400">
                      The exact system and user messages sent to the model this turn -
                      so you can audit what evidence and instructions it actually saw.
                    </p>
                    {hasPrompt ? (
                      <div className="mt-2 space-y-2">
                        {promptSystem && (
                          <div>
                            <div className="text-[10px] uppercase tracking-wider text-gray-500">System</div>
                            <pre className="mt-1 max-h-[240px] overflow-auto whitespace-pre-wrap break-words border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] text-gray-300">{promptSystem}</pre>
                          </div>
                        )}
                        {promptUser && (
                          <div>
                            <div className="text-[10px] uppercase tracking-wider text-gray-500">User</div>
                            <pre className="mt-1 max-h-[240px] overflow-auto whitespace-pre-wrap break-words border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] text-gray-300">{promptUser}</pre>
                          </div>
                        )}
                      </div>
                    ) : (
                      <p className="mt-2 text-xs italic text-gray-500">No prompt captured for this turn.</p>
                    )}
                  </div>

                  <div className="mt-4">
                    <div className="text-xs font-bold uppercase tracking-widest text-emerald-400">State across rounds</div>
                    <p className="mt-1 text-xs text-gray-400">
                      What we carry forward between turns - the Q&amp;A so far and the
                      facts you have asserted - so each question narrows, never repeats.
                    </p>
                    {turn.qa_history?.length > 0 ? (
                      <ul className="mt-2 space-y-1 text-xs">
                        {turn.qa_history.map((h, i) => (
                          <li key={i}>
                            <span className="text-gray-400 italic">Q: {h.question}</span>{" "}
                            <span className="font-semibold text-gray-100">A: {prettyOption(h.answer)}</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-2 text-xs italic text-gray-500">No questions answered yet.</p>
                    )}
                    {sessionFactsText && (
                      <div className="mt-2">
                        <div className="text-[10px] uppercase tracking-wider text-gray-500">User-asserted facts</div>
                        <pre className="mt-1 max-h-[200px] overflow-auto whitespace-pre-wrap break-words border border-gray-800 bg-gray-950 p-2 font-mono text-[11px] text-gray-300">{sessionFactsText}</pre>
                      </div>
                    )}
                  </div>

                  <div className="mt-4">
                    <div className="text-xs font-bold uppercase tracking-widest text-emerald-400">How the shortlist changed</div>
                    <p className="mt-1 text-xs text-gray-400">
                      The eliminate step: the frozen shortlist is split into survivors
                      (still in play) and ruled-out codes (with the model&apos;s reason).
                    </p>
                    {(frozenCount != null || survivorCount != null || ruledOutCount != null) && (
                      <p className="mt-2 text-xs text-gray-400">
                        <span className="font-mono text-gray-200">{frozenCount ?? "?"}</span> frozen{" "}
                        &rarr; <span className="font-mono text-emerald-300">{survivorCount ?? survivors.length}</span> survivors{" / "}
                        <span className="font-mono text-gray-300">{ruledOutCount ?? ruledOut.length}</span> ruled out
                      </p>
                    )}
                    {survivors.length > 0 && (
                      <div className="mt-2">
                        <div className="text-[10px] uppercase tracking-wider text-gray-500">Survivors (ranked)</div>
                        <ul className="mt-1 space-y-1 text-xs">
                          {survivors.map((s, i) => (
                            <li key={s.commodity_code ?? i} className="border-b border-gray-800 pb-1">
                              <span className="font-mono font-semibold text-blue-400">{s.commodity_code ?? s.code ?? "?"}</span>{" "}
                              {s.confidence != null && <span className="text-emerald-300">[{String(s.confidence)}]</span>}{" "}
                              {s.reasoning && <span className="text-gray-400">{s.reasoning}</span>}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {ruledOut.length > 0 && (
                      <div className="mt-2">
                        <div className="text-[10px] uppercase tracking-wider text-gray-500">Ruled out</div>
                        <ul className="mt-1 space-y-1 text-xs">
                          {ruledOut.map((r, i) => (
                            <li key={r.commodity_code ?? i} className="border-b border-gray-800 pb-1">
                              <span className="font-mono font-semibold text-gray-400 line-through">{r.commodity_code ?? r.code ?? "?"}</span>{" "}
                              {r.reason && <span className="text-gray-400">{r.reason}</span>}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {survivors.length === 0 && ruledOut.length === 0 && (
                      <p className="mt-2 text-xs italic text-gray-500">No eliminate trace for this turn.</p>
                    )}
                  </div>
                </details>
              );
            })()}

            {turn.mode === "answers" && turn.answers.length > 0 && (
              <div className="tj-card">
                <div className="flex justify-between items-baseline mb-3">
                  <span className="text-xs font-bold tracking-widest uppercase text-emerald-400">
                    Suggested codes
                  </span>
                  <span className="text-xs text-gray-400" title={turn.augmentation_summary?.candidate_selection?.model || ""}>
                    {turn.augmentation_summary?.candidate_selection?.mode === "llm"
                      ? "Ranked by AI"
                      : "Ranked automatically"}
                  </span>
                </div>
                <div className="mb-4 flex flex-col gap-3 border border-gray-700 bg-gray-950 p-3 text-sm text-gray-300 md:flex-row md:items-center md:justify-between">
                  <div>
                    <strong className="text-gray-100">
                      {finalCode ? "Classification Q&A resolved." : "Classification Q&A needs a selected code."}
                    </strong>
                    <p className="mt-1 text-xs text-gray-400">
                      {finalCode
                        ? "Continue with the selected code to customs value, or choose a different suggested code first."
                        : "Pick the best suggested code to unlock the customs-value step."}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={onNext}
                    className="tj-btn whitespace-nowrap"
                    disabled={!finalCode || loading || hydratingTarget !== null}
                  >
                    {finalCode ? `Use ${finalCode} → Customs value` : "Select a code"}
                  </button>
                </div>
                <ul className="space-y-2">
                  {turn.answers.map((a) => {
                    const isPicked = a.commodity_code === finalCode;
                    const isBestMatch = a.confidence === "Best match";
                    const isFromRetrieval = a.confidence === "From retrieval";
                    return (
                      <li key={a.commodity_code}>
                        <button
                          onClick={() => pickCode(a.commodity_code, a.description, a.confidence)}
                          disabled={loading || hydratingTarget !== null}
                          className={`w-full text-left border-2 px-4 py-3 transition-colors ${
                            isPicked
                              ? "border-emerald-600 bg-gray-900"
                              : isBestMatch
                                ? "border-emerald-700/70 bg-gray-900 hover:bg-gray-900"
                                : "border-gray-600 bg-gray-900 hover:bg-gray-900"
                          } ${isFromRetrieval && !isPicked ? "opacity-75" : ""}`}
                        >
                          <div className="flex justify-between items-baseline mb-1">
                            <span className="font-mono font-bold text-lg">{a.commodity_code}</span>
                            <span className="flex items-baseline gap-2">
                              {a.leaf_adjusted && (
                                <span
                                  className="border border-emerald-800 px-2 py-0.5 text-[11px] text-emerald-300"
                                  title="We moved this suggestion to its full-detail child code. Customs declarations can only be filed against declarable codes, not parent headings."
                                >
                                  refined to a declarable code
                                </span>
                              )}
                              <ConfidenceBadge level={a.confidence} />
                            </span>
                          </div>
                          <div className={`text-sm ${isFromRetrieval && !isPicked ? "text-gray-400" : "text-gray-100"}`}>{a.description}</div>
                        </button>
                      </li>
                    );
                  })}
                </ul>
                <p className="mt-4 text-xs text-gray-400">
                  This is guidance, not a binding ruling. For legal certainty{" "}
                  <a
                    href="https://www.gov.uk/guidance/apply-for-an-advance-tariff-ruling"
                    target="_blank"
                    rel="noreferrer"
                    className="text-blue-400 underline"
                  >
                    apply for an Advance Tariff Ruling (opens in new tab)
                  </a>.
                </p>

                {finalCode && (
                  <div className="mt-6 flex justify-end">
                    <button onClick={onNext} className="tj-btn" disabled={loading || hydratingTarget !== null}>
                      Use {finalCode} &rarr; Customs value
                    </button>
                  </div>
                )}
              </div>
            )}

            {rescueTriggered && (
              <p className="text-xs italic text-gray-400">We widened the search based on your answers.</p>
            )}

            <details className="tj-card">
              <summary className="cursor-pointer select-none text-xs font-bold uppercase tracking-widest text-gray-400">
                All matches we considered ({turn.candidates.length})
              </summary>
              <p className="text-xs text-gray-400 mt-1">
                The full list of tariff codes we compared before asking questions.
              </p>
              <div className="mt-3 max-h-[320px] overflow-y-auto">
                <ul className="space-y-1 text-sm">
                  {turn.candidates.map((c) => (
                    <li key={c.commodity_code} className="flex justify-between gap-3 border-b border-gray-800 pb-1">
                      <span className="font-mono font-semibold text-blue-400">{c.commodity_code}</span>
                      <span className="flex-1 text-gray-100">{c.description}</span>
                      <span className="hidden text-xs text-gray-400 italic">{c.sources.join("+")}</span>
                      {c.in_slice && <span className="text-xs text-emerald-400">extra facts</span>}
                    </li>
                  ))}
                </ul>
              </div>
            </details>

            <LiveHydrationPanel
              state={state}
              update={update}
              turn={turn}
              finalCode={finalCode}
              candidateHydration={candidateHydration}
              selectedHydration={selectedHydration}
              hydratingTarget={hydratingTarget}
              hydrationError={hydrationError}
              classificationBusy={loading}
              hydrationProgress={hydrationProgress}
              onHydrateCandidates={hydrateCandidates}
              onHydrateSelected={hydrateSelected}
            />
          </div>

          {hasSideContext && (
          <aside className="space-y-6 xl:col-span-2">
            {turn.qa_history.length > 0 && (
              <div className="tj-card">
                <span className="text-xs font-bold tracking-widest uppercase text-gray-400">
                  Conversation so far
                </span>
                <ul className="mt-3 space-y-3 text-sm">
                  {turn.qa_history.map((h, i) => (
                    <li key={i}>
                      <div className="text-gray-400 italic">Q: {h.question}</div>
                      <div className="font-semibold">A: {prettyOption(h.answer)}</div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {turn.facet_enrichment && (
              <div className="tj-card border-l-4 border-emerald-600">
                <span className="text-xs font-bold tracking-widest uppercase text-emerald-400">
                  Product fact sheet
                </span>
                <p className="text-xs text-gray-400 mt-1">
                  Facts we hold about this type of product, organised by facet (the
                  attribute our questions ask about), used to ask you better questions.
                </p>
                <table className="text-xs mt-2 w-full">
                  <thead>
                    <tr className="text-left text-gray-500 uppercase tracking-wider text-[10px]">
                      <th className="pr-2 font-semibold">Facet</th>
                      <th className="font-semibold">Fact (this product)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(turn.facet_enrichment.facets).map(([k, v]) => (
                      <tr key={k}>
                        <td className="text-gray-400 pr-2">{prettyOption(k)}</td>
                        <td className="font-mono">{v}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {turn.kg_notes.length > 0 && (
              <div className="tj-card">
                <span className="text-xs font-bold tracking-widest uppercase text-gray-400">
                  Tariff notes for these codes
                </span>
                <ul className="mt-3 space-y-3 text-sm">
                  {turn.kg_notes.map((n) => (
                    <li key={n.id}>
                      <div className="font-semibold">{n.title}</div>
                      <p className="text-gray-400 text-xs mt-1">{n.body}</p>
                      <p className="text-xs italic text-gray-400 mt-1">Source: {n.source}</p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </aside>
          )}
        </div>
      )}
    </div>
  );
}

const HYDRATION_MODELS = [
  { id: "gpt-5.5", label: "GPT-5.5 - best quality" },
  { id: "gpt-5-nano", label: "GPT-5 Nano - fastest/cheapest" },
  { id: "gpt-5-mini", label: "GPT-5 Mini - stronger small model" },
  { id: "gpt-4.1-nano", label: "GPT-4.1 Nano - legacy cheap" },
  { id: "gpt-4.1-mini", label: "GPT-4.1 Mini - legacy small" },
];

const HYDRATION_SOURCE_OPTIONS = [
  { key: "facets", label: "CC facts" },
  { key: "footnotes", label: "Footnotes" },
  { key: "measures", label: "Measures" },
  { key: "section_notes", label: "Section notes" },
  { key: "chapter_notes", label: "Chapter notes" },
  { key: "hsen", label: "HSEN notes" },
  { key: "atar", label: "ATAR rulings + scrape" },
  { key: "girs", label: "GIRs" },
] as const;

function ModelSelector({
  value,
  disabled,
  onChange,
  label,
}: {
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  label: string;
}) {
  const knownModel = HYDRATION_MODELS.some((m) => m.id === value);
  const [customMode, setCustomMode] = useState(!knownModel);
  const isCustom = customMode || !knownModel;
  return (
    <div className="min-w-0">
      <span className="tj-label block">{label}</span>
      <div className={isCustom ? "grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2" : "min-w-0"}>
        <select
          className="tj-input w-full"
          value={isCustom ? "custom" : value}
          disabled={disabled}
          aria-label={label}
          onChange={(e) => {
            if (e.currentTarget.value === "custom") {
              setCustomMode(true);
            } else {
              setCustomMode(false);
              onChange(e.currentTarget.value);
            }
          }}
        >
          {HYDRATION_MODELS.map((m) => (
            <option key={m.id} value={m.id}>{m.label}</option>
          ))}
          <option value="custom">Custom model</option>
        </select>
        {isCustom && (
          <input
            className="tj-input"
            value={value}
            disabled={disabled}
            onChange={(e) => onChange(e.currentTarget.value)}
            placeholder="OpenAI model id, e.g. gpt-5.5"
            aria-label={`${label} id`}
          />
        )}
      </div>
    </div>
  );
}

function LiveHydrationPanel({
  state,
  update,
  turn,
  finalCode,
  candidateHydration,
  selectedHydration,
  hydratingTarget,
  hydrationError,
  classificationBusy,
  hydrationProgress,
  onHydrateCandidates,
  onHydrateSelected,
}: {
  state: JourneyState;
  update: (patch: Partial<JourneyState>) => void;
  turn: ClassifyTurn;
  finalCode?: string;
  candidateHydration: CandidateHydrationRun | null;
  selectedHydration: CommodityHydration | null;
  hydratingTarget: "candidates" | "selected" | null;
  hydrationError: string | null;
  classificationBusy: boolean;
  hydrationProgress: HydrationProgress;
  onHydrateCandidates: () => void;
  onHydrateSelected: () => void;
}) {
  const model = state.hydrationModel || "gpt-5-nano";
  const isBusy = hydratingTarget !== null || classificationBusy;
  const updateSource = (key: keyof JourneyState["hydrationSources"], value: boolean) => {
    update({ hydrationSources: { ...state.hydrationSources, [key]: value } });
  };
  return (
    <details className="tj-card">
      <summary className="cursor-pointer select-none text-xs font-bold uppercase tracking-widest text-gray-400">
        Advanced: evidence lookup (for presenters)
      </summary>
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div className="max-w-3xl">
          <span className="text-xs font-bold tracking-widest uppercase text-gray-400">
            Live candidate hydration
          </span>
          <p className="mt-2 text-sm text-gray-300">
            Hydrates already-retrieved CCs with ATAR, HSEN, legal notes, product facts and footnotes.
            Model-assisted hydration is an explicit demo action; deterministic evidence still returns
            when the provider key or chosen model is unavailable.
          </p>
          <p className="mt-2 text-xs text-amber-200">
            Provider summaries can spend API credits. Cache write-back is off in this UI.
          </p>
        </div>

        <div className="w-full space-y-3 xl:w-[360px]">
          <details className="border border-gray-800 bg-gray-950/60">
            <summary className="cursor-pointer select-none px-3 py-2 text-xs font-bold uppercase tracking-widest text-gray-400">
              Advanced options
            </summary>
            <div className="space-y-3 border-t border-gray-800 p-3">
              <label className="flex items-start gap-3 text-sm text-gray-200">
                <input
                  type="checkbox"
                  className="mt-1 h-4 w-4"
                  checked={state.hydrationUseModel}
                  onChange={(e) => update({ hydrationUseModel: e.currentTarget.checked })}
                  disabled={isBusy}
                />
                <span>
                  Use provider model for hydration summary
                  <span className="block text-xs text-gray-400">
                    Off = deterministic evidence bundle only.
                  </span>
                </span>
              </label>
              <ModelSelector
                value={model}
                disabled={!state.hydrationUseModel || isBusy}
                onChange={(hydrationModel) => update({ hydrationModel })}
                label="Hydration summary model"
              />
              <div>
                <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-400">
                  Evidence sources
                </div>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {HYDRATION_SOURCE_OPTIONS.map((source) => (
                    <label key={source.key} className="flex items-center gap-2 text-xs text-gray-200">
                      <input
                        type="checkbox"
                      className="h-4 w-4"
                      checked={state.hydrationSources[source.key]}
                      disabled={isBusy}
                      onChange={(e) => updateSource(source.key, e.currentTarget.checked)}
                      />
                      <span>{source.label}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>
          </details>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button
              type="button"
              className="tj-btn"
              onClick={onHydrateCandidates}
              disabled={isBusy || turn.candidates.length === 0}
            >
              {hydratingTarget === "candidates" ? "Getting more detail..." : `Get more detail on all ${turn.candidates.length} codes`}
            </button>
            <button
              type="button"
              className="tj-btn-secondary"
              onClick={onHydrateSelected}
              disabled={isBusy || !finalCode}
            >
              {hydratingTarget === "selected" ? "Hydrating..." : finalCode ? `Hydrate ${finalCode}` : "Pick code first"}
            </button>
          </div>
          {hydrationProgress && (
            <HydrationProgressBar progress={hydrationProgress} />
          )}
        </div>
      </div>

      {hydrationError && (
        <div className="mt-4 border-l-4 border-red-500 bg-gray-900 p-3 text-sm">
          <strong>Hydration error:</strong> {hydrationError}
        </div>
      )}

      {candidateHydration && <CandidateHydrationSummary run={candidateHydration} />}
      {selectedHydration && (
        <CommodityHydrationSummary
          title="Selected code hydration"
          hydration={selectedHydration}
        />
      )}
    </details>
  );
}

function HydrationProgressBar({
  progress,
  compact = false,
}: {
  progress: HydrationProgress;
  compact?: boolean;
}) {
  if (!progress) return null;
  const width = `${Math.max(3, Math.min(100, progress.percent))}%`;
  const isFinalPass = progress.percent >= 94 && progress.percent < 100;
  return (
    <div className={`${compact ? "mt-3" : ""} border border-blue-900 bg-blue-950/30 p-3`}>
      <div className="mb-2 flex flex-col gap-1 text-xs text-blue-100 sm:flex-row sm:items-center sm:justify-between">
        <span className="font-semibold">
          {isFinalPass ? "Final hydration pass is still running..." : progress.label}
        </span>
        <span className="font-mono text-blue-200">
          {isFinalPass ? `processed ${progress.done}+/${progress.total} codes` : `${progress.done}/${progress.total} codes`}
        </span>
      </div>
      <div className="h-2 overflow-hidden bg-gray-800">
        <div
          className="h-full bg-gradient-to-r from-blue-500 via-cyan-300 to-blue-500 transition-all duration-300 ease-out"
          style={{ width }}
        />
      </div>
      {progress.percent < 100 && (
        <div className="mt-2 h-1 overflow-hidden bg-gray-900">
          <div className="h-full w-1/3 animate-pulse bg-cyan-300/70" />
        </div>
      )}
    </div>
  );
}

function CandidateHydrationSummary({ run }: { run: CandidateHydrationRun }) {
  const llmEnabled = run.hydrated.filter((item) => item.hydration.summary?.llm?.enabled).length;
  const llmFallback = run.hydrated.filter((item) => item.hydration.summary?.llm && !item.hydration.summary.llm.enabled).length;
  return (
    <div className="mt-5 border-t border-gray-800 pt-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-300">
        <span className="border border-gray-700 px-2 py-1">
          Hydrated {run.hydrated.length}/{run.candidate_count} candidates
        </span>
        <span className="border border-gray-700 px-2 py-1">
          Cache write: {run.cache_write ? "on" : "off"}
        </span>
        {run.summarize && (
          <span className="border border-gray-700 px-2 py-1">
            Model summaries: {llmEnabled} ok{llmFallback ? `, ${llmFallback} fallback` : ""}
          </span>
        )}
      </div>
      <p className="mt-2 text-xs text-gray-400">{run.retrieval_guardrail}</p>
      <CoverageChips counts={run.coverage_totals} />
      <div className="mt-3 grid gap-2">
        {run.hydrated.map((item) => (
          <CommodityHydrationSummary
            key={item.hydration.commodity_code}
            title={`${item.hydration.commodity_code} - ${item.candidate.description}`}
            hydration={item.hydration}
            compact
          />
        ))}
      </div>
    </div>
  );
}

function CommodityHydrationSummary({
  title,
  hydration,
  compact = false,
}: {
  title: string;
  hydration: CommodityHydration;
  compact?: boolean;
}) {
  const llm = hydration.summary?.llm;
  return (
    <div className={`${compact ? "" : "mt-5"} border border-gray-800 bg-gray-900 p-3`}>
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="font-mono text-sm font-bold text-blue-300">{title}</div>
          {!compact && (
            <p className="mt-1 text-xs text-gray-400">{hydration.candidate_guardrail}</p>
          )}
        </div>
        {hydration.model_requested && (
          <span className="w-fit border border-gray-700 px-2 py-1 text-xs text-gray-300">
            {hydration.model_requested}
          </span>
        )}
      </div>
      <CoverageChips counts={hydration.coverage.counts_by_kind} />
      {llm?.enabled && llm.text && (
        <p className="mt-2 whitespace-pre-wrap text-sm text-gray-200">{llm.text}</p>
      )}
      {llm && !llm.enabled && (
        <p className="mt-2 text-sm text-amber-200">
          Model fallback: {llm.reason || "provider summary unavailable"}.
        </p>
      )}
      {!llm && hydration.summary.bullets.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm text-gray-300">
          {hydration.summary.bullets.slice(0, compact ? 3 : 6).map((bullet) => (
            <li key={bullet}>{bullet}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CoverageChips({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts || {}).filter(([, count]) => count > 0);
  if (entries.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {entries.map(([kind, count]) => (
        <span key={kind} className="border border-gray-700 bg-gray-950 px-2 py-0.5 text-[11px] text-gray-300">
          {kind.replace(/_/g, " ")}: {count}
        </span>
      ))}
    </div>
  );
}

function AugmentationBadge({ summary }: { summary: ClassifyTurn["augmentation_summary"] }) {
  const facets = summary.candidates_with_facets ?? 0;
  const total = summary.total_candidates ?? 0;
  const kg = summary.kg_edges_applied ?? 0;
  if (facets === 0 && kg === 0) return null;
  return (
    <div className="text-xs text-gray-400 mb-3 border-l-2 border-emerald-600 pl-2">
      Using{" "}
      {facets > 0 && (
        <strong>
          extra product facts on {facets}/{total} possible codes
        </strong>
      )}
      {facets > 0 && kg > 0 && " + "}
      {kg > 0 && (
        <strong>
          {kg} extra tariff rule{kg === 1 ? "" : "s"}
        </strong>
      )}
      {" "}to improve your results.
    </div>
  );
}

function RetrievalStackBadge({ summary }: { summary: ClassifyTurn["augmentation_summary"] }) {
  const debug = (summary as any)?.debug || {};
  const legs = debug.retrieval_legs || [];
  if (!legs.length && !summary.candidates_with_facets && !summary.kg_edges_applied) return null;
  return (
    <details className="text-xs text-gray-300 mb-3 border border-gray-700 bg-gray-950 p-3">
      <summary className="cursor-pointer select-none font-bold text-gray-100">How we searched (technical)</summary>
      <div className="mt-1">
        Candidate generation used indexed tariff text, labels/search references, and any enabled
        cached facts or KG evidence before the Q&A step. This is separate from selected-code hydration for audit.
      </div>
      {legs.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {legs.map((leg: string) => (
            <span key={leg} className="border border-gray-600 px-2 py-0.5 font-mono text-[11px] text-emerald-300">
              {leg}
            </span>
          ))}
        </div>
      )}
    </details>
  );
}

function ConfidenceBadge({ level }: { level: string }) {
  const styleByLevel: Record<AnswerConfidence, string> = {
    "Best match": "bg-emerald-600 text-white",
    "Also possible": "bg-gray-700 text-gray-100",
    "From retrieval": "bg-gray-800 text-gray-400",
  };
  const style = styleByLevel[level as AnswerConfidence] || "bg-gray-700 text-gray-100";
  return <span className={`text-xs font-bold px-2 py-1 ${style}`}>{level.toUpperCase()}</span>;
}

const HEALTH_BANNERS: Record<Exclude<RetrievalHealth, "live">, { border: string; message: string }> = {
  fixture: {
    border: "border-amber-500",
    message: "Showing bundled example data - the live tariff database is not connected.",
  },
  degraded: {
    border: "border-amber-500",
    message: "AI assistance is limited right now - results come from tariff text search.",
  },
  "infra-error": {
    border: "border-red-500",
    message: "We could not reach the tariff database. This is a system problem, not your query - try again shortly.",
  },
};

function RetrievalHealthBanner({ health }: { health: RetrievalHealth | null | undefined }) {
  if (!health || health === "live") return null;
  const banner = HEALTH_BANNERS[health];
  if (!banner) return null;
  return (
    <div className={`border-l-4 ${banner.border} bg-gray-900 p-3 text-sm text-gray-200`}>
      {banner.message}
    </div>
  );
}
