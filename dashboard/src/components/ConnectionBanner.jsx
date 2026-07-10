import { useAtlasStore } from "../store/AtlasContext";

export default function ConnectionBanner() {
  const { state } = useAtlasStore();
  if (state.connection === "ok") return null;
  const cls = state.connection === "down" ? "conn-down" : "conn-degraded";
  const msg =
    state.connection === "down"
      ? "CONNECTION DOWN — API unreachable. Retrying…"
      : "CONNECTION DEGRADED — last fetch failed";
  return <div className={`conn-banner ${cls}`}>{msg}</div>;
}
