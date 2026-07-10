import { AtlasProvider } from "./store/AtlasContext";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import MissionControl from "./components/MissionControl";
import ConnectionBanner from "./components/ConnectionBanner";
import ToastBanner from "./components/ToastBanner";
import BotCards from "./components/BotCards";
import PositionTracker from "./components/PositionTracker";
import BudgetRisk from "./components/BudgetRisk";
import MetarGrid from "./components/MetarGrid";
import Candidates from "./components/Candidates";
import CalibrationPanel from "./components/CalibrationPanel";
import TradeHistory from "./components/TradeHistory";
import ScanFeed from "./components/ScanFeed";
import AtlasDrawer from "./components/AtlasDrawer";
import SettingsModal from "./components/SettingsModal";
import CheatSheet from "./components/CheatSheet";
import KillswitchConfirm from "./components/KillswitchConfirm";

function AppInner() {
  useKeyboardShortcuts();
  return (
    <div className="flex flex-col h-full">
      <MissionControl />
      <ConnectionBanner />
      <ToastBanner />
      <div className="flex-1 overflow-auto">
        <div className="grid-12">
          <BotCards />
          <PositionTracker />
          <BudgetRisk />
          <MetarGrid />
          <Candidates />
          <CalibrationPanel />
          <TradeHistory />
          <ScanFeed />
        </div>
      </div>
      <AtlasDrawer />
      <SettingsModal />
      <CheatSheet />
      <KillswitchConfirm />
    </div>
  );
}

export default function App() {
  return (
    <AtlasProvider>
      <AppInner />
    </AtlasProvider>
  );
}
