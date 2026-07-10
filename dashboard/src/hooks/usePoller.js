import { useCallback, useEffect, useRef } from "react";
import { api } from "../api/client";

const BASE_INTERVALS = {
  status: 15000,
  positions: 30000,
  balance: 30000,
  metar: 300000,
  logs: 60000,
  candidates: 60000,
  calibration: 300000,
  trades: 300000,
};

export function usePoller(dispatch, state) {
  const timers = useRef({});
  const inFlight = useRef({});
  const abortRefs = useRef({});
  const paused = state.pollingPaused || state.hidden;
  const backoff = state.pollBackoffMs || 0;

  const poll = useCallback(
    async (key, path, updatedKey) => {
      if (inFlight.current[key]) return;
      inFlight.current[key] = true;
      const controller = new AbortController();
      abortRefs.current[key] = controller;
      try {
        const data = await api(path, { signal: controller.signal });
        const payload = { [key]: data };
        dispatch({ type: "FETCH_OK", payload, updated: { [updatedKey || key]: Date.now() } });
      } catch (e) {
        if (e.name !== "AbortError") {
          dispatch({ type: "FETCH_FAIL" });
        }
      } finally {
        inFlight.current[key] = false;
      }
    },
    [dispatch]
  );

  useEffect(() => {
    const onVis = () => dispatch({ type: "SET", payload: { hidden: document.hidden } });
    document.addEventListener("visibilitychange", onVis);
    onVis();
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [dispatch]);

  useEffect(() => {
    Object.values(timers.current).forEach(clearInterval);
    timers.current = {};
    Object.values(abortRefs.current).forEach((c) => c?.abort());
    if (paused) return undefined;

    const schedule = (key, path, baseMs, updatedKey) => {
      const ms = baseMs + (key === "status" ? backoff : 0);
      poll(key, path, updatedKey);
      timers.current[key] = setInterval(() => poll(key, path, updatedKey), ms);
    };

    schedule("status", "/api/status", BASE_INTERVALS.status);
    schedule("positions", "/api/positions", BASE_INTERVALS.positions);
    schedule("balance", "/api/balance", BASE_INTERVALS.balance, "balance");
    schedule("metar", "/api/metar", BASE_INTERVALS.metar);
    schedule("logs", "/api/logs?lines=150", BASE_INTERVALS.logs);
    schedule("candidates", "/api/candidates", BASE_INTERVALS.candidates);
    schedule("calibration", "/api/calibration", BASE_INTERVALS.calibration);
    schedule("trades", "/api/trades?limit=200", BASE_INTERVALS.trades);

    return () => {
      Object.values(timers.current).forEach(clearInterval);
      Object.values(abortRefs.current).forEach((c) => c?.abort());
    };
  }, [paused, poll, backoff]);
}
