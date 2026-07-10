export const fmtUsd = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);
export const fmtPct = (v) => (v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`);
export const fmtTemp = (v) => (v == null ? "—" : `${Number(v).toFixed(1)}°F`);
export const fmtPnl = (v) => {
  if (v == null) return "—";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}$${n.toFixed(2)}`;
};
export const pnlClass = (v) => (v == null ? "" : Number(v) >= 0 ? "text-green" : "text-red");

export function formatCountdown(seconds) {
  if (seconds == null || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}
