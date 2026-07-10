import FlashValue from "./FlashValue";
import { useAtlasStore } from "../store/AtlasContext";
import { fmtUsd } from "../utils/format";

export default function BudgetRisk() {
  const { state } = useAtlasStore();
  const s = state.status || {};
  const b = state.balance || {};
  const dailyPct = s.daily_loss_limit ? (s.daily_loss / s.daily_loss_limit) * 100 : 0;
  const monthlyPct = s.monthly_loss_limit ? (s.monthly_loss / s.monthly_loss_limit) * 100 : 0;

  return (
    <div className="panel col-5">
      <div className="panel-header">Budget & Risk</div>
      <div className="panel-body space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <div className="text-dim text-xs uppercase mb-1">Balance</div>
            <FlashValue value={b.balance} format={fmtUsd} className="mono text-lg text-gold" />
            <div className="text-dim text-xs mt-1">
              src: {b.source || "—"} {b.cached_age_s != null && `(${b.cached_age_s}s)`}
            </div>
          </div>
          <div>
            <div className="text-dim text-xs uppercase mb-1">Running Budget</div>
            <FlashValue value={s.running_budget} format={fmtUsd} className="mono text-lg" />
          </div>
          <div>
            <div className="text-dim text-xs uppercase mb-1">Today&apos;s Budget</div>
            <FlashValue value={s.todays_budget} format={fmtUsd} className="mono text-lg text-data" />
          </div>
          <div>
            <div className="text-dim text-xs uppercase mb-1">Portfolio Value</div>
            <FlashValue value={s.portfolio_value} format={fmtUsd} className="mono text-lg" />
          </div>
        </div>
        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-dim">Daily Loss</span>
            <span className={dailyPct > 80 ? "text-loss" : ""}>
              {fmtUsd(s.daily_loss)} / {fmtUsd(s.daily_loss_limit)}
            </span>
          </div>
          <div className="progress-bar">
            <div
              className="progress-fill"
              style={{
                width: `${Math.min(dailyPct, 100)}%`,
                background: dailyPct > 80 ? "var(--loss)" : "var(--warn)",
              }}
            />
          </div>
        </div>
        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-dim">Monthly Loss</span>
            <span>
              {fmtUsd(s.monthly_loss)} / {fmtUsd(s.monthly_loss_limit)}
            </span>
          </div>
          <div className="progress-bar">
            <div
              className="progress-fill"
              style={{
                width: `${Math.min(monthlyPct, 100)}%`,
                background: monthlyPct > 80 ? "var(--loss)" : "var(--data)",
              }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
