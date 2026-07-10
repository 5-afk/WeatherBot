import { useEffect, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";

export default function SettingsModal() {
  const { state, dispatch, loadConfig, saveConfig } = useAtlasStore();
  const [editKey, setEditKey] = useState("");
  const [editVal, setEditVal] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (state.ui.settingsOpen) loadConfig();
  }, [state.ui.settingsOpen, loadConfig]);

  if (!state.ui.settingsOpen) return null;

  const config = state.config || {};
  const keys = Object.keys(config).sort();

  const handleSave = async () => {
    if (!editKey) return;
    setSaving(true);
    try {
      await saveConfig(editKey, editVal);
      setEditKey("");
      setEditVal("");
    } catch (e) {
      alert(e.message);
    }
    setSaving(false);
  };

  return (
    <div className="modal-overlay" onClick={() => dispatch({ type: "SET_UI", payload: { settingsOpen: false } })}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="panel-header">
          <span>Settings</span>
          <button className="btn text-xs" onClick={() => dispatch({ type: "SET_UI", payload: { settingsOpen: false } })}>
            ✕
          </button>
        </div>
        <div className="panel-body">
          <div className="flex gap-2 mb-4">
            <input
              className="flex-1 bg-[var(--bg-2)] border border-[var(--line)] rounded px-2 py-1 text-xs mono"
              placeholder="KEY"
              value={editKey}
              onChange={(e) => setEditKey(e.target.value)}
              list="config-keys"
            />
            <datalist id="config-keys">
              {keys.map((k) => (
                <option key={k} value={k} />
              ))}
            </datalist>
            <input
              className="flex-1 bg-[var(--bg-2)] border border-[var(--line)] rounded px-2 py-1 text-xs mono"
              placeholder="VALUE"
              value={editVal}
              onChange={(e) => setEditVal(e.target.value)}
            />
            <button className="btn btn-primary text-xs" onClick={handleSave} disabled={saving}>
              SAVE
            </button>
          </div>
          <div className="overflow-auto scrollbar" style={{ maxHeight: 400 }}>
            <table className="data">
              <thead>
                <tr>
                  <th>Key</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr
                    key={k}
                    className="cursor-pointer"
                    onClick={() => {
                      setEditKey(k);
                      setEditVal(config[k]);
                    }}
                  >
                    <td>{k}</td>
                    <td className="text-muted">{config[k]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
