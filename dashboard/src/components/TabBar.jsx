import { useAtlasStore } from "../store/AtlasContext";
import TradeHistory from "./TradeHistory";
import CalibrationPanel from "./CalibrationPanel";
import BudgetRisk from "./BudgetRisk";
import SettingsPanel from "./SettingsPanel";

const TABS = [
  { id: "history", label: "Trade History" },
  { id: "calibration", label: "Calibration" },
  { id: "budget", label: "Budget & Risk" },
  { id: "settings", label: "Settings" },
];

export default function TabBar() {
  const { state, dispatch } = useAtlasStore();
  const active = state.activeTab || "history";

  return (
    <div className="tabs-row h-full min-h-0">
      <div className="flex border-b border-border shrink-0">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.08em] min-h-[36px] ${
              active === t.id ? "text-text border-b-2 border-forge" : "text-text-3 hover:text-text-2"
            }`}
            onClick={() => dispatch({ type: "SET", payload: { activeTab: t.id } })}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto scrollbar p-3">
        {active === "history" && <TradeHistory />}
        {active === "calibration" && <CalibrationPanel />}
        {active === "budget" && <BudgetRisk />}
        {active === "settings" && <SettingsPanel />}
      </div>
    </div>
  );
}
