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


# Distinct loops: live scoring never waits for historical backfill.
_bootstrapped = threading.Event()
_live_busy = threading.Event()
_training_busy = threading.Event()
_checkpoint_requested = threading.Event()
_snapshot_mutex = threading.Lock()
_rate_limit_mutex = threading.Lock()
_venue_cooldown_until = 0.0


def set_venue_cooldown(seconds: int) -> None:
    global _venue_cooldown_until
    with _rate_limit_mutex:
        _venue_cooldown_until = max(_venue_cooldown_until, time.time() + seconds)


def venue_pause_seconds() -> float:
    with _rate_limit_mutex:
        return max(0.0, _venue_cooldown_until - time.time())


def on_market_retry(info: dict) -> None:
    event("warning", str(info.get("symbol", "?")) + ": " +
          str(info.get("kind", "network")) + " " +
          str(info.get("code", "")) + " retry " +
          str(info.get("attempt", "?")) + "/" + str(info.get("max_attempts", "?")))


def next_slot_delay(now: float | None = None) -> float:
    """Run shortly after UTC 5-minute candle close, independent of page visits."""
    now = time.time() if now is None else now
    step = INTERVAL_MS / 1000
    return max(1.0, (int(now // step) + 1) * step + 25 - now)


def live_cycle() -> None:
    """Lightweight incremental ingest and paper scoring; no archive or model fitting."""
    from .market import ingest
    from .features import load_candles, build_features
    from .learning import latest_score
    from .paper import install as install_paper, settle as settle_paper
    from .paper import queue_signal, summary as paper_summary

    _live_busy.set()
    try:
        save_state(phase="COLLECTING", message="Importing recently closed candles",
                   last_error=None, data_venue=PROVIDER)
        stage("collecting", 0, "Checking newest five-minute candles")
        with connect(DB) as db:
            ensure_history(db)
            install_paper(db)
            count = db.execute("SELECT COUNT(*) FROM candles WHERE symbol='BTCUSDT'").fetchone()[0]
            result = ingest(db, days=37 if count < 500 else 2,
                on_progress=lambda p: stage("collecting", p["percent"] * .78,
                    "Updating " + p["current_symbol"], collection=p),
                on_retry=on_market_retry)
            # Only recent candles are necessary for the existing 49-bar features.
            # This keeps a five-minute score independent of the size of our archive.
            candles = load_candles(db, days=3)
            features = build_features(candles)
            _bootstrapped.set()
            latest_file = MODEL_DIR / "latest.json"
            if not latest_file.exists():
                save_state(phase="TRAINING", message="Historical model initialisation required",
                           updated_at=timestamp(), next_forecast_due_at=timestamp())
                return

            stage("scoring", 97, "Scoring newest completed BTC candle")
            score = latest_score(features, MODEL_DIR)
            LOG.info("BTC SCORE model=%s candle=%s age_min=%s status=%s context_complete=%s missing_alt=%s",
                     score["model_id"], score["timestamp_open_ms"], score["data_age_minutes"],
                     score["status"], score.get("context_complete"),
                     score.get("missing_alt_features", []))
            record_forecast(db, score)
            settle_paper(db, candles["BTCUSDT"])
            if score["status"] != "STALE_DATA":
                atr = float(features.loc[int(score["timestamp_open_ms"]), "atr_at_decision"])
                if queue_signal(db, score, atr):
                    event("info", "Queued a virtual BTC long for the next eligible candle")
            ledger = paper_summary(db)
            decisions = decision_diagnostics(db, score, ledger)
            forward = evaluate_matured(db, candles, features)

            # A prediction is evidence only if we retain its original timestamp.
            # Persist individual forecasts/trades without waiting for bulk backups.
            persistence_ok = False
            try:
                from .durable_ledger import sync_from_sqlite
                sync = sync_from_sqlite(db)
                persistence_ok = bool(sync.get("enabled"))
                save_state(forecast_ledger_synced_at=timestamp(),
                           forecast_ledger_error=None)
            except Exception as exc:
                LOG.exception("FORWARD LEDGER SYNC FAILED")
                save_state(forecast_ledger_error=str(exc)[:220])
                event("error", "Forecast persistence failed; research history at risk")

            model = json.loads(latest_file.read_text())
            stage("complete", 100, "Forecast completed")
            message = ("BTC/altcoin context incomplete; new paper entries suspended"
                       if score["status"] == "PARTIAL_CONTEXT" else
                       "BTC data stale; scoring suspended"
                       if score["status"] == "STALE_DATA" else
                       "Live scoring active; paper-only forecasts recorded")
            save_state(phase=("STALE_DATA" if score["status"] == "STALE_DATA"
                              else "RUNNING" if persistence_ok else "PERSISTENCE_DEGRADED"),
                       message=message, updated_at=timestamp(), last_error=None,
                       score=score, forward_observations=forward, paper_trades=ledger,
                       decision_diagnostics=decisions, data_venue=PROVIDER,
                       last_training_at=model.get("trained_at"),
                       training_window_days=model["settings"]["training_days"],
                       ingest_completed_at=result["completed_at"],
                       symbols_collected=len(result["candles"]),
                       data_freshness_minutes=score["data_age_minutes"],
                       forecast_persisted=persistence_ok)
            LOG.info("LIVE SCORE CYCLE OK model=%s candle=%s durable=%s forecast_count=%s",
                     score["model_id"], score["timestamp_open_ms"],
                     persistence_ok, forward["observed"])
    finally:
        _live_busy.clear()


def heartbeat() -> None:
    """True process heartbeat; separate from five-minute data progress."""
    while True:
        try:
            save_state(worker_heartbeat_at=timestamp())
        except Exception:
            LOG.exception("Could not update process heartbeat")
        time.sleep(15)


def worker() -> None:
    """Live scheduler: only ingest, forecast and paper outcomes in this loop."""
    from .market import RateLimited
    save_state(data_venue=PROVIDER, scheduler_mode="DEDICATED_5_MINUTE",
               service_always_on_expected=True)
    while True:
        delay = venue_pause_seconds()
        if delay > 0:
            save_state(phase="RATE_LIMITED",
                       message="Exchange cooling down before another data request",
                       retry_at=datetime.fromtimestamp(time.time()+delay,timezone.utc).isoformat())
            time.sleep(min(30, max(1, delay)))
            continue
        try:
            live_cycle()
        except RateLimited as exc:
            set_venue_cooldown(exc.retry_seconds)
            LOG.warning("LIVE RATE LIMIT: %s", exc)
            event("warning", str(exc))
            save_state(phase="RATE_LIMITED", last_error=str(exc),
                       retry_at=datetime.fromtimestamp(
                           time.time()+exc.retry_seconds,timezone.utc).isoformat())
        except Exception as exc:
            LOG.exception("LIVE SCORE CYCLE FAILED")
            event("error", "Live scoring: "+str(exc)[:200])
            save_state(phase="ERROR", last_error=str(exc)[:220], updated_at=timestamp(),
                       message="Live scorer failed; no current prediction")
        # Scheduler runs without an open dashboard and doesn't wait for archives.
        delay = next_slot_delay()
        save_state(next_forecast_due_at=datetime.fromtimestamp(
            time.time()+delay,timezone.utc).isoformat())
        time.sleep(delay)


def archive_worker() -> None:
    """Low-priority archival backfill on its OWN SQLite connection."""
    from .history import backfill, coverage
    from .market import RateLimited
    _bootstrapped.wait()
    # Allow scoring to finish and give the exchange a short breathing space.
    time.sleep(15)
    while True:
        if _training_busy.is_set() or _live_busy.is_set() or venue_pause_seconds()>0:
            time.sleep(20)
            continue
        try:
            save_state(archive_state="BACKFILLING", archive_last_error=None)
            with connect(DB) as db:
                def progress(info):
                    save_state(archive=info["coverage"],
                               archive_latest_symbol=info["symbol"],
                               archive_latest_rows=info["rows_added"],
                               archive_last_progress_at=timestamp())
                result = backfill(db, pages=3, on_progress=progress,
                                  on_retry=on_market_retry)
            count = int(result["rows_added_this_cycle"])
            save_state(archive=result,
                       archive_state="COMPLETE" if result["archive_complete"] else "ACTIVE",
                       archive_last_rows=count, archive_last_success_at=timestamp())
            if count:
                _checkpoint_requested.set()
                LOG.info("ARCHIVE GROWTH rows=%s oldest_days=%s (pending durable snapshot)",
                         count, result["minimum_days_across_symbols"])
            if result["archive_complete"]:
                time.sleep(3600)
            else:
                time.sleep(300)
        except RateLimited as exc:
            set_venue_cooldown(exc.retry_seconds)
            LOG.warning("ARCHIVE RATE LIMITED: %s", exc)
            save_state(archive_state="RATE_LIMITED",
                       archive_last_error=str(exc), archive_last_failure_at=timestamp())
            time.sleep(30)
        except Exception as exc:
            LOG.exception("ARCHIVE WORKER FAILED")
            save_state(archive_state="ERROR",archive_last_error=str(exc)[:260],
                       archive_last_failure_at=timestamp())
            time.sleep(120)


def model_worker() -> None:
    """Train periodically WITHOUT blocking the live forecast loop."""
    from .features import load_candles, build_features
    from .history import coverage
    from .outcomes import build_outcomes
    from .learning import train
    _bootstrapped.wait()
    while True:
        latest_file = MODEL_DIR / "latest.json"
        try:
            with connect(DB) as db:
                archival = coverage(db)
            extended_ready = archival["all_symbols_ready_for_74_day_window"]
            settings = (Settings(training_days=74, test_days=14, half_life_days=21)
                        if extended_ready else Settings())
            trained_epoch = 0
            current = {}
            if latest_file.is_file():
                current = json.loads(latest_file.read_text())
                trained_at = current.get("trained_at")
                if trained_at:
                    trained_epoch = datetime.fromisoformat(trained_at).timestamp()
            # Avoid unnecessary model fitting on every deploy. Promote the
            # extended research window once there is enough valid history.
            need_train = (not current or
                time.time()-trained_epoch >= TRAIN_INTERVAL_SECONDS or
                current.get("settings", {}).get("training_days") != settings.training_days or
                "cost_audit" not in current.get("report", {}))
            if need_train:
                _training_busy.set()
                save_state(training_status="TRAINING",
                           training_started_at=timestamp())
                LOG.info("BACKGROUND TRAINING START window_days=%s",settings.training_days)
                with connect(DB) as db:
                    candles = load_candles(db, days=76 if extended_ready else 45)
                features = build_features(candles)
                outcome = build_outcomes(candles["BTCUSDT"], features, settings)
                result = train(features, outcome, settings, MODEL_DIR)
                LOG.info("BACKGROUND TRAINING OK model=%s", result["model_id"])
                save_state(training_status="READY",training_finished_at=timestamp(),
                           last_training_at=result["trained_at"],
                           training_window_days=settings.training_days)
                _checkpoint_requested.set()
                _training_busy.clear()
        except Exception as exc:
            _training_busy.clear()
            LOG.exception("BACKGROUND TRAINING FAILED")
            event("error", "Background model training: " + str(exc)[:180])
            save_state(training_status="ERROR",training_error=str(exc)[:260])
        time.sleep(300)


def persistence_worker() -> None:
    """Durable, compressed OHLCV checkpoints without blocking live scoring.

    Contemporaneous forecasts and paper trades have their own small PG ledger.
    Bulk snapshots are throttled to avoid excessive database writes.
    """
    from .persistence import checkpoint
    _bootstrapped.wait()
    last_saved = time.monotonic()
    while True:
        # Archive and model changes are persisted periodically; never capture
        # multiple overlapping versions in the same process.
        pending = _checkpoint_requested.wait(timeout=60)
        due = time.monotonic()-last_saved >= 900
        if pending and not due:
            continue
        if not pending and not due:
            continue
        if _training_busy.is_set():
            time.sleep(10)
            continue
        try:
            with _snapshot_mutex:
                checkpoint(DB, MODEL_DIR)
                last_saved = time.monotonic()
                _checkpoint_requested.clear()
            save_state(archive_persisted_at=timestamp(),
                       checkpoint_last_error=None, checkpoint_status="SAVED")
            LOG.info("NEON BULK CHECKPOINT OK: research history and model stored")
        except Exception as exc:
            LOG.exception("NEON BULK CHECKPOINT FAILED")
            save_state(checkpoint_status="ERROR",
                       checkpoint_last_error=str(exc)[:220])
            event("error","Neon archive checkpoint failed")
            time.sleep(60)


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
    # One-time forensic summary, derived solely from saved historical forecasts.
    # A wick can occur before its candle closes, so distinguish actual forecast
    # observation timestamps from the price-candle timestamps.
    try:
        from .api_audit import recent_forecast_audit
        audit = recent_forecast_audit(DB, hours=24)
        low = audit.get("min_low")
        if low:
            LOG.info("OVERNIGHT AUDIT low=%.2f candle_utc=%s recorded_forecasts=%s candles=%s",
                     float(low["price"]), low["candle_utc"],
                     audit["logged_forecasts_count"], audit["candles_count"])
            near = audit.get("forecasts_near_low", [])
            for entry in near:
                t = datetime.fromtimestamp(int(entry["candle_open_ms"])/1000,timezone.utc).isoformat()
                LOG.info("OVERNIGHT FORECAST candle_utc=%s observed_at=%s market=%.4f long=%.4f status=%s qualified=%s",
                         t, entry["observed_at"],float(entry["probability_market"]),
                         float(entry["probability_long"]),entry["research_status"],
                         entry["candidate"])
            before=audit.get("last_logged_forecast_at_or_before_low")
            after=audit.get("first_logged_forecast_after_low")
            for label,entry in (("PRIOR",before),("NEXT",after)):
                if entry:
                    LOG.info("OVERNIGHT %s observed_at=%s market=%.4f long=%.4f candle_ms=%s",
                             label,entry["observed_at"],entry["probability_market"],
                             entry["probability_long"],entry["candle_open_ms"])
    except Exception:
        LOG.exception("OVERNIGHT AUDIT FAILED; research service will still start")
    # Merge point-in-time forecasts saved after the most recent bulk checkpoint.
    try:
        from .durable_ledger import restore_into_sqlite
        from .paper import install as install_paper
        with connect(DB) as db:
            ensure_history(db)
            install_paper(db)
            previous = restore_into_sqlite(db)
        LOG.info("RESTORED NATIVE FORECAST LEDGER: %s", previous)
        save_state(forecast_ledger_restored=True)
    except Exception as exc:
        LOG.exception("Forecast ledger restore failed; bulk checkpoint remains intact")
        save_state(forecast_ledger_restored=False,
                   forecast_ledger_error=str(exc)[:230])

    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()
    threading.Thread(target=worker, name="live-scorer", daemon=True).start()
    threading.Thread(target=model_worker, name="model-trainer", daemon=True).start()
    threading.Thread(target=archive_worker, name="archive-backfill", daemon=True).start()
    threading.Thread(target=persistence_worker, name="neon-checkpointer", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("BTC research dashboard listening on 0.0.0.0:%s, DATA_DIR=%s", PORT, DATA_DIR)
    server.serve_forever()


if __name__ == "__main__":
    main()
