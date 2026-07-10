export const initialState = {
  status: null,
  positions: [],
  balance: null,
  metar: [],
  logs: [],
  candidates: [],
  calibration: null,
  trades: null,
  config: null,
  connection: "ok",
  failCount: 0,
  pollBackoffMs: 0,
  pollingPaused: false,
  hidden: false,
  controls: {
    pending: null,
    lastError: null,
    toast: null,
  },
  ui: {
    atlasOpen: false,
    settingsOpen: false,
    cheatsheetOpen: false,
    killswitchConfirm: false,
    logFilter: "",
  },
  atlasMessages: [],
  atlasPending: null,
  atlasStreaming: false,
  lastUpdated: {},
};

export function reducer(state, action) {
  switch (action.type) {
    case "SET":
      return { ...state, ...action.payload };
    case "SET_UI":
      return { ...state, ui: { ...state.ui, ...action.payload } };
    case "FETCH_OK":
      return {
        ...state,
        ...action.payload,
        connection: "ok",
        failCount: 0,
        pollBackoffMs: 0,
        lastUpdated: { ...state.lastUpdated, ...action.updated },
      };
    case "FETCH_FAIL": {
      const fc = state.failCount + 1;
      const backoff = Math.min(60000, Math.round((state.pollBackoffMs || 15000) * 1.5));
      return {
        ...state,
        failCount: fc,
        pollBackoffMs: backoff,
        connection: fc >= 3 ? "down" : "degraded",
      };
    }
    case "SET_CONTROLS":
      return { ...state, controls: { ...state.controls, ...action.payload } };
    case "ATLAS_MSG":
      return { ...state, atlasMessages: [...state.atlasMessages, action.msg] };
    case "ATLAS_UPDATE_LAST":
      return {
        ...state,
        atlasMessages: state.atlasMessages.map((m, i) =>
          i === state.atlasMessages.length - 1 ? { ...m, content: m.content + action.delta } : m
        ),
      };
    case "ATLAS_PENDING":
      return { ...state, atlasPending: action.pending };
    case "ATLAS_STREAMING":
      return { ...state, atlasStreaming: action.streaming };
    case "ATLAS_DONE_STREAMING":
      return {
        ...state,
        atlasStreaming: false,
        atlasMessages: state.atlasMessages.map((m, i) =>
          i === state.atlasMessages.length - 1 ? { ...m, streaming: false } : m
        ),
      };
    case "CLEAR_ATLAS":
      return { ...state, atlasMessages: [], atlasPending: null };
    default:
      return state;
  }
}
