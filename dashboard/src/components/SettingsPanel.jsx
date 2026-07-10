import { useEffect, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";
import { Button } from "./ui/Button";

const SLIDER_KEYS = {
  KELLY_FRACTION: { min: 0.05, max: 0.5, step: 0.01 },
  NO_SIDE_BIAS: { min: 1.0, max: 1.5, step: 0.01 },
};

export default function SettingsPanel() {
  const { state, loadConfig, saveConfig } = useAtlasStore();
  const [pending, setPending] = useState({});
  const [draft, setDraft] = useState({});

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const config = state.config || {};
  const keys = Object.keys(config).sort();
  const redacted = (k) => /SECRET|KEY|TOKEN|PASSWORD/i.test(k);

  const setLocal = (key, value) => setDraft((d) => ({ ...d, [key]: value }));

  const apply = async (key) => {
    const value = draft[key] ?? config[key];
    setPending((p) => ({ ...p, [key]: true }));
    try {
      await saveConfig(key, value);
    } finally {
      setPending((p) => ({ ...p, [key]: false }));
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-xs text-text-3">Changes apply on next scan. Redacted keys are read-only.</p>
      <div className="overflow-auto max-h-[160px]">
        <table className="w-full text-xs font-mono">
          <thead className="text-text-3 uppercase text-[10px]">
            <tr className="border-b border-border">
              <th className="text-left py-1">Key</th>
              <th className="text-left py-1">Value</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => {
              const locked = redacted(k);
              const val = draft[k] ?? config[k];
              const slider = SLIDER_KEYS[k];
              return (
                <tr key={k} className="border-b border-border/50">
                  <td className="py-2 pr-4">{k}</td>
                  <td className="py-2">
                    {locked ? (
                      <span className="text-text-3">••• 🔒</span>
                    ) : k === "DRY_RUN" ? (
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={val === "1" || val === "true" || val === true}
                          onChange={(e) => setLocal(k, e.target.checked ? "1" : "0")}
                        />
                        {val === "1" || val === "true" ? "DRY RUN" : "LIVE"}
                      </label>
                    ) : slider ? (
                      <div className="flex items-center gap-2">
                        <input
                          type="range"
                          min={slider.min}
                          max={slider.max}
                          step={slider.step}
                          value={Number(val) || slider.min}
                          onChange={(e) => setLocal(k, e.target.value)}
                          className="flex-1"
                        />
                        <span className="tabular-nums w-12">{Number(val).toFixed(2)}</span>
                      </div>
                    ) : (
                      <input
                        className="bg-surface-2 border border-border rounded px-2 py-1 w-full max-w-xs"
                        value={val ?? ""}
                        onChange={(e) => setLocal(k, e.target.value)}
                      />
                    )}
                  </td>
                  <td className="py-2 pl-2">
                    {!locked && k !== "MODEL_VERSION" && (
                      <Button pending={pending[k]} onClick={() => apply(k)}>
                        Apply
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
