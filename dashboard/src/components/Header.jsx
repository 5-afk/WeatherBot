import { useAtlasStore } from "../store/AtlasContext";
import { Stat } from "./ui/Stat";
import { Badge } from "./ui/Badge";
import { fmtPnl, fmtUsd, pnlClass } from "../utils/format";

function LiveDot({ status }) {
  const cls =
    status === "running"
      ? "bg-green shadow-[var(--green-glow)] animate-pulse"
      : status === "paused"
        ? "bg-amber"
        : status === "stale"
          ? "bg-red animate-pulse"
          : "bg-red";
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} />;
}

export default function Header() {
  const { state, dispatch } = useAtlasStore();
  const s = state.status || {};
  const proc = (s.bot_process_status || "stopped").toLowerCase();
  const isLive = s.mode === "LIVE";
  const offline = state.connection === "down";

  return (
    <header className="h-12 flex items-center justify-between px-4 border-b border-border bg-[rgba(8,8,8,0.95)] backdrop-blur shrink-0 z-20">
      <div className="flex items-center gap-3">
        <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden>
          <polygon points="12,2 22,8 22,16 12,22 2,16 2,8" fill="none" stroke="var(--forge)" strokeWidth="1.5" />
        </svg>
        <span className="font-mono font-bold tracking-widest text-sm">ATLAS</span>
        <LiveDot status={proc} />
        <Badge variant={proc === "running" ? "live" : proc === "paused" ? "paused" : proc === "stale" ? "stale" : "stopped"}>
          {proc.toUpperCase()}
        </Badge>
      </div>

      <div className={`flex items-center gap-4 ${offline ? "offline-dim" : ""}`}>
        <Stat label="Balance" value={s.portfolio_value} format={fmtUsd} className="text-cyan" />
        <Stat label="P&L" value={s.daily_pnl} format={fmtPnl} className={pnlClass(s.daily_pnl)} />
        <Stat label="Positions" value={`${s.open_positions ?? "—"}/${s.max_positions ?? "—"}`} />
        <Badge variant={isLive ? "live" : "dry"}>{s.mode || "…"}</Badge>
      </div>

      <button
        className="px-3 py-1.5 text-xs font-semibold uppercase border border-red/50 text-red rounded hover:shadow-[var(--red-glow)] transition-shadow min-h-[32px]"
        onClick={() => dispatch({ type: "SET_UI", payload: { killswitchConfirm: true } })}
      >
        KILL
      </button>
    </header>
  );
}
