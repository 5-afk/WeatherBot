import { useCallback, useEffect, useRef } from "react";
import { api, isOffline } from "../api/client";

const CADENCE = {
  status: 10000,
  positions: 30000,
  balance: 30000,
  logs: 10000,
  candidates: 60000,
  metar: 300000,
};

export function usePoller(dispatch, state) {
  const timers = useRef({});
  const inFlight = useRef({});
  const abortRefs = useRef({});
  const paused = state.pollingPaused || state.hidden;
  const backoff = state.pollBackoffMs || 0;

  const poll = useCallback(
    async (key, path) => {
      if (inFlight.current[key]) return;
      inFlight.current[key] = true;
      const controller = new AbortController();
      abortRefs.current[key] = controller;
      try {
        const data = await api(path, { signal: controller.signal });
        if (isOffline(data)) {
          dispatch({ type: "FETCH_FAIL" });
          return;
        }
        dispatch({ type: "FETCH_OK", payload: { [key]: data }, updated: { [key]: Date.now() } });
      } catch (e) {
        if (e.name !== "AbortError") dispatch({ type: "FETCH_FAIL" });
      } finally {
        inFlight.current[key] = false;
      }
    },
    [dispatch]
  );

  const pollTrades = useCallback(async () => {
    if (state.activeTab !== "history" && !state.trades) return;
    if (inFlight.current.trades) return;
    inFlight.current.trades = true;
    try {
      const data = await api("/api/trades?limit=200");
      if (isOffline(data)) {
        dispatch({ type: "FETCH_FAIL" });
        return;
      }
      dispatch({ type: "FETCH_OK", payload: { trades: data }, updated: { trades: Date.now() } });
    } catch {
      dispatch({ type: "FETCH_FAIL" });
    } finally {
      inFlight.current.trades = false;
    }
  }, [dispatch, state.activeTab, state.trades]);

  const pollCalibration = useCallback(async () => {
    if (state.activeTab !== "calibration" && !state.calibration) return;
    if (inFlight.current.calibration) return;
    inFlight.current.calibration = true;
    try {
      const data = await api("/api/calibration");
      if (isOffline(data)) {
        dispatch({ type: "FETCH_FAIL" });
        return;
      }
      dispatch({ type: "FETCH_OK", payload: { calibration: data }, updated: { calibration: Date.now() } });
    } catch {
      dispatch({ type: "FETCH_FAIL" });
    } finally {
      inFlight.current.calibration = false;
    }
  }, [dispatch, state.activeTab, state.calibration]);

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

    const schedule = (key, path, baseMs) => {
      const ms = key === "status" ? baseMs + backoff : baseMs;
      poll(key, path);
      timers.current[key] = setInterval(() => poll(key, path), ms);
    };

    schedule("status", "/api/status", CADENCE.status);
    schedule("positions", "/api/positions", CADENCE.positions);
    schedule("balance", "/api/balance", CADENCE.balance);
    schedule("logs", "/api/logs?lines=200", CADENCE.logs);
    schedule("candidates", "/api/candidates", CADENCE.candidates);
    schedule("metar", "/api/metar", CADENCE.metar);

    pollTrades();
    pollCalibration();
    timers.current.trades = setInterval(pollTrades, 300000);
    timers.current.calibration = setInterval(pollCalibration, 300000);

    return () => {
      Object.values(timers.current).forEach(clearInterval);
      Object.values(abortRefs.current).forEach((c) => c?.abort());
    };
  }, [paused, poll, pollTrades, pollCalibration, backoff]);
}
