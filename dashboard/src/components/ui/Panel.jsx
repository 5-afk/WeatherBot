export function Panel({ title, right, children, className = "" }) {
  return (
    <div className={`panel-zone h-full ${className}`}>
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-2 font-ui">{title}</span>
        {right}
      </div>
      <div className="panel-scroll scrollbar p-3">{children}</div>
    </div>
  );
}
