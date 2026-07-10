const variants = {
  live: "bg-green/15 text-green border-green/40",
  dry: "bg-amber/15 text-amber border-amber/40",
  paused: "bg-amber/15 text-amber border-amber/40",
  stopped: "bg-red/15 text-red border-red/40",
  stale: "bg-red/15 text-red border-red/40 animate-pulse",
  default: "bg-surface-2 text-text-2 border-border",
};

export function Badge({ children, variant = "default" }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold uppercase border ${variants[variant] || variants.default}`}
    >
      {children}
    </span>
  );
}
