import { useEffect, useRef, useState } from "react";

export default function FlashValue({ value, format, className = "" }) {
  const prev = useRef(value);
  const [flash, setFlash] = useState("");
  const reducedMotion =
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  useEffect(() => {
    if (reducedMotion) {
      prev.current = value;
      return undefined;
    }
    if (prev.current !== value && value != null && prev.current != null) {
      const n = parseFloat(value);
      const p = parseFloat(prev.current);
      if (!isNaN(n) && !isNaN(p)) {
        setFlash(n > p ? "flash-up" : n < p ? "flash-down" : "");
        const t = setTimeout(() => setFlash(""), 600);
        prev.current = value;
        return () => clearTimeout(t);
      }
    }
    prev.current = value;
    return undefined;
  }, [value, reducedMotion]);

  const display = format ? format(value) : value;
  return (
    <span className={`tabular-nums ${className} ${flash} inline-block rounded px-0.5`}>{display ?? "—"}</span>
  );
}
