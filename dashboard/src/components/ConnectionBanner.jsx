import { useAtlasStore } from "../store/AtlasContext";

export default function ConnectionBanner() {
  const { state, dispatch } = useAtlasStore();
  if (state.connection === "ok") return null;

  const down = state.connection === "down";
  return (
    <div className={`conn-banner ${down ? "conn-down" : "conn-degraded"} shrink-0`}>
      {down ? "KELLY IS OFFLINE — retrying" : "Connection degraded — last fetch failed"}
      {down && (
        <button
          type="button"
          className="ml-4 underline text-xs uppercase"
          onClick={() => dispatch({ type: "SET", payload: { pollBackoffMs: 0, failCount: 0 } })}
        >
          RETRY NOW
        </button>
      )}
    </div>
  );
}
