import { FormEvent, useEffect, useMemo, useState } from "react";

type Experiment = {
  rank: number | null;
  run_label: string;
  run_id: string;
  title: string;
  description: string;
  caveats: string[];
  headline_recall_pct: number;
  ott_baseline: boolean;
  runnable: boolean;
  matrix_runnable?: boolean;
  deploy_runnable?: boolean;
  deploy_requires_rewrite?: boolean;
  deploy_provider_steps?: string[];
  config: Record<string, unknown>;
};

type Candidate = {
  rank: number;
  commodity_code: string;
  description: string;
  score: number;
  sources?: string[];
};

type TrialResult = {
  query: string;
  expected_code: string;
  expected_code_normalized: string;
  experiment: Experiment;
  retrieval_limit: number;
  rank: number | null;
  hit_at_10: boolean;
  hit_at_100: boolean;
  hit_within_limit: boolean;
  leg_counts: Record<string, number>;
  top_candidates: Candidate[];
};

const TOP_RUN_LABEL = "no_curated_only";

const DEMO_EXPERIMENTS: Experiment[] = [
  {
    rank: 1,
    run_label: TOP_RUN_LABEL,
    run_id: "demo-no-curated-only",
    title: "Top overall: semantic + KG + facets, no Search References",
    description:
      "Demo fallback for the shipped retrieval stack: commodity text plus semantic vector search, structured facts, and KG evidence. The live catalogue normally supplies the full matrix.",
    caveats: ["Fallback row shown only when the experiment catalogue is unavailable."],
    headline_recall_pct: 89.4,
    ott_baseline: false,
    runnable: true,
    config: {
      retrieval_limit: 500,
      use_vector: true,
      use_facts: true,
      use_facts_vec: true,
      use_kg_context: true,
      use_kg_vec: true,
      use_curated: false,
      triage: false,
    },
  },
  {
    rank: 2,
    run_label: "all_legs_on",
    run_id: "demo-all-legs-on",
    title: "Semantic + KG + facets",
    description:
      "Reference fallback for the full evidence stack when Search References are present in the matrix setup.",
    caveats: ["Fallback row shown only when the experiment catalogue is unavailable."],
    headline_recall_pct: 89.2,
    ott_baseline: false,
    runnable: true,
    config: {
      retrieval_limit: 500,
      use_vector: true,
      use_facts: true,
      use_facts_vec: true,
      use_kg_context: true,
      use_kg_vec: true,
      use_curated: true,
      triage: false,
    },
  },
];

function pct(value: number | null | undefined) {
  return typeof value === "number" ? `${value.toFixed(1)}%` : "n/a";
}

function normalizedCode(value: string) {
  const digits = value.replace(/\D/g, "");
  return digits ? digits.padEnd(10, "0").slice(0, 10) : "";
}

function boolConfig(exp: Experiment, key: string) {
  return exp.config[key] === true;
}

function flagClass(enabled: boolean) {
  return enabled
    ? "border-emerald-700 bg-emerald-950/50 text-emerald-200"
    : "border-gray-800 bg-gray-900 text-gray-500";
}

function retrievalSourceLabel(source: string) {
  const labels: Record<string, string> = {
    fts: "Keyword text",
    substring: "Description substring",
    reference: "Search References",
    facts: "Facet FTS",
    facts_vec: "Facet vector",
    kg_context: "KG FTS",
    kg_vec: "KG vector",
    vector: "Semantic vector",
    fts_composite: "AI-enriched text FTS",
    vector_composite: "AI-enriched text vector",
  };
  return labels[source] ?? source.replace(/_/g, " ");
}

function providerSteps(experiment: Experiment | null) {
  if (!experiment) return [];
  if (experiment.deploy_provider_steps?.length) return experiment.deploy_provider_steps;
  const steps = [];
  if (boolConfig(experiment, "triage")) steps.push("rewrite");
  if (
    boolConfig(experiment, "use_vector") ||
    boolConfig(experiment, "use_facts_vec") ||
    boolConfig(experiment, "use_kg_vec")
  ) {
    steps.push("embedding");
  }
  return steps;
}

function ExperimentFlags({ experiment }: { experiment: Experiment }) {
  const flags = [
    ["use_composite", "AI-enriched text"],
    ["use_vector", "Vector"],
    ["use_facts", "Facet FTS"],
    ["use_facts_vec", "Facet vector"],
    ["use_kg_context", "KG FTS"],
    ["use_kg_vec", "KG vector"],
    ["use_curated", "Search References"],
    ["triage", "Rewrite"],
  ];
  return (
    <div className="flex flex-wrap gap-2">
      {flags.map(([key, label]) => {
        const enabled = boolConfig(experiment, key);
        return (
          <span
            key={key}
            className={`rounded border px-2 py-1 text-xs ${flagClass(enabled)}`}
          >
            {label}
          </span>
        );
      })}
    </div>
  );
}

function Metric({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "good" | "bad";
}) {
  const toneClass =
    tone === "good"
      ? "border-emerald-800 bg-emerald-950/40 text-emerald-100"
      : tone === "bad"
        ? "border-red-900 bg-red-950/40 text-red-100"
        : "border-gray-800 bg-gray-900 text-gray-100";
  return (
    <div className={`rounded border p-4 ${toneClass}`}>
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-2 text-2xl font-semibold">{value}</div>
    </div>
  );
}

async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : response.statusText;
    throw new Error(detail);
  }
  return data as T;
}

async function fetchApi(path: string, init?: RequestInit): Promise<Response> {
  try {
    const response = await fetch(path, init);
    if (response.status === 404 && shouldRetryLocalBackend(path)) {
      return fetch(`http://127.0.0.1:8000${path}`, init);
    }
    return response;
  } catch (err) {
    if (!shouldRetryLocalBackend(path)) throw err;
    return fetch(`http://127.0.0.1:8000${path}`, init);
  }
}

function shouldRetryLocalBackend(path: string): boolean {
  if (typeof window === "undefined") return false;
  if (!path.startsWith("/api/")) return false;
  return window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost";
}

export default function ExperimentsTab() {
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [selectedRunLabel, setSelectedRunLabel] = useState(TOP_RUN_LABEL);
  const [query, setQuery] = useState("");
  const [expectedCode, setExpectedCode] = useState("");
  const [retrievalLimit, setRetrievalLimit] = useState(500);
  const [allowProviderCalls, setAllowProviderCalls] = useState(false);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [trialError, setTrialError] = useState<string | null>(null);
  const [trial, setTrial] = useState<TrialResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setNotice(null);
    setTrialError(null);
    fetchApi("/api/retrieval/experiments")
      .then((response) => readJson<{ experiments: Experiment[] }>(response))
      .then((data) => {
        if (cancelled) return;
        const rows = data.experiments || [];
        setExperiments(rows);
        const top = rows.find((row) => row.run_label === TOP_RUN_LABEL && row.runnable);
        const firstRunnable = rows.find((row) => row.runnable);
        setSelectedRunLabel((top || firstRunnable || rows[0])?.run_label || TOP_RUN_LABEL);
      })
      .catch((err) => {
        if (!cancelled) {
          setExperiments(DEMO_EXPERIMENTS);
          setSelectedRunLabel(TOP_RUN_LABEL);
          setNotice(
            err instanceof Error
              ? `Experiment catalogue did not load (${err.message}). Showing demo fallback rows; other product panels are unaffected.`
              : "Experiment catalogue did not load. Showing demo fallback rows; other product panels are unaffected.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedExperiment = useMemo(
    () => experiments.find((row) => row.run_label === selectedRunLabel) || null,
    [experiments, selectedRunLabel],
  );

  const sortedExperiments = useMemo(
    () =>
      [...experiments].sort((a, b) => {
        const ar = a.rank ?? 9999;
        const br = b.rank ?? 9999;
        return ar - br || a.run_label.localeCompare(b.run_label);
      }),
    [experiments],
  );

  const expectedNormalized = normalizedCode(expectedCode);
  const selectedProviderSteps = useMemo(
    () => providerSteps(selectedExperiment),
    [selectedExperiment],
  );
  const selectedRequiresProvider = selectedProviderSteps.length > 0;
  const canRun =
    Boolean(query.trim()) &&
    Boolean(expectedNormalized) &&
    Boolean(selectedExperiment?.runnable) &&
    (!selectedRequiresProvider || allowProviderCalls) &&
    !running;

  async function runTrial(event: FormEvent) {
    event.preventDefault();
    if (!canRun) return;
    setRunning(true);
    setTrialError(null);
    setTrial(null);
    try {
      const response = await fetchApi("/api/retrieval/try", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          expected_code: expectedCode,
          run_label: selectedRunLabel,
          retrieval_limit: retrievalLimit,
          allow_spend: selectedRequiresProvider && allowProviderCalls,
        }),
      });
      const result = await readJson<TrialResult>(response);
      setTrial(result);
    } catch (err) {
      setTrialError(
        err instanceof Error
          ? `Retrieval trial unavailable: ${err.message}`
          : "Retrieval trial unavailable.",
      );
    } finally {
      setRunning(false);
    }
  }

  const rankTone = trial?.rank ? "good" : trial ? "bad" : "neutral";

  return (
    <div className="space-y-6" data-testid="experiments-tab">
      <section className="rounded border border-gray-800 bg-gray-900/60 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="max-w-3xl">
            <div className="text-xs uppercase tracking-wide text-gray-500">
              Experiment workspace
            </div>
            <h2 className="mt-2 text-2xl font-semibold text-gray-100">
              Retrieval experiments
            </h2>
            {selectedExperiment && (
              <div className="mt-4 space-y-3">
                <div>
                  <div className="text-sm text-gray-400">Current top pick</div>
                  <div className="mt-1 text-lg font-medium text-gray-100">
                    #{selectedExperiment.rank ?? "-"} {selectedExperiment.title}
                  </div>
                  <p className="mt-2 text-sm leading-6 text-gray-300">
                    {selectedExperiment.description}
                  </p>
                </div>
                <ExperimentFlags experiment={selectedExperiment} />
              </div>
            )}
          </div>
          <div className="grid min-w-[240px] grid-cols-2 gap-3">
            <Metric
              label="Recall@100"
              value={selectedExperiment ? pct(selectedExperiment.headline_recall_pct) : "n/a"}
              tone="good"
            />
            <Metric
              label="Runnable"
              value={selectedExperiment?.runnable ? "Yes" : "No"}
              tone={selectedExperiment?.runnable ? "good" : "bad"}
            />
          </div>
        </div>
      </section>

      <section className="rounded border border-gray-800 bg-gray-900/60 p-5">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
          <form className="space-y-4" onSubmit={runTrial}>
            <div>
              <label className="block text-sm font-medium text-gray-300">
                Experiment
              </label>
              <select
                value={selectedRunLabel}
                onChange={(event) => {
                  setSelectedRunLabel(event.target.value);
                  setAllowProviderCalls(false);
                }}
                className="mt-2 w-full rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
              >
                {sortedExperiments.map((experiment) => (
                  <option key={experiment.run_label} value={experiment.run_label}>
                    #{experiment.rank ?? "-"} {experiment.title}
                    {experiment.runnable ? "" : " (catalog only)"}
                  </option>
                ))}
              </select>
            </div>

            <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
              <div>
                <label className="block text-sm font-medium text-gray-300">
                  Input query
                </label>
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="mt-2 w-full rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
                  placeholder="fresh goods description"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-300">
                  Expected commodity code
                </label>
                <input
                  value={expectedCode}
                  onChange={(event) => setExpectedCode(event.target.value)}
                  className="mt-2 w-full rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
                  placeholder="10 digit code"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-end gap-3">
              <label className="block">
                <span className="block text-sm font-medium text-gray-300">
                  Retrieval limit
                </span>
                <input
                  type="number"
                  min={10}
                  max={500}
                  step={10}
                  value={retrievalLimit}
                  onChange={(event) => setRetrievalLimit(Number(event.target.value))}
                  className="mt-2 w-36 rounded border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
                />
              </label>
              <label className="flex min-h-[40px] items-center gap-2 rounded border border-gray-800 bg-gray-950 px-3 py-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={allowProviderCalls}
                  disabled={!selectedRequiresProvider}
                  onChange={(event) => setAllowProviderCalls(event.target.checked)}
                  className="h-4 w-4 rounded border-gray-700 bg-gray-950"
                />
                Provider calls
              </label>
              <button
                type="submit"
                disabled={!canRun}
                className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
              >
                {running ? "Running..." : "Run retrieval"}
              </button>
              {expectedNormalized && (
                <span className="pb-2 text-sm text-gray-500">
                  Normalized: {expectedNormalized}
                </span>
              )}
              {selectedRequiresProvider && !allowProviderCalls && (
                <span className="pb-2 text-sm text-amber-300">
                  Requires {selectedProviderSteps.join(" + ")}
                </span>
              )}
            </div>
          </form>

          <aside className="rounded border border-gray-800 bg-gray-950 p-4">
            <div className="text-sm font-medium text-gray-200">Selected experiment</div>
            {selectedExperiment ? (
              <div className="mt-3 space-y-3">
                <div>
                  <div className="text-xs uppercase tracking-wide text-gray-500">
                    {selectedExperiment.run_label}
                  </div>
                  <div className="mt-1 text-sm text-gray-300">
                    {selectedExperiment.title}
                  </div>
                </div>
                <ExperimentFlags experiment={selectedExperiment} />
                <ul className="space-y-2 text-sm leading-5 text-gray-400">
                  {selectedExperiment.caveats.map((caveat) => (
                    <li key={caveat}>{caveat}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="mt-3 text-sm text-gray-500">
                {loading ? "Loading..." : "No experiments loaded"}
              </div>
            )}
          </aside>
        </div>
      </section>

      {notice && (
        <div className="rounded border border-amber-900/80 bg-amber-950/30 p-4 text-sm text-amber-100">
          {notice}
        </div>
      )}

      {trialError && (
        <div className="rounded border border-red-900 bg-red-950/40 p-4 text-sm text-red-100">
          {trialError}
        </div>
      )}

      {trial && (
        <section className="rounded border border-gray-800 bg-gray-900/60 p-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <h3 className="text-lg font-semibold text-gray-100">Trial result</h3>
              <div className="mt-1 text-sm text-gray-400">
                {trial.experiment.title} against {trial.expected_code_normalized}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Metric
                label="Gold rank"
                value={trial.rank ? `#${trial.rank}` : "Miss"}
                tone={rankTone}
              />
              <Metric label="Hit@10" value={trial.hit_at_10 ? "Yes" : "No"} tone={trial.hit_at_10 ? "good" : "bad"} />
              <Metric label="Hit@100" value={trial.hit_at_100 ? "Yes" : "No"} tone={trial.hit_at_100 ? "good" : "bad"} />
              <Metric
                label={`Within ${trial.retrieval_limit}`}
                value={trial.hit_within_limit ? "Yes" : "No"}
                tone={trial.hit_within_limit ? "good" : "bad"}
              />
            </div>
          </div>

          <div className="mt-5 flex flex-wrap gap-2">
            {Object.entries(trial.leg_counts).map(([leg, count]) => (
              <span
                key={leg}
                className="rounded border border-gray-800 bg-gray-950 px-2 py-1 text-xs text-gray-300"
              >
                {retrievalSourceLabel(leg)}: {count}
              </span>
            ))}
          </div>

          <div className="mt-5 overflow-x-auto">
            <table className="w-full min-w-[760px] border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-gray-400">
                  <th className="py-2 pr-3 font-medium">Rank</th>
                  <th className="py-2 pr-3 font-medium">Code</th>
                  <th className="py-2 pr-3 font-medium">Score</th>
                  <th className="py-2 pr-3 font-medium">Sources</th>
                  <th className="py-2 pr-3 font-medium">Description</th>
                </tr>
              </thead>
              <tbody>
                {trial.top_candidates.map((candidate) => {
                  const match = normalizedCode(candidate.commodity_code) === trial.expected_code_normalized;
                  return (
                    <tr
                      key={`${candidate.rank}-${candidate.commodity_code}`}
                      className={`border-b border-gray-800/70 ${match ? "bg-emerald-950/50" : ""}`}
                    >
                      <td className="py-3 pr-3 text-gray-300">#{candidate.rank}</td>
                      <td className="py-3 pr-3 font-mono text-gray-100">{candidate.commodity_code}</td>
                      <td className="py-3 pr-3 text-gray-300">{candidate.score.toFixed(5)}</td>
                      <td className="py-3 pr-3 text-gray-300">
                        {(candidate.sources || []).map(retrievalSourceLabel).join(", ")}
                      </td>
                      <td className="py-3 pr-3 text-gray-300">{candidate.description}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="rounded border border-gray-800 bg-gray-900/60 p-5">
        <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div>
            <h3 className="text-lg font-semibold text-gray-100">
              Experiment catalogue
            </h3>
            <div className="mt-1 text-sm text-gray-500">
              {sortedExperiments.length} matrix rows ranked by code macro recall@100
            </div>
          </div>
          <a
            href="/eval/matrix"
            target="_blank"
            rel="noreferrer"
            className="text-sm font-medium text-blue-400 hover:text-blue-300"
          >
            Open matrix snapshot
          </a>
        </div>

        <div className="mt-5 space-y-4">
          {sortedExperiments.length === 0 && (
            <div className="rounded border border-gray-800 bg-gray-950 p-4 text-sm text-gray-400">
              No experiment rows are loaded yet. Other product panels are still available.
            </div>
          )}
          {sortedExperiments.map((experiment) => (
            <article
              key={experiment.run_label}
              className="rounded border border-gray-800 bg-gray-950 p-4"
            >
              <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div className="max-w-4xl">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300">
                      #{experiment.rank ?? "-"}
                    </span>
                    <span className="rounded bg-blue-950/60 px-2 py-1 text-xs text-blue-200">
                      {pct(experiment.headline_recall_pct)}
                    </span>
                    <span
                      className={`rounded px-2 py-1 text-xs ${
                        experiment.runnable
                          ? "bg-emerald-950/60 text-emerald-200"
                          : "bg-amber-950/60 text-amber-200"
                      }`}
                    >
                      {experiment.runnable ? "Runnable" : "Catalog only"}
                    </span>
                    {experiment.ott_baseline && (
                      <span className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300">
                        OTT baseline
                      </span>
                    )}
                  </div>
                  <h4 className="mt-3 text-base font-semibold text-gray-100">
                    {experiment.title}
                  </h4>
                  <div className="mt-1 font-mono text-xs text-gray-500">
                    {experiment.run_label}
                  </div>
                  <p className="mt-3 text-sm leading-6 text-gray-300">
                    {experiment.description}
                  </p>
                  <ul className="mt-3 space-y-2 text-sm leading-5 text-gray-500">
                    {experiment.caveats.map((caveat) => (
                      <li key={caveat}>{caveat}</li>
                    ))}
                  </ul>
                </div>
                <div className="lg:w-[360px]">
                  <ExperimentFlags experiment={experiment} />
                  <details className="mt-3 rounded border border-gray-800 bg-gray-900">
                    <summary className="cursor-pointer px-3 py-2 text-sm text-gray-300">
                      Config JSON
                    </summary>
                    <pre className="max-h-56 overflow-auto border-t border-gray-800 p-3 text-xs text-gray-400">
                      {JSON.stringify(experiment.config, null, 2)}
                    </pre>
                  </details>
                </div>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
