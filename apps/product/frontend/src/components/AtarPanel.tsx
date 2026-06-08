import { useEffect, useMemo, useState } from "react";
import {
  type AtarDraft,
  approveAtarDraft,
  discardAtarDraft,
  ingestAtarBatch,
  listAtarDrafts,
  patchAtarDraft,
  regenerateAtarFacts,
} from "../api";

type Filter = "pending" | "approved" | "all";

export default function AtarPanel() {
  const [drafts, setDrafts] = useState<AtarDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("pending");
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [ingestCount, setIngestCount] = useState(20);
  const [ingestBusy, setIngestBusy] = useState(false);
  const [ingestMsg, setIngestMsg] = useState("");
  // Local edit buffer for the selected draft's fact sheet. We commit to the
  // backend on Save / Approve, so users can fiddle without thrashing the API.
  const [factEdits, setFactEdits] = useState<AtarDraft["gold_facts"]>([]);
  const [savingFacts, setSavingFacts] = useState(false);
  const [regenBusy, setRegenBusy] = useState(false);
  const [actionMsg, setActionMsg] = useState("");

  const refresh = async () => {
    setLoading(true);
    try {
      const { drafts } = await listAtarDrafts();
      setDrafts(drafts);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const visible = useMemo(
    () =>
      drafts.filter((d) => {
        if (filter === "all") return true;
        if (filter === "pending") return d.status === "pending";
        if (filter === "approved") return d.status === "approved";
        return true;
      }),
    [drafts, filter],
  );

  const selected = useMemo(
    () => drafts.find((d) => d.ref === selectedRef) || null,
    [drafts, selectedRef],
  );

  // Reset the local edit buffer when the user picks a different draft.
  useEffect(() => {
    if (selected) setFactEdits(selected.gold_facts.map((f) => ({ ...f })));
    else setFactEdits([]);
    setActionMsg("");
  }, [selectedRef]);

  const runIngest = async () => {
    setIngestBusy(true);
    setIngestMsg("");
    try {
      const r = await ingestAtarBatch(ingestCount);
      setIngestMsg(
        `Ingested ${r.ingested_count} new draft${r.ingested_count === 1 ? "" : "s"}.` +
          (r.skipped_existing.length ? ` Skipped ${r.skipped_existing.length} already-ingested.` : ""),
      );
      await refresh();
    } catch (e) {
      setIngestMsg(`Ingest failed: ${String(e)}`);
    } finally {
      setIngestBusy(false);
    }
  };

  const updateFact = (i: number, patch: Partial<AtarDraft["gold_facts"][0]>) => {
    setFactEdits((cur) => cur.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));
  };
  const addFact = () =>
    setFactEdits((cur) => [...cur, { slot: "", answer: "" }]);
  const removeFact = (i: number) =>
    setFactEdits((cur) => cur.filter((_, idx) => idx !== i));

  const saveFacts = async () => {
    if (!selected) return;
    setSavingFacts(true);
    setActionMsg("");
    try {
      const cleaned = factEdits.filter((f) => f.slot.trim() && f.answer.trim());
      const updated = await patchAtarDraft(selected.ref, { gold_facts: cleaned });
      setDrafts((cur) => cur.map((d) => (d.ref === updated.ref ? updated : d)));
      setActionMsg("Saved.");
    } catch (e) {
      setActionMsg(`Save failed: ${String(e)}`);
    } finally {
      setSavingFacts(false);
    }
  };

  const regenFacts = async () => {
    if (!selected) return;
    setRegenBusy(true);
    setActionMsg("");
    try {
      const r = await regenerateAtarFacts(selected.ref);
      setFactEdits(r.gold_facts);
      setActionMsg(`Regenerated ${r.gold_facts.length} facts. Save to persist.`);
    } catch (e) {
      setActionMsg(`Regenerate failed: ${String(e)}`);
    } finally {
      setRegenBusy(false);
    }
  };

  const approve = async () => {
    if (!selected) return;
    setActionMsg("");
    try {
      const cleaned = factEdits.filter((f) => f.slot.trim() && f.answer.trim());
      const r = await approveAtarDraft(selected.ref, cleaned);
      setActionMsg(`Approved as prompt #${r.prompt_index}.`);
      await refresh();
    } catch (e) {
      setActionMsg(`Approve failed: ${String(e)}`);
    }
  };

  const discard = async () => {
    if (!selected) return;
    if (!confirm(`Discard draft ${selected.ref}?`)) return;
    setActionMsg("");
    try {
      await discardAtarDraft(selected.ref);
      setSelectedRef(null);
      await refresh();
    } catch (e) {
      setActionMsg(`Discard failed: ${String(e)}`);
    }
  };

  const counts = useMemo(() => {
    const c = { pending: 0, approved: 0, discarded: 0 };
    for (const d of drafts) {
      if (d.status === "pending") c.pending++;
      else if (d.status === "approved") c.approved++;
      else if (d.status === "discarded") c.discarded++;
    }
    return c;
  }, [drafts]);

  return (
    <div className="space-y-4">
      {/* Ingest controls */}
      <section className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-end justify-between gap-2 flex-wrap">
          <div>
            <h3 className="text-sm font-medium">ATaR (Advance Tariff Rulings)</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Scrape the GOV.UK ATaR search, extract a fact sheet via LLM, and approve as ground-truth prompts. Each ruling lands as a draft with gold_code + gold_facts + oracle_text; approval promotes it into the regular prompt pool.
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="text-gray-500">Ingest</span>
            <input
              type="number"
              min={1}
              max={100}
              value={ingestCount}
              onChange={(e) => setIngestCount(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))}
              className="w-16 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-center"
              disabled={ingestBusy}
            />
            <span className="text-gray-500">new rulings</span>
            <button
              onClick={runIngest}
              disabled={ingestBusy}
              className="px-3 py-1.5 bg-blue-700 hover:bg-blue-600 rounded disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {ingestBusy ? "Ingesting..." : "Ingest"}
            </button>
          </div>
        </div>
        {ingestMsg && (
          <div className="mt-2 text-xs text-emerald-400">{ingestMsg}</div>
        )}
        <div className="mt-3 text-xs text-gray-500 flex gap-4">
          <span>
            Pending: <span className="text-yellow-400 font-medium">{counts.pending}</span>
          </span>
          <span>
            Approved: <span className="text-emerald-400 font-medium">{counts.approved}</span>
          </span>
          <span>
            Discarded: <span className="text-gray-400 font-medium">{counts.discarded}</span>
          </span>
        </div>
      </section>

      <div className="flex items-center gap-2">
        {(["pending", "approved", "all"] as Filter[]).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3 py-1.5 text-xs rounded ${
              filter === f
                ? "bg-blue-600 text-white"
                : "bg-gray-800 hover:bg-gray-700 text-gray-300"
            }`}
          >
            {f}
          </button>
        ))}
        <button
          onClick={refresh}
          className="ml-auto px-3 py-1.5 text-xs bg-gray-800 hover:bg-gray-700 rounded"
        >
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-4">
        {/* Draft list */}
        <div className="space-y-1.5 max-h-[700px] overflow-y-auto">
          {loading && <div className="text-xs text-gray-500 p-2">Loading...</div>}
          {!loading && visible.length === 0 && (
            <div className="text-xs text-gray-500 p-2">
              No drafts in this filter. Use Ingest above to scrape new ATaR rulings.
            </div>
          )}
          {visible.map((d) => (
            <div
              key={d.ref}
              onClick={() => setSelectedRef(d.ref)}
              className={`bg-gray-900 rounded p-2.5 cursor-pointer border transition-colors text-xs ${
                selectedRef === d.ref ? "border-blue-500" : "border-gray-800 hover:border-gray-700"
              }`}
            >
              <div className="flex items-center gap-2 mb-0.5">
                <span className="font-mono text-gray-500">{d.ref}</span>
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded ${
                    d.status === "approved"
                      ? "bg-emerald-900/60 text-emerald-300"
                      : d.status === "discarded"
                        ? "bg-gray-800 text-gray-400"
                        : "bg-yellow-900/60 text-yellow-300"
                  }`}
                >
                  {d.status}
                </span>
                {d.status === "approved" && d.approved_prompt_index != null && (
                  <span className="text-[10px] text-gray-400">#{d.approved_prompt_index}</span>
                )}
              </div>
              <div className="font-mono text-emerald-400 mb-0.5">{d.gold_code}</div>
              <div className="text-gray-400 truncate" title={d.raw_query}>
                {d.raw_query}
              </div>
              <div className="text-gray-600 mt-1">
                {d.gold_facts.length} facts · {d.formatted_results.length} OS results
              </div>
            </div>
          ))}
        </div>

        {/* Detail */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          {!selected && (
            <div className="text-sm text-gray-500">Select a draft to review.</div>
          )}
          {selected && (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="text-xs text-gray-500">
                    Ruling{" "}
                    <a
                      href={`https://www.tax.service.gov.uk/search-for-advance-tariff-rulings/ruling/${selected.ref}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-blue-400 hover:underline"
                    >
                      {selected.ref}
                    </a>{" "}
                    · {selected.ruling.start_date} → {selected.ruling.expiry_date}
                  </div>
                  <div className="text-base font-mono text-emerald-300 mt-1">
                    {selected.gold_code}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={discard}
                    disabled={selected.status === "approved"}
                    className="px-3 py-1.5 text-xs bg-gray-800 hover:bg-red-900/60 hover:text-red-200 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Discard
                  </button>
                  <button
                    onClick={approve}
                    disabled={selected.status === "approved"}
                    className="px-3 py-1.5 text-xs bg-emerald-700 hover:bg-emerald-600 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {selected.status === "approved"
                      ? `Approved (#${selected.approved_prompt_index})`
                      : "Approve as prompt"}
                  </button>
                </div>
              </div>

              {selected.ruling.keywords.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {selected.ruling.keywords.map((k, i) => (
                    <span
                      key={i}
                      className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300"
                    >
                      {k}
                    </span>
                  ))}
                </div>
              )}

              <div>
                <div className="text-xs text-gray-500 mb-1">Description (raw_query)</div>
                <div className="text-sm text-gray-200 bg-gray-950/40 p-2 rounded whitespace-pre-wrap max-h-32 overflow-y-auto">
                  {selected.raw_query}
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <div className="text-xs text-gray-500">
                    Gold facts ({factEdits.length})
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={regenFacts}
                      disabled={regenBusy || selected.status === "approved"}
                      className="px-2 py-1 text-xs bg-gray-800 hover:bg-gray-700 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Re-run the LLM extractor on this ruling"
                    >
                      {regenBusy ? "Regenerating..." : "Regenerate"}
                    </button>
                    <button
                      onClick={addFact}
                      disabled={selected.status === "approved"}
                      className="px-2 py-1 text-xs bg-gray-800 hover:bg-gray-700 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      + Add row
                    </button>
                    <button
                      onClick={saveFacts}
                      disabled={savingFacts || selected.status === "approved"}
                      className="px-2 py-1 text-xs bg-blue-700 hover:bg-blue-600 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {savingFacts ? "Saving..." : "Save edits"}
                    </button>
                  </div>
                </div>
                <div className="space-y-1">
                  {factEdits.length === 0 && (
                    <div className="text-xs text-gray-600 italic p-2">
                      No facts yet. Use Regenerate or Add row.
                    </div>
                  )}
                  {factEdits.map((f, i) => (
                    <div key={i} className="flex items-center gap-1">
                      <input
                        type="text"
                        value={f.slot}
                        placeholder="snake_case_slot"
                        onChange={(e) => updateFact(i, { slot: e.target.value })}
                        disabled={selected.status === "approved"}
                        className="font-mono text-xs bg-gray-800 border border-gray-700 rounded px-2 py-1 w-44 disabled:opacity-60"
                      />
                      <span className="text-gray-600 text-xs">=</span>
                      <input
                        type="text"
                        value={f.answer}
                        placeholder="value"
                        onChange={(e) => updateFact(i, { answer: e.target.value })}
                        disabled={selected.status === "approved"}
                        className="text-xs bg-gray-800 border border-gray-700 rounded px-2 py-1 flex-1 disabled:opacity-60"
                      />
                      <button
                        onClick={() => removeFact(i)}
                        disabled={selected.status === "approved"}
                        className="px-2 py-1 text-xs text-gray-500 hover:text-red-400 disabled:opacity-40 disabled:cursor-not-allowed"
                        title="Remove fact"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
                {actionMsg && (
                  <div className="text-xs text-emerald-400 mt-2">{actionMsg}</div>
                )}
              </div>

              <details>
                <summary className="text-xs text-purple-300 cursor-pointer hover:text-purple-200">
                  Oracle text ({selected.oracle_text.length} chars)
                </summary>
                <div className="mt-1 text-xs text-gray-400 whitespace-pre-wrap max-h-72 overflow-y-auto bg-gray-950/40 p-2 rounded">
                  {selected.oracle_text}
                </div>
              </details>

              <details>
                <summary className="text-xs text-blue-300 cursor-pointer hover:text-blue-200">
                  OpenSearch+vector context ({selected.formatted_results.length} candidates)
                </summary>
                <div className="mt-1 text-xs max-h-72 overflow-y-auto bg-gray-950/40 p-2 rounded">
                  {selected.formatted_results.slice(0, 30).map((r, i) => (
                    <div key={i} className="flex justify-between gap-2 py-0.5 border-b border-gray-900/70">
                      <span className="text-gray-600 w-6 shrink-0">{i + 1}</span>
                      <span
                        className={`font-mono shrink-0 ${
                          r.commodity_code === selected.gold_code ? "text-yellow-300" : "text-emerald-300"
                        }`}
                      >
                        {r.commodity_code}
                      </span>
                      <span className="text-gray-400 truncate flex-1">
                        {r.description.replace(/<br>/g, " ").slice(0, 100)}
                      </span>
                      <span className="text-gray-500 shrink-0">{r.score.toFixed(3)}</span>
                    </div>
                  ))}
                  {selected.formatted_results.length > 30 && (
                    <div className="text-gray-600 italic mt-1">
                      ... and {selected.formatted_results.length - 30} more
                    </div>
                  )}
                </div>
              </details>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
