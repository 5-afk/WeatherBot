# src/api.py — ATLAS API
# Run standalone:  python -m src.api
# Or launched by discord_launcher.py in a daemon thread/process.

"""Flask API for ATLAS Command Center — bot state, controls, and AI assistant."""

from __future__ import annotations

import json
import hmac
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import zoneinfo
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "positions.db"
AGENTS_FILE = DATA_DIR / "agents.json"
CANDIDATES_FILE = DATA_DIR / "candidates.json"
LOG_PATH = PROJECT_ROOT / "logs" / "bot.log"
ENV_PATH = PROJECT_ROOT / ".env"
ET = zoneinfo.ZoneInfo("America/New_York")

load_dotenv(dotenv_path=ENV_PATH)

# TTL caches: {key: (value, monotonic_ts)}
_cache: dict[str, tuple[Any, float]] = {}
_cache_lock = threading.Lock()

# Agent registry
AGENTS: dict[str, dict[str, Any]] = {}
_agents_lock = threading.Lock()

# improvemodel jobs
_improve_jobs: dict[str, dict[str, Any]] = {}
_improve_lock = threading.Lock()

# ATLAS chat pending action confirmations (LIVE mode)
_pending_actions: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()

# Config allowlist for POST /api/config (beyond existing .env keys)
CONFIG_ALLOWLIST = {
    "MODEL_VERSION", "MIN_EV", "MIN_EDGE", "MIN_EV_PER_CONTRACT", "KELLY_FRACTION",
    "MIN_TRADEABLE_PRICE", "MAX_TRADEABLE_PRICE", "MIN_BET_USD", "MAX_BET_USD",
    "MAX_BANKROLL_DEPLOYMENT", "POSITION_COUNT_CEILING", "DAILY_LOSS_LIMIT",
    "MONTHLY_LOSS_LIMIT", "MAX_CLAUDE_CALLS_PER_DAY",
    "MIN_SIGNAL_SCORE", "MIN_CONFIDENCE", "CITY_COUNT", "SCAN_INTERVAL_MINUTES",
    "DRY_RUN", "KALSHI_ENV",
}

STATIONS = [
    ("KLAX", "Los Angeles"), ("KNYC", "New York"), ("KMDW", "Chicago"),
    ("KMIA", "Miami"), ("KDEN", "Denver"), ("KOKC", "Oklahoma City"),
    ("KBOS", "Boston"), ("KDCA", "Washington DC"), ("KSEA", "Seattle"),
    ("KSFO", "San Francisco"), ("KATL", "Atlanta"), ("KDFW", "Dallas"),
    ("KMSP", "Minneapolis"),
]

atlas = Flask(__name__, static_folder=None)
CORS(
    atlas,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Content-Type", "X-Atlas-Secret"],
    methods=["GET", "POST", "OPTIONS"],
    max_age=600,
    supports_credentials=False,
)

DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")
_MUTATE_LAST: dict[str, float] = {}
_MUTATE_LOCK = threading.Lock()
_MUTATE_MIN_INTERVAL = 0.2  # ~5 req/s per IP on mutating endpoints


def check_secret() -> None:
    """Fail closed unless X-Atlas-Secret matches DASHBOARD_SECRET."""
    if not DASHBOARD_SECRET:
        abort(500, description="DASHBOARD_SECRET not configured")
    sent = request.headers.get("X-Atlas-Secret", "")
    if not hmac.compare_digest(sent, DASHBOARD_SECRET):
        abort(401, description="Unauthorized")


@atlas.before_request
def _atlas_before_request() -> Any:
    """Auth POST mutations and rate-limit control endpoints."""
    if request.method == "OPTIONS":
        return None
    path = request.path
    if request.method == "POST" and path.startswith("/api/"):
        check_secret()
        ip = request.remote_addr or "unknown"
        now = time.monotonic()
        with _MUTATE_LOCK:
            last = _MUTATE_LAST.get(ip, 0.0)
            if now - last < _MUTATE_MIN_INTERVAL:
                return _err("Rate limited — wait before retrying", 429)
            _MUTATE_LAST[ip] = now
    return None


def _ts() -> str:
    return datetime.now(ET).isoformat()


def _ok(data: Any, code: int = 200):
    return jsonify({"ok": True, "data": data, "ts": _ts()}), code


def _err(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message, "ts": _ts()}), code


def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        if key in _cache:
            val, ts = _cache[key]
            if now - ts < ttl:
                return val, now - ts
    val = fn()
    with _cache_lock:
        _cache[key] = (val, now)
    return val, 0.0


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _load_agents() -> None:
    global AGENTS
    if not AGENTS_FILE.exists():
        return
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
        with _agents_lock:
            AGENTS.update(data)
    except Exception as exc:
        logging.warning("Failed to load agents.json: %s", exc)


def _save_agents() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _agents_lock:
        AGENTS_FILE.write_text(json.dumps(AGENTS, indent=2), encoding="utf-8")


def _mark_stale_agents() -> None:
    now = datetime.now(timezone.utc)
    with _agents_lock:
        for agent in AGENTS.values():
            hb = agent.get("last_heartbeat")
            if not hb:
                continue
            try:
                ts = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts).total_seconds() > 180:
                    agent["status"] = "error"
                    agent["stale"] = True
            except Exception:
                pass


def _is_dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").strip().lower() in {"1", "true", "yes"}


def _is_live() -> bool:
    return not _is_dry_run()


def _mode_label() -> str:
    return "DRY_RUN" if _is_dry_run() else "LIVE"


def _killswitch_active() -> bool:
    from src.bot_control import is_paused

    if is_paused():
        return True
    try:
        with _db_connect() as conn:
            row = conn.execute(
                "SELECT value FROM risk_state WHERE key='permanent_halt'"
            ).fetchone()
        return row is not None and str(row[0]).lower() in {"true", "1"}
    except Exception:
        return False


def _scalar(query: str, params: tuple = (), default: float = 0.0) -> float:
    if not DB_PATH.exists():
        return default
    try:
        with _db_connect() as conn:
            row = conn.execute(query, params).fetchone()
        return default if row is None else float(row[0])
    except Exception:
        return default


def _risk_state(key: str, default: str = "") -> str:
    if not DB_PATH.exists():
        return default
    try:
        with _db_connect() as conn:
            row = conn.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row[0])
    except Exception:
        return default


def _dynamic_max_positions() -> int:
    import math

    min_bet = float(os.getenv("MIN_BET_USD", "10"))
    max_dep = float(os.getenv("MAX_BANKROLL_DEPLOYMENT", "0.70"))
    ceiling = int(os.getenv("POSITION_COUNT_CEILING", "6"))
    bankroll = float(_risk_state("running_budget", "100") or 100)
    if min_bet <= 0 or bankroll <= 0:
        return 1
    return max(1, min(ceiling, math.floor(max_dep * bankroll / min_bet)))


def _get_balance_live() -> tuple[float | None, str, float]:
    def fetch():
        from src.kalshi_client import KalshiClient

        client = KalshiClient()
        bal = client.get_balance()
        if bal is not None:
            return {"balance": round(float(bal), 2), "source": "kalshi"}
        raise RuntimeError("Kalshi balance unavailable")

    try:
        result, age = _cached("balance", 30.0, fetch)
        return result["balance"], result["source"], age
    except Exception:
        row_bal = _scalar(
            "SELECT running_budget FROM budget_history ORDER BY created_at DESC LIMIT 1",
            default=0.0,
        )
        if row_bal <= 0:
            row_bal = float(_risk_state("running_budget", "0") or 0)
        return round(row_bal, 2), "db_fallback", 999.0


def _portfolio_value() -> float:
    balance, _, _ = _get_balance_live()
    exposure = _scalar(
        "SELECT COALESCE(SUM(stake), 0) FROM positions WHERE status='open' AND dry_run=0"
    )
    return round((balance or 0) + exposure, 2)


def _daily_pnl() -> float:
    today = datetime.now(ET).date().isoformat()
    realized = _scalar(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM pnl WHERE DATE(created_at)=?",
        (today,),
    )
    return round(realized, 2)


def _parse_log_level(line: str) -> str:
    """
    Log level mapping for /api/logs:
    - BET custom level / 'placed order' / '[DRY RUN]' bet lines -> BET
    - '[SETTLEMENT] WON' / 'settled WIN' -> WIN
    - '[SETTLEMENT] LOST' / 'settled LOSS' -> LOSS
    - '[SKIP]' / 'rejected' / Claude rejected -> SKIP
    - 'candidate' / CANDIDATE level -> CANDIDATE
    - ERROR / CRITICAL / Traceback -> ERROR
    - default -> INFO
    """
    upper = line.upper()
    if " BET " in f" {upper} " or "[DRY RUN]" in upper and "@" in line:
        return "BET"
    if "SETTLEMENT] WON" in upper or "SETTLED WIN" in upper:
        return "WIN"
    if "SETTLEMENT] LOST" in upper or "SETTLED LOSS" in upper:
        return "LOSS"
    if "[SKIP]" in upper or "REJECTED" in upper and "CLAUDE" in upper:
        return "SKIP"
    if "CANDIDATE" in upper:
        return "CANDIDATE"
    if " ERROR " in f" {upper} " or "CRITICAL" in upper or "TRACEBACK" in upper:
        return "ERROR"
    return "INFO"


def _parse_log_line(line: str) -> dict[str, str] | None:
    if not line.strip():
        return None
    ts = ""
    msg = line
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+(.*)$", line)
    if m:
        ts, _lvl, msg = m.group(1), m.group(2), m.group(3)
    level = _parse_log_level(line)
    bot = "whetherbot"
    if "LAUNCHER" in line:
        bot = "launcher"
    return {"ts": ts, "bot": bot, "level": level, "msg": msg}


def _redact_key(key: str) -> bool:
    upper = key.upper()
    return any(x in upper for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))


def _read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if _redact_key(key):
            result[key] = "•••redacted•••"
        else:
            result[key] = val.strip()
    return result


def _write_env_key(key: str, value: str) -> None:
    if _redact_key(key):
        raise PermissionError("Cannot write redacted key")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            seen = True
            break
    if not seen:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (DATA_DIR / "config_dirty.flag").write_text(_ts(), encoding="utf-8")


# --- Routes ---

DASHBOARD_DIST = PROJECT_ROOT / "dashboard" / "dist"


@atlas.route("/")
def dashboard():
    """Serve the Vite-built React dashboard (run `pnpm build` in dashboard/)."""
    built = DASHBOARD_DIST / "index.html"
    if built.exists():
        return send_file(built)
    # ATLAS-NOTE: fallback to Vite dev index when dist not built yet
    dev_index = PROJECT_ROOT / "dashboard" / "index.html"
    if dev_index.exists():
        return _err("Dashboard not built — run `cd dashboard && pnpm build`", 503)
    return _err("Dashboard not found", 404)


@atlas.route("/assets/<path:filename>")
def dashboard_assets(filename: str):
    """Serve Vite build assets."""
    assets_dir = DASHBOARD_DIST / "assets"
    if not assets_dir.exists():
        return _err("Dashboard assets not found — run pnpm build", 404)
    return send_from_directory(assets_dir, filename)


@atlas.route("/api/agents/register", methods=["POST"])
def agents_register():
    body = request.get_json(silent=True) or {}
    agent_id = body.get("id")
    if not agent_id:
        return _err("id required", 400)
    with _agents_lock:
        existing = AGENTS.get(agent_id, {})
        AGENTS[agent_id] = {**existing, **body, "last_heartbeat": _ts()}
    _save_agents()
    return _ok(AGENTS[agent_id])


@atlas.route("/api/agents/heartbeat", methods=["POST"])
def agents_heartbeat():
    body = request.get_json(silent=True) or {}
    agent_id = body.get("id")
    if not agent_id:
        return _err("id required", 400)
    with _agents_lock:
        if agent_id not in AGENTS:
            return _err(f"Agent {agent_id} not registered", 404)
        AGENTS[agent_id].update({k: v for k, v in body.items() if k != "id"})
        AGENTS[agent_id]["last_heartbeat"] = _ts()
        AGENTS[agent_id].pop("stale", None)
    _save_agents()
    return _ok({"id": agent_id, "status": AGENTS[agent_id].get("status")})


@atlas.route("/api/agents")
def agents_list():
    _mark_stale_agents()
    with _agents_lock:
        return _ok(list(AGENTS.values()))


def _bot_process_status() -> dict[str, Any]:
    """Truthful process status from PID file, heartbeat, and agent registry."""
    from src.bot_controller import BotController

    ctrl = BotController()
    proc_status = ctrl.status()
    pid = ctrl.pid()
    next_scan_at = None
    heartbeat_age_s = None
    hb_path = DATA_DIR / "heartbeat"
    scan_in_progress = (DATA_DIR / "scan.trigger").exists()
    if hb_path.exists():
        try:
            heartbeat_age_s = round(time.time() - hb_path.stat().st_mtime, 1)
        except Exception:
            pass
    with _agents_lock:
        agent = AGENTS.get("whetherbot") or {}
    if agent.get("next_scan"):
        next_scan_at = agent["next_scan"]
    elif agent.get("last_scan", {}).get("ts"):
        try:
            last = datetime.fromisoformat(str(agent["last_scan"]["ts"]).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            interval = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
            next_scan_at = (last + timedelta(minutes=interval)).astimezone(ET).isoformat()
        except Exception:
            pass
    merged_status = proc_status
    if proc_status == "running" and agent.get("status") == "paused":
        merged_status = "paused"
    return {
        "process_status": merged_status,
        "pid": pid,
        "heartbeat_age_s": heartbeat_age_s,
        "next_scan_at": next_scan_at,
        "scan_in_progress": scan_in_progress,
    }


@atlas.route("/api/status")
def api_status():
    _mark_stale_agents()
    with _agents_lock:
        agents = list(AGENTS.values())
    bot_proc = _bot_process_status()
    return _ok({
        "mode": _mode_label(),
        "killswitch": _killswitch_active(),
        "portfolio_value": _portfolio_value(),
        "daily_pnl": _daily_pnl(),
        "running_budget": float(_risk_state("running_budget", "100") or 100),
        "todays_budget": max(0, float(_risk_state("running_budget", "100") or 100) - _scalar(
            "SELECT COALESCE(SUM(stake),0) FROM positions WHERE DATE(opened_at)=DATE('now') AND status='open' AND dry_run=0"
        )),
        "daily_loss": abs(min(0, _daily_pnl())),
        "daily_loss_limit": float(os.getenv("DAILY_LOSS_LIMIT", "225")),
        "monthly_loss": abs(min(0, _scalar(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM pnl WHERE SUBSTR(created_at,1,7)=strftime('%Y-%m','now')"
        ))),
        "monthly_loss_limit": float(os.getenv("MONTHLY_LOSS_LIMIT", "500")),
        "drawdown_pct": _compute_drawdown(),
        "open_positions": int(_scalar("SELECT COUNT(*) FROM positions WHERE status='open' AND dry_run=0")),
        "max_positions": _dynamic_max_positions(),
        "agents": agents,
        "bot_process_status": bot_proc["process_status"],
        "bot_pid": bot_proc["pid"],
        "heartbeat_age_s": bot_proc["heartbeat_age_s"],
        "next_scan_at": bot_proc["next_scan_at"],
        "scan_in_progress": bot_proc.get("scan_in_progress", False),
    })


def _compute_drawdown() -> float:
    peak = float(_risk_state("peak_balance", "0") or 0)
    current = float(_risk_state("running_budget", "100") or 100)
    if peak <= 0:
        return 0.0
    return round(max(0, (peak - current) / peak) * 100, 1)


@atlas.route("/api/positions")
def api_positions():
    from src.edge_engine import estimate_probability
    from src.kalshi_client import KalshiClient
    from src.metar_tracker import MetarTracker
    from src.weather_client import WeatherClient

    if not DB_PATH.exists():
        return _ok([])

    kalshi = KalshiClient()
    weather = WeatherClient()
    metar = MetarTracker(DATA_DIR / "metar_obs.db")
    rows: list[dict[str, Any]] = []

    with _db_connect() as conn:
        positions = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()

    for pos in positions:
        p = dict(pos)
        ticker = p["ticker"]
        side = str(p["side"]).upper()
        contracts = int(p["contracts"])
        avg_price = float(p["price"])
        cost = float(p["stake"])
        bot = "whetherbot"

        market = kalshi.get_market(ticker)
        market_price = avg_price
        if market:
            market_price = (market.yes_bid if side == "YES" else market.no_bid) or avg_price
        market_value = round(contracts * market_price, 2)
        unrealized = round(market_value - cost, 2)
        payout = round(contracts * 1.0, 2)

        settlement_ts = None
        hours_to = None
        threshold = None
        station = ""
        title = ticker
        if market:
            title = market.title or ticker
            if market.settlement_time:
                settlement_ts = market.settlement_time.astimezone(ET).isoformat()
                hours_to = round(
                    (market.settlement_time - datetime.now(timezone.utc)).total_seconds() / 3600, 1
                )
            raw = market.raw
            threshold = float(raw.get("floor_strike") or raw.get("cap_strike") or 0)
            rules = str(raw.get("rules_primary", ""))
            station = weather.parse_settlement_station(rules) or ""

        city = p.get("city") or ticker.split("-", 1)[0]
        metar_max = None
        if station:
            obs = metar.update_station(station)
            if obs:
                metar_max = obs.get("daily_max_f")

        settle_prob = 0.5
        settle_source = "heuristic"
        if market and threshold and station:
            try:
                target_date = kalshi._target_date_from_ticker(ticker)
                market_type = "high" if "HIGH" in ticker.upper() else "low"
                strike_type = str(market.raw.get("strike_type", "greater"))
                city_cfg = weather.city_for_market(ticker.split("-", 1)[0], ticker)
                forecast = weather.get_station_forecast(
                    station, target_date, market_type, city=city_cfg
                ) if target_date and city_cfg else None
                if forecast and forecast.temperature_f is not None:
                    yes_prob = estimate_probability(
                        forecast.temperature_f, threshold, market_type,
                        strike_type=strike_type, station_id=station,
                        target_date=target_date, hours_until_settlement=hours_to or 12,
                    )
                    settle_prob = yes_prob if side == "YES" else (1 - yes_prob)
                    settle_source = "model"
            except Exception as exc:
                logging.debug("settle_prob fallback: %s", exc)
                if metar_max is not None and threshold:
                    diff = metar_max - threshold
                    settle_prob = 0.65 if (side == "YES" and diff >= 0) or (side == "NO" and diff < 0) else 0.35

        rows.append({
            "id": p["id"],
            "bot": bot,
            "ticker": ticker,
            "title": title,
            "city": city,
            "station": station,
            "side": side,
            "contracts": contracts,
            "avg_price": round(avg_price, 4),
            "cost": round(cost, 2),
            "market_price": round(market_price, 4),
            "market_value": market_value,
            "unrealized_pnl": unrealized,
            "payout_if_win": payout,
            "settlement_ts": settlement_ts,
            "hours_to_settlement": hours_to,
            "metar_max_today": metar_max,
            "threshold": threshold,
            "settle_prob": round(settle_prob, 3),
            "settle_prob_source": settle_source,
        })

    return _ok(rows)


@atlas.route("/api/balance")
def api_balance():
    balance, source, age = _get_balance_live()
    return _ok({"balance": balance, "source": source, "cached_age_s": round(age, 1)})


@atlas.route("/api/metar")
def api_metar():
    from src.metar_tracker import MetarTracker

    metar = MetarTracker(DATA_DIR / "metar_obs.db")
    thresholds_by_station: dict[str, list[tuple[str, float]]] = {}
    if DB_PATH.exists():
        with _db_connect() as conn:
            for row in conn.execute(
                "SELECT ticker, side FROM positions WHERE status='open'"
            ).fetchall():
                ticker, side = row[0], str(row[1]).upper()
                from src.kalshi_client import KalshiClient
                m = KalshiClient().get_market(ticker)
                if not m:
                    continue
                rules = str(m.raw.get("rules_primary", ""))
                from src.weather_client import WeatherClient
                st = WeatherClient().parse_settlement_station(rules)
                th = float(m.raw.get("floor_strike") or m.raw.get("cap_strike") or 0)
                if st and th:
                    thresholds_by_station.setdefault(st, []).append((side, th))

    def fetch_all():
        out = []
        for station, city in STATIONS:
            obs = metar.update_station(station)
            if not obs:
                out.append({"station": station, "city": city, "alert": "green", "temp_f": None})
                continue
            trend_raw = metar.get_temperature_trend(station) or "stable"
            trend = "rising" if "rising" in trend_raw.lower() or "warm" in trend_raw.lower() else (
                "falling" if "fall" in trend_raw.lower() or "cool" in trend_raw.lower() else "stable"
            )
            max_f = obs.get("daily_max_f", obs["temp_f"])
            alert = "green"
            for side, th in thresholds_by_station.get(station, []):
                diff = abs(max_f - th)
                against = (side == "YES" and max_f >= th) or (side == "NO" and max_f < th)
                if against or diff <= 1.5:
                    alert = "red"
                    break
                if diff <= 4.0:
                    alert = "yellow"
            wind = f"{obs.get('wind_dir', '')}@{obs.get('wind_speed_kt', '')}KT"
            out.append({
                "station": station,
                "city": city,
                "temp_f": obs["temp_f"],
                "max_today_f": max_f,
                "min_today_f": obs.get("daily_min_f", obs["temp_f"]),
                "trend": trend,
                "wind": wind,
                "sky": str(obs.get("sky_cover", "")),
                "raw": obs.get("raw_metar", ""),
                "obs_ts": obs.get("obs_time_utc", ""),
                "alert": alert,
            })
        return out

    data, age = _cached("metar", 300.0, fetch_all)
    return _ok(data)


@atlas.route("/api/calibration")
def api_calibration():
    from src.calibration import CalibrationEngine

    engine = CalibrationEngine(str(DB_PATH))
    report = engine.full_report()
    if "error" in report:
        return _ok({
            "brier": None, "trade_count": 0, "clv_series": [],
            "calibration_curve": [], "forecast_error_by_city": [],
            "sigma_table": [], "winrate_by_lead": [],
        })

    trades = engine.get_settled_trades()
    clv_series = [
        {"trade_id": t["id"], "date": (t.get("bet_placed_at") or "")[:10], "clv": t.get("clv")}
        for t in trades[-30:]
        if t.get("clv") is not None
    ]
    curve = [
        {"bucket": b["bucket"], "predicted": b["predicted"], "actual": b["actual"], "n": b["count"]}
        for b in report.get("calibration_curve", [])
    ]
    forecast = [
        {"city": e["city"], "bias_f": e["avg_forecast_error_f"], "mae_f": abs(e["avg_forecast_error_f"]), "n": e["trade_count"]}
        for e in report.get("forecast_error_by_city", [])
    ]
    sigma_table = [
        {"city": s["city"], "sigma": s["sigma_used"], "actual_mae": s["actual_mae_f"],
         "verdict": s["verdict"].replace("✅ ", "").replace("⬆️ ", "").replace("⬇️ ", "")}
        for s in report.get("sigma_accuracy", [])
    ]
    winrate = [
        {"bucket": w["window"], "winrate": w["win_rate"], "n": w["trade_count"]}
        for w in report.get("win_rate_by_hours", [])
    ]
    return _ok({
        "brier": report.get("brier_score"),
        "trade_count": report.get("total_settled_trades", 0),
        "clv_series": clv_series,
        "calibration_curve": curve,
        "forecast_error_by_city": forecast,
        "sigma_table": sigma_table,
        "winrate_by_lead": winrate,
    })


@atlas.route("/api/trades")
def api_trades():
    bot = request.args.get("bot", "")
    city = request.args.get("city", "")
    side = request.args.get("side", "")
    outcome = request.args.get("outcome", "")
    dry_run = request.args.get("dry_run", "")
    limit = min(int(request.args.get("limit", "200")), 500)

    if not DB_PATH.exists():
        return _ok({"trades": [], "summary": {}})

    # ATLAS-NOTE: ROW_NUMBER prevents calibration_log fan-out when joining on ticker alone.
    query = """
        SELECT
            p.id AS position_id,
            p.ticker,
            p.city,
            p.side,
            p.stake AS position_stake,
            p.realized_pnl,
            p.opened_at,
            p.closed_at,
            p.dry_run,
            c.id AS cal_id,
            c.bet_placed_at,
            c.model_probability,
            c.ev,
            c.signal_score,
            c.nws_forecast_f,
            c.actual_settlement_temp_f,
            c.forecast_error_f,
            c.profit,
            c.payout,
            c.bet_won,
            c.clv
        FROM positions p
        LEFT JOIN (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY id DESC) AS rn
            FROM calibration_log
        ) c ON c.ticker = p.ticker AND c.rn = 1
        WHERE p.status = 'closed'
        ORDER BY COALESCE(c.bet_placed_at, p.closed_at, p.opened_at) DESC
        LIMIT ?
    """
    with _db_connect() as conn:
        rows = [dict(r) for r in conn.execute(query, (limit,)).fetchall()]

    trades = []
    for r in rows:
        if dry_run == "0" and int(r.get("dry_run") or 0) == 1:
            continue
        if dry_run == "1" and int(r.get("dry_run") or 0) == 0:
            continue
        if city and city.lower() not in str(r.get("city", "")).lower():
            continue
        side_val = str(r.get("side", "")).upper()
        if side and side.upper() != side_val:
            continue
        profit = float(r.get("profit") if r.get("profit") is not None else r.get("realized_pnl") or 0)
        won = bool(r.get("bet_won")) if r.get("bet_won") is not None else profit > 0
        oc = "win" if won else "loss"
        if outcome and outcome.lower() != oc:
            continue
        stake = float(r.get("position_stake") or r.get("stake") or 0)
        trades.append({
            "id": r.get("cal_id") or r.get("position_id"),
            "position_id": r.get("position_id"),
            "date": (r.get("bet_placed_at") or r.get("closed_at") or r.get("opened_at") or "")[:10],
            "bot": "whetherbot",
            "ticker": r["ticker"],
            "city": r.get("city", ""),
            "side": side_val,
            "stake": round(stake, 2),
            "payout": round(float(r.get("payout") or 0), 2),
            "profit": round(profit, 2),
            "outcome": oc,
            "nws_forecast_f": r.get("nws_forecast_f"),
            "actual_temp_f": r.get("actual_settlement_temp_f"),
            "forecast_error_f": r.get("forecast_error_f"),
            "signal_score": r.get("signal_score"),
            "ev": r.get("ev"),
            "model_probability": r.get("model_probability"),
            "clv": r.get("clv"),
        })

    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    wr_all = len(wins) / len(trades) if trades else 0
    avg_win = sum(t["profit"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["profit"] for t in losses) / len(losses) if losses else 0
    cum = 0.0
    equity = []
    for t in reversed(trades):
        cum += t["profit"]
        equity.append({"date": t["date"], "cum_pnl": round(cum, 2)})

    return _ok({
        "trades": trades,
        "summary": {
            "total_trades": len(trades),
            "total_pnl": round(sum(t["profit"] for t in trades), 2),
            "winrate_all": round(wr_all, 3),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "equity_curve": equity,
        },
    })


@atlas.route("/api/logs")
def api_logs():
    lines_n = min(int(request.args.get("lines", "100")), 500)
    level_filter = request.args.get("level", "").upper()
    bot_filter = request.args.get("bot", "")

    if not LOG_PATH.exists():
        return _ok([])

    raw_lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-lines_n:]
    parsed = []
    for line in raw_lines:
        entry = _parse_log_line(line)
        if not entry:
            continue
        if level_filter and entry["level"] != level_filter:
            continue
        if bot_filter and entry["bot"] != bot_filter:
            continue
        parsed.append(entry)
    return _ok(parsed)


@atlas.route("/api/candidates")
def api_candidates():
    if CANDIDATES_FILE.exists():
        try:
            data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
            return _ok(data.get("candidates", []))
        except Exception:
            pass
    return _ok([])


@atlas.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return _ok(_read_env())

    body = request.get_json(silent=True) or {}
    key = body.get("key", "").strip()
    value = str(body.get("value", ""))
    if not key:
        return _err("key required", 400)
    if _redact_key(key):
        return _err("Cannot modify redacted key", 403)
    existing = _read_env()
    if key not in existing and key not in CONFIG_ALLOWLIST and not key.startswith(("SIGMA_OVERRIDE_", "BIAS_")):
        return _err(f"Key {key} not allowed", 403)
    try:
        _write_env_key(key, value)
        load_dotenv(dotenv_path=ENV_PATH, override=True)
    except PermissionError as exc:
        return _err(str(exc), 403)
    except Exception as exc:
        logging.exception("config write failed")
        return _err(str(exc), 500)
    return _ok({"key": key, "value": value, "config_dirty": True})


@atlas.route("/api/control", methods=["POST"])
def api_control():
    from src.atlas_control import ControlError, execute_control

    body = request.get_json(silent=True) or {}
    bot = body.get("bot", "whetherbot")
    action = body.get("action", "")
    if not action:
        return _err("action required", 400)
    try:
        result = execute_control(bot, action)
        with _agents_lock:
            if bot in AGENTS:
                AGENTS[bot]["status"] = result.get("status", AGENTS[bot].get("status"))
            elif bot == "all":
                for a in AGENTS.values():
                    a["status"] = "stopped"
        _save_agents()
        return _ok(result)
    except ControlError as exc:
        return _err(str(exc), exc.code)
    except Exception as exc:
        logging.exception("control failed")
        return _err(str(exc), 500)


@atlas.route("/api/sell", methods=["POST"])
def api_sell():
    body = request.get_json(silent=True) or {}
    position_id = body.get("position_id")
    if position_id is None:
        return _err("position_id required", 400)

    if not DB_PATH.exists():
        return _err("No database", 404)

    with _db_connect() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=? AND status='open'", (position_id,)).fetchone()
    if not row:
        return _err("Position not found or already closed", 404)

    pos = dict(row)
    if _is_dry_run():
        return _ok({"sold": False, "reason": "DRY RUN — sell blocked", "dry_run": True})

    from src.kalshi_client import KalshiClient

    k = KalshiClient()
    result = k.sell_position(pos["ticker"], str(pos["side"]).lower(), int(pos["contracts"]))
    if result["filled"]:
        proceeds = result["contracts_filled"] * result["price"]
        cost = float(pos["stake"])
        pnl = round(proceeds - cost, 2)
        from src.risk_manager import RiskManager
        RiskManager(DB_PATH).record_resolution(pos["ticker"], pnl)
        return _ok({"sold": True, "fill_price": result["price"], "pnl": pnl})
    return _ok({"sold": False, "reason": "No immediate fill — low liquidity"})


def _run_improvemodel(job_id: str) -> None:
    from src.calibration import CalibrationEngine

    try:
        engine = CalibrationEngine(str(DB_PATH))
        trades = engine.get_settled_trades()
        if len(trades) < 10:
            rec = {"error": f"Only {len(trades)} trades — need 10+"}
        else:
            report = engine.full_report()
            rec = {"report": report, "recommendation": _improvemodel_pick(report, len(trades))}
        with _improve_lock:
            _improve_jobs[job_id] = {"status": "done", "result": rec}
    except Exception as exc:
        with _improve_lock:
            _improve_jobs[job_id] = {"status": "error", "result": {"error": str(exc)}}


def _improvemodel_pick(report: dict, n: int) -> dict:
    """Single recommendation — mirrors discord_launcher !improvemodel logic."""
    errors = report.get("forecast_error_by_city", [])
    sigmas = report.get("sigma_accuracy", [])
    clv = report.get("clv_analysis", {})
    curve = report.get("calibration_curve", [])
    recommendations = []
    if clv.get("avg_clv") is not None and clv["avg_clv"] < -0.02:
        recommendations.append({"priority": 1, "issue": "Negative CLV", "fix": "Raise MIN_SIGNAL_SCORE", "impact": "HIGH"})
    for s in sigmas:
        if s.get("trade_count", 0) >= 5 and s.get("ratio") and s["ratio"] > 1.3:
            recommendations.append({"priority": 2, "issue": f"{s['city']} sigma too small", "fix": f"Increase sigma to {s.get('suggested_sigma')}", "impact": "HIGH"})
    for e in errors:
        if e.get("trade_count", 0) >= 5 and abs(e.get("avg_forecast_error_f", 0)) > 2.0:
            recommendations.append({"priority": 3, "issue": f"{e['city']} bias", "fix": f"Update bias by {e.get('suggested_correction')}", "impact": "MEDIUM"})
    for bucket in curve:
        if bucket.get("count", 0) >= 5 and bucket.get("error", 0) < -0.10:
            recommendations.append({"priority": 4, "issue": f"Overconfident {bucket['bucket']}", "fix": "Increase sigma", "impact": "MEDIUM"})
    if not recommendations:
        return {"message": f"No issues in {n} trades. Brier={report.get('brier_score')}"}
    return sorted(recommendations, key=lambda x: x["priority"])[0]


@atlas.route("/api/improvemodel", methods=["POST"])
def api_improvemodel_start():
    job_id = str(uuid.uuid4())[:8]
    with _improve_lock:
        _improve_jobs[job_id] = {"status": "running", "result": None}
    threading.Thread(target=_run_improvemodel, args=(job_id,), daemon=True).start()
    return _ok({"job_id": job_id})


@atlas.route("/api/improvemodel/<job_id>")
def api_improvemodel_poll(job_id: str):
    with _improve_lock:
        job = _improve_jobs.get(job_id)
    if not job:
        return _err("Job not found", 404)
    return _ok(job)


# --- ATLAS Chat ---

def _build_system_prompt() -> str:
    _mark_stale_agents()
    with _agents_lock:
        agents = list(AGENTS.values())
    status_data = {
        "mode": _mode_label(),
        "killswitch": _killswitch_active(),
        "portfolio_value": _portfolio_value(),
        "daily_pnl": _daily_pnl(),
        "open_positions": int(_scalar("SELECT COUNT(*) FROM positions WHERE status='open' AND dry_run=0")),
        "max_positions": _dynamic_max_positions(),
        "daily_loss": abs(min(0, _daily_pnl())),
        "daily_loss_limit": float(os.getenv("DAILY_LOSS_LIMIT", "225")),
        "agents": agents,
    }
    balance, _, _ = _get_balance_live()
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

    positions: list[dict] = []
    if DB_PATH.exists():
        with _db_connect() as conn:
            for row in conn.execute("SELECT ticker, side, contracts, price, stake FROM positions WHERE status='open' LIMIT 20"):
                positions.append(dict(row))

    candidates: list = []
    if CANDIDATES_FILE.exists():
        try:
            candidates = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8")).get("candidates", [])[:5]
        except Exception:
            pass

    from src.calibration import CalibrationEngine
    engine = CalibrationEngine(str(DB_PATH))
    report = engine.full_report()
    cal = {"brier": report.get("brier_score"), "trade_count": report.get("total_settled_trades", 0),
           "forecast_error_by_city": report.get("forecast_error_by_city", [])}

    pos_lines = [f"{p.get('ticker')} {p.get('side')} {p.get('contracts')}@{p.get('price')}" for p in positions]
    cand_lines = [f"{c.get('ticker')} {c.get('side')} EV={c.get('ev')} {c.get('claude_decision')}" for c in candidates]
    agents_lines = [f"{a.get('id')} {a.get('status')}" for a in agents]
    last_scan = (agents[0].get("last_scan") if agents else {}) or {}
    worst = (cal.get("forecast_error_by_city") or [{}])[0] if cal.get("forecast_error_by_city") else {}

    errors: list[str] = []
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
            e = _parse_log_line(line)
            if e and e["level"] == "ERROR":
                errors.append(e["msg"])
    errors = errors[-5:]

    return f"""You are ATLAS, the operations assistant for a live Kalshi trading system. You are embedded
in its dashboard and have real-time state below plus tools to inspect and control the bots.

Operator style: terse, precise, quantitative. Lead with the answer. Use tickers and numbers,
not filler. Never invent data — if state below or a tool can't answer it, say so.

Safety: this system trades real money when MODE=LIVE. You cannot execute trades or control the bot —
you analyze and recommend only. Direct the operator to Bot Controls or Settings for actions.
Summarize risks plainly. Never invent data.

=== CURRENT STATE ({now_et}) ===
MODE: {status_data.get('mode')} | KILLSWITCH: {status_data.get('killswitch')}
BALANCE: ${balance} | PORTFOLIO: ${status_data.get('portfolio_value')} | DAY P&L: {status_data.get('daily_pnl'):+}
AGENTS: {'; '.join(agents_lines) or 'none'}
OPEN POSITIONS ({len(positions)}): {' | '.join(pos_lines) or 'none'}
LAST SCAN: {last_scan.get('ts', 'unknown')}, {last_scan.get('markets_checked', 0)} markets, {last_scan.get('candidates', 0)} candidates
TOP CANDIDATES: {' | '.join(cand_lines) or 'none'}
CALIBRATION: brier {cal.get('brier')} over {cal.get('trade_count')} trades | worst city bias: {worst.get('city')} {worst.get('bias_f')}
RISK: positions {status_data.get('open_positions')}/{status_data.get('max_positions')} | daily loss ${status_data.get('daily_loss')}/${status_data.get('daily_loss_limit')}
RECENT ERRORS (if any): {' | '.join(errors) or 'none'}
"""


def _tool_handlers() -> dict[str, Callable]:
    return {
        "get_positions": lambda _: json.loads(api_positions()[0].get_data())["data"],
        "get_metar": lambda args: [
            m for m in json.loads(api_metar()[0].get_data())["data"]
            if not args.get("station") or m["station"] == args["station"].upper()
        ],
        "get_calibration": lambda _: json.loads(api_calibration()[0].get_data())["data"],
        "get_candidates": lambda _: json.loads(api_candidates()[0].get_data())["data"],
        "get_logs": lambda args: json.loads(api_logs()[0].get_data())["data"],
        "get_config": lambda _: _read_env(),
        "control_bot": lambda args: _control_tool(args),
        "sell_position": lambda args: _sell_tool(args),
    }


def _control_tool(args: dict) -> dict:
    from src.atlas_control import ControlError, execute_control
    try:
        return execute_control(args.get("bot", "whetherbot"), args.get("action", ""))
    except ControlError as exc:
        return {"error": str(exc)}


def _sell_tool(args: dict) -> dict:
    position_id = args.get("position_id")
    if not DB_PATH.exists():
        return {"error": "No database"}
    with _db_connect() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=? AND status='open'", (position_id,)).fetchone()
    if not row:
        return {"error": "Position not found"}
    pos = dict(row)
    if _is_dry_run():
        return {"sold": False, "reason": "DRY RUN — sell blocked"}
    from src.kalshi_client import KalshiClient
    from src.risk_manager import RiskManager
    k = KalshiClient()
    result = k.sell_position(pos["ticker"], str(pos["side"]).lower(), int(pos["contracts"]))
    if result["filled"]:
        pnl = round(result["contracts_filled"] * result["price"] - float(pos["stake"]), 2)
        RiskManager(DB_PATH).record_resolution(pos["ticker"], pnl)
        return {"sold": True, "fill_price": result["price"], "pnl": pnl}
    return {"sold": False, "reason": "No immediate fill"}


@atlas.route("/api/atlas/chat", methods=["POST"])
def atlas_chat():
    from flask import stream_with_context

    body = request.get_json(silent=True) or {}
    messages = body.get("messages", [])
    if not messages:
        return _err("messages required", 400)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _err("ANTHROPIC_API_KEY not configured", 503)

    try:
        import anthropic
    except ImportError:
        return _err("anthropic package not installed", 500)

    client = anthropic.Anthropic(api_key=api_key)
    # ATLAS-NOTE: verify model id against Anthropic docs at deploy time
    model = os.getenv("ATLAS_MODEL", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))

    system = _build_system_prompt()
    system_block: list[dict[str, Any]] = [{"type": "text", "text": system}]
    if len(system) > 4000:
        system_block[0]["cache_control"] = {"type": "ephemeral"}

    chat_messages = [
        {"role": m["role"], "content": m["content"]}
        for m in messages[-20:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    def generate():
        try:
            with client.messages.stream(
                model=model,
                max_tokens=1500,
                system=system_block,
                messages=chat_messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logging.exception("atlas chat error")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream")
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@atlas.route("/api/atlas/confirm", methods=["POST"])
def atlas_confirm():
    body = request.get_json(silent=True) or {}
    req_id = body.get("request_id")
    if not req_id:
        return _err("request_id required", 400)
    with _pending_lock:
        pending = _pending_actions.pop(req_id, None)
    if not pending:
        return _err("Request expired or not found", 404)

    handlers = _tool_handlers()
    handler = handlers.get(pending["name"])
    if not handler:
        return _err("Unknown action", 400)
    try:
        result = handler(pending["args"])
        return _ok({"executed": True, "name": pending["name"], "result": result})
    except Exception as exc:
        logging.exception("confirm failed")
        return _err(str(exc), 500)


def run_api(host: str | None = None, port: int | None = None) -> None:
    """Start the ATLAS API server (waitress in production, Flask dev server otherwise)."""
    _load_agents()
    if host is None:
        host = "0.0.0.0" if os.getenv("ATLAS_LAN", "0").strip() == "1" else "127.0.0.1"
    port = port or int(os.getenv("ATLAS_PORT", "5000"))
    production = os.getenv("ATLAS_PRODUCTION", "0").strip() == "1"
    logging.info("ATLAS API listening on http://%s:%d (production=%s)", host, port, production)
    if production:
        from waitress import serve

        serve(atlas, host=host, port=port, threads=8)
    else:
        atlas.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_api()
