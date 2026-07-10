import { useEffect, useRef, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";

export default function AtlasDrawer() {
  const { state, dispatch, atlasChat, atlasConfirm } = useAtlasStore();
  const [input, setInput] = useState("");
  const bottomRef = useRef(null);
  const open = state.ui.atlasOpen;

  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.atlasMessages, open]);

  const send = () => {
    if (!input.trim() || state.atlasStreaming) return;
    atlasChat(input.trim());
    setInput("");
  };

  if (!open) return null;

  return (
    <>
      <div className="drawer-overlay" onClick={() => dispatch({ type: "SET_UI", payload: { atlasOpen: false } })} />
      <div className="drawer open">
        <div className="panel-header">
          <span className="text-data">ATLAS Assistant</span>
          <div className="flex gap-2">
            <button className="btn text-xs" onClick={() => dispatch({ type: "CLEAR_ATLAS" })}>
              CLEAR
            </button>
            <button className="btn text-xs" onClick={() => dispatch({ type: "SET_UI", payload: { atlasOpen: false } })}>
              ✕
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-auto scrollbar p-4">
          {state.atlasMessages.length === 0 && (
            <div className="text-dim text-sm">Ask ATLAS about positions, METAR, calibration, or bot controls.</div>
          )}
          {state.atlasMessages.map((m, i) => (
            <div key={i} className={`atlas-msg ${m.role === "user" ? "atlas-msg-user" : ""}`}>
              <div className={`atlas-bubble ${m.role === "user" ? "atlas-bubble-user" : "atlas-bubble-assistant"}`}>
                {m.content}
                {m.streaming && <span className="text-data animate-pulse">▊</span>}
              </div>
              {m.action && (
                <div className="mono text-xs text-muted mt-1">
                  [{m.action.label || ""}
                  {m.action.name}] {JSON.stringify(m.action.result || {}).slice(0, 120)}
                </div>
              )}
            </div>
          ))}
          {state.atlasPending && (
            <div className="action-card">
              <div className="mono text-sm text-warn font-semibold mb-2">⚠ LIVE ACTION CONFIRMATION REQUIRED</div>
              <div className="mono text-xs mb-3">
                <div>
                  Action: <span className="text-data">{state.atlasPending.name}</span>
                </div>
                <div>Args: {JSON.stringify(state.atlasPending.args)}</div>
              </div>
              <div className="flex gap-2">
                <button className="btn btn-warn" onClick={() => atlasConfirm(state.atlasPending.request_id)}>
                  CONFIRM
                </button>
                <button className="btn" onClick={() => dispatch({ type: "ATLAS_PENDING", pending: null })}>
                  CANCEL
                </button>
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        <div className="p-3 border-t border-[var(--line)]">
          <div className="flex gap-2">
            <input
              className="flex-1 bg-[var(--bg-2)] border border-[var(--line)] rounded px-3 py-2 text-sm mono text-[var(--text-0)]"
              placeholder="Ask ATLAS…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
              disabled={state.atlasStreaming}
            />
            <button className="btn btn-primary" onClick={send} disabled={state.atlasStreaming || !input.trim()}>
              {state.atlasStreaming ? "…" : "SEND"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
