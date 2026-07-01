import { useEffect, useState } from "react";
import Stepper from "./components/Stepper";
import ClassifyStage from "./stages/ClassifyStage";
import ValueStage from "./stages/ValueStage";
import DutyStage from "./stages/DutyStage";
import LandedStage from "./stages/LandedStage";
import DeclareStage from "./stages/DeclareStage";
import { initialJourneyState, type JourneyState } from "./types";

// Native end-to-end trader journey (Classify -> Value -> Duty inputs -> Import costs -> Declare).
// Calls /api/* (relative paths) proxied to the single :8000 backend.
// The original standalone app's page chrome (header/footer) was dropped - the fan-out
// shell provides the header + tab nav.

export type Stage = "classify" | "value" | "duty" | "landed" | "declare";

const ORDER: Stage[] = ["classify", "value", "duty", "landed", "declare"];

type CostInfo = {
  usd: number;
  threshold_usd: number;
  over: boolean;
  calls: number;
  estimated?: boolean;
};

// Best-effort daily OpenAI spend banner. Polls /api/cost; flashes red past the cap.
function CostBanner() {
  const [cost, setCost] = useState<CostInfo | null>(null);
  useEffect(() => {
    let alive = true;
    async function poll() {
      try {
        const r = await fetch("/api/cost");
        if (r.ok && alive) setCost(await r.json());
      } catch {
        /* ignore transient errors */
      }
    }
    poll();
    const id = setInterval(poll, 20000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!cost) return null;
  const usd = cost.usd ?? 0;
  const cap = cost.threshold_usd ?? 5;

  if (cost.over) {
    return (
      <div className="animate-pulse border-b border-red-400 bg-red-600 px-6 py-3 text-sm font-semibold text-white">
        WARNING: estimated AI spend today is ${usd.toFixed(2)}, over the ${cap.toFixed(2)}/day cap for this
        test app. Pause testing or raise the cap (cap changes go through Gabriel).
      </div>
    );
  }
  return (
    <div className="flex items-center justify-end border-b border-gray-800 bg-gray-900 px-6 py-1.5 text-xs text-gray-500">
      <span>
        Est. AI spend today:{" "}
        <span className="font-semibold text-gray-300">${usd.toFixed(2)}</span> / ${cap.toFixed(2)} cap
      </span>
    </div>
  );
}

export default function TraderJourneyApp() {
  const [stage, setStage] = useState<Stage>("classify");
  const [state, setState] = useState<JourneyState>(initialJourneyState);

  function update(patch: Partial<JourneyState>) {
    setState((s) => ({ ...s, ...patch }));
  }

  const completed = new Set<Stage>();
  if (state.finalCommodity) completed.add("classify");
  if (state.valuationResult) completed.add("value");
  if (state.dutyResult) completed.add("duty");
  if (state.landedResult) completed.add("landed");
  if (state.declarationResult) completed.add("declare");

  function jump(s: Stage) {
    setStage(s);
  }

  function next() {
    const i = ORDER.indexOf(stage);
    if (i < ORDER.length - 1) setStage(ORDER[i + 1]);
  }

  function back() {
    const i = ORDER.indexOf(stage);
    if (i > 0) setStage(ORDER[i - 1]);
  }

  function startOver() {
    setState(initialJourneyState);
    setStage("classify");
  }

  return (
    <div className="bg-gray-900 text-gray-100 rounded border border-gray-800 overflow-hidden">
      <CostBanner />
      <div className="flex items-center justify-between border-b border-gray-700 px-6 py-3 bg-gray-900">
        <div className="text-sm text-gray-400">
          Your import journey:{" "}
          <span className="font-semibold text-gray-100">
            Classify -&gt; Value -&gt; Duty details -&gt; Import costs -&gt; Declare
          </span>
        </div>
      </div>

      <div className="px-6 pt-4">
        <Stepper current={stage} completed={completed} onJump={jump} />
      </div>

      <main className="px-6 py-6">
        {stage === "classify" && (
          <ClassifyStage state={state} update={update} onNext={next} />
        )}
        {stage === "value" && (
          <ValueStage state={state} update={update} onNext={next} onBack={back} />
        )}
        {stage === "duty" && (
          <DutyStage state={state} update={update} onNext={next} onBack={back} />
        )}
        {stage === "landed" && (
          <LandedStage state={state} update={update} onNext={next} onBack={back} />
        )}
        {stage === "declare" && (
          <DeclareStage state={state} update={update} onBack={back} onStartOver={startOver} />
        )}
      </main>

      <footer className="border-t border-gray-700 px-6 py-3 text-xs text-gray-400 bg-gray-900">
        Demo only - this is not a live HMRC service and all figures are estimates. For official guidance see{" "}
        <a
          href="https://www.gov.uk/goods-sent-from-abroad/tax-and-duty"
          target="_blank"
          rel="noreferrer"
          className="text-blue-400 underline hover:text-blue-300"
        >
          tax and duty on goods sent from abroad on GOV.UK (opens in new tab)
        </a>
        .
        <details className="mt-1">
          <summary className="cursor-pointer text-gray-500 hover:text-gray-300">Data sources (technical)</summary>
          <span>
            Local demo prefers live tariff_db KG/eval data, falls back to offline examples, and only uses
            provider-backed extraction when configured.
          </span>
        </details>
      </footer>
    </div>
  );
}
