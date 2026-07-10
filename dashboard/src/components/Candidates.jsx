import { useAtlasStore } from "../store/AtlasContext";

export default function Candidates() {
  const { state } = useAtlasStore();
  const cands = state.candidates || [];

  return (
    <div className="panel col-6">
      <div className="panel-header">Scan Candidates ({cands.length})</div>
      <div className="panel-body overflow-auto scrollbar" style={{ maxHeight: 280 }}>
        {cands.length === 0 ? (
          <div className="text-dim text-sm">No candidates from last scan</div>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>City</th>
                <th>Side</th>
                <th>EV</th>
                <th>Signal</th>
                <th>Price</th>
                <th>Claude</th>
              </tr>
            </thead>
            <tbody>
              {cands.map((c, i) => (
                <tr key={c.ticker + i} className={c.bet_placed ? "opacity-100" : ""}>
                  <td>{c.ticker?.slice(0, 18)}</td>
                  <td>{c.city}</td>
                  <td className={c.side === "YES" ? "text-profit" : "text-loss"}>{c.side}</td>
                  <td className="text-data">{c.ev?.toFixed(3)}</td>
                  <td>{c.signal_score?.toFixed(2)}</td>
                  <td>{c.price?.toFixed(2)}</td>
                  <td
                    className={
                      c.claude_decision === "approved"
                        ? "text-profit"
                        : c.claude_decision === "rejected"
                          ? "text-loss"
                          : "text-warn"
                    }
                  >
                    {c.claude_decision}
                    {c.bet_placed ? " ★" : ""}
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
