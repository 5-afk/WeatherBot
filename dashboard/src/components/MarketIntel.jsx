import { useAtlasStore } from "../store/AtlasContext";
import { Panel } from "./ui/Panel";
import { Button } from "./ui/Button";

export default function MarketIntel() {
  const { state, control } = useAtlasStore();
  const cands = [...(state.candidates || [])].sort((a, b) => (b.ev ?? 0) - (a.ev ?? 0));
  const pending = state.controls?.pending;

  return (
    <Panel
      title="Market Intel"
      right={
        <Button variant="primary" pending={pending === "scan"} disabled={!!pending} onClick={() => control("whetherbot", "scan")}>
          ⚡ FORCE SCAN
        </Button>
      }
      className={state.connection === "down" ? "offline-dim" : ""}
    >
      {cands.length === 0 ? (
        <p className="text-sm text-text-3">
          Last scan found 0 candidates above threshold — Kelly is being selective.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <thead className="text-text-3 uppercase text-[10px]">
              <tr className="border-b border-border">
                <th className="text-left py-1">Ticker</th>
                <th className="text-left py-1">City</th>
                <th className="text-left py-1">Side</th>
                <th className="text-right py-1">EV</th>
                <th className="text-right py-1">Signal</th>
                <th className="text-right py-1">Buffer</th>
                <th className="text-left py-1">Claude</th>
              </tr>
            </thead>
            <tbody>
              {cands.map((c, i) => (
                <tr key={c.ticker + i} className="border-b border-border/50">
                  <td className="py-1 uppercase">{c.ticker?.slice(0, 14)}</td>
                  <td>{c.city}</td>
                  <td className={c.side === "YES" ? "text-green" : "text-red"}>{c.side}</td>
                  <td className="text-right text-cyan tabular-nums">{c.ev?.toFixed(3)}</td>
                  <td className="text-right tabular-nums">{c.signal_score?.toFixed(2)}</td>
                  <td className="text-right tabular-nums">{c.buffer?.toFixed?.(1) ?? c.buffer ?? "—"}</td>
                  <td>
                    <span
                      className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                        c.claude_decision === "approved"
                          ? "bg-green/15 text-green"
                          : "bg-red/15 text-red"
                      }`}
                      title={c.claude_reason || c.rejection_reason || ""}
                    >
                      {c.claude_decision === "approved" ? "GO ✓" : "NOGO"}
                    </span>
                    {c.bet_placed && <span className="ml-1 text-amber text-[10px]">BET</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
