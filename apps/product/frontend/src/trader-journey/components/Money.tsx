export function money(n: number, currency = "GBP"): string {
  if (!Number.isFinite(n)) return "-";
  return new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(n);
}

export function Money({ value, currency = "GBP", className = "" }: { value: number; currency?: string; className?: string }) {
  return <span className={`tabular-nums ${className}`}>{money(value, currency)}</span>;
}
