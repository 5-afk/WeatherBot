import { useAtlasStore } from "../store/AtlasContext";
import { Panel } from "./ui/Panel";
import { fmtPnl, fmtUsd, pnlClass } from "../utils/format";
import { useLiveValue } from "../hooks/useLiveValue";

function PnlCell({ value }) {
  const pulse = useLiveValue(value);
  const pos = value != null && Number(value) >= 0;
  return (
    <td
      className={`font-mono tabular-nums ${pulse} ${pos ? "text-green bg-green/10" : "text-red bg-red/10"}`}
    >
      {fmtPnl(value)}
    </td>
  );
}

export default function LivePositions() {
  const { state, sellPosition } = useAtlasStore();
  const positions = state.positions || [];
  const offline = state.connection === "down";

  const totals = positions.reduce(
    (a, p) => ({
      cost: a.cost + (p.cost || 0),
      value: a.value + (p.market_value || p.cost || 0),
      pnl: a.pnl + (p.unrealized_pnl || 0),
    }),
    { cost: 0, value: 0, pnl: 0 }
  );

  return (
    <Panel title={`Open Positions (${positions.length})`} className={offline ? "offline-dim" : ""}>
      {positions.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-text-3 text-sm">
          <div className="text-3xl mb-3 animate-spin" style={{ animationDuration: "8s" }}>
            ◎
          </div>
          <div>Kelly is watching markets</div>
          <div className="text-text-2 mt-1">No open positions</div>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <thead className="text-text-3 uppercase text-[10px]">
              <tr className="border-b border-border">
                <th className="text-left py-1 pr-2">Market</th>
                <th className="text-left py-1">Side</th>
                <th className="text-right py-1">Qty</th>
                <th className="text-right py-1">Avg</th>
                <th className="text-right py-1">Cost</th>
                <th className="text-right py-1">Value</th>
                <th className="text-right py-1">P&L</th>
                <th className="text-right py-1">Settle</th>
                <th className="text-center py-1">METAR</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id} className="border-b border-border/50">
                  <td className="py-1.5 pr-2 uppercase" title={p.title || p.ticker}>
                    {p.ticker?.slice(0, 16)}
                  </td>
                  <td className={p.side === "YES" ? "text-green" : "text-red"}>{p.side}</td>
                  <td className="text-right tabular-nums">{p.contracts}</td>
                  <td className="text-right tabular-nums">{p.avg_price?.toFixed(2)}</td>
                  <td className="text-right tabular-nums">{fmtUsd(p.cost)}</td>
                  <td className="text-right tabular-nums">{fmtUsd(p.market_value)}</td>
                  <PnlCell value={p.unrealized_pnl} />
                  <td className="text-right tabular-nums text-text-2">
                    {p.hours_to_settlement != null ? `${p.hours_to_settlement.toFixed?.(1) ?? p.hours_to_settlement}h` : "—"}
                  </td>
                  <td className="text-center">
                    {p.metar_verdict === "confirm"
                      ? "✓"
                      : p.metar_verdict === "contradict"
                        ? "✗"
                        : "~"}
                  </td>
                  <td>
                    <button
                      className="text-[10px] uppercase text-text-2 hover:text-red border border-transparent hover:border-red/40 px-2 py-0.5 rounded"
                      onClick={() => sellPosition(p.id)}
                    >
                      SELL
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="text-text-2 font-semibold">
                <td colSpan={4} className="pt-2">
                  Totals
                </td>
                <td className="text-right tabular-nums pt-2">{fmtUsd(totals.cost)}</td>
                <td className="text-right tabular-nums pt-2">{fmtUsd(totals.value)}</td>
                <td className={`text-right tabular-nums pt-2 ${pnlClass(totals.pnl)}`}>{fmtPnl(totals.pnl)}</td>
                <td colSpan={3} />
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </Panel>
  );
}
