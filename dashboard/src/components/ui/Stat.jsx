import { useLiveValue } from "../../hooks/useLiveValue";

export function Stat({ label, value, format, className = "" }) {
  const pulse = useLiveValue(value);
  const display = format ? format(value) : value ?? "—";
  return (
    <div className="text-center px-2">
      <div className="text-[10px] uppercase tracking-wider text-text-3 mb-0.5">{label}</div>
      <div className={`font-mono tabular-nums text-sm font-semibold ${pulse} ${className}`}>{display}</div>
    </div>
  );
}
