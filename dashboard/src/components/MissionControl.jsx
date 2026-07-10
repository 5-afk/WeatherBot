import FlashValue from "./FlashValue";
import ScanCountdown from "./ScanCountdown";
import { useAtlasStore } from "../store/AtlasContext";
import { fmtPnl, fmtUsd, pnlClass } from "../utils/format";

export default function MissionControl() {
  const { state, dispatch, control } = useAtlasStore();
  const s = state.status || {};
  const isLive = s.mode === "LIVE";
  const pending = state.controls?.pending;
  const disabled = Boolean(pending);

  return (
    <header className="panel" style={{ borderRadius: 0, borderLeft: 0, borderRight: 0, borderTop: 0 }}>
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center gap-4">
          <h1 className="mono text-lg font-semibold tracking-wider">
            <span className="text-data">ATLAS</span> COMMAND CENTER
          </h1>
          <span className={`badge ${isLive ? "badge-live" : "badge-dry"}`}>{s.mode || "…"}</span>
          {s.killswitch && <span className="badge badge-error">KILLSWITCH</span>}
          {state.pollingPaused && <span className="badge badge-warn">POLLING PAUSED</span>}
        </div>
        <div className="flex items-center gap-6 mono text-sm">
          <div>
            <span className="text-dim text-xs mr-2">PORTFOLIO</span>
            <FlashValue value={s.portfolio_value} format={fmtUsd} className="text-gold font-semibold" />
          </div>
          <div>
            <span className="text-dim text-xs mr-2">DAY P&L</span>
            <FlashValue
              value={s.daily_pnl}
              format={fmtPnl}
              className={`font-semibold ${pnlClass(s.daily_pnl)}`}
            />
          </div>
          <div>
            <span className="text-dim text-xs mr-2">POSITIONS</span>
            <span>
              {s.open_positions ?? "—"}/{s.max_positions ?? "—"}
            </span>
          </div>
          <div>
            <span className="text-dim text-xs mr-2">DRAWDOWN</span>
            <span className={`tabular-nums ${s.drawdown_pct > 10 ? "text-warn" : ""}`}>{s.drawdown_pct ?? 0}%</span>
          </div>
          <ScanCountdown />
        </div>
        <div className="flex items-center gap-2">
          <button
            className="btn btn-primary"
            disabled={disabled}
            onClick={() => control("whetherbot", "scan")}
            title="Scan (S)"
          >
            {pending === "scan" ? "…" : "SCAN"}
          </button>
          <button
            className="btn"
            disabled={disabled}
            onClick={() => control("whetherbot", s.killswitch ? "resume" : "pause")}
            title="Pause (P)"
          >
            {pending === "pause" || pending === "resume" ? "…" : s.killswitch ? "RESUME" : "PAUSE"}
          </button>
          <button
            className="btn btn-danger"
            onClick={() => {
              if (isLive) dispatch({ type: "SET_UI", payload: { killswitchConfirm: true } });
              else control("whetherbot", "killswitch");
            }}
            title="Killswitch (K)"
          >
            KILL
          </button>
          <button
            className="btn"
            onClick={() => dispatch({ type: "SET_UI", payload: { atlasOpen: true } })}
            title="ATLAS (A)"
          >
            ATLAS
          </button>
          <button className="btn" onClick={() => dispatch({ type: "SET_UI", payload: { settingsOpen: true } })}>
            ⚙
          </button>
          <button
            className="btn"
            onClick={() => dispatch({ type: "SET", payload: { pollingPaused: !state.pollingPaused } })}
          >
            {state.pollingPaused ? "▶ POLL" : "⏸ POLL"}
          </button>
        </div>
      </div>
    </header>
  );
}
