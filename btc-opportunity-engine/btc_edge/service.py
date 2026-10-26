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
        reasons.append("Bitcoin candle data is stale")
    if score.get("missing_alt_feature_count", 0):
        reasons.append(str(score["missing_alt_feature_count"]) +
            " altcoin context features are incomplete: research score only, no paper entry")
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
        "feature_context_complete": bool(score.get("context_complete", True)),
        "missing_alt_features": score.get("missing_alt_features", []),
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
        latest_file = MODEL_DIR / "latest.json"
        trained = latest_file.exists()
        # Re-evaluate existing historical data once when a new audit is added.
        # Otherwise preserve the regular six-hour retraining cadence.
        requires_cost_audit = trained and "cost_audit" not in json.loads(
            latest_file.read_text()).get("report", {})
        if not trained or requires_cost_audit or time.time() - _last_train >= TRAIN_INTERVAL_SECONDS:
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
        LOG.info("BTC SCORE model=%s candle=%s age_min=%s status=%s context_complete=%s missing_alt=%s",
                 score["model_id"], score["timestamp_open_ms"], score["data_age_minutes"],
                 score["status"], score.get("context_complete"),
                 score.get("missing_alt_features", []))
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
            message=("Some altcoin context is missing. The latest BTC candle is scored with"
                     " neutral trained values; paper entries are suspended."
                     if score["status"] == "PARTIAL_CONTEXT" else
                     "Market data too old; forecasts suspended."
                     if score["status"] == "STALE_DATA" else
                     "Collecting observations and issuing paper-only forecasts."),
            updated_at=timestamp(), last_error=None, score=score,
            forward_observations=forward, paper_trades=ledger,
            decision_diagnostics=decisions,
            last_training_at=json.loads((MODEL_DIR / "latest.json").read_text()).get("trained_at"),
            ingest_completed_at=result["completed_at"],
            symbols_collected=len(result["candles"]), database_path=str(DB),
            persistent_volume_configured=bool(os.environ.get("RENDER_DISK_MOUNT_PATH") == str(DATA_DIR)),
        )
        # Archive has its own status, counts and durable checkpoint. Any work
        # acknowledged as saved is backed by Neon before the cycle completes.
        try:
            save_state(archive_state="BACKFILLING", archive_last_error=None)
            def archival_progress(progress):
                save_state(archive=progress["coverage"],
                           archive_latest_symbol=progress["symbol"],
                           archive_latest_rows=progress["rows_added"],
                           archive_last_progress_at=timestamp())
            archive = backfill(db, pages=5, on_progress=archival_progress, on_retry=retry)
            saved_rows = int(archive.get("rows_added_this_cycle", 0))
            if saved_rows > 0 and os.environ.get("DATABASE_URL"):
                from .persistence import checkpoint
                checkpoint(DB, MODEL_DIR)
                _last_checkpoint = time.time()
                _last_paper_digest = digest
                save_state(archive_persisted_at=timestamp())
                LOG.info("ARCHIVE CHECKPOINT OK rows_added=%s min_days=%s",
                         saved_rows, archive["minimum_days_across_symbols"])
                event("info", f"Saved {saved_rows} archival candles to PostgreSQL")
            elif saved_rows > 0:
                LOG.warning("ARCHIVE NOT DURABLE: %s rows added without database URL", saved_rows)
            save_state(archive=archive, archive_state="COMPLETE" if archive["archive_complete"] else "ACTIVE",
                       archive_last_rows=saved_rows, archive_last_success_at=timestamp())
            if archive["archive_complete"] and not archive_before["archive_complete"]:
                event("info", "180-day archive complete across all tracked markets")
        except RateLimited as archive_rate:
            LOG.warning("ARCHIVE RATE LIMITED: %s", archive_rate)
            save_state(archive_state="RATE_LIMITED", archive_last_error=str(archive_rate),
                       archive_last_failure_at=timestamp())
            raise
        except Exception as archive_error:
            LOG.exception("ARCHIVE BACKFILL FAILED")
            save_state(archive_state="ERROR", archive_last_error=str(archive_error)[:300],
                       archive_last_failure_at=timestamp())
            event("error", "Archive backfill failed: " + str(archive_error)[:220])


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
                from .initial_render import render_initial
                from .api_market import market_snapshot
                market = None
                market_error = None
                try:
                    market = market_snapshot(DB)
                except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                    market_error = str(exc)[:180]
                    LOG.warning("Server-rendered market reading unavailable: %s", exc)
                try:
                    contents = render_initial(p.read_text(encoding="utf-8"),
                                              snapshot(), market, market_error)
                except Exception:
                    LOG.exception("Initial dashboard rendering failed; serving basic HTML")
                    contents = p.read_text(encoding="utf-8")
                return self.respond(contents.encode("utf-8"), "text/html; charset=utf-8")
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
        if path == "/api/audit":
            from .api_audit import recent_forecast_audit
            try:
                result = recent_forecast_audit(DB, hours=24)
                return self.respond(json.dumps(result).encode(),
                                    "application/json; charset=utf-8")
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                LOG.warning("Forecast audit unavailable: %s", exc)
                return self.respond(
                    json.dumps({"error": "Stored forecast audit unavailable"}).encode(),
                    "application/json; charset=utf-8", 503)
        if path == "/api/state":
            return self.respond(json.dumps(snapshot()).encode(), "application/json; charset=utf-8")
        if path == "/api/health":
            state = snapshot()
            now_ms = int(time.time() * 1000)
            def age_seconds(value):
                if not value:
                    return None
                try:
                    return round(max(0.0, time.time() - datetime.fromisoformat(
                        value.replace("Z", "+00:00")).timestamp()), 1)
                except (ValueError, TypeError):
                    return None
            worker_age = age_seconds(state.get("worker_heartbeat_at"))
            score = state.get("score") or {}
            last_candle = score.get("timestamp_open_ms")
            score_age_min = (round((now_ms - int(last_candle) - INTERVAL_MS) / 60_000, 1)
                             if last_candle is not None else None)
            report = {
                "service": "ok",
                "research_phase": state.get("phase"),
                "market_venue": state.get("data_venue"),
                "worker_age_seconds": worker_age,
                "worker_alive": worker_age is not None and worker_age < 90,
                "score_age_minutes": score_age_min,
                "last_completed_cycle_at": state.get("updated_at"),
                "archive_state": state.get("archive_state"),
                "durable_archive_checkpoint_at": state.get("archive_persisted_at"),
            }
            return self.respond(json.dumps(report).encode(), "application/json; charset=utf-8")
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
                save_state(database_restored_from_neon=True)
            else:
                LOG.info("PostgreSQL database connected: no existing checkpoint")
        except Exception:
            LOG.exception("PostgreSQL restore failed; refusing to proceed unsafely")
            raise
    # Verify the same stored-candle query that powers /api/market.
    # An old checkpoint is labelled stale, never treated as a fresh quote.
    try:
        from .api_market import market_snapshot
        chart = market_snapshot(DB)
        LOG.info("MARKET DATA SELF-CHECK OK: venue=%s btc_candles=%d age_minutes=%.1f stale=%s",
                 chart["venue"], len(chart["symbols"]["BTCUSDT"]),
                 chart["age_minutes"], chart["stale"])
        save_state(chart_data_readable=True, chart_data_stale=chart["stale"])
    except Exception as exc:
        LOG.warning("MARKET DATA SELF-CHECK NOT READY: %s", exc)
        save_state(chart_data_readable=False, chart_data_error=str(exc)[:180])
    # Restart should not retrain an already-saved model just because the process
    # clock has reset. The next scheduled training cycle remains due at +6h.
    saved_model = MODEL_DIR / "latest.json"
    if saved_model.is_file():
        try:
            global _last_train
            trained = json.loads(saved_model.read_text()).get("trained_at")
            trained_epoch = datetime.fromisoformat(trained).timestamp()
            if 0 < time.time() - trained_epoch < TRAIN_INTERVAL_SECONDS:
                _last_train = trained_epoch
                LOG.info("Restored recent trained model; skipped unnecessary restart retraining")
        except (OSError, ValueError, TypeError, KeyError):
            LOG.warning("Saved model metadata could not be used to restore training clock")
    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()
    threading.Thread(target=worker, name="research-worker", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("BTC research dashboard listening on 0.0.0.0:%s, DATA_DIR=%s", PORT, DATA_DIR)
    server.serve_forever()


if __name__ == "__main__":
    main()
