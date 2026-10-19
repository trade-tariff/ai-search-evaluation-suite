// Synthetic, offline fixture. Run npm run dev and open /tests/hydration-browser.html.
import ReactDOM from "react-dom/client";
import ExperimentsTab from "../src/components/ExperimentsTab";
import "../src/index.css";

const candidate = { rank: 1, commodity_code: "1234567890", score: 1, description: "Synthetic cotton goods", sources: [] };
let hydrationCalls = 0;
const requestBodies: string[] = [];
const errors: string[] = [];
window.addEventListener("error", event => errors.push(event.message));
Object.assign(window, { hydrationFixture: { requestBodies, errors } });
window.fetch = async (input, init) => {
  const path = String(input);
  let data: unknown;
  if (path === "/api/retrieval/experiments") {
    data = { experiments: [{ run_label: "offline", rank: 1, title: "Offline synthetic fixture", description: "No services or provider calls", runnable: true, config: {}, caveats: [] }] };
  } else if (path === "/api/retrieval/try") {
    data = { query: "Synthetic goods", experiment: { title: "Offline synthetic fixture" }, leg_counts: {}, retrieval_limit: 10, candidate_count: 1, candidates: [candidate], top_candidates: [candidate], elapsed_ms: 1, timings: {} };
  } else if (path === "/api/hydration/candidates") {
    hydrationCalls += 1;
    requestBodies.push(String(init?.body));
    if (hydrationCalls === 2) return new Response(JSON.stringify({ detail: "Synthetic temporary failure" }), { status: 503 });
    data = {
      hydrated: [], candidate_count: 1,
      qa_state: { round: hydrationCalls === 1 ? 1 : 2, in_scope_count: 1 },
      question_hint: { question: hydrationCalls === 1 ? "What is the material?" : "Is it woven?", options: hydrationCalls === 1 ? ["Cotton", "Wool"] : ["Yes", "No"], source: "facet_rules", facet_key: "material" },
    };
  } else {
    throw new Error(`Offline fixture blocked unexpected request: ${path}`);
  }
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
};
ReactDOM.createRoot(document.getElementById("root")!).render(<main className="p-6"><p>Offline synthetic fixture. No live services.</p><ExperimentsTab /></main>);
