import { useEffect, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";
import { Panel } from "./ui/Panel";
import { Button } from "./ui/Button";
import { Badge } from "./ui/Badge";
import { formatCountdown } from "../utils/format";

export default function BotControls() {
  const { state, control } = useAtlasStore();
  const s = state.status || {};
  const proc = (s.bot_process_status || "stopped").toLowerCase();
  const pending = state.controls?.pending;
  const scan = s.last_scan || {};
  const [countdown, setCountdown] = useState(null);

  useEffect(() => {
    const tick = () => {
      if (!s.next_scan_at) {
        setCountdown(null);
        return;
      }
      const diff = Math.max(0, Math.floor((new Date(s.next_scan_at).getTime() - Date.now()) / 1000));
      setCountdown(diff);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [s.next_scan_at]);

  const canStart = proc === "stopped";
  const canStop = proc === "running" || proc === "paused" || proc === "stale";
  const canPause = proc === "running";
  const canResume = proc === "paused";
  const highlightRestart = proc === "stale";

  const claudeUsed = scan.claude_calls ?? 0;
  const claudeMax = scan.claude_max ?? 10;
  const claudePct = claudeMax ? (claudeUsed / claudeMax) * 100 : 0;

  return (
    <Panel
      title="Kelly"
      right={<Badge variant={proc === "running" ? "live" : proc === "paused" ? "paused" : "stopped"}>{proc}</Badge>}
      className={state.connection === "down" ? "offline-dim" : ""}
    >
      <div className="space-y-3 font-mono text-xs text-text-2">
        <div>
          ● Kelly · <span className="text-text uppercase">{proc}</span>
          {s.started_at && (
            <span className="text-text-3 ml-2">since {new Date(s.started_at).toLocaleTimeString()}</span>
          )}
        </div>
        <div>Last scan: {scan.finished_at ? new Date(scan.finished_at).toLocaleTimeString() : "—"}</div>
        <div>
          Next scan: <span className="text-cyan tabular-nums">{formatCountdown(countdown)}</span>
        </div>
        <div>
          Markets: {scan.markets_checked ?? "—"} · Candidates: {scan.candidates ?? "—"}
        </div>
        <div>
          Claude: {claudeUsed}/{claudeMax}
          <div className="h-1 mt-1 bg-surface-3 rounded overflow-hidden">
            <div
              className={`h-full ${claudePct > 90 ? "bg-red" : claudePct > 70 ? "bg-amber" : "bg-cyan"}`}
              style={{ width: `${Math.min(100, claudePct)}%` }}
            />
          </div>
        </div>
        {s.scan_in_progress && (
          <div className="text-cyan animate-pulse">⟳ Scan in progress…</div>
        )}
      </div>

      <div className="flex flex-wrap gap-2 mt-4">
        <Button variant="primary" pending={pending === "start"} disabled={!canStart || !!pending} onClick={() => control("whetherbot", "start")}>
          START
        </Button>
        <Button variant="danger" pending={pending === "stop"} disabled={!canStop || !!pending} onClick={() => control("whetherbot", "stop")}>
          STOP
        </Button>
        <Button variant={highlightRestart ? "warn" : "default"} pending={pending === "restart"} disabled={!!pending} onClick={() => control("whetherbot", "restart")}>
          RESTART
        </Button>
        {canPause && (
          <Button pending={pending === "pause"} disabled={!!pending} onClick={() => control("whetherbot", "pause")}>
            PAUSE
          </Button>
        )}
        {canResume && (
          <Button variant="primary" pending={pending === "resume"} disabled={!!pending} onClick={() => control("whetherbot", "resume")}>
            RESUME
          </Button>
        )}
        <Button variant="primary" pending={pending === "scan"} disabled={!!pending} onClick={() => control("whetherbot", "scan")}>
          SCAN NOW
        </Button>
      </div>
    </Panel>
  );
}
