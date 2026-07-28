import { useState, useRef } from "react";
import { startBenchmarkSSE, cancelBenchmark } from "../api";

interface Props {
  promptIndices: number[];
  modelIds: string[];
  opensearchLimit: number;
}

interface LogEntry {
  time: string;
  event: string;
  data: Record<string, unknown>;
}

interface LiveCommit {
  prompt_index: number;
  slot: string;
  answer: string;
  source_question: string;
  source_model: string;
  source_round: number;
  time: string;
}

interface ReferenceRow {
  prompt_index: number;
  model_id: string;
  response_type: string;
  total_rounds: number;
  total_latency_ms: number;
  total_cost: number;
  top_code: string | null;
  error: string | null;
  // Arrival-order ordinal per (prompt, model). pass_ordinal=1 unless the same
  // model runs multi-pass against the same prompt.
  pass_ordinal: number;
}

interface CandidateCompletion {
  prompt_index: number;
  model_id: string;
  response_type: string;
  total_rounds: number;
  top_code: string | null;
  error: string | null;
}

function SlotTag({ slot }: { slot: string }) {
  // Same stable hashed colours as AnalysisPanel so a slot looks the same
  // everywhere it shows up.
  const palette = [
    "bg-purple-900/60 text-purple-200 border-purple-700",
    "bg-sky-900/60 text-sky-200 border-sky-700",
    "bg-emerald-900/60 text-emerald-200 border-emerald-700",
    "bg-pink-900/60 text-pink-200 border-pink-700",
    "bg-indigo-900/60 text-indigo-200 border-indigo-700",
    "bg-lime-900/60 text-lime-200 border-lime-700",
    "bg-teal-900/60 text-teal-200 border-teal-700",
    "bg-fuchsia-900/60 text-fuchsia-200 border-fuchsia-700",
    "bg-cyan-900/60 text-cyan-200 border-cyan-700",
    "bg-rose-900/60 text-rose-200 border-rose-700",
  ];
  let h = 0;
  for (let i = 0; i < slot.length; i++) h = (h * 31 + slot.charCodeAt(i)) | 0;
  const cls = palette[Math.abs(h) % palette.length];
  return (
    <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${cls}`}>
      {slot}
    </span>
  );
}

export default function BenchmarkPanel({ promptIndices, modelIds, opensearchLimit }: Props) {
  const [running, setRunning] = useState(false);
  // Count of completed tasks (reference + candidate); progress is derived
  // from this so the denominator can settle as start events arrive.
  const [tasksDone, setTasksDone] = useState(0);
  const [goldMode, setGoldMode] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [done, setDone] = useState(false);
  const [fanoutTotal, setFanoutTotal] = useState(0);
  const [fanoutDone, setFanoutDone] = useState(0);
  const [commits, setCommits] = useState<LiveCommit[]>([]);
  const [referenceRows, setReferenceRows] = useState<ReferenceRow[]>([]);
  const [referenceTotalTasks, setReferenceTotalTasks] = useState<number>(0);
  const [candidateCompletions, setCandidateCompletions] = useState<CandidateCompletion[]>([]);
  const [factStoreView, setFactStoreView] = useState<"timeline" | "matrix">("matrix");
  // Live per-prompt reference top code for on-the-fly accuracy calc
  const [referenceCodeByPrompt, setReferenceCodeByPrompt] = useState<Record<number, string>>({});
  // Gold code per prompt (from benchmark:start). When a prompt has one it is
  // the truth to mark against; the reference is only a fallback.
  const [goldCodeByPrompt, setGoldCodeByPrompt] = useState<Record<number, string>>({});
  // Models dropped from the candidate set because they are the reference.
  const [dedupedNote, setDedupedNote] = useState<string | null>(null);
  // Prompts that will run but cannot be scored under the chosen mode.
  const [unscoredNote, setUnscoredNote] = useState<string | null>(null);
  // Scoring mode. "gold" is the default and is pinned, not auto-detected:
  // dropping one non-gold prompt into the selection used to flip the whole
  // run back to reference scoring silently, which inverted the numbers.
  const [scoring, setScoring] = useState<"gold" | "reference">("gold");
  const ctrlRef = useRef<AbortController | null>(null);

  const addLog = (event: string, data: Record<string, unknown>) => {
    const time = new Date().toLocaleTimeString();
    setLogs((prev) => [...prev, { time, event, data }]);
  };

  const start = () => {
    if (promptIndices.length === 0 || modelIds.length === 0) return;
    if (!confirm(
      `Run the benchmark on ${promptIndices.length} prompt${promptIndices.length === 1 ? "" : "s"} x ` +
      `${modelIds.length} model${modelIds.length === 1 ? "" : "s"}? This spends real money on AI calls.`
    )) return;

    setRunning(true);
    setDone(false);
    setLogs([]);
    setTasksDone(0);
    setGoldMode(false);
    setFanoutTotal(0);
    setFanoutDone(0);
    setCommits([]);
    setReferenceRows([]);
    setReferenceTotalTasks(0);
    setCandidateCompletions([]);
    setReferenceCodeByPrompt({});
    setGoldCodeByPrompt({});
    setDedupedNote(null);
    setUnscoredNote(null);

    ctrlRef.current = startBenchmarkSSE(
      promptIndices,
      modelIds,
      (event, data) => {

        addLog(event, data);

        if (event === "benchmark:start") {
          // Gold codes arrive up-front so live marking never has to wait on a
          // reference phase that may not run at all.
          const golds = (data.gold_codes ?? {}) as Record<string, string>;
          const parsed: Record<number, string> = {};
          for (const [pi, code] of Object.entries(golds)) parsed[Number(pi)] = code;
          setGoldCodeByPrompt(parsed);
        }
        if (event === "benchmark:gold_mode") {
          // Gold mode: the reference/consensus/judge phases are skipped, so
          // no panel:* events will arrive for this run.
          setGoldMode(Boolean(data.enabled));
        }
        if (event === "benchmark:model_deduped") {
          setDedupedNote(String(data.message || ""));
        }
        if (event === "benchmark:unscored_prompts") {
          setUnscoredNote(String(data.message || ""));
        }
        if (event === "fanout:start") {
          setFanoutTotal(Number(data.total_tasks) || 0);
          setFanoutDone(0);
        }
        if (event === "panel:start") {
          // Backend emits total_tasks = prompts * reference_panel_size, which
          // accounts for all three reference modes (single/multi-pass/panel).
          setReferenceTotalTasks(Number(data.total_tasks) || 0);
        }
        if (event === "model:complete") {
          setFanoutDone((prev) => prev + 1);
          // Track candidate per-prompt completion for the matrix view.
          setCandidateCompletions((prev) => [
            ...prev,
            {
              prompt_index: Number(data.prompt_index),
              model_id: String(data.model_id),
              response_type: String(data.response_type || "unknown"),
              total_rounds: Number(data.total_rounds || 0),
              top_code: (data.top_code as string | null) ?? null,
              error: (data.error as string | null) ?? null,
            },
          ]);
        }
        if (event === "panel:complete" || event === "model:complete") {
          // Both reference (panel) and candidate (model) completions count
          // toward overall progress; the denominator is derived at render
          // time from fanout:start + panel:start totals.
          setTasksDone((prev) => prev + 1);
        }
        if (event === "panel:complete") {
          // Track reference top-code per prompt for live accuracy calc. Use
          // first panel call's top_code as the running reference; consensus
          // recomputes it at end-of-phase but the live display is
          // approximate by nature.
          const pi = Number(data.prompt_index);
          const topCode = (data.top_code as string | null) ?? null;
          if (topCode) {
            setReferenceCodeByPrompt((prev) =>
              prev[pi] ? prev : { ...prev, [pi]: topCode },
            );
          }
          // Keep EVERY reference call as its own row so multi-pass shows each
          // pass with its own code (important - panel_agreement signal comes
          // from inter-pass divergence). Panel mode = multiple model_ids.
          // Single mode = 1 row per prompt.
          setReferenceRows((prev) => {
            const pi = Number(data.prompt_index);
            const mid = String(data.model_id);
            // Count prior rows for this (prompt, model) to assign ordinal.
            const prior = prev.filter(
              (r) => r.prompt_index === pi && r.model_id === mid,
            ).length;
            const row: ReferenceRow = {
              prompt_index: pi,
              model_id: mid,
              response_type: String(data.response_type || "unknown"),
              total_rounds: Number(data.total_rounds || 0),
              total_latency_ms: Number(data.total_latency_ms || 0),
              total_cost: Number(data.total_cost || 0),
              top_code: (data.top_code as string | null) ?? null,
              error: (data.error as string | null) ?? null,
              pass_ordinal: prior + 1,
            };
            return [...prev, row];
          });
        }
        if (event === "simulator:commit") {
          const now = new Date().toLocaleTimeString();
          setCommits((prev) => [
            ...prev,
            {
              prompt_index: Number(data.prompt_index),
              slot: String(data.slot),
              answer: String(data.answer),
              source_question: String(data.source_question),
              source_model: String(data.source_model),
              source_round: Number(data.source_round),
              time: now,
            },
          ]);
        }
      },
      () => {
        setRunning(false);
        setDone(true);
      },
      (err) => {
        addLog("error", { message: err });
        setRunning(false);
      },
      opensearchLimit,
      scoring === "gold",
    );
  };

  const stop = async () => {
    // Tell the backend to actually stop (cancels in-flight tasks, no more
    // provider/judge calls). Then abort the SSE stream on the client.
    try {
      await cancelBenchmark();
    } catch {
      /* best-effort; still abort the stream */
    }
    ctrlRef.current?.abort();
    setRunning(false);
  };

  const formatLog = (entry: LogEntry) => {
    const d = entry.data;
    switch (entry.event) {
      case "benchmark:start": {
        const panel = d.panel_models as string[] | undefined;
        const panelInfo = panel && panel.length > 1 ? ` (panel: ${panel.join(", ")})` : "";
        return `Starting benchmark: ${d.total_prompts} prompts x ${d.total_models} models${panelInfo} (max ${d.max_rounds} rounds/loop)`;
      }
      case "benchmark:gold_mode":
        return `Gold mode: skipping reference, consensus and judge phases${d.reason ? ` (${d.reason})` : ""}`;
      case "panel:start":
        return `Consensus panel: running ${(d.panel_models as string[])?.join(", ") ?? "?"} on all prompts (${d.total_tasks} tasks)...`;
      case "panel:complete":
        return `Panel ${d.model_id}: prompt #${d.prompt_index} -> ${d.response_type} (${d.total_rounds} rounds, ${Number(d.total_latency_ms).toFixed(0)}ms, $${Number(d.total_cost).toFixed(4)})${d.error ? ` ERROR: ${d.error}` : ""}`;
      case "consensus:complete":
        return `Consensus computed for ${d.prompt_count} prompts (avg panel agreement: ${Number(d.avg_panel_agreement).toFixed(2)})`;
      case "baseline:start":
        return `Reference: starting prompt #${d.prompt_index}...`;
      case "baseline:complete":
        return `Reference: prompt #${d.prompt_index} -> ${d.response_type} (${d.total_rounds} rounds, ${Number(d.total_latency_ms).toFixed(0)}ms, $${Number(d.total_cost).toFixed(4)})${d.error ? ` ERROR: ${d.error}` : ""}`;
      case "fanout:start":
        return `Fan-out: sending ${d.total_tasks} tasks to candidate models...`;
      case "model:complete":
        return `${d.model_id}: prompt #${d.prompt_index} -> ${d.response_type} (${d.total_rounds} rounds, ${Number(d.total_latency_ms).toFixed(0)}ms${d.error ? `, ERROR: ${d.error}` : ""})`;
      case "evaluation:start":
        return "Evaluating candidates against consensus...";
      case "judge:start":
        return `LLM Judge (${d.model || "judge model"}): scoring ${d.total} candidate responses...`;
      case "judge:complete":
        return `Judge: ${d.done}/${d.total} scored${d.model_id ? ` (${d.model_id})` : ""}`;
      case "benchmark:complete":
        return `Benchmark complete! ${d.summary_count} model summaries generated.`;
      case "simulator:commit":
        return `   +slot  prompt #${d.prompt_index}  ${d.slot} = "${d.answer}"  (by ${d.source_model} r${d.source_round})`;
      case "model:question": {
        const qs = (d.questions as Array<{ question: string }> | undefined) ?? [];
        const refTag = d.is_panel ? "[REF] " : "";
        const summary = qs.map((q) => q.question).join(" | ");
        return `   ?  ${refTag}${d.model_id} prompt #${d.prompt_index} r${d.round_number}: ${qs.length} question${qs.length === 1 ? "" : "s"}: ${summary}`;
      }
      case "model:round": {
        const refTag = d.is_panel ? "[REF] " : "";
        const errMark = d.response_type === "error" ? " ERROR" : "";
        const codePart = d.top_code ? `, code=${d.top_code}` : "";
        return `   .  ${refTag}${d.model_id} prompt #${d.prompt_index} r${d.round_number} -> ${d.response_type}${errMark} (${Number(d.latency_ms).toFixed(0)}ms, $${Number(d.cost).toFixed(4)}${codePart})`;
      }
      case "error":
        return `ERROR: ${d.message}`;
      default:
        return `${entry.event}: ${JSON.stringify(d)}`;
    }
  };

  const canStart = promptIndices.length > 0 && modelIds.length > 0;

  // Overall progress denominator: candidate tasks plus (unless gold mode
  // skipped them) reference/panel tasks. fanout:start carries the real
  // candidate total; fall back to the selection size so the bar cannot
  // overshoot during the panel phase before fanout:start arrives.
  const candidateTaskTotal = fanoutTotal || promptIndices.length * modelIds.length;
  const totalTaskCount = candidateTaskTotal + (goldMode ? 0 : referenceTotalTasks);
  const progress = done
    ? 1
    : totalTaskCount > 0
    ? Math.min(tasksDone / totalTaskCount, 1)
    : 0;

  return (
    <div className="space-y-6">
      {/* What this tab is for, and when to use the gold-eval jobs instead */}
      <div className="border border-gray-800 bg-gray-900/60 text-gray-300 text-xs rounded p-2 space-y-1">
        <div>
          Runs several models head-to-head over the same prompts. Every model
          shares one simulated trader: the first model to ask about a concept
          sets the answer, and every later model asking the same concept gets
          that answer back - so the comparison measures the models, not
          variation in what the trader happened to say.
        </div>
        <div className="text-gray-500">
          Scoring is set below and stays where you put it - it is not inferred
          from which prompts you picked. To compare <em>pipeline</em>
          configuration - retrieval base, persona, question mode - at a fixed
          model, use Gold Eval - E2E instead.
        </div>
      </div>
      {/* Controls */}
      <div className="flex items-center gap-4">
        {!running ? (
          <button
            onClick={start}
            disabled={!canStart}
            className="px-6 py-2.5 bg-green-600 hover:bg-green-700 rounded font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Run Benchmark
          </button>
        ) : (
          <button
            onClick={stop}
            className="px-6 py-2.5 bg-red-600 hover:bg-red-700 rounded font-medium"
          >
            Stop
          </button>
        )}
        <div className="text-sm text-gray-400">
          {promptIndices.length} prompts x {modelIds.length} models
          {!canStart && (
            <span className="text-amber-400 ml-2">
              (Select prompts and models first)
            </span>
          )}
        </div>
        {done && (
          <span className="text-green-400 text-sm font-medium">
            Complete - see Legacy &gt; Benchmark Results
          </span>
        )}
      </div>

      {/* Scoring mode. Pinned, not inferred from the selection. */}
      <div className="flex items-start gap-3 text-xs">
        <span className="text-gray-500 pt-1 shrink-0">Score against</span>
        <div className="flex rounded overflow-hidden border border-gray-700 shrink-0">
          <button
            onClick={() => setScoring("gold")}
            disabled={running}
            className={`px-3 py-1 ${scoring === "gold" ? "bg-yellow-900/60 text-yellow-200" : "bg-gray-900 text-gray-400 hover:text-gray-200"} disabled:opacity-50`}
          >
            Gold answers
          </button>
          <button
            onClick={() => setScoring("reference")}
            disabled={running}
            className={`px-3 py-1 border-l border-gray-700 ${scoring === "reference" ? "bg-amber-900/60 text-amber-200" : "bg-gray-900 text-gray-400 hover:text-gray-200"} disabled:opacity-50`}
          >
            Reference model (legacy)
          </button>
        </div>
        <span className="text-gray-500 pt-1">
          {scoring === "gold"
            ? "Answers are marked against the ATaR gold code. No reference, consensus or judge phases. Prompts without a gold code still run but are not scored."
            : "Answers are marked against the configured reference model, with consensus and the LLM judge. Use for prompts that have no gold answer."}
        </span>
      </div>

      {/* A selected model that is also the reference is not scored as a
          candidate. Say so - it used to disappear from the results with no
          explanation. */}
      {dedupedNote && (
        <div className="rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          {dedupedNote}
        </div>
      )}

      {/* Selected prompts with no gold code under gold scoring. */}
      {unscoredNote && (
        <div className="rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          {unscoredNote}
        </div>
      )}

      {/* Progress bar */}
      {(running || done) && (
        <div className="bg-gray-800 rounded-full h-3 overflow-hidden">
          <div
            className="bg-blue-500 h-full transition-all duration-300"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      )}

      {/* Fan-out progress */}
      {running && fanoutTotal > 0 && fanoutDone < fanoutTotal && (
        <div className="flex items-center gap-3 text-sm">
          <span className="text-gray-400">Fan-out:</span>
          <div className="flex-1 bg-gray-800 rounded-full h-2 overflow-hidden">
            <div
              className="bg-purple-500 h-full transition-all duration-300"
              style={{ width: `${(fanoutDone / fanoutTotal) * 100}%` }}
            />
          </div>
          <span className="text-purple-400 font-mono">{fanoutDone}/{fanoutTotal}</span>
        </div>
      )}

      {/* Live Ranking - per-candidate partial stats as events arrive */}
      {(running || candidateCompletions.length > 0) && candidateCompletions.length > 0 && (
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="flex items-end justify-between mb-3">
            <div>
              <h3 className="text-sm font-medium">Live Ranking</h3>
              <p className="text-xs text-gray-500 mt-0.5">
                Running top-1 accuracy per candidate, marked against the gold code where the prompt has one and against the reference otherwise. Updated as each candidate-prompt completes. Approximate mid-run; final composite is on the Benchmark Results tab.
              </p>
            </div>
          </div>
          {(() => {
            // Aggregate candidate completions by model_id into running stats
            const byModel = new Map<string, {
              completed: number;
              correct: number;
              scored: number;
              totalLatency: number;
              totalCost: number;
              totalRounds: number;
              errored: number;
            }>();
            // Mark against gold where the prompt has one; fall back to the
            // reference only for prompts with no gold code. `scored` counts the
            // completions we could actually mark, so the percentage is over
            // markable prompts rather than being diluted by unmarkable ones.
            for (const c of candidateCompletions) {
              if (!byModel.has(c.model_id)) {
                byModel.set(c.model_id, {
                  completed: 0, correct: 0, scored: 0, totalLatency: 0,
                  totalCost: 0, totalRounds: 0, errored: 0,
                });
              }
              const s = byModel.get(c.model_id)!;
              s.completed += 1;
              if (c.error) s.errored += 1;
              const truth = goldCodeByPrompt[c.prompt_index] ?? referenceCodeByPrompt[c.prompt_index];
              if (c.top_code && truth) {
                s.scored += 1;
                if (c.top_code === truth) s.correct += 1;
              }
              // We don't have latency/cost in the SSE payload for this code path,
              // but we can approximate from what's in candidateCompletions
            }
            const rows = Array.from(byModel.entries())
              .map(([model_id, s]) => ({
                model_id,
                completed: s.completed,
                scored: s.scored,
                top1: s.scored > 0 ? s.correct / s.scored : null,
                errored: s.errored,
              }))
              .sort((a, b) => (b.top1 ?? -1) - (a.top1 ?? -1));
            const referenceCodesKnown = Object.keys(referenceCodeByPrompt).length;
            const goldCodesKnown = Object.keys(goldCodeByPrompt).length;
            return (
              <div>
                <div className="text-[10px] text-gray-500 mb-2">
                  {goldCodesKnown > 0
                    ? `Marked against gold for ${goldCodesKnown} / ${promptIndices.length || "?"} prompts` +
                      (goldMode ? "" : `; reference top-codes known for ${referenceCodesKnown}`)
                    : `Reference top-codes known for ${referenceCodesKnown} / ${promptIndices.length || "?"} prompts`}
                </div>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-gray-500 border-b border-gray-800">
                      <th className="py-1 pr-3 w-6">#</th>
                      <th className="py-1 pr-3">Model</th>
                      <th className="py-1 pr-3 text-right">Done</th>
                      <th className="py-1 pr-3 text-right">Top-1 so far</th>
                      <th className="py-1 pr-3 text-right">Errors</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr key={r.model_id} className="border-b border-gray-900/70">
                        <td className="py-1 pr-3 font-mono text-gray-600">{i + 1}</td>
                        <td className="py-1 pr-3 text-gray-200">{r.model_id}</td>
                        <td className="py-1 pr-3 text-right font-mono text-gray-400">
                          {r.completed} / {promptIndices.length || "?"}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono">
                          {r.top1 == null ? (
                            <span className="text-gray-600" title="Nothing markable yet - no gold code and no reference top-code for these prompts">
                              n/a
                            </span>
                          ) : (
                            <span className={
                              r.top1 >= 0.8 ? "text-emerald-300"
                              : r.top1 >= 0.5 ? "text-amber-300"
                              : "text-red-300"
                            }>
                              {(r.top1 * 100).toFixed(0)}%
                            </span>
                          )}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono text-gray-500">
                          {r.errored > 0 ? <span className="text-red-400">{r.errored}</span> : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })()}
        </div>
      )}

      {/* Reference Build - per-prompt status for each reference call. In
          gold mode the reference/consensus phases are skipped, so show a
          one-line note instead of waiting on panel events that never come. */}
      {goldMode && (running || done) && (
        <div className="bg-gray-900 rounded-lg p-4 text-xs text-gray-500 italic">
          Reference Build skipped - gold mode (prompts are anchored to gold
          answers; no reference, consensus or judge phases).
        </div>
      )}
      {!goldMode && (running || referenceRows.length > 0) && (
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="flex items-end justify-between mb-3">
            <div>
              <h3 className="text-sm font-medium">Reference Build</h3>
              <p className="text-xs text-gray-500 mt-0.5">
                Pinned reference model(s) running per prompt. Candidates are judged against these.
              </p>
            </div>
            <div className="text-xs text-gray-400">
              {referenceRows.length} / {referenceTotalTasks || promptIndices.length || "?"} done
            </div>
          </div>
          {referenceRows.length === 0 ? (
            <div className="text-xs text-gray-500 italic">
              Waiting for reference calls to land...
            </div>
          ) : (
            <div className="space-y-3">
              {/* Group by prompt so multi-pass / panel modes are visually obvious */}
              {(() => {
                const groups = new Map<number, ReferenceRow[]>();
                for (const r of referenceRows) {
                  if (!groups.has(r.prompt_index)) groups.set(r.prompt_index, []);
                  groups.get(r.prompt_index)!.push(r);
                }
                const ordered = Array.from(groups.entries()).sort(
                  (a, b) => a[0] - b[0],
                );
                return ordered.map(([pi, rows]) => {
                  const codes = rows.map((r) => r.top_code).filter(Boolean);
                  const uniqueCodes = new Set(codes);
                  const allAgreed = uniqueCodes.size === 1 && rows.every((r) => r.top_code);
                  const anyFailed = rows.some(
                    (r) => r.error || r.response_type === "unknown" || r.response_type === "error",
                  );
                  return (
                    <div
                      key={pi}
                      className={`rounded border ${
                        anyFailed
                          ? "border-red-900/60 bg-red-950/10"
                          : allAgreed && rows.length > 1
                          ? "border-emerald-900/60 bg-emerald-950/10"
                          : "border-gray-800 bg-gray-950/40"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-gray-800">
                        <div className="flex items-center gap-2 text-xs">
                          <span className="font-mono text-gray-500">#{pi}</span>
                          <span className="text-gray-400">
                            {rows.length} reference call{rows.length === 1 ? "" : "s"}
                          </span>
                        </div>
                        <div className="text-[10px] font-mono">
                          {rows.length > 1 && (
                            uniqueCodes.size === 1 ? (
                              <span className="px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-300">
                                converged · agreement 1.00
                              </span>
                            ) : uniqueCodes.size > 1 ? (
                              <span className="px-1.5 py-0.5 rounded bg-amber-900/50 text-amber-300">
                                diverged · {uniqueCodes.size} distinct codes
                              </span>
                            ) : (
                              <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">
                                no codes yet
                              </span>
                            )
                          )}
                        </div>
                      </div>
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-left text-gray-600">
                            <th className="px-3 py-1 font-normal">Model</th>
                            <th className="px-3 py-1 font-normal">Result</th>
                            <th className="px-3 py-1 font-normal">Top code</th>
                            <th className="px-3 py-1 font-normal text-right">Rounds</th>
                            <th className="px-3 py-1 font-normal text-right">Latency</th>
                            <th className="px-3 py-1 font-normal text-right">Cost</th>
                          </tr>
                        </thead>
                        <tbody>
                          {rows.map((r) => {
                            const ok = r.response_type === "answers" && !r.error;
                            const failed =
                              r.error || r.response_type === "unknown" || r.response_type === "error";
                            return (
                              <tr
                                key={`${r.model_id}-${r.pass_ordinal}`}
                                className="border-t border-gray-900/50"
                              >
                                <td className="px-3 py-1.5 text-gray-200">
                                  {r.model_id}
                                  {rows.filter((x) => x.model_id === r.model_id).length > 1 && (
                                    <span className="ml-1 text-[10px] px-1 rounded bg-sky-900/60 text-sky-200 font-mono">
                                      pass {r.pass_ordinal}
                                    </span>
                                  )}
                                </td>
                                <td className="px-3 py-1.5">
                                  {failed ? (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/60 text-red-200 border border-red-700">
                                      {r.error ? "error" : r.response_type}
                                    </span>
                                  ) : ok ? (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/60 text-emerald-200 border border-emerald-700">
                                      answered
                                    </span>
                                  ) : (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-200 border border-amber-700">
                                      {r.response_type}
                                    </span>
                                  )}
                                </td>
                                <td className="px-3 py-1.5 font-mono text-emerald-300">
                                  {r.top_code ?? <span className="text-gray-600">—</span>}
                                </td>
                                <td className="px-3 py-1.5 text-right font-mono text-gray-400">
                                  {r.total_rounds}
                                </td>
                                <td className="px-3 py-1.5 text-right font-mono text-gray-400">
                                  {r.total_latency_ms.toFixed(0)}ms
                                </td>
                                <td className="px-3 py-1.5 text-right font-mono text-gray-400">
                                  ${r.total_cost.toFixed(4)}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  );
                });
              })()}
            </div>
          )}
        </div>
      )}

      {/* Live Fact Store - visible while the simulator commits slots */}
      {(running || commits.length > 0) && (
        <div className="bg-gray-900 rounded-lg p-4">
          <div className="flex items-end justify-between mb-3 gap-3 flex-wrap">
            <div>
              <h3 className="text-sm font-medium">Live Fact Store</h3>
              <p className="text-xs text-gray-500 mt-0.5">
                The simulator records one answer per semantic slot per prompt. First model to ask about a concept sets the answer; every later model asking the same concept gets the same answer back. That's what makes candidates apples-to-apples comparable.
              </p>
            </div>
            <div className="flex items-center gap-3">
              <div className="text-xs text-gray-400">
                {commits.length} slot{commits.length === 1 ? "" : "s"} set
              </div>
              {/* View mode toggle */}
              <div className="flex text-xs rounded border border-gray-700 overflow-hidden">
                <button
                  onClick={() => setFactStoreView("matrix")}
                  className={`px-2.5 py-1 ${
                    factStoreView === "matrix"
                      ? "bg-blue-700 text-white"
                      : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                  }`}
                >
                  Matrix
                </button>
                <button
                  onClick={() => setFactStoreView("timeline")}
                  className={`px-2.5 py-1 ${
                    factStoreView === "timeline"
                      ? "bg-blue-700 text-white"
                      : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                  }`}
                >
                  Timeline
                </button>
              </div>
            </div>
          </div>

          {commits.length === 0 ? (
            <div className="text-xs text-gray-500 italic">
              Waiting for the simulator to set its first slot...
            </div>
          ) : factStoreView === "matrix" ? (
            <div className="space-y-4">
              {(() => {
                // Per-prompt matrix: rows = rounds, cols = models, cells = slots set.
                // Build structures from commits + referenceRows + candidateCompletions.
                const byPrompt = new Map<number, LiveCommit[]>();
                for (const c of commits) {
                  if (!byPrompt.has(c.prompt_index)) byPrompt.set(c.prompt_index, []);
                  byPrompt.get(c.prompt_index)!.push(c);
                }
                // Union of prompt indices across all data sources
                const promptSet = new Set<number>();
                for (const pi of byPrompt.keys()) promptSet.add(pi);
                for (const r of referenceRows) promptSet.add(r.prompt_index);
                for (const c of candidateCompletions) promptSet.add(c.prompt_index);
                const prompts = Array.from(promptSet).sort((a, b) => a - b);

                return prompts.map((pi) => {
                  const promptCommits = byPrompt.get(pi) ?? [];
                  // Models that participated in this prompt: reference(s) +
                  // candidates, deduped, reference first.
                  const refModels = referenceRows
                    .filter((r) => r.prompt_index === pi)
                    .map((r) => ({ id: r.model_id, isRef: true, totalRounds: r.total_rounds, topCode: r.top_code, error: r.error }));
                  const candModels = candidateCompletions
                    .filter((c) => c.prompt_index === pi)
                    .map((c) => ({ id: c.model_id, isRef: false, totalRounds: c.total_rounds, topCode: c.top_code, error: c.error }));
                  const seen = new Set<string>();
                  const models: typeof refModels = [];
                  for (const m of [...refModels, ...candModels]) {
                    if (!seen.has(m.id)) {
                      seen.add(m.id);
                      models.push(m);
                    }
                  }
                  const maxRound = Math.max(
                    1,
                    ...promptCommits.map((c) => c.source_round),
                    ...models.map((m) => m.totalRounds),
                  );
                  const rounds = Array.from({ length: maxRound }, (_, i) => i + 1);

                  // Index commits by (model, round) for fast lookup
                  const cellCommits = new Map<string, LiveCommit[]>();
                  for (const c of promptCommits) {
                    const k = `${c.source_model}|${c.source_round}`;
                    if (!cellCommits.has(k)) cellCommits.set(k, []);
                    cellCommits.get(k)!.push(c);
                  }

                  // What to mark against: the gold code when this prompt has
                  // one, otherwise agreement with the reference. Without this,
                  // a gold-mode run has no reference and every model is stamped
                  // wrong regardless of whether it got the answer right.
                  const goldCode = goldCodeByPrompt[pi] ?? null;
                  const refTopCode = models.find((m) => m.isRef)?.topCode ?? null;
                  const truthCode = goldCode ?? refTopCode;

                  return (
                    <div key={pi} className="rounded border border-gray-800 bg-gray-950/40 overflow-x-auto">
                      <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-gray-800">
                        <div className="flex items-center gap-2 text-xs">
                          <span className="font-mono text-gray-500">#{pi}</span>
                          <span className="text-gray-400">
                            {promptCommits.length} slot{promptCommits.length === 1 ? "" : "s"} set across {models.length} model{models.length === 1 ? "" : "s"}
                          </span>
                          {goldCode && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-950/40 border border-yellow-900/60 text-yellow-300 font-mono">
                              gold {goldCode}
                            </span>
                          )}
                        </div>
                      </div>
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-left text-gray-500 border-b border-gray-800">
                            <th className="px-3 py-1.5 font-normal w-16">Round</th>
                            {models.map((m) => (
                              <th key={m.id} className="px-3 py-1.5 font-normal">
                                <span className={m.isRef ? "text-amber-300" : "text-gray-300"}>
                                  {m.id}
                                </span>
                                {m.isRef && (
                                  <span className="ml-1 text-[9px] px-1 rounded bg-amber-900/60 text-amber-200">
                                    ref
                                  </span>
                                )}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {rounds.map((r) => (
                            <tr key={r} className="border-b border-gray-900/50">
                              <td className="px-3 py-1.5 font-mono text-gray-500">r{r}</td>
                              {models.map((m) => {
                                const slots = cellCommits.get(`${m.id}|${r}`) ?? [];
                                const didParticipate = r <= m.totalRounds;
                                return (
                                  <td
                                    key={m.id}
                                    className={`px-3 py-1.5 align-top ${
                                      didParticipate ? "" : "bg-gray-950/30"
                                    }`}
                                  >
                                    {slots.length > 0 ? (
                                      <div className="flex flex-wrap gap-1">
                                        {slots.map((s) => (
                                          <SlotTag key={s.slot} slot={s.slot} />
                                        ))}
                                      </div>
                                    ) : didParticipate ? (
                                      <span className="text-gray-600 text-[10px] italic">
                                        {m.totalRounds === 1 && r === 1
                                          ? "direct answer"
                                          : "no new slots"}
                                      </span>
                                    ) : (
                                      <span className="text-gray-700 text-[10px]">—</span>
                                    )}
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                          {/* Footer row: final top code per model */}
                          <tr className="bg-gray-900/60">
                            <td className="px-3 py-1.5 font-mono text-gray-500">final</td>
                            {models.map((m) => {
                              const match = truthCode != null && m.topCode === truthCode;
                              // The reference is the yardstick, so it is never
                              // marked against itself - but it IS marked when a
                              // gold code exists, because then it is just
                              // another answer that can be wrong.
                              const marked = truthCode != null && (goldCode != null || !m.isRef);
                              return (
                                <td key={m.id} className="px-3 py-1.5">
                                  {m.error ? (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/60 text-red-200">error</span>
                                  ) : m.topCode ? (
                                    <span
                                      className={`font-mono ${
                                        marked
                                          ? match ? "text-emerald-300" : "text-red-300"
                                          : "text-amber-300"
                                      }`}
                                    >
                                      {m.topCode}
                                      {marked ? (match ? " ✓" : " ✗") : null}
                                    </span>
                                  ) : (
                                    <span className="text-gray-600 text-[10px]">—</span>
                                  )}
                                </td>
                              );
                            })}
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  );
                });
              })()}
            </div>
          ) : (
            <div className="space-y-3">
              {/* Timeline view (grouped per prompt, chronological) */}
              {(() => {
                const byPrompt = new Map<number, LiveCommit[]>();
                for (const c of commits) {
                  if (!byPrompt.has(c.prompt_index)) byPrompt.set(c.prompt_index, []);
                  byPrompt.get(c.prompt_index)!.push(c);
                }
                const ordered = Array.from(byPrompt.entries()).sort(
                  (a, b) => a[0] - b[0],
                );
                return ordered.map(([pi, list]) => (
                  <div key={pi} className="rounded border border-gray-800 bg-gray-950/40">
                    <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-800">
                      <span className="text-xs font-mono text-gray-500">#{pi}</span>
                      <span className="text-xs text-gray-400">
                        {list.length} slot{list.length === 1 ? "" : "s"} set
                      </span>
                      <div className="flex items-center gap-1 flex-wrap ml-auto">
                        {list.map((c) => (
                          <SlotTag key={c.slot} slot={c.slot} />
                        ))}
                      </div>
                    </div>
                    <ol className="p-2 space-y-1">
                      {list.map((c, i) => (
                        <li
                          key={`${c.slot}-${i}`}
                          className="flex items-start gap-2 text-xs"
                        >
                          <span className="text-gray-600 font-mono w-6 shrink-0 text-right">
                            {i + 1}.
                          </span>
                          <span className="px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-200 border border-emerald-700/60 shrink-0">
                            set
                          </span>
                          <SlotTag slot={c.slot} />
                          <span className="text-gray-500 font-mono shrink-0">r{c.source_round}</span>
                          <span className="text-gray-500 shrink-0">by {c.source_model}</span>
                          <span className="text-gray-400 italic truncate">"{c.source_question}"</span>
                          <span className="text-gray-600 shrink-0">→</span>
                          <span className="text-gray-100 font-medium truncate">
                            {c.answer}
                          </span>
                          <span className="text-gray-600 font-mono shrink-0 ml-auto">
                            {c.time}
                          </span>
                        </li>
                      ))}
                    </ol>
                  </div>
                ));
              })()}
            </div>
          )}
        </div>
      )}

      {/* Live log */}
      <div className="bg-gray-900 rounded-lg p-4">
        <h3 className="text-sm font-medium mb-3">Execution Log</h3>
        <div className="space-y-1 max-h-[500px] overflow-y-auto font-mono text-xs">
          {logs.length === 0 ? (
            <div className="text-gray-500">
              No logs yet. Click "Run Benchmark" to start.
            </div>
          ) : (
            logs.map((entry, i) => (
              <div
                key={i}
                className={`py-0.5 ${
                  entry.event.includes("error") || entry.data.error
                    ? "text-red-400"
                    : entry.event.includes("complete")
                    ? "text-green-400"
                    : "text-gray-300"
                }`}
              >
                <span className="text-gray-600">[{entry.time}]</span>{" "}
                {formatLog(entry)}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
