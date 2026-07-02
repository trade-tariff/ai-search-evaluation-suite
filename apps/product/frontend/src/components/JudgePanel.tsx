import { useEffect, useState } from "react";
import { getConfig, updateConfig } from "../api";
import type { JudgeConfig } from "../types";

const DEFAULT_SYSTEM_PROMPT = `You are an expert evaluator for UK goods classification (HS/commodity code assignment).

You will be given:
1. The original goods description query
2. A BASELINE response (the best available reference from a consensus of strong models)
3. A TARGET response (the model being evaluated)

IMPORTANT: The baseline represents one valid classification path. However, different clarifying questions and answers can lead to different but equally valid HS codes for the same goods description. Score the target on its own merits. Use the baseline as a quality reference, not an answer key.

Score the TARGET response on these criteria (0-10 scale each):

**classification_accuracy** (0-10):
- 9-10: Correct HS code(s) at full 10-digit commodity code level, well-justified
- 7-8: Correct at 6-8 digit level (right chapter and heading), minor subheading differences
- 5-6: Broadly correct chapter/heading but wrong subheading, or missing a better code
- 3-4: Related product area but wrong classification approach
- 1-2: Completely wrong chapter or classification
- 0: No classification provided, gibberish, or codes from wrong tariff

**question_quality** (0-10):
Evaluate whether the model's strategy (direct answer vs. clarifying questions) was appropriate for the query. This is an AI classifier whose purpose is to ask clarifying questions to arrive at the correct HS code. A model that blindly guesses a code without asking questions when the goods description is ambiguous is a failure - even if the guess happens to be correct.

For direct-answer responses (1 round, no questions asked):
- 9-10: ONLY if the query is completely unambiguous with a single obvious HS code
- 5-6: Direct answer but the query had some ambiguity - questions would have helped
- 3-4: Direct answer when the query was clearly ambiguous and needed clarification
- 0-2: Guessed a code when the description could map to multiple HS headings

For direct-answer responses (multi-round - asked questions in prior rounds):
- 9-10: Asked good questions first, then gave a well-narrowed classification
- 7-8: Asked reasonable questions, arrived at a defensible answer
- 5-6: Asked questions but they were not sufficient to resolve key ambiguities
- 3-4: Asked poor questions that did not help, then guessed anyway

For question responses (still asking):
- 9-10: Questions are highly relevant and would efficiently narrow the HS classification
- 7-8: Good questions, mostly relevant to determining the correct code
- 5-6: Questions are somewhat relevant but generic or redundant
- 3-4: Poor questions, mostly irrelevant to classification
- 0-2: Questions are nonsensical or would not help classification

CONTEXT: Each response may be the result of a multi-round Q&A dialogue. The metadata section shows how many rounds the model took. A "round" is one question-answer loop (the model asks clarifying questions, receives answers, then continues). Reaching a correct classification in fewer rounds is more efficient.

**structured_output** (0-10):
- 9-10: Valid JSON, correct schema (answers/questions format), all required fields
- 7-8: Valid JSON with minor schema deviations
- 5-6: Parseable but non-standard structure
- 3-4: Partially valid JSON or mixed format
- 1-2: Mostly freeform text with some structure
- 0: Completely unstructured or unparseable

**overall** (0-10):
Your primary quality judgment. Weight classification accuracy most heavily (~60%), then response strategy appropriateness (~30%), then output structure (~10%).

If the target arrives at a different code than the baseline via a different but valid reasoning path, this is NOT a penalty. A response that classifies correctly should score well overall even if it chose a different path than the baseline.

Respond ONLY with valid JSON in this exact format:
{
  "classification_accuracy": <0-10>,
  "question_quality": <0-10>,
  "structured_output": <0-10>,
  "overall": <0-10>,
  "reasoning": "<1-2 sentence explanation>"
}`;

const JUDGE_MODEL_PRESETS = [
  { id: "gpt-5.2", name: "GPT-5.2 (default)", inputCost: 1.75, outputCost: 14.0 },
  { id: "gpt-5.4", name: "GPT-5.4", inputCost: 2.5, outputCost: 15.0 },
  { id: "gpt-4.1", name: "GPT-4.1", inputCost: 2.0, outputCost: 8.0 },
  { id: "gpt-4.1-mini", name: "GPT-4.1 Mini", inputCost: 0.4, outputCost: 1.6 },
  { id: "gpt-4.1-nano", name: "GPT-4.1 Nano", inputCost: 0.1, outputCost: 0.4 },
];

const REASONING_EFFORT_OPTIONS = ["low", "medium", "high", "xhigh"];

function getPreset(modelId: string) {
  return JUDGE_MODEL_PRESETS.find((p) => p.id === modelId);
}

export default function JudgePanel() {
  const [config, setConfig] = useState<JudgeConfig | null>(null);
  const [draft, setDraft] = useState<JudgeConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [showPrompt, setShowPrompt] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  useEffect(() => {
    getConfig().then((c) => {
      setConfig(c.judge_config);
      setDraft(c.judge_config);
    });
  }, []);

  const hasChanges =
    config && draft && JSON.stringify(config) !== JSON.stringify(draft);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      await updateConfig({ judge_config: draft });
      setConfig(draft);
      setMsg("Saved");
    } catch (e) {
      setMsg(`Error: ${e}`);
    }
    setSaving(false);
    setTimeout(() => setMsg(""), 3000);
  };

  const reset = () => {
    if (config) setDraft({ ...config });
  };

  const resetToDefaults = () => {
    setDraft({
      enabled: true,
      model: "gpt-5.2",
      reasoning_effort: "xhigh",
      max_response_length: 30000,
      input_cost_per_million: 1.75,
      output_cost_per_million: 14.0,
      system_prompt: "",
    });
  };

  const selectPreset = (presetId: string) => {
    if (!draft) return;
    const preset = getPreset(presetId);
    if (preset) {
      setDraft({
        ...draft,
        model: preset.id,
        input_cost_per_million: preset.inputCost,
        output_cost_per_million: preset.outputCost,
      });
    }
  };

  if (!draft)
    return <div className="text-gray-400">Loading judge config...</div>;

  const effectivePrompt = draft.system_prompt.trim() || DEFAULT_SYSTEM_PROMPT;
  const isCustomPrompt = draft.system_prompt.trim().length > 0;
  const preset = getPreset(draft.model);
  const isCustomModel = !preset;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">LLM-as-Judge Configuration</h2>
          <p className="text-sm text-gray-400 mt-1">
            Configure the judge model that evaluates benchmark responses against
            the baseline.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {msg && (
            <span
              className={`text-sm ${msg.startsWith("Error") ? "text-red-400" : "text-green-400"}`}
            >
              {msg}
            </span>
          )}
          {hasChanges && (
            <button
              onClick={reset}
              className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded"
            >
              Discard
            </button>
          )}
          <button
            onClick={save}
            disabled={saving || !hasChanges}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded text-sm font-medium disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </div>

      {/* Enable/Disable */}
      <section className="bg-gray-900 rounded-lg p-5">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="font-medium">Judge Evaluation</h3>
            <p className="text-sm text-gray-400 mt-1">
              When enabled, a strong LLM scores each model response on 4
              dimensions (0-10) after the benchmark completes. This adds cost but
              provides much richer quality metrics.
            </p>
          </div>
          <button
            onClick={() => setDraft({ ...draft, enabled: !draft.enabled })}
            className={`relative w-12 h-6 rounded-full transition-colors ${
              draft.enabled ? "bg-blue-600" : "bg-gray-700"
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
                draft.enabled ? "translate-x-6" : ""
              }`}
            />
          </button>
        </div>
      </section>

      {/* Model Selection */}
      <section
        className={`bg-gray-900 rounded-lg p-5 ${!draft.enabled ? "opacity-50 pointer-events-none" : ""}`}
      >
        <h3 className="font-medium mb-3">Judge Model</h3>

        <div>
          <label className="block text-sm text-gray-400 mb-1">Model</label>
          <select
            value={preset ? draft.model : "__custom"}
            onChange={(e) => {
              if (e.target.value === "__custom") return;
              selectPreset(e.target.value);
            }}
            className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
          >
            {JUDGE_MODEL_PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
            {isCustomModel && (
              <option value="__custom">Custom: {draft.model}</option>
            )}
          </select>
        </div>

        {/* Pricing info - read-only from preset */}
        {preset && (
          <div className="mt-3 flex items-center gap-4 text-sm text-gray-400">
            <span>
              Input: <span className="text-gray-200">${preset.inputCost}/M tokens</span>
            </span>
            <span>
              Output: <span className="text-gray-200">${preset.outputCost}/M tokens</span>
            </span>
          </div>
        )}

        {/* Reasoning Effort */}
        <div className="mt-4">
          <label className="block text-sm text-gray-400 mb-2">Reasoning Effort</label>
          <div className="flex items-center gap-2">
            {REASONING_EFFORT_OPTIONS.map((level) => (
              <button
                key={level}
                onClick={() => setDraft({ ...draft, reasoning_effort: level })}
                className={`px-3 py-1.5 text-sm rounded-full border transition-colors ${
                  draft.reasoning_effort === level
                    ? level === "low" ? "bg-green-700 border-green-600 text-white"
                    : level === "medium" ? "bg-blue-600 border-blue-500 text-white"
                    : level === "high" ? "bg-amber-700 border-amber-600 text-white"
                    : "bg-red-700 border-red-600 text-white"
                    : "bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-500"
                }`}
              >
                {level === "xhigh" ? "XHigh" : level.charAt(0).toUpperCase() + level.slice(1)}
              </button>
            ))}
          </div>
          <p className="text-xs text-gray-500 mt-1">
            Higher effort = deeper reasoning but slower and more expensive. XHigh recommended for judge quality.
          </p>
        </div>
      </section>

      {/* System Prompt */}
      <section
        className={`bg-gray-900 rounded-lg p-5 ${!draft.enabled ? "opacity-50 pointer-events-none" : ""}`}
      >
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="font-medium">Evaluation Prompt</h3>
            <p className="text-sm text-gray-400 mt-1">
              {isCustomPrompt
                ? "Using custom prompt"
                : "Using built-in default prompt"}{" "}
              - defines the scoring criteria sent to the judge model.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {isCustomPrompt && (
              <button
                onClick={() => setDraft({ ...draft, system_prompt: "" })}
                className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded"
              >
                Reset to Default
              </button>
            )}
            <button
              onClick={() => setShowPrompt(!showPrompt)}
              className="px-3 py-1.5 text-sm bg-gray-800 hover:bg-gray-700 rounded"
            >
              {showPrompt ? "Hide" : "Show"} Prompt
            </button>
          </div>
        </div>

        {showPrompt && (
          <div className="mt-3">
            <textarea
              value={
                isCustomPrompt ? draft.system_prompt : DEFAULT_SYSTEM_PROMPT
              }
              onChange={(e) =>
                setDraft({ ...draft, system_prompt: e.target.value })
              }
              rows={20}
              className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm font-mono focus:outline-none focus:border-blue-500 resize-y"
              placeholder="Enter custom system prompt..."
            />
            <div className="flex items-center justify-between mt-2 text-xs text-gray-500">
              <span>{effectivePrompt.length} characters</span>
              <span>
                The judge model receives this as the system message, followed by
                the original query, baseline response, and target response.
              </span>
            </div>
          </div>
        )}
      </section>

      {/* Scoring Info - the judge now scores TWO dimensions; everything else
          has a deterministic equivalent computed directly from run data. */}
      <section
        className={`bg-gray-900 rounded-lg p-5 ${!draft.enabled ? "opacity-50 pointer-events-none" : ""}`}
      >
        <h3 className="font-medium mb-1">Scoring Dimensions</h3>
        <p className="text-xs text-gray-500 mb-4">
          The judge scores only the two dimensions below. Accuracy, schema
          validity, rounds/question efficiency, speed, and cost are computed
          deterministically from run data (see Scoring Weights on Legacy &gt;
          Benchmark Results). This keeps the judge cheap and unbiased.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[
            {
              name: "Fact consistency",
              color: "text-emerald-400",
              desc: "Does the final commodity code respect every committed fact in the per-prompt fact store? This is the one thing the judge catches that deterministic code-match can't - semantic agreement between code and facts. (0-10)",
            },
            {
              name: "Question quality",
              color: "text-indigo-400",
              desc: "Phrasing clarity, option coverage, and discriminativeness of the clarifying questions the model asked. Deterministic question_efficiency complements this - one measures redundancy, this measures nuance. (0-10)",
            },
          ].map((d) => (
            <div key={d.name} className="flex gap-3">
              <div className={`text-lg font-bold ${d.color} w-8`}>10</div>
              <div>
                <div className={`text-sm font-medium ${d.color}`}>{d.name}</div>
                <div className="text-xs text-gray-400">{d.desc}</div>
              </div>
            </div>
          ))}
        </div>

        <div className="mt-4 p-3 bg-gray-800 rounded text-xs text-gray-400">
          <div className="mb-1 font-medium text-gray-300">Deterministic dimensions (not judged)</div>
          Top-1 match, top-3 hit, MRR, heading/chapter match, top-5 overlap,
          schema valid, rounds efficiency, question efficiency, speed factor,
          cost per classification. All weights are editable in the Scoring
          Weights panel on Legacy &gt; Benchmark Results; the composite verdict
          is a weighted sum across all 13 dimensions.
        </div>
      </section>

      {/* Advanced */}
      <section
        className={`bg-gray-900 rounded-lg ${!draft.enabled ? "opacity-50 pointer-events-none" : ""}`}
      >
        <button
          onClick={() => setShowAdvanced(!showAdvanced)}
          className="w-full flex items-center justify-between p-5 text-left"
        >
          <h3 className="font-medium text-gray-400">Advanced Settings</h3>
          <span className="text-gray-500 text-sm">
            {showAdvanced ? "-" : "+"}
          </span>
        </button>

        {showAdvanced && (
          <div className="px-5 pb-5 space-y-4">
            <div className="max-w-md">
              <label className="block text-sm text-gray-400 mb-1">
                Max Response Length (chars)
              </label>
              <input
                type="number"
                value={draft.max_response_length}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    max_response_length: parseInt(e.target.value) || 1000,
                  })
                }
                min={500}
                max={200000}
                step={1000}
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
              />
              <p className="text-xs text-gray-500 mt-1">
                Reference and target response text are truncated to this length
                before being sent to the judge. Default: 30000.
              </p>
            </div>

            {/* Custom model pricing override */}
            {isCustomModel && (
              <div className="max-w-md">
                <label className="block text-sm text-gray-400 mb-1">
                  Custom Model Pricing (per M tokens)
                </label>
                <div className="flex gap-3">
                  <div className="flex-1">
                    <label className="block text-xs text-gray-500 mb-0.5">
                      Input
                    </label>
                    <input
                      type="number"
                      value={draft.input_cost_per_million}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          input_cost_per_million:
                            parseFloat(e.target.value) || 0,
                        })
                      }
                      min={0}
                      step={0.1}
                      className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="block text-xs text-gray-500 mb-0.5">
                      Output
                    </label>
                    <input
                      type="number"
                      value={draft.output_cost_per_million}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          output_cost_per_million:
                            parseFloat(e.target.value) || 0,
                        })
                      }
                      min={0}
                      step={0.1}
                      className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
                    />
                  </div>
                </div>
                <p className="text-xs text-gray-500 mt-1">
                  Set pricing for your custom model ID so judge costs are tracked
                  correctly.
                </p>
              </div>
            )}

            {/* Custom model ID override */}
            <div className="max-w-md">
              <label className="block text-sm text-gray-400 mb-1">
                Custom Model ID
              </label>
              <input
                type="text"
                value={draft.model}
                onChange={(e) =>
                  setDraft({ ...draft, model: e.target.value })
                }
                placeholder="e.g. gpt-5.2"
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-blue-500"
              />
              <p className="text-xs text-gray-500 mt-1">
                Override the model ID directly. Use this for models not in the
                preset list. Must be available via your OpenAI API key.
              </p>
            </div>
          </div>
        )}
      </section>

      {/* Reset */}
      <div className="flex justify-end">
        <button
          onClick={resetToDefaults}
          className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200 bg-gray-800 hover:bg-gray-700 rounded"
        >
          Reset All to Defaults
        </button>
      </div>
    </div>
  );
}
