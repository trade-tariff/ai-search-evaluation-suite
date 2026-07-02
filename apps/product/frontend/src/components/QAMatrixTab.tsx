// Embeds the live Q&A-only matrix served by the backend.
export default function QAMatrixTab() {
  return (
    <div className="-mx-6 -my-6">
      <iframe
        src="/eval/qa-matrix"
        title="Q&A question-mode matrix"
        style={{ width: "100%", height: "calc(100vh - 120px)", border: "none", background: "#0b0f19" }}
      />
      <p className="text-xs text-gray-500 px-6 py-2">
        Live Q&A-only view from <code>kg.e2e_eval_runs</code> and <code>kg.e2e_eval_results</code>, conditioned on gold being present after retrieval.
      </p>
    </div>
  );
}
