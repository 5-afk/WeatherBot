import { useEffect, useRef, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";

const SUGGESTIONS = [
  "strongest position?",
  "why was OKC skipped?",
  "should I sell anything?",
  "how's calibration?",
];

export default function AtlasAI({ mobileFull = false }) {
  const { state, dispatch, atlasChat, stopAtlasChat } = useAtlasStore();
  const [input, setInput] = useState("");
  const [expanded, setExpanded] = useState(mobileFull);
  const bottomRef = useRef(null);
  const touchY = useRef(null);
  const offline = state.connection === "down";

  useEffect(() => {
    if (expanded) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.atlasMessages, expanded]);

  const send = (text) => {
    const q = (text || input).trim();
    if (!q || state.atlasStreaming || offline) return;
    atlasChat(q);
    setInput("");
    setExpanded(true);
  };

  const onTouchStart = (e) => {
    touchY.current = e.touches[0].clientY;
  };
  const onTouchEnd = (e) => {
    if (touchY.current == null) return;
    const dy = touchY.current - e.changedTouches[0].clientY;
    if (dy > 40) setExpanded(true);
    if (dy < -40) setExpanded(false);
    touchY.current = null;
  };

  return (
    <>
      {expanded && (
        <div
          className={mobileFull ? "flex flex-col h-full min-h-0 bg-surface" : "drawer-panel open"}
          style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
        >
          <div className="flex items-center justify-between px-4 py-2 border-b border-purple/30 shrink-0">
            <span className="text-purple font-mono text-sm">⬡ ATLAS AI</span>
            <div className="flex gap-2">
              <button
                type="button"
                className="text-xs text-text-3 hover:text-text"
                onClick={() => dispatch({ type: "CLEAR_ATLAS" })}
              >
                CLEAR
              </button>
              <button type="button" className="text-xs text-text-3 hover:text-text" onClick={() => setExpanded(false)}>
                ✕
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto scrollbar p-4 min-h-0">
            {state.atlasMessages.length === 0 && (
              <div className="flex flex-wrap gap-2 mb-4">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="text-xs px-3 py-1.5 rounded-full border border-purple/30 text-purple hover:bg-purple/10"
                    onClick={() => send(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
            {state.atlasMessages.map((m, i) => (
              <div
                key={i}
                className={`mb-3 flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[85%] text-sm px-3 py-2 rounded ${
                    m.role === "user"
                      ? "bg-surface-2 text-text"
                      : "border-l-2 border-purple text-text pl-3"
                  }`}
                >
                  {m.role === "assistant" && <span className="text-purple mr-1">⬡</span>}
                  {m.content}
                  {m.streaming && <span className="text-purple animate-pulse">▊</span>}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
          <div className="p-3 border-t border-border shrink-0">
            <div className="flex gap-2">
              <input
                className="flex-1 bg-surface-2 border border-border rounded px-3 py-2 text-sm font-mono text-text min-h-[44px]"
                placeholder={offline ? "Can't reach Kelly right now" : "Ask Kelly anything…"}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
                disabled={state.atlasStreaming || offline}
              />
              {state.atlasStreaming ? (
                <button
                  type="button"
                  className="px-4 py-2 text-xs uppercase border border-red/40 text-red rounded min-h-[44px]"
                  onClick={stopAtlasChat}
                >
                  STOP
                </button>
              ) : (
                <button
                  type="button"
                  className="px-4 py-2 text-xs uppercase border border-purple/40 text-purple rounded min-h-[44px]"
                  onClick={() => send()}
                  disabled={!input.trim() || offline}
                >
                  SEND
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {!mobileFull && (
        <div
          className="drawer-bar shrink-0"
          onClick={() => setExpanded((v) => !v)}
          onTouchStart={onTouchStart}
          onTouchEnd={onTouchEnd}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => e.key === "Enter" && setExpanded((v) => !v)}
          style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
        >
          <span className="shadow-[0_0_12px_#a855f740]">⬡</span>
          <span className="ml-2">ATLAS AI — Ask Kelly anything…</span>
        </div>
      )}
    </>
  );
}
