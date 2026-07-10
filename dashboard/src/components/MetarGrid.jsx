import { useAtlasStore } from "../store/AtlasContext";
import { Panel } from "./ui/Panel";
import { fmtTemp } from "../utils/format";

export default function MetarGrid() {
  const { state } = useAtlasStore();
  const stations = [...(state.metar || [])].sort((a, b) => {
    if (a.has_position && !b.has_position) return -1;
    if (!a.has_position && b.has_position) return 1;
    return 0;
  });

  return (
    <Panel title={`METAR (${stations.length})`} className={state.connection === "down" ? "offline-dim" : ""}>
      <div className="grid grid-cols-2 gap-2">
        {stations.map((m) => {
          const alertCls =
            m.alert === "green" || m.alert_level === "green"
              ? "alert-green"
              : m.alert === "amber" || m.alert === "yellow" || m.alert_level === "amber"
                ? "alert-amber"
                : m.alert === "red" || m.alert_level === "red"
                  ? "alert-red"
                  : "";
          return (
            <div key={m.station} className={`metar-card ${alertCls}`} title={m.raw}>
              <div className="flex justify-between items-center mb-1">
                <span className="font-mono text-xs font-semibold">{m.station}</span>
                <span className="text-xs text-text-3">{m.trend || "→"}</span>
              </div>
              <div className="font-mono text-xl tabular-nums">{fmtTemp(m.temp_f)}</div>
              <div className="text-text-3 text-[10px] mt-1">
                MAX {m.max_today_f ?? "—"} · MIN {m.min_today_f ?? "—"}
              </div>
              <div className="text-text-3 text-[10px]">
                {m.wind} {m.sky}
              </div>
              {m.age_min != null && (
                <div className="text-[10px] text-text-3 mt-1">{m.age_min} min ago</div>
              )}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
