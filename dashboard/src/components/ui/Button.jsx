export function Button({ children, variant = "default", pending, disabled, className = "", ...props }) {
  const base =
    "relative inline-flex items-center justify-center px-3 py-1.5 rounded text-xs font-semibold uppercase tracking-wide transition-colors disabled:opacity-40 min-h-[32px]";
  const styles = {
    default: "bg-surface-2 border border-border text-text hover:border-border-2",
    primary: "bg-cyan/15 border border-cyan/40 text-cyan hover:bg-cyan/25",
    danger: "bg-red/10 border border-red/40 text-red hover:shadow-[var(--red-glow)]",
    warn: "bg-amber/10 border border-amber/40 text-amber",
  };
  return (
    <button className={`${base} ${styles[variant] || styles.default} ${className}`} disabled={disabled || pending} {...props}>
      {pending ? "…" : children}
    </button>
  );
}
