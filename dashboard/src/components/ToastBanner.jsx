import { useEffect } from "react";
import { useAtlasStore } from "../store/AtlasContext";

export default function ToastBanner() {
  const { state, dispatch } = useAtlasStore();
  const toast = state.controls?.toast;

  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => {
      dispatch({ type: "SET_CONTROLS", payload: { toast: null } });
    }, 5000);
    return () => clearTimeout(t);
  }, [toast, dispatch]);

  if (!toast) return null;

  const cls = toast.type === "error" ? "conn-down" : toast.type === "success" ? "conn-ok" : "conn-degraded";
  return (
    <div className={`conn-banner ${cls}`} role="status">
      {toast.message}
    </div>
  );
}
