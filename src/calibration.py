"""
Calibration analytics for WhetherBot feedback loop.

Measures:
- Brier score: overall forecast accuracy (lower = better, 0 = perfect)
- Calibration curve: predicted probability vs actual win rate by bucket
- Forecast error by city: NWS bias vs actual settlement temps
- CLV: closing line value (positive = finding real edge)
- Win rate by hours-until-settlement: validates intraday sigma collapse
- Sigma accuracy: is the city sigma correct?

Run after every 10+ settled trades to identify biggest source of error.
Make ONE fix at a time. Re-run to confirm improvement.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


class CalibrationEngine:
    """Analyze settled calibration_log rows and produce improvement reports."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def get_settled_trades(self) -> list[dict[str, Any]]:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM calibration_log
                WHERE actual_outcome IS NOT NULL
                AND model_probability IS NOT NULL
                ORDER BY bet_placed_at ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def brier_score(self, trades: list[dict[str, Any]]) -> float | None:
        """
        Brier score = mean((predicted_prob - actual_outcome)^2)
        Lower is better. Perfect = 0.0. Random = 0.25.
        Target: < 0.10 for a well-calibrated model.
        """
        if not trades:
            return None
        scores = []
        for t in trades:
            p = t["model_probability"]
            o = 1.0 if t["bet_won"] else 0.0
            scores.append((p - o) ** 2)
        return round(sum(scores) / len(scores), 4)

    def calibration_curve(self, trades: list[dict[str, Any]], buckets: int = 5) -> list[dict[str, Any]]:
        """
        Group trades by predicted probability bucket.
        For each bucket: show predicted vs actual win rate.
        Perfect calibration = predicted == actual in every bucket.
        """
        bucket_size = 1.0 / buckets
        bucket_data: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"wins": 0, "total": 0, "probs": []}
        )

        for t in trades:
            p = t["model_probability"]
            bucket_idx = min(int(p / bucket_size), buckets - 1)
            bucket_data[bucket_idx]["wins"] += t["bet_won"]
            bucket_data[bucket_idx]["total"] += 1
            bucket_data[bucket_idx]["probs"].append(p)

        results = []
        for idx in sorted(bucket_data.keys()):
            d = bucket_data[idx]
            if d["total"] == 0:
                continue
            lo = idx * bucket_size * 100
            hi = (idx + 1) * bucket_size * 100
            predicted = sum(d["probs"]) / len(d["probs"])
            actual = d["wins"] / d["total"]
            results.append({
                "bucket": f"{lo:.0f}-{hi:.0f}%",
                "predicted": round(predicted, 3),
                "actual": round(actual, 3),
                "count": d["total"],
                "error": round(actual - predicted, 3),
                "overconfident": actual < predicted,
            })
        return results

    def forecast_error_by_city(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Average NWS forecast error per city.
        forecast_error = actual_settlement_temp - nws_forecast
        Positive = NWS underforecast (actual was hotter than predicted)
        Negative = NWS overforecast (actual was cooler than predicted)
        """
        city_data: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"errors": [], "wins": 0, "total": 0}
        )
        for t in trades:
            if t.get("forecast_error_f") is not None:
                city = t.get("city", t.get("station_id", "unknown"))
                city_data[city]["errors"].append(t["forecast_error_f"])
                city_data[city]["wins"] += t["bet_won"]
                city_data[city]["total"] += 1

        results = []
        for city, d in sorted(city_data.items()):
            errors = d["errors"]
            avg_error = sum(errors) / len(errors)
            results.append({
                "city": city,
                "avg_forecast_error_f": round(avg_error, 2),
                "bias_direction": "NWS underforecasts" if avg_error > 0 else "NWS overforecasts",
                "suggested_correction": round(-avg_error, 2),
                "win_rate": round(d["wins"] / d["total"], 3) if d["total"] else 0,
                "trade_count": d["total"],
            })
        return sorted(results, key=lambda x: abs(x["avg_forecast_error_f"]), reverse=True)

    def clv_analysis(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Closing Line Value analysis.
        Positive average CLV = finding real edge before market adjusts.
        Negative CLV = market was right, we were wrong.
        Target: average CLV > 0.03
        """
        clvs = [t["clv"] for t in trades if t.get("clv") is not None]
        if not clvs:
            return {"avg_clv": None, "positive_clv_pct": None, "count": 0}
        pos = sum(1 for c in clvs if c > 0)
        avg = sum(clvs) / len(clvs)
        return {
            "avg_clv": round(avg, 4),
            "positive_clv_pct": round(pos / len(clvs) * 100, 1),
            "count": len(clvs),
            "verdict": (
                "✅ Finding real edge" if avg > 0.03
                else "⚠️ Edge questionable" if avg > 0
                else "❌ Market knows more than us"
            ),
        }

    def win_rate_by_hours(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Win rate bucketed by hours until settlement at time of bet.
        Validates intraday sigma collapse — later bets should be more accurate.
        """
        buckets = [
            ("0-4h", 0, 4),
            ("4-8h", 4, 8),
            ("8-12h", 8, 12),
            ("12-18h", 12, 18),
            ("18-24h", 18, 24),
            ("24-36h", 24, 36),
        ]
        results = []
        for label, lo, hi in buckets:
            subset = [
                t for t in trades
                if t.get("hours_until_settlement") is not None
                and lo <= t["hours_until_settlement"] < hi
            ]
            if not subset:
                continue
            wins = sum(t["bet_won"] for t in subset)
            results.append({
                "window": label,
                "win_rate": round(wins / len(subset), 3),
                "trade_count": len(subset),
                "avg_predicted_prob": round(
                    sum(t["model_probability"] for t in subset) / len(subset), 3
                ),
            })
        return results

    def sigma_accuracy(self, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Check if the sigma used for each city matches actual forecast errors.
        If sigma_used >> actual MAE: model is too uncertain (underconfident)
        If sigma_used << actual MAE: model is too certain (overconfident)
        """
        city_sigma: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: {"sigma_vals": [], "errors": []}
        )
        for t in trades:
            if t.get("sigma_used") and t.get("forecast_error_f") is not None:
                city = t.get("city", t.get("station_id", "unknown"))
                city_sigma[city]["sigma_vals"].append(t["sigma_used"])
                city_sigma[city]["errors"].append(abs(t["forecast_error_f"]))

        results = []
        for city, d in sorted(city_sigma.items()):
            avg_sigma = sum(d["sigma_vals"]) / len(d["sigma_vals"])
            actual_mae = sum(d["errors"]) / len(d["errors"])
            ratio = actual_mae / avg_sigma if avg_sigma > 0 else None
            results.append({
                "city": city,
                "sigma_used": round(avg_sigma, 2),
                "actual_mae_f": round(actual_mae, 2),
                "ratio": round(ratio, 2) if ratio else None,
                "verdict": (
                    "✅ Well calibrated" if ratio and 0.8 <= ratio <= 1.2
                    else "⬆️ Increase sigma" if ratio and ratio > 1.2
                    else "⬇️ Decrease sigma" if ratio and ratio < 0.8
                    else "insufficient data"
                ),
                "suggested_sigma": round(actual_mae * 1.1, 2),
                "trade_count": len(d["errors"]),
            })
        return sorted(results, key=lambda x: abs((x["ratio"] or 1) - 1), reverse=True)

    def full_report(self) -> dict[str, Any]:
        """Generate complete calibration report."""
        trades = self.get_settled_trades()
        if not trades:
            return {"error": "No settled trades in calibration log yet"}

        brier = self.brier_score(trades)
        return {
            "total_settled_trades": len(trades),
            "overall_win_rate": round(sum(t["bet_won"] for t in trades) / len(trades), 3),
            "total_profit": round(sum(t.get("profit", 0) or 0 for t in trades), 2),
            "brier_score": brier,
            "brier_verdict": (
                "✅ Excellent (<0.10)" if brier is not None and brier < 0.10
                else "⚠️ Acceptable (0.10-0.20)"
                if brier is not None and brier < 0.20
                else "❌ Poor (>0.20) — model needs recalibration"
            ),
            "calibration_curve": self.calibration_curve(trades),
            "forecast_error_by_city": self.forecast_error_by_city(trades),
            "clv_analysis": self.clv_analysis(trades),
            "win_rate_by_hours": self.win_rate_by_hours(trades),
            "sigma_accuracy": self.sigma_accuracy(trades),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
