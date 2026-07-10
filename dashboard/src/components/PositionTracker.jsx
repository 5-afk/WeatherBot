import { useAtlasStore } from "../store/AtlasContext";
import { fmtPnl, fmtUsd, pnlClass } from "../utils/format";

export default function PositionTracker() {
  const { state, sellPosition } = useAtlasStore();
  const positions = state.positions || [];

  return (
    <div className="panel col-7">
      <div className="panel-header">
        <span>Open Positions ({positions.length})</span>
      </div>
      <div className="panel-body overflow-auto" style={{ maxHeight: 340 }}>
        {positions.length === 0 ? (
          <div className="text-dim text-sm">No open positions</div>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Avg</th>
                <th>Mkt</th>
                <th>Cost</th>
                <th>Unreal</th>
                <th>Settle%</th>
                <th>ETA</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id}>
                  <td title={p.title}>{p.ticker?.slice(0, 20)}</td>
                  <td className={p.side === "YES" ? "text-profit" : "text-loss"}>{p.side}</td>
                  <td>{p.contracts}</td>
                  <td>{p.avg_price?.toFixed(2)}</td>
                  <td>{p.market_price?.toFixed(2)}</td>
                  <td>{fmtUsd(p.cost)}</td>
                  <td className={pnlClass(p.unrealized_pnl)}>{fmtPnl(p.unrealized_pnl)}</td>
                  <td className="text-data">
                    {p.settle_prob != null ? `${(p.settle_prob * 100).toFixed(0)}%` : "—"}
                  </td>
                  <td className="text-muted">
                    {p.hours_to_settlement != null ? `${p.hours_to_settlement}h` : "—"}
                  </td>
                  <td>
                    <button className="btn btn-danger text-xs py-1 px-2" onClick={() => sellPosition(p.id)}>
                      SELL
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
