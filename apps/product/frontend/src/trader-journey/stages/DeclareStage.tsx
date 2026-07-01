import { useState } from "react";
import { api } from "../api";
import { Money, money } from "../components/Money";
import type { DeclarationResult, DutyResult, FilingIntentResult, JourneyState, LandedResult } from "../types";

interface Props {
  state: JourneyState;
  update: (patch: Partial<JourneyState>) => void;
  onBack: () => void;
  onStartOver: () => void;
}

export default function DeclareStage({ state, update, onBack, onStartOver }: Props) {
  const [description, setDescription] = useState(state.descriptionOfGoods || state.query);
  const [netMass, setNetMass] = useState(state.netMassKg ?? "");
  const [result, setResult] = useState<DeclarationResult | null>(state.declarationResult);
  const [phase, setPhase] = useState<"edit" | "review" | "result">(state.declarationResult ? "result" : "edit");
  const [filing, setFiling] = useState<FilingIntentResult | null>(state.filingIntent);
  const [loading, setLoading] = useState(false);
  const [filingLoading, setFilingLoading] = useState(false);
  const [downloadLoading, setDownloadLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const commodity = state.finalCommodity;
  const duty = state.dutyResult;
  const landed = state.landedResult;

  if (!commodity || !duty || !landed) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold mb-2">5. Draft customs declaration</h2>
        <div className="tj-card max-w-3xl space-y-3">
          <p className="text-sm text-gray-300">
            Complete the earlier steps first - we need your commodity code, customs value and duty results to draft a declaration.
          </p>
          <button onClick={onBack} className="tj-btn-secondary">
            &larr; Back
          </button>
        </div>
      </div>
    );
  }

  async function generate() {
    if (!commodity || !duty || !landed) return;
    const parsedNetMass = parseOptionalNonNegative(netMass);
    if (parsedNetMass === undefined) {
      setError("Enter the net weight in kg as a number (0 or more), or leave it blank.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const pref = !duty.rate_source.startsWith("MFN") && state.hasProofOfOrigin ? duty.rate_source : null;
      const res = await api.declaration({
        commodity_code: commodity.commodity_code,
        description_of_goods: description,
        country_of_origin: state.countryOfOrigin,
        import_date: state.importDate,
        customs_value_gbp: state.customsValueGbp!,
        quantity_units: state.quantityUnits,
        quantity_unit_type: state.quantityUnitType,
        net_mass_kg: parsedNetMass,
        duty_gbp: duty.customs_duty_gbp,
        excise_gbp: duty.excise_duty_gbp,
        vat_gbp: landed.vat_gbp,
        has_proof_of_origin: state.hasProofOfOrigin,
        preference_claimed: pref,
        valuation_method: state.valuationMethodCode,
        additional_codes: additionalCodesFromDuty(duty),
        original_query: state.query,
        qa_history: state.qaHistory,
        rejected_candidates: rejectedCandidates(state),
      });
      setResult(res);
      update({
        descriptionOfGoods: description,
        netMassKg: parsedNetMass,
        declarationResult: res,
        filingIntent: null,
      });
      setFiling(null);
      setPhase("result");
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally {
      setLoading(false);
    }
  }

  async function fileForMe() {
    if (!result) return;
    setFilingLoading(true);
    setError(null);
    try {
      const res = await api.declarationFileIntent(result);
      setFiling(res);
      update({ filingIntent: res });
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally {
      setFilingLoading(false);
    }
  }

  async function downloadData() {
    if (!result) return;
    setDownloadLoading(true);
    setError(null);
    try {
      const journeyState = {
        query: state.query,
        qa_history: state.qaHistory,
        classification_turn: state.lastClassifyTurn,
        commodity: state.finalCommodity,
        classification_confidence: state.classifyConfidence,
        fixed_candidates: state.fixedCandidates,
        valuation_method: state.valuationMethodCode,
        valuation_result: state.valuationResult,
        valuation_guide_result: state.valuationGuideResult,
        customs_value_gbp: state.customsValueGbp,
        duty_inference: state.dutyInference,
        duty: state.dutyResult,
        duty_explainer_text: state.dutyExplainerText,
        landed: state.landedResult,
        meursing_inputs: state.meursingInputs,
      };
      const blob = await api.declarationDownload(result, journeyState);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `declaration-${commodity?.commodity_code || "draft"}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally {
      setDownloadLoading(false);
    }
  }

  function printSummary() {
    if (!result || !commodity || !duty || !landed) return;
    const w = window.open("", "_blank");
    if (!w) {
      setError("Your browser blocked the print window - allow pop-ups for this site and try again.");
      return;
    }
    setError(null);
    w.document.open();
    w.document.write(buildPrintHtml({ state, result, commodity, duty, landed, description }));
    w.document.close();
    w.focus();
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold mb-2">5. Draft customs declaration</h2>
        <p className="text-gray-400 mb-4">
          A pre-filled CDS-style declaration based on everything you've entered so far. This is a learning aid, not a submission - you (or your customs broker) submit through HMRC's Customs Declaration Service.{" "}
          <a href="https://www.gov.uk/guidance/making-a-full-import-declaration" target="_blank" rel="noreferrer" className="text-blue-400 underline">
            How to make a full import declaration on GOV.UK (opens in new tab)
          </a>
        </p>
      </div>

      {phase === "edit" && (
      <div className="tj-card space-y-3">
        <div>
          <label className="tj-label">Description of goods (DE 6/8)</label>
          <span className="tj-hint">A clear plain-English description as it would appear on the declaration</span>
          <input
            type="text"
            className="tj-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="tj-label">Net mass (kg)</label>
            <span className="tj-hint">Total weight of goods without packaging</span>
            <input
              type="number"
              inputMode="decimal"
              className="tj-input"
              value={netMass}
              onChange={(e) => setNetMass(e.target.value as any)}
              min="0"
              step="0.01"
            />
          </div>
          <div className="flex items-end">
            <button
              onClick={() => {
                const parsedNetMass = parseOptionalNonNegative(netMass);
                if (parsedNetMass === undefined) {
                  setError("Enter the net weight in kg as a number (0 or more), or leave it blank.");
                  return;
                }
                setError(null);
                setPhase("review");
              }}
              className="tj-btn w-full"
            >
              Review declaration inputs
            </button>
          </div>
        </div>
      </div>
      )}

      {error && (
        <div className="border-l-4 border-red-500 bg-gray-900 p-4">
          <strong>Error:</strong> {error}
        </div>
      )}

      {phase === "review" && commodity && duty && landed && (
        <div className="tj-card max-w-3xl">
          <h3 className="text-xl font-bold mb-3">Review declaration inputs</h3>
          <table className="w-full text-sm">
            <tbody>
              {[
                ["Commodity code", commodity.commodity_code],
                ["Description of goods", description],
                ["Country of origin", state.countryOfOrigin],
                ["Import date", state.importDate || ""],
                ["Customs value", state.customsValueGbp != null ? `GBP ${state.customsValueGbp.toFixed(2)}` : ""],
                ["Valuation method", state.valuationMethodCode ? state.valuationMethodCode.replace(/_/g, " ").replace(/^method /, "Method ") : ""],
                ["Quantity", state.quantityUnits ? `${state.quantityUnits} ${state.quantityUnitType || ""}` : ""],
                ["Net mass", netMass ? `${netMass} kg` : ""],
                ["Additional code", duty.meursing?.additional_code ? `${duty.meursing.code_type} ${duty.meursing.additional_code}` : ""],
                ["Customs duty", `GBP ${duty.customs_duty_gbp.toFixed(2)}`],
                ["Excise", `GBP ${duty.excise_duty_gbp.toFixed(2)}`],
                ["VAT", `GBP ${landed.vat_gbp.toFixed(2)}`],
              ].map(([label, value]) => (
                <tr key={label} className="border-b border-gray-800">
                  <td className="py-2 text-gray-400 w-1/2">{label}</td>
                  <td className="py-2 font-mono">{value || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex justify-between mt-6">
            <button onClick={() => setPhase("edit")} className="tj-btn-secondary">&larr; Change inputs</button>
            <button onClick={generate} className="tj-btn" disabled={loading}>
              {loading ? "Generating..." : "Generate declaration draft"}
            </button>
          </div>
        </div>
      )}

      {phase === "result" && result && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-4">
            <div className="tj-card">
              <h3 className="text-lg font-bold mb-3">CDS data elements</h3>
              <table className="w-full text-sm">
                <tbody>
                  {Object.entries(result.cds_box_values).map(([k, v]) => (
                    <tr key={k} className="border-b border-gray-800 last:border-b-0">
                      <td className="py-2 pr-3 text-gray-400 align-top w-1/2">{k}</td>
                      <td className="py-2 font-mono text-sm">
                        {v || <span className="text-gray-500 italic">(blank)</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {result.required_document_codes.length > 0 && (
              <div className="tj-card">
                <h3 className="text-lg font-bold mb-3">Required documents (DE 2/3)</h3>
                <ul className="space-y-2">
                  {result.required_document_codes.map((d, i) => (
                    <li key={i} className="flex gap-3 text-sm">
                      <span className="font-mono font-bold text-blue-400 w-14 shrink-0">{d.code}</span>
                      <span className="flex-1">
                        {d.description}
                        {d.inherited && d.attached_at && (
                          <span className="block text-xs text-gray-400 italic mt-0.5">
                            applies to all goods under code {d.attached_at}
                          </span>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {result.audit_summary && (
              <details className="tj-card">
                <summary className="text-lg font-bold cursor-pointer">Audit trail</summary>
                <div className="mt-3 text-sm space-y-2">
                  {result.audit_summary.original_query && (
                    <div>
                      <span className="text-gray-400 uppercase text-xs tracking-wider">Original query</span>
                      <div className="font-mono">{result.audit_summary.original_query}</div>
                    </div>
                  )}
                  {result.audit_summary.qa_history?.length > 0 && (
                    <div>
                      <span className="text-gray-400 uppercase text-xs tracking-wider">Q&A path</span>
                      <ol className="list-decimal list-inside space-y-1 mt-1">
                        {result.audit_summary.qa_history.map((q, i) => (
                          <li key={i}>
                            <span className="text-gray-400">{q.question}</span>
                            {q.answer && <strong> -&gt; {q.answer}</strong>}
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}
                  <div>
                    <span className="text-gray-400 uppercase text-xs tracking-wider">Chosen code</span>
                    <div className="font-mono">{result.audit_summary.chosen_code} - {result.audit_summary.chosen_description}</div>
                  </div>
                  {result.audit_summary.rejected_candidates?.length > 0 && (
                    <div>
                      <span className="text-gray-400 uppercase text-xs tracking-wider">Rejected alternatives</span>
                      <ul className="list-disc list-inside space-y-0.5 mt-1">
                        {result.audit_summary.rejected_candidates.map((c, i) => (
                          <li key={i}>
                            <span className="font-mono">{c.code}</span> - {c.description}
                            {c.reason && <em className="text-gray-400"> ({c.reason})</em>}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <span className="text-gray-400 uppercase tracking-wider">Tariff measures checked ({result.audit_summary.measure_ids.length})</span>
                      <div className="font-mono">
                        {result.audit_summary.measure_ids.slice(0, 6).join(", ")}
                        {result.audit_summary.measure_ids.length > 6 && ` +${result.audit_summary.measure_ids.length - 6}`}
                      </div>
                    </div>
                    <div>
                      <span className="text-gray-400 uppercase tracking-wider">Where the document codes came from</span>
                      <ul className="space-y-0.5 mt-0.5">
                        {Object.entries(result.audit_summary.document_codes_by_source).map(([src, codes]) => (
                          <li key={src}><strong>{codes.length}</strong> from {src}</li>
                        ))}
                      </ul>
                    </div>
                  </div>
                </div>
              </details>
            )}
          </div>

          <aside className="space-y-4">
            <div className="tj-card">
              <h3 className="text-lg font-bold mb-3">Money summary</h3>
              <table className="w-full text-sm">
                <tbody>
                  <tr>
                    <td className="py-1 text-gray-400">Customs value</td>
                    <td className="py-1 text-right tabular-nums"><Money value={result.summary.customs_value_gbp} /></td>
                  </tr>
                  <tr>
                    <td className="py-1 text-gray-400">Customs duty</td>
                    <td className="py-1 text-right tabular-nums"><Money value={result.summary.duty_gbp} /></td>
                  </tr>
                  <tr>
                    <td className="py-1 text-gray-400">Excise duty</td>
                    <td className="py-1 text-right tabular-nums"><Money value={result.summary.excise_gbp} /></td>
                  </tr>
                  <tr>
                    <td className="py-1 text-gray-400">VAT</td>
                    <td className="py-1 text-right tabular-nums"><Money value={result.summary.vat_gbp} /></td>
                  </tr>
                  <tr className="border-t-2 border-gray-600 font-bold">
                    <td className="py-1">Total taxes</td>
                    <td className="py-1 text-right tabular-nums"><Money value={result.summary.total_taxes_gbp} /></td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div className="tj-card">
              <h3 className="text-lg font-bold mb-3">Next steps</h3>
              <ol className="space-y-2 text-sm list-decimal list-inside text-gray-100">
                {result.next_steps.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
              <a href="https://www.gov.uk/guidance/get-access-to-the-customs-declaration-service" target="_blank" rel="noreferrer" className="block text-sm text-blue-400 underline mt-2">
                Get access to the Customs Declaration Service on GOV.UK (opens in new tab)
              </a>
            </div>

              <div className="tj-card space-y-3">
                <h3 className="text-lg font-bold">Declaration options</h3>
                <button onClick={fileForMe} className="tj-btn w-full" disabled={filingLoading}>
                {filingLoading ? "Preparing handoff..." : "File for me (broker handoff)"}
              </button>
              <button onClick={downloadData} className="tj-btn-secondary w-full" disabled={downloadLoading}>
                {downloadLoading ? "Preparing download..." : "Download data for declaration"}
              </button>
              <button onClick={printSummary} className="tj-btn-secondary w-full">
                Print summary
              </button>
              <button onClick={() => setPhase("edit")} className="tj-btn-secondary w-full">
                Change inputs &amp; regenerate
              </button>
              {error && <p className="text-sm text-red-400">{error}</p>}
              {filing && (
                <div className="text-sm border-l-4 border-emerald-500 pl-3 text-gray-200">
                  <div className="font-mono text-emerald-300">{filing.reference}</div>
                  <p className="text-xs text-gray-400 mt-1">{filing.message}</p>
                </div>
              )}
            </div>
          </aside>
        </div>
      )}

      <div className="flex justify-between">
        <button onClick={onBack} className="tj-btn-secondary">
          &larr; Back
        </button>
        <button
          onClick={() => {
            if (window.confirm("Start a new journey? This clears everything you've entered.")) onStartOver();
          }}
          className="tj-btn-secondary"
        >
          Start a new journey
        </button>
      </div>
    </div>
  );
}

function additionalCodesFromDuty(duty: NonNullable<JourneyState["dutyResult"]>): Record<string, unknown>[] {
  const m = duty.meursing;
  if (!m?.additional_code) return [];
  return [{
    type: m.code_type || "Meursing",
    code: m.additional_code,
    source: m.lookup_url || "journey.duty.meursing",
  }];
}

function rejectedCandidates(state: JourneyState): Record<string, unknown>[] {
  const picked = state.finalCommodity?.commodity_code;
  return (state.fixedCandidates || [])
    .filter((c: any) => c.commodity_code && c.commodity_code !== picked)
    .slice(0, 5)
    .map((c: any) => ({
      code: c.commodity_code,
      description: c.description || "",
      reason: "Not selected after classification Q&A",
    }));
}

function parseOptionalNonNegative(raw: string | number): number | null | undefined {
  if (raw === "") return null;
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n) || n < 0) return undefined;
  return n;
}

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Builds a self-contained, light-theme (black-on-white) HTML document for the
// "Print summary" button - opened via window.open, prints itself on load.
function buildPrintHtml(opts: {
  state: JourneyState;
  result: DeclarationResult;
  commodity: NonNullable<JourneyState["finalCommodity"]>;
  duty: DutyResult;
  landed: LandedResult;
  description: string;
}): string {
  const { state, result, commodity, duty, landed, description } = opts;
  const qaHistory = result.audit_summary?.qa_history?.length ? result.audit_summary.qa_history : state.qaHistory;
  const originalQuery = result.audit_summary?.original_query || state.query;
  const warnings = duty.warnings ?? [];
  const generatedAt = new Date().toLocaleString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  const moneyRow = (labelHtml: string, value: number, rowClass = "") =>
    `<tr${rowClass ? ` class="${rowClass}"` : ""}><td>${labelHtml}</td><td class="num">${escapeHtml(money(value))}</td></tr>`;

  const exciseLabel = duty.excise_detail?.label || duty.excise?.band_label;
  const costRows = [
    moneyRow("Customs value", result.summary.customs_value_gbp),
    moneyRow(`Customs duty <span class="muted">(${escapeHtml(duty.rate_expression)})</span>`, result.summary.duty_gbp),
    moneyRow(`Excise duty${exciseLabel ? ` <span class="muted">(${escapeHtml(exciseLabel)})</span>` : ""}`, result.summary.excise_gbp),
  ];
  if (duty.excise_detail && duty.excise_detail.components.length > 1) {
    for (const c of duty.excise_detail.components) {
      costRows.push(
        `<tr><td class="muted" style="padding-left: 16px">${escapeHtml(c.label)}</td><td class="num muted">${escapeHtml(money(c.amount_gbp))}</td></tr>`
      );
    }
  }
  costRows.push(
    moneyRow(`VAT <span class="muted">(at ${duty.vat_rate}%)</span>`, result.summary.vat_gbp),
    moneyRow("Total taxes", result.summary.total_taxes_gbp, "total"),
    moneyRow("Total import cost", landed.total_landed_cost_gbp, "total")
  );
  if (landed.cash_at_border_gbp != null) {
    costRows.push(moneyRow("Cash payable at the border", landed.cash_at_border_gbp));
  }
  if (landed.total_tax_liability_gbp != null) {
    costRows.push(moneyRow("Total tax liability (duty + excise + VAT)", landed.total_tax_liability_gbp));
  }

  const vatTreatmentNote =
    landed.vat_treatment === "postponed"
      ? "VAT is postponed to your VAT return (postponed VAT accounting), so it is not paid at the border."
      : landed.vat_treatment === "supply_vat"
        ? "VAT is collected as supply VAT at the point of sale (GBP 135 low-value rules), not as import VAT at the border."
        : "";

  const warningsHtml = warnings
    .map(
      (w) =>
        `<div class="warn"><strong>${escapeHtml(w.kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()))}:</strong> ${escapeHtml(w.message)}${
          w.rate_range ? ` <span class="muted">(${escapeHtml(w.rate_range)})</span>` : ""
        }</div>`
    )
    .join("");

  const qaHtml = qaHistory.length
    ? `<ol>${qaHistory
        .map((q) => `<li>${escapeHtml(q.question)}<br /><strong>${escapeHtml(q.answer ?? "")}</strong></li>`)
        .join("")}</ol>`
    : `<p class="muted">No clarifying questions were needed.</p>`;

  const documentsHtml = result.required_document_codes.length
    ? `<ul class="checklist">${result.required_document_codes
        .map(
          (d) =>
            `<li><span class="box"></span><span class="mono">${escapeHtml(d.code)}</span> ${escapeHtml(d.description)}${
              d.inherited && d.attached_at ? ` <span class="muted">(applies to all goods under code ${escapeHtml(d.attached_at)})</span>` : ""
            }</li>`
        )
        .join("")}</ul>`
    : `<p class="muted">No document codes were identified for this commodity and origin.</p>`;

  const nextStepsHtml = result.next_steps.length
    ? `<ol>${result.next_steps.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol>`
    : `<p class="muted">No next steps were returned.</p>`;

  const scenarioLine = `Imported from ${duty.country_name || state.countryOfOrigin}${state.importDate ? `, import date ${state.importDate}` : ""}`;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Import summary - ${escapeHtml(commodity.commodity_code)}</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #111827; background: #ffffff; max-width: 720px; margin: 32px auto; padding: 0 16px; font-size: 14px; line-height: 1.5; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 15px; margin: 24px 0 6px; padding-bottom: 4px; border-bottom: 1px solid #9ca3af; }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 4px 12px 4px 0; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; padding-right: 0; }
  tr.total td { border-top: 2px solid #111827; border-bottom: none; font-weight: 700; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .muted { color: #4b5563; font-size: 12px; }
  .warn { border-left: 4px solid #b91c1c; padding: 4px 10px; margin: 8px 0; font-size: 13px; }
  ol, ul { margin: 4px 0; padding-left: 20px; }
  ol li { margin: 4px 0; }
  ul.checklist { list-style: none; padding-left: 0; }
  ul.checklist li { margin: 6px 0; }
  .box { display: inline-block; width: 11px; height: 11px; border: 1.5px solid #111827; margin-right: 8px; }
  footer { margin-top: 28px; padding-top: 8px; border-top: 1px solid #9ca3af; }
  @media print { body { margin: 0; max-width: none; } }
</style>
</head>
<body>
<h1>Import summary</h1>
<p class="muted">${escapeHtml(scenarioLine)}</p>

<h2>Goods</h2>
<table>
  <tr><td>Description</td><td>${escapeHtml(description || "-")}</td></tr>
  <tr><td>Commodity code</td><td><span class="mono">${escapeHtml(commodity.commodity_code)}</span><br /><span class="muted">${escapeHtml(commodity.description)}</span></td></tr>
</table>

<h2>How the code was chosen (Q&amp;A audit)</h2>
${originalQuery ? `<p class="muted">Original description: "${escapeHtml(originalQuery)}"</p>` : ""}
${qaHtml}

<h2>Costs</h2>
<table>
${costRows.join("\n")}
</table>
${vatTreatmentNote ? `<p class="muted">${escapeHtml(vatTreatmentNote)}</p>` : ""}
${warningsHtml}

<h2>Required documents</h2>
${documentsHtml}

<h2>Next steps</h2>
${nextStepsHtml}

<footer class="muted">
Generated ${escapeHtml(generatedAt)}. Demo only - not a live HMRC service; figures are estimates, not a binding ruling.
</footer>
<script>window.addEventListener("load", function () { window.print(); });</script>
</body>
</html>`;
}
