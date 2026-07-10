import { useRef, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";
import { Panel } from "./ui/Panel";

const FILTERS = ["", "CANDIDATE", "BET", "ERROR", "WIN"];

export default function ScanFeed() {
  const { state, dispatch } = useAtlasStore();
  const logs = state.logs || [];
  const filter = state.ui.logFilter;
  const search = state.ui.logSearch || "";
  const scrollRef = useRef(null);
  const [pinned, setPinned] = useState(true);
  const [newLines, setNewLines] = useState(0);

  const filtered = logs.filter((l) => {
    if (filter && l.level !== filter) return false;
    if (search && !l.msg?.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setPinned(atBottom);
    if (atBottom) setNewLines(0);
  };

  return (
    <Panel
      title="Scan Feed"
      className={state.connection === "down" ? "offline-dim" : ""}
      right={
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f || "ALL"}
              type="button"
              className={`text-[10px] px-1.5 py-0.5 rounded uppercase ${
                filter === f ? "bg-cyan/15 text-cyan" : "text-text-3"
              }`}
              onClick={() => dispatch({ type: "SET_UI", payload: { logFilter: f } })}
            >
              {f || "ALL"}
            </button>
          ))}
        </div>
      }
    >
      <input
        className="w-full mb-2 bg-surface-2 border border-border rounded px-2 py-1 text-xs font-mono"
        placeholder="Search logs…"
        value={search}
        onChange={(e) => dispatch({ type: "SET_UI", payload: { logSearch: e.target.value } })}
      />
      <div className="relative flex-1 min-h-0">
        <div
          ref={scrollRef}
          className="h-full max-h-[180px] overflow-y-auto scrollbar font-mono text-[12px]"
          onScroll={onScroll}
        >
          {filtered
            .slice()
            .reverse()
            .map((l, i) => (
              <div key={`${l.ts}-${l.msg}-${i}`} className={`log-line log-${l.level}`}>
                <span className="text-text-3">{l.ts}</span> [{l.level}] {l.msg}
              </div>
            ))}
        </div>
        {!pinned && newLines > 0 && (
          <button
            type="button"
            className="absolute bottom-2 right-2 text-[10px] bg-surface-2 border border-border px-2 py-1 rounded"
            onClick={() => {
              scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
              setPinned(true);
              setNewLines(0);
            }}
          >
            ▼ RESUME {newLines > 0 && `(${newLines})`}
          </button>
        )}
      </div>
    </Panel>
  );
}
