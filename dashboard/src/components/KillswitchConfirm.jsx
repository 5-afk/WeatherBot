import { useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";
import { Button } from "./ui/Button";

export default function KillswitchConfirm() {
  const { state, dispatch, control } = useAtlasStore();
  const [typed, setTyped] = useState("");
  if (!state.ui.killswitchConfirm) return null;

  const isLive = state.status?.mode === "LIVE";
  const canConfirm = !isLive || typed === "KILL";

  const confirm = () => {
    control("whetherbot", isLive ? "killswitch" : "stop");
    dispatch({ type: "SET_UI", payload: { killswitchConfirm: false } });
    setTyped("");
  };

  return (
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={() => dispatch({ type: "SET_UI", payload: { killswitchConfirm: false } })}
    >
      <div
        className="bg-surface border border-red/50 rounded-lg p-6 max-w-md w-full mx-4 shadow-[var(--red-glow)]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <h2 className="text-red font-semibold uppercase text-sm mb-2">Kill Switch</h2>
        <p className="text-sm text-text-2 mb-4">
          {isLive
            ? "LIVE mode — this stops the bot and pauses trading. Type KILL to confirm."
            : "Stop the bot process?"}
        </p>
        {isLive && (
          <input
            autoFocus
            className="w-full mb-4 bg-surface-2 border border-border rounded px-3 py-2 font-mono text-sm uppercase"
            placeholder="Type KILL"
            value={typed}
            onChange={(e) => setTyped(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && canConfirm && confirm()}
          />
        )}
        <div className="flex gap-2 justify-end">
          <Button onClick={() => dispatch({ type: "SET_UI", payload: { killswitchConfirm: false } })}>
            Cancel
          </Button>
          <Button variant="danger" disabled={!canConfirm} onClick={confirm}>
            Confirm Kill
          </Button>
        </div>
      </div>
    </div>
  );
}
