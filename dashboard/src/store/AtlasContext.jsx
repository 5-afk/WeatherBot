import { createContext, useCallback, useContext, useReducer } from "react";
import { api, API_BASE, authHeaders } from "../api/client";
import { fmtPnl } from "../utils/format";
import { usePoller } from "../hooks/usePoller";
import { initialState, reducer } from "./reducer";

const AtlasContext = createContext(null);

export function useAtlasStore() {
  const ctx = useContext(AtlasContext);
  if (!ctx) throw new Error("useAtlasStore outside provider");
  return ctx;
}

export function AtlasProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState);

  const refetchStatus = useCallback(async () => {
    try {
      const data = await api("/api/status");
      dispatch({ type: "FETCH_OK", payload: { status: data }, updated: { status: Date.now() } });
    } catch (e) {
      dispatch({ type: "FETCH_FAIL" });
    }
  }, []);

  const control = useCallback(
    async (bot, action) => {
      dispatch({ type: "SET_CONTROLS", payload: { pending: action, lastError: null } });
      try {
        const result = await api("/api/control", {
          method: "POST",
          body: JSON.stringify({ bot, action }),
        });
        dispatch({
          type: "SET_CONTROLS",
          payload: {
            pending: null,
            toast: { type: "success", message: `${action.toUpperCase()} OK — status: ${result.status || "ok"}` },
          },
        });
        await refetchStatus();
      } catch (e) {
        dispatch({
          type: "SET_CONTROLS",
          payload: {
            pending: null,
            lastError: e.message,
            toast: { type: "error", message: `${action.toUpperCase()} failed: ${e.message}` },
          },
        });
      }
    },
    [refetchStatus]
  );

  const sellPosition = useCallback(async (positionId) => {
    try {
      const result = await api("/api/sell", {
        method: "POST",
        body: JSON.stringify({ position_id: positionId }),
      });
      if (result.sold) {
        dispatch({
          type: "SET_CONTROLS",
          payload: { toast: { type: "success", message: `Sold @ ${result.fill_price}, P&L: ${fmtPnl(result.pnl)}` } },
        });
      } else {
        dispatch({
          type: "SET_CONTROLS",
          payload: { toast: { type: "warn", message: result.reason || "Sell failed" } },
        });
      }
    } catch (e) {
      dispatch({
        type: "SET_CONTROLS",
        payload: { toast: { type: "error", message: e.message } },
      });
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const data = await api("/api/config");
      dispatch({ type: "SET", payload: { config: data } });
    } catch (e) {
      console.error(e);
    }
  }, []);

  const saveConfig = useCallback(
    async (key, value) => {
      await api("/api/config", { method: "POST", body: JSON.stringify({ key, value }) });
      await loadConfig();
    },
    [loadConfig]
  );

  const atlasChat = useCallback(
    async (text) => {
      const userMsg = { role: "user", content: text };
      const messages = [
        ...state.atlasMessages
          .filter((m) => m.role)
          .map((m) => ({ role: m.role, content: m.content })),
        { role: "user", content: text },
      ];
      dispatch({ type: "ATLAS_MSG", msg: userMsg });
      dispatch({ type: "ATLAS_STREAMING", streaming: true });
      const assistantMsg = { role: "assistant", content: "", streaming: true };
      dispatch({ type: "ATLAS_MSG", msg: assistantMsg });

      try {
        const res = await fetch(`${API_BASE}/api/atlas/chat`, {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ messages }),
        });

        if (!res.ok) {
          const errText = await res.text().catch(() => "");
          throw new Error(errText || `${res.status} ${res.statusText}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";

          for (const frame of parts) {
            const line = frame.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            try {
              const evt = JSON.parse(line.slice(6));
              if (evt.type === "text") {
                dispatch({ type: "ATLAS_UPDATE_LAST", delta: evt.delta });
              } else if (evt.type === "action_request") {
                dispatch({ type: "ATLAS_PENDING", pending: evt });
              } else if (evt.type === "action") {
                dispatch({
                  type: "ATLAS_MSG",
                  msg: {
                    role: "system",
                    content: `[${evt.label || ""}${evt.name}] ${JSON.stringify(evt.result)}`,
                    action: evt,
                  },
                });
              } else if (evt.type === "error") {
                dispatch({ type: "ATLAS_UPDATE_LAST", delta: `\n⚠ ${evt.message}` });
              } else if (evt.type === "done") {
                dispatch({ type: "ATLAS_DONE_STREAMING" });
              }
            } catch {
              /* ignore malformed SSE chunks */
            }
          }
        }
      } catch (e) {
        dispatch({ type: "ATLAS_UPDATE_LAST", delta: `\n⚠ ${e.message}` });
      }
      dispatch({ type: "ATLAS_DONE_STREAMING" });
    },
    [state.atlasMessages]
  );

  const atlasConfirm = useCallback(async (requestId) => {
    try {
      const result = await api("/api/atlas/confirm", {
        method: "POST",
        body: JSON.stringify({ request_id: requestId }),
      });
      dispatch({ type: "ATLAS_PENDING", pending: null });
      dispatch({
        type: "ATLAS_MSG",
        msg: { role: "system", content: `✓ Executed ${result.name}: ${JSON.stringify(result.result)}` },
      });
    } catch (e) {
      dispatch({ type: "ATLAS_MSG", msg: { role: "system", content: `✗ Confirm failed: ${e.message}` } });
    }
  }, []);

  const ctx = {
    state,
    dispatch,
    control,
    sellPosition,
    loadConfig,
    saveConfig,
    atlasChat,
    atlasConfirm,
    refetchStatus,
  };
  usePoller(dispatch, state);

  return <AtlasContext.Provider value={ctx}>{children}</AtlasContext.Provider>;
}
