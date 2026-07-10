import { createContext, useCallback, useContext, useReducer, useRef } from "react";
import { api, ApiError, isOffline } from "../api/client";
import { fmtPnl } from "../utils/format";
import { usePoller } from "../hooks/usePoller";
import { initialState, reducer } from "./reducer";

const AtlasContext = createContext(null);
let toastId = 0;

export function useAtlasStore() {
  const ctx = useContext(AtlasContext);
  if (!ctx) throw new Error("useAtlasStore outside provider");
  return ctx;
}

export function AtlasProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const chatAbort = useRef(null);

  const pushToast = useCallback((type, message) => {
    const toast = { id: ++toastId, type, message };
    dispatch({ type: "PUSH_TOAST", toast });
    const ms = type === "error" ? 5000 : 3000;
    setTimeout(() => dispatch({ type: "DISMISS_TOAST", id: toast.id }), ms);
  }, []);

  const refetchStatus = useCallback(async () => {
    try {
      const data = await api("/api/status");
      if (isOffline(data)) {
        dispatch({ type: "FETCH_FAIL" });
        return;
      }
      dispatch({ type: "FETCH_OK", payload: { status: data }, updated: { status: Date.now() } });
    } catch {
      dispatch({ type: "FETCH_FAIL" });
    }
  }, []);

  const control = useCallback(
    async (bot, action) => {
      dispatch({ type: "SET_CONTROLS", payload: { pending: action, lastError: null } });
      try {
        const result = await api("/api/control", {
          method: "POST",
          body: { bot, action },
        });
        if (isOffline(result)) {
          pushToast("error", "Kelly is offline — control not sent");
          dispatch({ type: "SET_CONTROLS", payload: { pending: null } });
          return;
        }
        pushToast("success", `${action.toUpperCase()} OK — ${result.status || "ok"}`);
        dispatch({ type: "SET_CONTROLS", payload: { pending: null } });
        await refetchStatus();
      } catch (e) {
        const msg = e instanceof ApiError ? e.message : String(e);
        pushToast("error", `${action.toUpperCase()} failed: ${msg}`);
        dispatch({ type: "SET_CONTROLS", payload: { pending: null, lastError: msg } });
      }
    },
    [refetchStatus, pushToast]
  );

  const sellPosition = useCallback(
    async (positionId) => {
      try {
        const result = await api("/api/sell", {
          method: "POST",
          body: { position_id: positionId },
        });
        if (result.sold) {
          pushToast("success", `Sold @ ${result.fill_price}, P&L: ${fmtPnl(result.pnl)}`);
        } else {
          pushToast("warn", result.reason || "Sell failed");
        }
      } catch (e) {
        pushToast("error", e instanceof ApiError ? e.message : String(e));
      }
    },
    [pushToast]
  );

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
      await api("/api/config", { method: "POST", body: { key, value } });
      pushToast("success", "Changes apply on next scan");
      await loadConfig();
    },
    [loadConfig, pushToast]
  );

  const stopAtlasChat = useCallback(() => {
    chatAbort.current?.abort();
    chatAbort.current = null;
    dispatch({ type: "ATLAS_DONE_STREAMING" });
  }, []);

  const atlasChat = useCallback(
    async (text) => {
      if (state.connection === "down") return;
      const userMsg = { role: "user", content: text };
      const messages = [
        ...state.atlasMessages
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({ role: m.role, content: m.content })),
        { role: "user", content: text },
      ].slice(-20);

      dispatch({ type: "ATLAS_MSG", msg: userMsg });
      dispatch({ type: "ATLAS_STREAMING", streaming: true });
      dispatch({ type: "ATLAS_MSG", msg: { role: "assistant", content: "", streaming: true } });

      const controller = new AbortController();
      chatAbort.current = controller;
      const SECRET = import.meta.env.VITE_DASHBOARD_SECRET || "";

      try {
        const res = await fetch("/api/atlas/chat", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(SECRET ? { "X-Atlas-Secret": SECRET } : {}),
          },
          body: JSON.stringify({ messages }),
          signal: controller.signal,
        });

        if (!res.ok) {
          const errText = await res.text().catch(() => "");
          throw new Error(errText || `${res.status} ${res.statusText}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() || "";

          for (const frame of frames) {
            const line = frame.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            const payload = line.slice(6);
            if (payload === "[DONE]") break;
            try {
              const evt = JSON.parse(payload);
              if (evt.text) dispatch({ type: "ATLAS_UPDATE_LAST", delta: evt.text });
            } catch {
              /* malformed SSE chunk */
            }
          }
        }
      } catch (e) {
        if (e.name !== "AbortError") {
          dispatch({ type: "ATLAS_UPDATE_LAST", delta: `\n⚠ ${e.message}` });
        }
      } finally {
        chatAbort.current = null;
        dispatch({ type: "ATLAS_DONE_STREAMING" });
      }
    },
    [state.atlasMessages, state.connection]
  );

  const ctx = {
    state,
    dispatch,
    control,
    sellPosition,
    loadConfig,
    saveConfig,
    atlasChat,
    stopAtlasChat,
    refetchStatus,
    pushToast,
  };
  usePoller(dispatch, state);

  return <AtlasContext.Provider value={ctx}>{children}</AtlasContext.Provider>;
}
