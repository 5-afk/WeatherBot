import { useAtlasStore } from "../store/AtlasContext";

export default function MetarGrid() {
  const { state } = useAtlasStore();
  const stations = state.metar || [];

  return (
    <div className="panel col-6">
      <div className="panel-header">METAR Grid ({stations.length} stations)</div>
      <div className="panel-body">
        <div className="grid grid-cols-4 gap-2" style={{ maxHeight: 280, overflowY: "auto" }}>
          {stations.map((m) => (
            <div
              key={m.station}
              className={`metar-card ${m.alert === "red" ? "alert-red" : m.alert === "yellow" ? "alert-yellow" : ""}`}
              title={m.raw}
            >
              <div className="flex justify-between items-center mb-1">
                <span className="mono text-xs font-semibold">{m.station}</span>
                <span
                  className={`text-xs ${m.alert === "red" ? "text-loss" : m.alert === "yellow" ? "text-warn" : "text-profit"}`}
                >
                  ●
                </span>
              </div>
              <div className="mono text-lg">{m.temp_f != null ? `${m.temp_f}°F` : "—"}</div>
              <div className="text-dim text-xs">{m.city}</div>
              <div className="text-dim text-xs mt-1">
                H:{m.max_today_f ?? "—"} L:{m.min_today_f ?? "—"} {m.trend}
              </div>
              <div className="text-dim text-xs">
                {m.wind} {m.sky}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
