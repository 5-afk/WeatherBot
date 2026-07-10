import { useAtlasStore } from "../store/AtlasContext";
import { fmtPct, fmtPnl, fmtUsd, pnlClass } from "../utils/format";

export default function TradeHistory() {
  const { state } = useAtlasStore();
  const trades = state.trades?.trades || [];
  const summary = state.trades?.summary || {};

  return (
    <div className="panel col-6">
      <div className="panel-header">
        <span>Trade History</span>
        <span className="mono text-xs">
          P&L: <span className={pnlClass(summary.total_pnl)}>{fmtPnl(summary.total_pnl)}</span>
          {" | "}WR: {fmtPct(summary.winrate_all)}
          {" | "}WR10: {fmtPct(summary.winrate_10)}
        </span>
      </div>
      <div className="panel-body overflow-auto scrollbar" style={{ maxHeight: 280 }}>
        <table className="data">
          <thead>
            <tr>
              <th>Date</th>
              <th>Ticker</th>
              <th>Side</th>
              <th>Stake</th>
              <th>Profit</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {trades.slice(0, 50).map((t) => (
              <tr key={t.id}>
                <td>{t.date}</td>
                <td>{t.ticker?.slice(0, 16)}</td>
                <td className={t.side === "YES" ? "text-profit" : "text-loss"}>{t.side}</td>
                <td>{fmtUsd(t.stake)}</td>
                <td className={pnlClass(t.profit)}>{fmtPnl(t.profit)}</td>
                <td className={t.outcome === "win" ? "text-profit" : "text-loss"}>{t.outcome?.toUpperCase()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
