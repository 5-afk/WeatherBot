import { useAtlasStore } from "../store/AtlasContext";

export default function KillswitchConfirm() {
  const { state, dispatch } = useAtlasStore();
  if (!state.ui.killswitchConfirm) return null;

  return (
    <div className="killswitch-overlay">
      <div className="modal" style={{ maxWidth: 420, border: "2px solid var(--loss)" }}>
        <div className="panel-header" style={{ borderColor: "var(--loss)" }}>
          <span className="text-loss">⚠ KILLSWITCH — LIVE MODE</span>
        </div>
        <div className="panel-body text-center">
          <p className="text-sm mb-4">This will pause trading and stop the bot process.</p>
          <p className="mono text-lg text-loss mb-4">
            Type <span className="text-gold font-bold">KILL</span> to confirm
          </p>
          <button className="btn" onClick={() => dispatch({ type: "SET_UI", payload: { killswitchConfirm: false } })}>
            Cancel (Esc)
          </button>
        </div>
      </div>
    </div>
  );
}
