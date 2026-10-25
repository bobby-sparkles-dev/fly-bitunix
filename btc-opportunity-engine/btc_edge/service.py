"""Read-only research service: continuously ingest, retrain, score, and serve dashboard.

No exchange credentials or order routes. All predictions and synthetic outcomes
are paper-only. Use a persistent DATA_DIR volume for uninterrupted history.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import INTERVAL_MS, Settings
from .market import PROVIDER
from .storage import connect

LOG = logging.getLogger("btc_edge.service")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB = DATA_DIR / ("research.sqlite" if PROVIDER == "binance_global" else f"research-{PROVIDER}.sqlite")
MODEL_DIR = DATA_DIR / ("models" if PROVIDER == "binance_global" else f"models-{PROVIDER}")
STATE = DATA_DIR / "state.json"
PORT = int(os.environ.get("PORT", "8080"))
INTERVAL_SECONDS = 300
TRAIN_INTERVAL_SECONDS = 6 * 3600
_lock = threading.RLock()
_cache = {
    "phase": "STARTING",
    "mode": "PAPER_ONLY",
    "message": "Research service starting; no trading orders are supported.",
    "updated_at": None,
    "score": None,
    "last_error": None,
    "progress": {"stage": "starting", "percent": 0},
    "events": [],
    "heartbeat_at": None,
    "worker_heartbeat_at": None,
    "data_venue": None,
    "retry_at": None,
    "rate_limit": None,
}
_last_train = 0.0
_last_checkpoint = 0.0
_last_paper_digest = None


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_state(**changes) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        _cache.update(changes)
        # Atomically publish last-known state without fabricating freshness.
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(_cache, default=str))
        temporary.replace(STATE)


def event(level: str, message: str) -> None:
    with _lock:
        events = list(_cache.get("events", []))
        events.append({"at": timestamp(), "level": level, "message": message[:400]})
        save_state(events=events[-35:])


def stage(name: str, percent: float, message: str, **extra) -> None:
    progress = {"stage": name, "percent": round(max(0, min(percent, 100)), 1),
                "heartbeat_at": timestamp(), **extra}
    save_state(progress=progress, heartbeat_at=timestamp(), message=message)


def snapshot() -> dict:
    with _lock:
        return json.loads(json.dumps(_cache, default=str))


def ensure_history(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS live_forecasts (
        candle_open_ms INTEGER PRIMARY KEY,
        model_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        probability_market REAL NOT NULL,
        probability_long REAL NOT NULL,
        research_status TEXT NOT NULL,
        candidate INTEGER NOT NULL,
        outcome_net_r REAL,
        outcome_win INTEGER,
        evaluated_at TEXT
    );
    """)
    db.commit()


def record_forecast(db: sqlite3.Connection, score: dict) -> None:
    # One immutable forecast per observed candle; later model retrains cannot rewrite it.
    db.execute("""
      INSERT OR IGNORE INTO live_forecasts
      (candle_open_ms, model_id, observed_at, probability_market,
       probability_long, research_status, candidate)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        int(score["timestamp_open_ms"]), score["model_id"], timestamp(),
        float(score["p_tradeable"]), float(score["p_profitable_btc_long"]),
        score["status"], int(bool(score["candidate"]))
    ))
    db.commit()


def evaluate_matured(db: sqlite3.Connection, candles: dict, features) -> dict:
    """Only evaluate a prediction after its complete forward horizon exists."""
    from .outcomes import build_outcomes
    from .config import Settings
    btc = candles["BTCUSDT"]
    outcome = build_outcomes(btc, features, Settings())
    if outcome.empty:
        return {"observed": 0, "evaluated": 0, "candidates": 0}
    now_close = int(btc.index.max()) + INTERVAL_MS
    future_ms = Settings().horizon_bars * INTERVAL_MS
    pending = db.execute("""
       SELECT candle_open_ms FROM live_forecasts
       WHERE outcome_net_r IS NULL AND candle_open_ms + ? <= ?
       ORDER BY candle_open_ms LIMIT 500
    """, (future_ms + INTERVAL_MS, now_close)).fetchall()
    for (candle_ms,) in pending:
        if candle_ms not in outcome.index:
            continue
        row = outcome.loc[candle_ms]
        db.execute("""
            UPDATE live_forecasts SET outcome_net_r=?, outcome_win=?, evaluated_at=?
            WHERE candle_open_ms=? AND outcome_net_r IS NULL
        """, (float(row["net_r"]), int(row["long_win"]), timestamp(), candle_ms))
    db.commit()
    count, evaluated, candidates, candidate_wins = db.execute("""
        SELECT COUNT(*), COUNT(outcome_win),
          SUM(CASE WHEN candidate=1 AND outcome_win IS NOT NULL THEN 1 ELSE 0 END),
          SUM(CASE WHEN candidate=1 AND outcome_win=1 THEN 1 ELSE 0 END)
        FROM live_forecasts
    """).fetchone()
    return {
        "observed": count or 0, "evaluated": evaluated or 0,
        "candidates_evaluated": candidates or 0,
        "candidate_wins": candidate_wins or 0,
        "note": "Overlapping prediction observations are NOT independent trades or a verified win rate."
    }



def decision_diagnostics(db: sqlite3.Connection, score: dict, ledger: dict) -> dict:
    """Explain observed predictions and the *current* paper entry gate.

    This is an operational explanation, NOT evidence of model accuracy.
    """
    settings = Settings()
    pm = float(score["p_tradeable"])
    pl = float(score["p_profitable_btc_long"])
    total, eligible = db.execute("""
      SELECT COUNT(*), COALESCE(SUM(candidate), 0) FROM live_forecasts
    """).fetchone()
    reasons = []
    if score["status"] == "STALE_DATA":
        reasons.append("Market data is stale")
    if pm < settings.tradeability_threshold:
        reasons.append(f"Market score {pm:.1%} is below {settings.tradeability_threshold:.0%}")
    if pl < settings.long_threshold:
        reasons.append(f"BTC long score {pl:.1%} is below {settings.long_threshold:.0%}")
    if ledger["queued"] or ledger["open"]:
        reasons.append("One paper trade is already queued/open")
    if not reasons:
        reasons.append("Both score thresholds met; paper entry evaluation eligible")
    return {
        "mode": "PAPER_ONLY",
        "evaluated_candles": int(total),
        "score_qualified_candles": int(eligible),
        "market_threshold": settings.tradeability_threshold,
        "long_threshold": settings.long_threshold,
        "current_market_score": round(pm, 4),
        "current_long_score": round(pl, 4),
        "eligible_now": bool(score.get("candidate")) and score["status"] != "STALE_DATA",
        "explanations": reasons,
        "last_decision_open_ms": score["timestamp_open_ms"],
        "note": "An eligible market does not guarantee a fill or profit."
    }


def cycle() -> None:
    """Fetch most recent closed bars, occasionally retrain, log forward scores."""
    global _last_train, _last_checkpoint, _last_paper_digest
    from .market import ingest, PROVIDER, RateLimited
    from .history import coverage, backfill
    from .features import load_candles, build_features
    from .learning import train, latest_score
    from .paper import install as install_paper, settle as settle_paper, queue_signal, summary as paper_summary

    save_state(phase="COLLECTING", message="Downloading completed exchange candles...",
               last_error=None, retry_at=None, rate_limit=None, data_venue=PROVIDER)
    stage("collecting", 0, "Starting candle collection")
    with connect(DB) as db:
        ensure_history(db)
        install_paper(db)
        has_history = db.execute("SELECT COUNT(*) FROM candles WHERE symbol='BTCUSDT'").fetchone()[0]
        days = 37 if has_history < 500 else 2
        event("info", f"Beginning {days}-day candle refresh")
        def report(p):
            stage("collecting", p["percent"] * 0.78,
                  f"Downloading {p['current_symbol']} · {p['completed_symbols']}/{p['total_symbols']} markets complete",
                  collection=p)
        def retry(p):
            event("warning", f"{p['symbol']}: {p['kind']} retry {p['attempt']}/{p['max_attempts']}" +
                  (f" (HTTP {p['code']})" if "code" in p else ""))
        result = ingest(db, days=days, on_progress=report, on_retry=retry)
        event("info", f"Finished candle refresh across {len(result['candles'])} assets")
        stage("features", 80, "Calculating market features")
        archive_before = coverage(db)
        extended_ready = archive_before["all_symbols_ready_for_74_day_window"]
        candles = load_candles(db, days=76 if extended_ready else 45)
        features = build_features(candles)
        stage("features", 85, "Features calculated")
        trained = MODEL_DIR.joinpath("latest.json").exists()
        if not trained or time.time() - _last_train >= TRAIN_INTERVAL_SECONDS:
            save_state(phase="TRAINING", message="Fitting models and testing past-only holdout...")
            stage("training", 88, "Preparing future outcome labels")
            active_settings = Settings(training_days=74, test_days=14, half_life_days=21) if extended_ready else Settings()
            train_result = train(
                features,
                __import__("btc_edge.outcomes", fromlist=["build_outcomes"]).build_outcomes(
                    candles["BTCUSDT"], features, active_settings),
                active_settings,
                MODEL_DIR,
            )
            stage("training", 96, "Training and historical evaluation complete")
            _last_train = time.time()
            LOG.info("Model updated %s", train_result["model_id"])
        stage("scoring", 97, "Scoring latest completed BTC candle")
        score = latest_score(features, MODEL_DIR)
        record_forecast(db, score)
        # Settle only from completed candles, then queue a new position strictly
        # after the forecast was actually observed.
        settle_paper(db, candles["BTCUSDT"])
        if score.get("status") != "STALE_DATA":
            current_atr = float(features.loc[int(score["timestamp_open_ms"]), "atr_at_decision"])
            if queue_signal(db, score, current_atr):
                event("info", "Queued a paper BTC long for the next full five-minute candle")
        ledger = paper_summary(db)
        decisions = decision_diagnostics(db, score, ledger)
        forward = evaluate_matured(db, candles, features)
        stage("complete", 100, "Research cycle complete")
        event("info", f"Research cycle finished · {score['status']}")
        # The free database must not receive a multi-megabyte full snapshot
        # every five minutes. Snapshot at first successful run, on meaningful
        # paper-trade changes, and at most once per hour otherwise.
        digest = (ledger["queued"], ledger["open"], ledger["completed"], ledger["invalid"])
        should_save = (_last_checkpoint == 0.0 or
                       time.time() - _last_checkpoint >= 3600 or
                       digest != _last_paper_digest)
        if os.environ.get("DATABASE_URL") and should_save:
            from .persistence import checkpoint
            checkpoint(DB, MODEL_DIR)
            _last_checkpoint = time.time()
            _last_paper_digest = digest
            event("info", "Saved durable PostgreSQL research checkpoint")
        save_state(
            archive=archive_before, training_window_days=json.loads((MODEL_DIR / "latest.json").read_text())["settings"]["training_days"],
            phase="RUNNING" if score["status"] != "STALE_DATA" else "STALE_DATA",
            message=("Collecting observations and issuing paper-only forecasts."
                     if score["status"] != "STALE_DATA" else "Market data too old; forecasts suspended."),
            updated_at=timestamp(), last_error=None, score=score,
            forward_observations=forward, paper_trades=ledger,
            decision_diagnostics=decisions,
            last_training_at=json.loads((MODEL_DIR / "latest.json").read_text()).get("trained_at"),
            ingest_completed_at=result["completed_at"],
            symbols_collected=len(result["candles"]), database_path=str(DB),
            persistent_volume_configured=bool(os.environ.get("RENDER_DISK_MOUNT_PATH") == str(DATA_DIR)),
        )
        # Lower-priority archival retrieval: never prevents the current model
        # from being scored or its results from being displayed.
        try:
            archive = backfill(db, pages=5,
                on_progress=lambda progress: save_state(archive=progress["coverage"]),
                on_retry=retry)
            save_state(archive=archive)
            if archive["rows_added_this_cycle"]:
                event("info", f"Added {archive['rows_added_this_cycle']} older candle rows to the local archive")
            if archive["archive_complete"] and not archive_before["archive_complete"]:
                event("info", "180-day archive is complete across all tracked markets")
            # Archive additions are included in the next regular hourly durable
            # checkpoint rather than uploading a large DB snapshot every cycle.
        except RateLimited:
            # A venue-wide throttle applies to the entire worker, not merely
            # archive requests: the outer worker parks until the retry deadline.
            raise
        except Exception as archive_error:
            event("warning", "Archive backfill paused: " + str(archive_error)[:220])


def heartbeat() -> None:
    """Independent liveness timestamp, not a claim that candles are progressing."""
    while True:
        try:
            save_state(worker_heartbeat_at=timestamp())
        except Exception:
            LOG.exception("Could not update worker heartbeat")
        time.sleep(15)


def worker() -> None:
    from .market import RateLimited, PROVIDER
    save_state(data_venue=PROVIDER)
    if STATE.is_file():
        try:
            saved = json.loads(STATE.read_text())
            with _lock:
                _cache.update(saved)
            save_state(phase="STARTING", message="Restoring data, checking source freshness...")
        except (OSError, ValueError):
            LOG.warning("Unable to restore previous research status", exc_info=True)
    while True:
        started = time.monotonic()
        try:
            cycle()
        except RateLimited as e:
            LOG.warning("Exchange throttle: %s", e)
            try:
                if DB.is_file() and os.environ.get("DATABASE_URL"):
                    from .persistence import checkpoint
                    checkpoint(DB, MODEL_DIR)
                    event("info", "Saved partial progress in PostgreSQL before API cooldown")
            except Exception as backup_error:
                LOG.exception("Unable to save partial progress")
                event("error", f"Checkpoint failed: {backup_error}")
            next_at = datetime.fromtimestamp(time.time() + e.retry_seconds, timezone.utc).isoformat()
            event("warning", str(e))
            save_state(phase="RATE_LIMITED", rate_limit={"status":e.status,"symbol":e.symbol,
                "retry_seconds":e.retry_seconds}, retry_at=next_at,
                last_error=str(e), message=f"Exchange has limited requests; pausing until {next_at}")
            stage("rate_limited", _cache.get("progress", {}).get("percent", 0),
                  "Exchange rate limit; waiting before further requests")
            time.sleep(max(30, e.retry_seconds))
            continue
        except Exception as e:
            LOG.exception("Research cycle failed")
            event("error", f"{type(e).__name__}: {str(e)[:250]}")
            stage("error", _cache.get("progress", {}).get("percent", 0), "Research cycle failed")
            save_state(phase="ERROR", message="Research cycle failed; previous scores may be stale.",
                       last_error=f"{type(e).__name__}: {str(e)[:250]}", updated_at=timestamp())
        time.sleep(max(30, INTERVAL_SECONDS - (time.monotonic() - started)))


class Handler(BaseHTTPRequestHandler):
    def respond(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            p = ROOT / "preview" / "index.html"
            if p.is_file():
                return self.respond(p.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/market":
            from .api_market import market_snapshot
            try:
                payload = market_snapshot(DB)
                return self.respond(json.dumps(payload).encode(), "application/json; charset=utf-8")
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                LOG.warning("Chart data unavailable: %s", exc)
                return self.respond(
                    json.dumps({"error": "Stored market data is not ready", "details": str(exc)[:180],
                                "source": "backend_saved_completed_candles"}).encode(),
                    "application/json; charset=utf-8", 503)
        if path == "/api/state":
            return self.respond(json.dumps(snapshot()).encode(), "application/json; charset=utf-8")
        if path == "/api/health":
            return self.respond(json.dumps({"service": "ok", "research_phase": snapshot()["phase"]}).encode(),
                                "application/json; charset=utf-8")
        self.respond(b'{"error":"not found"}', "application/json; charset=utf-8", 404)

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if os.environ.get("DATABASE_URL"):
        try:
            from .persistence import restore
            if restore(DB, MODEL_DIR):
                LOG.info("Restored SQLite research from durable PostgreSQL checkpoint")
            else:
                LOG.info("PostgreSQL database connected: no existing checkpoint")
        except Exception:
            LOG.exception("PostgreSQL restore failed; refusing to proceed unsafely")
            raise
    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()
    threading.Thread(target=worker, name="research-worker", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("BTC research dashboard listening on 0.0.0.0:%s, DATA_DIR=%s", PORT, DATA_DIR)
    server.serve_forever()


if __name__ == "__main__":
    main()
