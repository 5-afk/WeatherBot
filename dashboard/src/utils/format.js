export const fmtUsd = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);
export const fmtPct = (v) => (v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`);
export const fmtPnl = (v) => {
  if (v == null) return "—";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}$${n.toFixed(2)}`;
};
export const pnlClass = (v) => (v == null ? "" : Number(v) >= 0 ? "text-profit" : "text-loss");
