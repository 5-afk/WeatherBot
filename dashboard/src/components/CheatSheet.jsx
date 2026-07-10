import { useAtlasStore } from "../store/AtlasContext";

const SHORTCUTS = [
  ["S", "Trigger scan"],
  ["K", "Killswitch (type KILL to confirm in LIVE)"],
  ["P", "Pause / Resume"],
  ["A", "Toggle ATLAS drawer"],
  ["?", "This cheat sheet"],
];

export default function CheatSheet() {
  const { state, dispatch } = useAtlasStore();
  if (!state.ui.cheatsheetOpen) return null;

  return (
    <div className="modal-overlay" onClick={() => dispatch({ type: "SET_UI", payload: { cheatsheetOpen: false } })}>
      <div className="modal" style={{ maxWidth: 400 }} onClick={(e) => e.stopPropagation()}>
        <div className="panel-header">
          <span>Keyboard Shortcuts</span>
          <button className="btn text-xs" onClick={() => dispatch({ type: "SET_UI", payload: { cheatsheetOpen: false } })}>
            ✕
          </button>
        </div>
        <div className="panel-body space-y-3">
          {SHORTCUTS.map(([k, desc]) => (
            <div key={k} className="flex items-center gap-3">
              <span className="shortcut-key">{k}</span>
              <span className="text-sm text-muted">{desc}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
