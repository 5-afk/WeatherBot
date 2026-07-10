import { useEffect, useState } from "react";
import { AtlasProvider, useAtlasStore } from "./store/AtlasContext";
import { useKeyboard } from "./hooks/useKeyboard";
import Header from "./components/Header";
import ConnectionBanner from "./components/ConnectionBanner";
import Toast from "./components/Toast";
import BotControls from "./components/BotControls";
import LivePositions from "./components/LivePositions";
import MetarGrid from "./components/MetarGrid";
import ScanFeed from "./components/ScanFeed";
import MarketIntel from "./components/MarketIntel";
import TabBar from "./components/TabBar";
import AtlasAI from "./components/AtlasAI";
import CheatSheet from "./components/CheatSheet";
import KillswitchConfirm from "./components/KillswitchConfirm";

const MOBILE_VIEWS = [
  { id: "status", label: "Status" },
  { id: "positions", label: "Positions" },
  { id: "metar", label: "METAR" },
  { id: "history", label: "History" },
  { id: "atlas", label: "ATLAS" },
];

function MobileNav() {
  const { state, dispatch } = useAtlasStore();
  const view = state.mobileView || "status";
  return (
    <nav className="mobile-nav mobile-only shrink-0">
      {MOBILE_VIEWS.map((v) => (
        <button
          key={v.id}
          type="button"
          className={view === v.id ? "active" : ""}
          onClick={() => dispatch({ type: "SET", payload: { mobileView: v.id } })}
        >
          {v.label}
        </button>
      ))}
    </nav>
  );
}

function MobileBody() {
  const { state } = useAtlasStore();
  const view = state.mobileView || "status";
  if (view === "status") return <BotControls />;
  if (view === "positions") return <LivePositions />;
  if (view === "metar") return <MetarGrid />;
  if (view === "history") return <TabBar />;
  if (view === "atlas") return <AtlasAI mobileFull />;
  return null;
}

function AppInner() {
  useKeyboard();
  const { state } = useAtlasStore();
  const [mobile, setMobile] = useState(() => typeof window !== "undefined" && window.innerWidth < 1024);

  useEffect(() => {
    const onResize = () => setMobile(window.innerWidth < 1024);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return (
    <div className={`atlas-root ${mobile ? "mobile" : ""}`}>
      <Header />
      <ConnectionBanner />
      <Toast />

      {mobile ? (
        <div className="min-h-0 overflow-hidden flex flex-col mobile-only">
          <div className="flex-1 min-h-0 overflow-hidden">
            <MobileBody />
          </div>
          {state.mobileView !== "atlas" && <MobileNav />}
        </div>
      ) : (
        <>
          <div className="main-grid min-h-0">
            <div className="grid-bot-controls">
              <BotControls />
            </div>
            <div className="grid-positions">
              <LivePositions />
            </div>
            <div className="grid-metar">
              <MetarGrid />
            </div>
            <div className="grid-scan">
              <ScanFeed />
            </div>
            <div className="grid-intel">
              <MarketIntel />
            </div>
          </div>
          <TabBar />
        </>
      )}

      {!mobile && <AtlasAI />}
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
