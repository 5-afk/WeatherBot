# Kalshi Weather Bot

This is a beginner-friendly Python 3.11+ trading bot for Kalshi weather prediction markets. It starts in dry-run mode, watches NWS forecast update times, scans KXHIGH/KXLOW markets, uses NWS gridded hourly forecasts with a normal-distribution probability model, asks Claude for a conservative sanity check, and only then simulates or places a limit order.

## What It Does

- Runs the full pipeline at 03:30, 09:30, 15:30, and 21:30 UTC when new NWS forecast data should be available.
- Runs a backup scan every 30 minutes.
- Watches 13 US cities (KXHIGH/KXLOW/KXLOWT markets).
- Pulls free NWS gridded hourly forecast data (no API key, no rate limits).
- Estimates trade probability using NWS forecast + normal CDF (sigma 3.5°F).
- Trades only in dry-run mode unless `DRY_RUN=false`.
- Uses SQLite at `data/positions.db` and JSON P&L at `data/pnl.json`.

## Windows Setup

1. Open PowerShell in this folder:

```powershell
cd C:\Users\Crazy\OneDrive\Desktop\TradingBot\kalshi-weather-bot
```

2. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

4. Create your local `.env` file:

```powershell
Copy-Item .env.example .env
```

5. Edit `.env` and add your keys. Keep `DRY_RUN=true` until logs look correct.

6. Start the bot:

```powershell
python main.py
```

## Important Safety Notes

This is not financial advice. Prediction markets can lose money quickly. The bot has hard stops for daily loss, monthly loss, drawdown, duplicate tickers, max open positions, and price extremes, but those controls do not remove market risk.

Live trading requires valid Kalshi API credentials and `DRY_RUN=false`. The bot always submits limit orders and never submits market orders.

## Files

- `main.py` starts the bot and scheduler.
- `src/nws_watcher.py` schedules model-cycle and backup scans.
- `src/trader.py` runs the full market pipeline.
- `src/kalshi_client.py` talks to Kalshi.
- `src/weather_client.py` talks to NWS gridded forecasts and station observations.
- `src/edge_engine.py` calculates probabilities, confidence, and edge.
- `src/claude_checker.py` asks Claude for GO/NOGO.
- `src/position_sizer.py` calculates Kelly position size.
- `src/risk_manager.py` enforces hard stops and writes SQLite data.
- `data/pnl.json` tracks dry-run/live open risk and simulated P&L.
- `logs/bot.log` stores every bet, skip, cycle, and error.
