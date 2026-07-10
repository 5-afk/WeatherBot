import { useEffect, useRef } from "react";
import { useAtlasStore } from "../store/AtlasContext";

export function useKeyboardShortcuts() {
  const { state, dispatch, control } = useAtlasStore();
  const killInput = useRef("");

  useEffect(() => {
    const handler = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") {
        return;
      }

      if (state.ui.killswitchConfirm) {
        if (e.key.length === 1) killInput.current += e.key.toUpperCase();
        if (killInput.current.endsWith("KILL")) {
          control("whetherbot", "killswitch");
          dispatch({ type: "SET_UI", payload: { killswitchConfirm: false } });
          killInput.current = "";
        }
        if (e.key === "Escape") {
          dispatch({ type: "SET_UI", payload: { killswitchConfirm: false } });
          killInput.current = "";
        }
        return;
      }

      switch (e.key.toLowerCase()) {
        case "s":
          e.preventDefault();
          control("whetherbot", "scan");
          break;
        case "k":
          e.preventDefault();
          killInput.current = "";
          if (state.status?.mode === "LIVE") {
            dispatch({ type: "SET_UI", payload: { killswitchConfirm: true } });
          } else {
            control("whetherbot", "killswitch");
          }
          break;
        case "p":
          e.preventDefault();
          control("whetherbot", state.status?.killswitch ? "resume" : "pause");
          break;
        case "a":
          e.preventDefault();
          dispatch({ type: "SET_UI", payload: { atlasOpen: !state.ui.atlasOpen } });
          break;
        case "?":
          e.preventDefault();
          dispatch({ type: "SET_UI", payload: { cheatsheetOpen: !state.ui.cheatsheetOpen } });
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [state, dispatch, control]);
}
