import { useAtlasStore } from "../store/AtlasContext";

export default function BotCards() {
  const { state, control } = useAtlasStore();
  const agents = state.status?.agents || [];
  const pending = state.controls?.pending;
  const procStatus = state.status?.bot_process_status || "unknown";
  const disabled = Boolean(pending);

  const defaultAgent = {
    id: "whetherbot",
    status: procStatus,
    pid: state.status?.bot_pid,
    stale: procStatus === "stale",
  };

  const list = agents.length ? agents : [defaultAgent];

  return (
    <div className="col-12 flex gap-3">
      {list.map((a) => {
        const st = (a.status || procStatus || "unknown").toLowerCase();
        const badge =
          st === "running"
            ? "badge-ok"
            : st === "paused"
              ? "badge-warn"
              : st === "stale" || st === "error"
                ? "badge-error"
                : "badge-warn";
        return (
          <div key={a.id} className="panel flex-1">
            <div className="panel-header">
              <span>{a.id}</span>
              <span className={`badge ${badge}`}>{a.status || procStatus || "?"}</span>
            </div>
            <div className="panel-body">
              <div className="mono text-xs text-muted space-y-1">
                <div>PID: {a.pid || state.status?.bot_pid || "—"}</div>
                <div>Process: {procStatus}</div>
                <div>Heartbeat: {a.last_heartbeat ? new Date(a.last_heartbeat).toLocaleTimeString() : "—"}</div>
                {a.last_scan && (
                  <div>
                    Last scan: {a.last_scan.markets_checked ?? 0} mkts, {a.last_scan.candidates ?? 0} cands
                  </div>
                )}
                {(a.stale || procStatus === "stale") && <div className="text-loss">STALE</div>}
                {!agents.length && (
                  <div className="text-dim">No agent heartbeat — controls use process PID fallback</div>
                )}
              </div>
              <div className="flex gap-2 mt-3 flex-wrap">
                {st !== "running" && (
                  <button
                    className="btn btn-primary text-xs"
                    disabled={disabled}
                    onClick={() => control(a.id, "start")}
                  >
                    {pending === "start" ? "…" : "START"}
                  </button>
                )}
                {st === "running" && (
                  <button className="btn text-xs" disabled={disabled} onClick={() => control(a.id, "pause")}>
                    {pending === "pause" ? "…" : "PAUSE"}
                  </button>
                )}
                {st === "paused" && (
                  <button
                    className="btn btn-primary text-xs"
                    disabled={disabled}
                    onClick={() => control(a.id, "resume")}
                  >
                    {pending === "resume" ? "…" : "RESUME"}
                  </button>
                )}
                <button className="btn text-xs" disabled={disabled} onClick={() => control(a.id, "scan")}>
                  {pending === "scan" ? "…" : "SCAN"}
                </button>
                <button className="btn text-xs" disabled={disabled} onClick={() => control(a.id, "restart")}>
                  {pending === "restart" ? "…" : "RESTART"}
                </button>
                <button className="btn btn-danger text-xs" disabled={disabled} onClick={() => control(a.id, "stop")}>
                  {pending === "stop" ? "…" : "STOP"}
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
