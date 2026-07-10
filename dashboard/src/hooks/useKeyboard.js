import { useEffect } from "react";
import { useAtlasStore } from "../store/AtlasContext";

export function useKeyboard() {
  const { state, dispatch, control } = useAtlasStore();

  useEffect(() => {
    const handler = (e) => {
      const tag = e.target?.tagName;
      const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      const modalOpen =
        state.ui.killswitchConfirm || state.ui.cheatsheetOpen || state.ui.settingsOpen;

      if (inInput && e.key !== "Escape") return;
      if (modalOpen && e.key !== "Escape") return;

      if (e.key === "Escape") {
        dispatch({
          type: "SET_UI",
          payload: { killswitchConfirm: false, cheatsheetOpen: false, settingsOpen: false },
        });
        return;
      }

      if (inInput || modalOpen) return;

      switch (e.key.toLowerCase()) {
        case "s":
          e.preventDefault();
          control("whetherbot", "scan");
          break;
        case "p":
          e.preventDefault();
          control("whetherbot", state.status?.killswitch ? "resume" : "pause");
          break;
        case "k":
          e.preventDefault();
          dispatch({ type: "SET_UI", payload: { killswitchConfirm: true } });
          break;
        case "a":
          e.preventDefault();
          dispatch({ type: "SET_UI", payload: { atlasOpen: !state.ui.atlasOpen } });
          break;
        case "r":
          e.preventDefault();
          control("whetherbot", "restart");
          break;
        case "1":
          dispatch({ type: "SET", payload: { activeTab: "history" } });
          break;
        case "2":
          dispatch({ type: "SET", payload: { activeTab: "calibration" } });
          break;
        case "3":
          dispatch({ type: "SET", payload: { activeTab: "budget" } });
          break;
        case "4":
          dispatch({ type: "SET", payload: { activeTab: "settings" } });
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
