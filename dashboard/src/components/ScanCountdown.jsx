import { useEffect, useState } from "react";
import { useAtlasStore } from "../store/AtlasContext";

function formatCountdown(seconds) {
  if (seconds == null || seconds < 0) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function ScanCountdown() {
  const { state } = useAtlasStore();
  const nextAt = state.status?.next_scan_at;
  const [remaining, setRemaining] = useState(null);

  useEffect(() => {
    const tick = () => {
      if (!nextAt) {
        setRemaining(null);
        return;
      }
      const target = new Date(nextAt).getTime();
      const diff = Math.max(0, Math.floor((target - Date.now()) / 1000));
      setRemaining(diff);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [nextAt]);

  if (!nextAt) return null;

  return (
    <span className="mono text-xs text-dim">
      NEXT SCAN <span className="text-data tabular-nums">{formatCountdown(remaining)}</span>
    </span>
  );
}
