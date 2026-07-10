import { useEffect, useRef, useState } from "react";

/** Fires value-pulse animation only on real value changes. */
export function useLiveValue(value) {
  const prev = useRef(value);
  const [pulse, setPulse] = useState(false);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || prev.current === value || value == null) {
      prev.current = value;
      return;
    }
    setPulse(true);
    const id = requestAnimationFrame(() => {
      const t = setTimeout(() => setPulse(false), 600);
      return () => clearTimeout(t);
    });
    prev.current = value;
    return () => cancelAnimationFrame(id);
  }, [value]);

  return pulse ? "live-value updated" : "live-value";
}
