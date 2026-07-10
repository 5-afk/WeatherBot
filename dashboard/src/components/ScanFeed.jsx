import { useAtlasStore } from "../store/AtlasContext";

const LEVELS = ["BET", "WIN", "LOSS", "SKIP", "CANDIDATE", "ERROR", "INFO"];

export default function ScanFeed() {
  const { state, dispatch } = useAtlasStore();
  const logs = state.logs || [];
  const filter = state.ui.logFilter;

  const filtered = filter ? logs.filter((l) => l.level === filter) : logs;

  return (
    <div className="panel col-12">
      <div className="panel-header">
        <span>Scan Feed</span>
        <div className="flex gap-1">
          <button
            className={`btn text-xs py-0 px-2 ${!filter ? "btn-primary" : ""}`}
            onClick={() => dispatch({ type: "SET_UI", payload: { logFilter: "" } })}
          >
            ALL
          </button>
          {LEVELS.map((l) => (
            <button
              key={l}
              className={`btn text-xs py-0 px-2 ${filter === l ? "btn-primary" : ""}`}
              onClick={() => dispatch({ type: "SET_UI", payload: { logFilter: l } })}
            >
              {l}
            </button>
          ))}
        </div>
      </div>
      <div className="panel-body overflow-auto scrollbar mono" style={{ maxHeight: 200, fontSize: 11 }}>
        {filtered
          .slice()
          .reverse()
          .map((l, i) => (
            <div key={i} className={`log-line log-${l.level}`}>
              <span className="text-dim">{l.ts}</span> <span className="text-dim">[{l.bot}]</span>{" "}
              <span className="text-dim">[{l.level}]</span> {l.msg}
            </div>
          ))}
      </div>
    </div>
  );
}
