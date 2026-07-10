import { useEffect, useRef } from "react";
import { Chart } from "chart.js/auto";
import { useAtlasStore } from "../store/AtlasContext";

function brierColor(brier) {
  if (brier == null) return "rgba(139,152,169,0.5)";
  if (brier < 0.1) return "rgba(46,255,138,0.85)";
  if (brier < 0.2) return "rgba(255,178,36,0.85)";
  return "rgba(255,77,94,0.85)";
}

export default function CalibrationPanel() {
  const { state } = useAtlasStore();
  const cal = state.calibration || {};
  const curveRef = useRef(null);
  const clvRef = useRef(null);
  const winRef = useRef(null);
  const brierRef = useRef(null);
  const charts = useRef({});
  const loading = state.calibration === null && state.connection !== "down";

  useEffect(() => {
    if (!cal.calibration_curve?.length && !cal.clv_series?.length) return;

    const token = {
      data: "rgba(56,217,245,0.8)",
      profit: "rgba(46,255,138,0.8)",
      loss: "rgba(255,77,94,0.8)",
      gold: "rgba(255,215,94,0.8)",
      grid: "rgba(31,41,55,0.5)",
      text: "#8b98a9",
    };

    const opts = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: token.text, font: { family: "JetBrains Mono", size: 10 } } } },
      scales: {
        x: { ticks: { color: token.text, font: { size: 9 } }, grid: { color: token.grid } },
        y: { ticks: { color: token.text, font: { size: 9 } }, grid: { color: token.grid } },
      },
    };

    if (curveRef.current && cal.calibration_curve?.length) {
      charts.current.curve?.destroy();
      charts.current.curve = new Chart(curveRef.current, {
        type: "bar",
        data: {
          labels: cal.calibration_curve.map((b) => b.bucket),
          datasets: [
            { label: "Predicted", data: cal.calibration_curve.map((b) => b.predicted), backgroundColor: token.data },
            { label: "Actual", data: cal.calibration_curve.map((b) => b.actual), backgroundColor: token.gold },
          ],
        },
        options: {
          ...opts,
          plugins: { ...opts.plugins, title: { display: true, text: "Calibration Curve", color: token.text } },
        },
      });
    }

    if (clvRef.current && cal.clv_series?.length) {
      charts.current.clv?.destroy();
      charts.current.clv = new Chart(clvRef.current, {
        type: "line",
        data: {
          labels: cal.clv_series.map((c) => c.date || c.trade_id),
          datasets: [
            {
              label: "CLV",
              data: cal.clv_series.map((c) => c.clv),
              borderColor: token.profit,
              backgroundColor: "rgba(46,255,138,0.1)",
              fill: true,
              tension: 0.3,
            },
          ],
        },
        options: { ...opts, plugins: { ...opts.plugins, title: { display: true, text: "CLV Series", color: token.text } } },
      });
    }

    if (winRef.current && cal.winrate_by_lead?.length) {
      charts.current.win?.destroy();
      charts.current.win = new Chart(winRef.current, {
        type: "bar",
        data: {
          labels: cal.winrate_by_lead.map((w) => w.bucket),
          datasets: [
            {
              label: "Win Rate",
              data: cal.winrate_by_lead.map((w) => w.winrate),
              backgroundColor: cal.winrate_by_lead.map((w) => (w.winrate >= 0.5 ? token.profit : token.loss)),
            },
          ],
        },
        options: {
          ...opts,
          plugins: { ...opts.plugins, title: { display: true, text: "Win Rate by Lead", color: token.text } },
        },
      });
    }

    if (brierRef.current && cal.brier != null) {
      charts.current.brier?.destroy();
      const score = Math.min(1, Math.max(0, cal.brier));
      charts.current.brier = new Chart(brierRef.current, {
        type: "doughnut",
        data: {
          labels: ["Brier", "Remainder"],
          datasets: [
            {
              data: [score, 1 - score],
              backgroundColor: [brierColor(cal.brier), "rgba(31,41,55,0.6)"],
              borderWidth: 0,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "70%",
          plugins: {
            legend: { display: false },
            title: { display: true, text: `Brier ${cal.brier.toFixed(4)}`, color: token.text, font: { size: 10 } },
          },
        },
      });
    }

    return () => Object.values(charts.current).forEach((c) => c?.destroy());
  }, [cal]);

  if (loading) {
    return (
      <div className="panel col-6">
        <div className="panel-header">Calibration</div>
        <div className="panel-body text-dim text-sm">Loading calibration data…</div>
      </div>
    );
  }

  if (!cal.trade_count && !cal.brier) {
    return (
      <div className="panel col-6">
        <div className="panel-header">Calibration</div>
        <div className="panel-body text-dim text-sm">No settled trades yet — calibration appears after first settlements.</div>
      </div>
    );
  }

  return (
    <div className="panel col-6">
      <div className="panel-header">
        <span>Calibration</span>
        <span className="mono text-xs">
          Brier: <span className="text-data">{cal.brier?.toFixed(4) ?? "—"}</span>
          {" | "}Trades: {cal.trade_count ?? 0}
        </span>
      </div>
      <div className="panel-body">
        <div className="grid grid-cols-4 gap-2 mb-3" style={{ height: 140 }}>
          <div>
            <canvas ref={brierRef} />
          </div>
          <div>
            <canvas ref={curveRef} />
          </div>
          <div>
            <canvas ref={clvRef} />
          </div>
          <div>
            <canvas ref={winRef} />
          </div>
        </div>
        {cal.sigma_table?.length > 0 && (
          <table className="data">
            <thead>
              <tr>
                <th>City</th>
                <th>Sigma</th>
                <th>MAE</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {cal.sigma_table.map((s) => (
                <tr key={s.city}>
                  <td>{s.city}</td>
                  <td>{s.sigma?.toFixed(2)}</td>
                  <td>{s.actual_mae?.toFixed(2)}</td>
                  <td className="text-warn">{s.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
