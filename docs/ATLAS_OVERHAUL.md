# ATLAS Dashboard Overhaul — Technical Specification

Corrected spec for the WhetherBot ATLAS Command Center (React/Vite + Flask).

## Architecture

- **Frontend:** `dashboard/` — Vite dev server on `:5173`, production build served from Flask `dashboard/dist`
- **API:** `src/api.py` — Flask on `ATLAS_PORT` (default 5000)
- **Control:** `src/atlas_control.py` → discord_launcher subprocess, `BotController` fallback, or systemd
- **Agent telemetry:** `src/atlas_client.py` — register + heartbeat from `main.py`

## Authentication

All `POST /api/*` routes require header `X-Atlas-Secret` matching `DASHBOARD_SECRET` in `.env`.

```python
import hmac, os
from flask import request, abort

SECRET = os.environ.get("DASHBOARD_SECRET")

def check_secret():
    if not SECRET:
        abort(500, "DASHBOARD_SECRET not configured")
    sent = request.headers.get("X-Atlas-Secret", "")
    if not hmac.compare_digest(sent, SECRET):
        abort(401)
```

Frontend (`dashboard/.env.local`):

```
VITE_DASHBOARD_SECRET=<same value as DASHBOARD_SECRET>
```

Restart `pnpm dev` after editing `.env.local`.

## CORS

flask-cors must allow `X-Atlas-Secret` and `OPTIONS` for `/api/*`.

Dev alternative: Vite proxy in `vite.config.js`:

```js
proxy: { "/api": "http://127.0.0.1:5000" }
```

Leave `VITE_API_URL` empty in dev for same-origin requests (no preflight).

## Fetch wrapper

`dashboard/src/api/client.js` must:

1. Check `res.ok` before parsing JSON
2. Throw with status + body text on failure
3. Send `authHeaders()` on all POST requests

## Control flow

1. Button → `control(bot, action)` with pending state + toast
2. `POST /api/control` with secret header
3. `execute_control()` → launcher / BotController / systemd
4. On success: toast + refetch `/api/status`
5. On error: toast with message (never silent `console.error` only)

## Process management

- **PID file:** `data/main.pid` for `main.py` (BotController fallback)
- **Heartbeat:** `data/heartbeat.ts` touched each scan loop in `trader.run_full_pipeline()`
- **Status:** `stopped | running | paused | stale` (stale = PID alive but heartbeat > 2× scan interval)
- **Production:** `ATLAS_USE_SYSTEMD=1` shells to `systemctl --user`
- **LAN bind:** `ATLAS_LAN=1` binds `0.0.0.0` (opt-in)

## SQLite concurrency

On every API and risk_manager connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

## Trades query — JOIN warning

**Never** use `positions p LEFT JOIN calibration_log c ON c.ticker = p.ticker` — fans out rows.

Use latest calibration per ticker:

```sql
LEFT JOIN (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY bet_placed_at DESC) rn
  FROM calibration_log
) c ON c.ticker = p.ticker AND c.rn = 1
```

Current `/api/trades` queries `calibration_log` directly (safe).

## ATLAS chat (SSE)

- Model: `claude-sonnet-5` (env `ANTHROPIC_MODEL` override)
- Server builds system prompt + live state in `_build_system_prompt()`
- `client.messages.stream()` relayed as `text/event-stream`
- Headers: `X-Accel-Buffering: no`, `Cache-Control: no-cache`
- Client: **fetch + ReadableStream** (not EventSource) with `authHeaders()`
- Prompt caching: `cache_control: {"type": "ephemeral"}` on system block when > ~1024 tokens

## Server runtime

- Dev: `atlas.run(threaded=True)`
- Production: `ATLAS_PRODUCTION=1` → waitress with 8 threads

## Security

- Rate-limit `/api/control` and `/api/sell` (~2 req/s per IP)
- `/api/config` redacts KEY/SECRET/TOKEN/PASSWORD vars
- Refuse config writes to redacted keys
- `ATLAS_LAN=0` by default (localhost only)

## UI patterns

- Chart.js (not recharts): Brier doughnut gauge, CLV line, calibration bars
- `tabular-nums` on all numeric displays
- Flash-on-change via `FlashValue` with `prefers-reduced-motion` guard
- Scan countdown from `status.next_scan_at` (1s client tick)
- Connection banner + toast for API/control errors
