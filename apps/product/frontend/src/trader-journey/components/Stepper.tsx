import { Stage } from "../TraderJourneyApp";

const STAGES: { key: Stage; label: string; subtitle: string }[] = [
  { key: "classify", label: "Classify", subtitle: "Find the commodity code" },
  { key: "value", label: "Value", subtitle: "Calculate customs value" },
  { key: "duty", label: "Duty details", subtitle: "Origin, duty rates and VAT" },
  { key: "landed", label: "Import costs", subtitle: "Duty, VAT and total" },
  { key: "declare", label: "Declare", subtitle: "Draft customs declaration (CDS)" },
];

interface Props {
  current: Stage;
  completed: Set<Stage>;
  onJump: (s: Stage) => void;
}

export default function Stepper({ current, completed, onJump }: Props) {
  return (
    <ol className="grid grid-cols-5 border-y border-gray-700 bg-gray-900">
      {STAGES.map((s, i) => {
        const isCurrent = current === s.key;
        const isDone = completed.has(s.key);
        const isClickable = isDone || isCurrent;
        return (
          <li
            key={s.key}
            className={`relative px-5 py-4 ${
              i < STAGES.length - 1 ? "border-r border-gray-700" : ""
            } ${isCurrent ? "bg-gray-800" : ""} ${
              isClickable ? "cursor-pointer hover:bg-gray-800" : "opacity-60 cursor-not-allowed"
            }`}
            onClick={() => isClickable && onJump(s.key)}
            onKeyDown={(e) => {
              if (isClickable && (e.key === "Enter" || e.key === " ")) onJump(s.key);
            }}
            role="button"
            tabIndex={isClickable ? 0 : -1}
            aria-disabled={!isClickable}
            title={isClickable ? undefined : "Finish the earlier steps to unlock this one"}
          >
            <div className="flex items-start gap-3">
              <span
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-sm font-bold ${
                  isDone
                    ? "bg-emerald-600 text-white"
                    : isCurrent
                    ? "bg-blue-600 text-white"
                    : "bg-gray-700 text-gray-100"
                }`}
              >
                {isDone ? "✓" : i + 1}
              </span>
              <div className="min-w-0">
                <div className="font-semibold text-sm text-gray-100 truncate">
                  {s.label}
                </div>
                <div className="text-xs text-gray-400 truncate">{s.subtitle}</div>
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
