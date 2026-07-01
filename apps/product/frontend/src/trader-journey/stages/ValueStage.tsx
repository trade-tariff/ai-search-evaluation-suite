import { useMemo, useState } from "react";
import { api } from "../api";
import { Money } from "../components/Money";
import type { JourneyState, ValuationGuideResult, ValuationResult } from "../types";

interface Props {
  state: JourneyState;
  update: (patch: Partial<JourneyState>) => void;
  onNext: () => void;
  onBack: () => void;
}

type Mode = "known" | "guided" | null;
type Phase = "edit" | "review" | "result";

const CURRENCIES = [
  { code: "GBP", rate: 1.0 },
  { code: "EUR", rate: 0.86 },
  { code: "USD", rate: 0.79 },
  { code: "JPY", rate: 0.0053 },
  { code: "CNY", rate: 0.11 },
  { code: "INR", rate: 0.0095 },
];

const FX_RATE_NOTE = "Estimated exchange rate - check HMRC's published exchange rate for your import month before you submit your declaration.";

const METHOD_FIELDS: Record<string, { key: string; label: string; hint?: string }[]> = {
  method_1_transaction_value: [
    { key: "invoice_value", label: "Invoice value" },
    { key: "freight_gbp", label: "Freight to UK border (GBP)" },
    { key: "insurance_gbp", label: "Insurance to UK border (GBP)" },
    { key: "other_costs_gbp", label: "Other additions (GBP)" },
  ],
  method_2_identical_goods: [
    { key: "accepted_identical_value_gbp", label: "HMRC-accepted customs value for identical goods (GBP)" },
    { key: "quantity_adjustment_gbp", label: "Quantity adjustment (GBP)" },
    { key: "commercial_level_adjustment_gbp", label: "Commercial level adjustment (GBP)" },
    { key: "transport_adjustment_gbp", label: "Transport adjustment (GBP)" },
  ],
  method_3_similar_goods: [
    { key: "accepted_similar_value_gbp", label: "HMRC-accepted customs value for similar goods (GBP)" },
    { key: "quantity_adjustment_gbp", label: "Quantity adjustment (GBP)" },
    { key: "commercial_level_adjustment_gbp", label: "Commercial level adjustment (GBP)" },
    { key: "transport_adjustment_gbp", label: "Transport adjustment (GBP)" },
  ],
  method_4_deductive: [
    { key: "uk_resale_price_gbp", label: "UK resale unit price in greatest aggregate quantity (GBP)" },
    { key: "commissions_or_profit_gbp", label: "Usual commission, profit, and general expenses to deduct (GBP)" },
    { key: "uk_transport_gbp", label: "UK transport/handling after import to deduct (GBP)" },
    { key: "uk_duties_taxes_gbp", label: "UK duties and taxes to deduct (GBP)" },
  ],
  method_5_computed: [
    { key: "materials_gbp", label: "Materials (GBP)" },
    { key: "manufacturing_gbp", label: "Manufacturing (GBP)" },
    { key: "producer_profit_gbp", label: "Producer profit and general expenses (GBP)" },
    { key: "packing_gbp", label: "Packing (GBP)" },
    { key: "transport_to_import_gbp", label: "Transport to UK border (GBP)" },
  ],
  method_6_fallback: [
    { key: "reasonable_base_value_gbp", label: "Reasonable base value (GBP)" },
    { key: "adjustments_gbp", label: "Adjustments (GBP)" },
  ],
};

function initialValuationFlags(state: JourneyState) {
  const method =
    state.valuationGuideResult?.choice?.method_code ||
    state.valuationMethodCode ||
    "method_1_transaction_value";
  const flags = {
    has_sale_for_export: true,
    has_usable_transaction_value: true,
    has_identical_goods_value: false,
    has_similar_goods_value: false,
    has_uk_resale_price: false,
    has_production_costs: false,
    try_computed_before_deductive: false,
  };
  if (method !== "method_1_transaction_value") {
    flags.has_sale_for_export = false;
    flags.has_usable_transaction_value = false;
  }
  if (method === "method_2_identical_goods") flags.has_identical_goods_value = true;
  if (method === "method_3_similar_goods") flags.has_similar_goods_value = true;
  if (method === "method_4_deductive") flags.has_uk_resale_price = true;
  if (method === "method_5_computed") {
    flags.has_production_costs = true;
    flags.try_computed_before_deductive = Boolean(state.valuationGuideResult?.choice?.method_code === "method_5_computed");
  }
  return flags;
}

function initialValuationInputs(state: JourneyState): Record<string, string> {
  const inputs: Record<string, string> = {
    invoice_value: state.invoiceValue !== null ? String(state.invoiceValue) : "",
    freight_gbp: String(state.freightGbp || 0),
    insurance_gbp: String(state.insuranceGbp || 0),
    other_costs_gbp: String(state.otherCostsGbp || 0),
  };
  const breakdown = state.valuationResult?.breakdown || {};
  const aliases: Record<string, string> = {
    invoice_in_gbp: "invoice_value",
    less_commissions_or_profit_gbp: "commissions_or_profit_gbp",
    less_uk_transport_gbp: "uk_transport_gbp",
    less_uk_duties_taxes_gbp: "uk_duties_taxes_gbp",
  };
  for (const [key, value] of Object.entries(breakdown)) {
    if (key === "customs_value_gbp") continue;
    const target = aliases[key] || key;
    if (typeof value === "number" && Number.isFinite(value)) {
      inputs[target] = String(value);
    }
  }
  return inputs;
}

function initialKnownCustomsValue(state: JourneyState): string {
  if (state.customsValueGbp !== null) return String(state.customsValueGbp);
  const seed = state.selectedExample?.seed || {};
  const usingSeed = state.invoiceValue === null;
  const invoiceValue = state.invoiceValue ?? seed.invoice_value ?? null;
  if (invoiceValue === null) return "";
  const cifInvoice = state.invoiceIncludesFreight === true;
  const seededTotal =
    Number(invoiceValue) +
    (cifInvoice ? 0 : Number(usingSeed ? (seed.freight_gbp ?? 0) : state.freightGbp)) +
    (cifInvoice ? 0 : Number(usingSeed ? (seed.insurance_gbp ?? 0) : state.insuranceGbp)) +
    Number(usingSeed ? (seed.other_costs_gbp ?? 0) : state.otherCostsGbp);
  return Number.isFinite(seededTotal) && seededTotal > 0 ? String(seededTotal) : "";
}

export default function ValueStage({ state, update, onNext, onBack }: Props) {
  const [mode, setMode] = useState<Mode>(
    state.knowsCustomsValue === null ? null : state.knowsCustomsValue ? "known" : "guided"
  );
  const [phase, setPhase] = useState<Phase>(state.valuationResult ? "result" : "edit");
  const [knownValue, setKnownValue] = useState<string>(
    initialKnownCustomsValue(state)
  );
  const [currency, setCurrency] = useState(state.invoiceCurrency);
  const [fx, setFx] = useState(state.fxRateToGbp);
  const [flags, setFlags] = useState({
    ...initialValuationFlags(state),
  });
  const [inputs, setInputs] = useState<Record<string, string>>(initialValuationInputs(state));
  const [includesFreight, setIncludesFreight] = useState<boolean>(state.invoiceIncludesFreight ?? false);
  const [result, setResult] = useState<ValuationResult | null>(state.valuationResult);
  const [guide, setGuide] = useState<ValuationGuideResult | null>(state.valuationGuideResult);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const methodCode = useMemo(() => chooseMethod(flags), [flags]);
  const fields = METHOD_FIELDS[methodCode] || METHOD_FIELDS.method_6_fallback;

  function pickCurrency(code: string) {
    setCurrency(code);
    const found = CURRENCIES.find((c) => c.code === code);
    if (found) setFx(found.rate);
  }

  function chooseKnownMode() {
    setMode("known");
    if (!knownValue) setKnownValue(initialKnownCustomsValue(state));
  }

  function setInput(key: string, value: string) {
    setInputs((prev) => ({ ...prev, [key]: value }));
  }

  function answerIncoterms(value: boolean) {
    setIncludesFreight(value);
    update({ invoiceIncludesFreight: value });
  }

  function invalidateValuationAndDownstream() {
    setResult(null);
    setGuide(null);
    update({
      customsValueGbp: null,
      valuationResult: null,
      valuationGuideResult: null,
      valuationMethodCode: null,
      dutyResult: null,
      dutyInference: null,
      dutyExplainerText: null,
      landedResult: null,
      declarationResult: null,
      filingIntent: null,
    });
  }

  function reworkValuation() {
    invalidateValuationAndDownstream();
    setPhase("edit");
  }

  function review() {
    setError(null);
    const knownParsed = parsePositiveNumber(knownValue);
    if (mode === "known" && knownParsed === null) {
      setError("Enter a customs value above zero.");
      return;
    }
    if (mode === "guided") {
      const missing = fields.filter(
        (f) =>
          !(includesFreight && isFreightOrInsurance(f.key)) &&
          !isSignedAdjustment(f.key) &&
          (inputs[f.key] ?? "") === ""
      );
      if (missing.length) {
        setError(`Please enter an amount for: ${missing.map((m) => m.label).join(", ")}`);
        return;
      }
      const invalid = fields.filter((f) => {
        if (includesFreight && isFreightOrInsurance(f.key)) return false;
        const raw = inputs[f.key] ?? "";
        if (raw === "" && isSignedAdjustment(f.key)) return false;
        const parsed = parseFiniteNumber(raw);
        if (parsed === null) return true;
        return !isSignedAdjustment(f.key) && parsed < 0;
      });
      if (invalid.length) {
        setError(`Invalid valuation inputs: ${invalid.map((m) => m.label).join(", ")}`);
        return;
      }
      if (currency !== "GBP" && (!Number.isFinite(fx) || fx <= 0)) {
        setError("Enter an exchange rate above zero.");
        return;
      }
    }
    update({ knowsCustomsValue: mode === "known" });
    setPhase("review");
  }

  async function calculate() {
    if (!mode) return;
    setLoading(true);
    setError(null);
    try {
      let res: ValuationResult;
      let guideRes: ValuationGuideResult | null = null;
      if (mode === "known") {
        const parsedKnown = parsePositiveNumber(knownValue);
        if (parsedKnown === null) throw new Error("Enter a customs value above zero.");
        res = await api.valuation({ known_customs_value_gbp: parsedKnown });
      } else {
        const methodInputs = normalizeInputs(inputs);
        methodInputs.invoice_currency = currency;
        methodInputs.fx_rate_to_gbp = fx;
        if (includesFreight) {
          methodInputs.freight_gbp = 0;
          methodInputs.insurance_gbp = 0;
        }
        guideRes = await api.valuationGuide({ ...flags, inputs: methodInputs });
        if (!guideRes.result) {
          throw new Error(guideRes.notes.join(" ") || "The selected valuation method is missing inputs.");
        }
        res = guideRes.result;
      }
      setResult(res);
      setGuide(guideRes);
      update({
        knowsCustomsValue: mode === "known",
        invoiceIncludesFreight: includesFreight,
        invoiceValue: finiteOrNull(inputs.invoice_value),
        invoiceCurrency: currency,
        fxRateToGbp: fx,
        freightGbp: includesFreight ? 0 : finiteOrZero(inputs.freight_gbp),
        insuranceGbp: includesFreight ? 0 : finiteOrZero(inputs.insurance_gbp),
        otherCostsGbp: finiteOrZero(inputs.other_costs_gbp),
        customsValueGbp: res.customs_value_gbp,
        valuationResult: res,
        valuationGuideResult: guideRes,
        valuationMethodCode: res.method_code,
        dutyResult: null,
        dutyInference: null,
        dutyExplainerText: null,
        landedResult: null,
        declarationResult: null,
        filingIntent: null,
      });
      setPhase("result");
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold mb-2">2. Customs value</h2>
        <p className="text-gray-400 mb-4">
          Tell us what your goods are worth for customs. Enter the value if you already know it, or answer a few
          questions and we will work it out using HMRC's valuation methods.{" "}
          <a
            href="https://www.gov.uk/government/collections/working-out-the-customs-value-of-your-imported-goods"
            target="_blank"
            rel="noreferrer"
            className="underline text-blue-400 hover:text-blue-300"
          >
            Read GOV.UK guidance on working out the customs value (opens in new tab)
          </a>
        </p>
      </div>

      {error && (
        <div className="border-l-4 border-red-500 bg-gray-900 p-4">
          <strong>Error:</strong> {error}
        </div>
      )}

      {phase === "edit" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="tj-card space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <button
                className={`border-2 p-4 text-left ${mode === "known" ? "border-emerald-500 bg-gray-900" : "border-gray-700 bg-gray-900 hover:border-gray-500"}`}
                onClick={chooseKnownMode}
              >
                <div className="font-bold">I know the customs value</div>
                <div className="text-xs text-gray-400 mt-1">Use the stated value and skip valuation-method maths.</div>
              </button>
              <button
                className={`border-2 p-4 text-left ${mode === "guided" ? "border-emerald-500 bg-gray-900" : "border-gray-700 bg-gray-900 hover:border-gray-500"}`}
                onClick={() => setMode("guided")}
              >
                <div className="font-bold">Help me work it out</div>
                <div className="text-xs text-gray-400 mt-1">Walk through the six valuation methods in order.</div>
              </button>
            </div>

            {mode === "known" && (
              <div className="space-y-4">
                <IncotermsQuestion value={includesFreight} onChange={answerIncoterms} />
                <div>
                  <label className="tj-label" htmlFor="known-customs-value-gbp">Known customs value (GBP)</label>
                  <input
                    id="known-customs-value-gbp"
                    type="number"
                    inputMode="decimal"
                    className="tj-input max-w-xs"
                    value={knownValue}
                    onChange={(e) => setKnownValue(e.target.value)}
                    min="0"
                    step="0.01"
                  />
                  <p className="tj-hint mt-1">Usually the price paid for the goods plus freight and insurance to the UK border.</p>
                </div>
              </div>
            )}

            {mode === "guided" && (
              <div className="space-y-4">
                <p className="tj-hint">
                  Tick the statements that are true for your import. We will select the first valuation method that
                  fits, in the order HMRC requires.
                </p>
                <ValuationQuestions flags={flags} setFlags={setFlags} />
                <div className="border-t border-gray-700 pt-4">
                  <div className="text-xs font-bold tracking-widest uppercase text-emerald-400 mb-2">
                    Selected method
                  </div>
                  <div className="font-semibold">{methodLabel(methodCode)}</div>
                  {methodCode === "method_1_transaction_value" && (
                    <div className="mt-3">
                      <label className="tj-label" htmlFor="invoice-currency">Invoice currency</label>
                      <div className="flex gap-2">
                        <select id="invoice-currency" className="tj-input w-32" value={currency} onChange={(e) => pickCurrency(e.target.value)}>
                          {CURRENCIES.map((c) => <option key={c.code} value={c.code}>{c.code}</option>)}
                        </select>
                        {currency !== "GBP" && (
                          <div className="flex-1 min-w-56">
                            <label className="tj-label" htmlFor="invoice-fx-rate">Exchange rate to GBP</label>
                            <input
                              id="invoice-fx-rate"
                              type="number"
                              className="tj-input max-w-xs"
                              value={fx}
                              step="0.0001"
                              onChange={(e) => setFx(Number(e.target.value))}
                            />
                            <div className="text-xs text-gray-400 mt-1">{FX_RATE_NOTE}</div>
                          </div>
                        )}
                      </div>
                      <div className="mt-3">
                        <IncotermsQuestion value={includesFreight} onChange={answerIncoterms} />
                      </div>
                    </div>
                  )}
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
                    {fields.map((f) => {
                      const coveredByInvoice = includesFreight && isFreightOrInsurance(f.key);
                      return (
                        <div key={f.key}>
                          <label className="tj-label" htmlFor={`valuation-${f.key}`}>{f.label}</label>
                          <input
                            id={`valuation-${f.key}`}
                            type="number"
                            inputMode="decimal"
                            className="tj-input"
                            value={coveredByInvoice ? "0" : inputs[f.key] ?? ""}
                            onChange={(e) => setInput(f.key, e.target.value)}
                            disabled={coveredByInvoice}
                            min={f.key.includes("adjustment") ? undefined : "0"}
                            step="0.01"
                          />
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}

            <div className="flex justify-between pt-2">
              <button onClick={onBack} className="tj-btn-secondary">&larr; Back</button>
              <div className="flex items-center gap-3">
                {!mode && <span className="text-xs text-gray-400">Choose an option above to continue</span>}
                <button onClick={review} className="tj-btn" disabled={!mode}>Review answers</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {phase === "review" && (
        <div className="tj-card max-w-3xl">
          <h3 className="text-xl font-bold mb-2">Review customs value answers</h3>
          <ReviewRows rows={reviewRows(mode, knownValue, methodCode, inputs, currency, fx, includesFreight)} />
          <div className="flex justify-between mt-6">
            <button onClick={reworkValuation} className="tj-btn-secondary">&larr; Change answers</button>
            <button onClick={calculate} className="tj-btn" disabled={loading}>
              {loading ? "Calculating..." : "Calculate customs value"}
            </button>
          </div>
        </div>
      )}

      {phase === "result" && result && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="tj-card">
            <h3 className="text-lg font-bold mb-3">Customs value</h3>
            <div className="text-4xl font-bold tabular-nums mb-4 text-emerald-400">
              <Money value={result.customs_value_gbp} />
            </div>
            <table className="w-full text-sm">
              <tbody>
                {Object.entries(result.breakdown).map(([k, v]) => (
                  <tr key={k} className={k === "customs_value_gbp" ? "font-bold border-t-2 border-gray-600" : ""}>
                    <td className="py-1 text-gray-400">{prettyKey(k)}</td>
                    <td className="py-1 text-right tabular-nums"><Money value={v} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {guide && <p className="text-xs text-gray-400 mt-3">{guide.choice.reason}</p>}
            {result.notes.length > 0 && (
              <ul className="mt-4 text-xs text-gray-400 list-disc list-inside space-y-1">
                {result.notes.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            )}
            <p className="text-xs italic text-gray-400 mt-3">{result.method}</p>
          </div>
          <div className="flex items-end justify-between gap-3">
            <div className="flex flex-col gap-1">
              <button onClick={reworkValuation} className="tj-btn-secondary">&larr; Change answers</button>
              <span className="text-xs text-gray-500">This resets the duty, import cost and declaration steps.</span>
            </div>
            <button onClick={onNext} className="tj-btn">Next: Duty details &rarr;</button>
          </div>
        </div>
      )}
    </div>
  );
}

function IncotermsQuestion({ value, onChange }: { value: boolean; onChange: (value: boolean) => void }) {
  return (
    <div>
      <div className="tj-label">
        Does your invoice price already include freight and insurance to the UK? (e.g. CIF terms)
      </div>
      <div className="space-y-2 mt-2">
        <label className="flex items-center gap-2">
          <input type="radio" checked={value} onChange={() => onChange(true)} className="h-4 w-4" />
          <span>Yes</span>
        </label>
        <label className="flex items-center gap-2">
          <input type="radio" checked={!value} onChange={() => onChange(false)} className="h-4 w-4" />
          <span>No</span>
        </label>
      </div>
      {value && <p className="tj-hint mt-2">Included in your invoice price - we won't add them again.</p>}
    </div>
  );
}

function ValuationQuestions({ flags, setFlags }: any) {
  const rows = [
    ["has_sale_for_export", "There is a sale for export to the UK."],
    ["has_usable_transaction_value", "The price actually paid or payable is usable after valuation adjustments."],
    ["has_identical_goods_value", "I know an HMRC-accepted value for IDENTICAL goods (same in all respects) from the same country, imported at about the same time. Unlocks Method 2."],
    ["has_similar_goods_value", "I know an HMRC-accepted value for SIMILAR goods (not identical, but comparable and interchangeable) from the same country, imported at about the same time. Unlocks Method 3."],
    ["has_production_costs", "The producer can provide materials, manufacturing, profit/general expenses, packing, and transport evidence."],
    ["try_computed_before_deductive", "I want to use Method 5 (computed value) before Method 4 (deductive value)."],
    ["has_uk_resale_price", "The imported, identical, or similar goods are sold in the UK in the condition imported, to unrelated buyers."],
  ];
  return (
    <div className="space-y-2">
      {rows.map(([key, label]) => (
        <label key={key} className="flex items-center justify-between gap-3 border border-gray-700 px-3 py-2">
          <span>{label}</span>
          <input
            type="checkbox"
            checked={Boolean(flags[key])}
            onChange={(e) => setFlags((prev: any) => ({ ...prev, [key]: e.target.checked }))}
            className="h-4 w-4"
          />
        </label>
      ))}
    </div>
  );
}

function chooseMethod(flags: any): string {
  if (flags.has_sale_for_export && flags.has_usable_transaction_value) return "method_1_transaction_value";
  if (flags.has_identical_goods_value) return "method_2_identical_goods";
  if (flags.has_similar_goods_value) return "method_3_similar_goods";
  if (flags.try_computed_before_deductive && flags.has_production_costs) return "method_5_computed";
  if (flags.has_uk_resale_price) return "method_4_deductive";
  if (flags.has_production_costs) return "method_5_computed";
  return "method_6_fallback";
}

function methodLabel(code: string): string {
  return {
    method_1_transaction_value: "Method 1: transaction value",
    method_2_identical_goods: "Method 2: identical goods",
    method_3_similar_goods: "Method 3: similar goods",
    method_4_deductive: "Method 4: deductive value",
    method_5_computed: "Method 5: computed value",
    method_6_fallback: "Method 6: fallback value",
  }[code] || code;
}

function normalizeInputs(inputs: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(inputs)) out[k] = finiteOrZero(v);
  return out;
}

function parseFiniteNumber(raw: string | number | null | undefined): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(n) ? n : null;
}

function parsePositiveNumber(raw: string | number | null | undefined): number | null {
  const n = parseFiniteNumber(raw);
  return n !== null && n > 0 ? n : null;
}

function finiteOrNull(raw: string | number | null | undefined): number | null {
  const n = parseFiniteNumber(raw);
  return n === null ? null : n;
}

function finiteOrZero(raw: string | number | null | undefined): number {
  return parseFiniteNumber(raw) ?? 0;
}

function isSignedAdjustment(key: string): boolean {
  return key.includes("adjustment") || key === "adjustments_gbp";
}

function isFreightOrInsurance(key: string): boolean {
  return key === "freight_gbp" || key === "insurance_gbp";
}

function reviewRows(mode: Mode, known: string, method: string, inputs: Record<string, string>, currency: string, fx: number, includesFreight: boolean) {
  const incotermsRow = { label: "Invoice price already includes freight and insurance to the UK", value: includesFreight ? "Yes" : "No" };
  if (mode === "known") {
    return [{ label: "Known customs value", value: `GBP ${Number(known || 0).toFixed(2)}` }, incotermsRow];
  }
  const rows = [{ label: "Valuation method", value: methodLabel(method) }];
  if (method === "method_1_transaction_value") {
    rows.push({ label: "Invoice currency", value: currency === "GBP" ? "GBP" : `${currency} (exchange rate ${fx} to GBP)` });
    rows.push(incotermsRow);
  }
  for (const f of METHOD_FIELDS[method] || []) {
    const coveredByInvoice = includesFreight && isFreightOrInsurance(f.key);
    rows.push({ label: f.label, value: coveredByInvoice ? "0 (included in invoice price)" : inputs[f.key] || "0" });
  }
  return rows;
}

function ReviewRows({ rows }: { rows: { label: string; value: string }[] }) {
  return (
    <table className="w-full text-sm mt-3">
      <tbody>
        {rows.map((r) => (
          <tr key={r.label} className="border-b border-gray-800">
            <td className="py-2 text-gray-400 w-1/2">{r.label}</td>
            <td className="py-2 font-mono">{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function prettyKey(k: string): string {
  return k.replace(/_/g, " ").replace(/gbp/i, "(GBP)").replace(/^./, (m) => m.toUpperCase());
}
