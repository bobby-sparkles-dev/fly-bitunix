"""Forward-only ledger acceptance tests. SQLite in memory, no network needed."""
import sqlite3
from datetime import datetime, timezone
import pandas as pd
from btc_edge.paper import install, queue_signal, settle, summary
from btc_edge.config import INTERVAL_MS


def db():
    c = sqlite3.connect(":memory:")
    install(c)
    return c


def score(bar_ms):
    return {"model_id": "test-model", "timestamp_open_ms": bar_ms,
            "candidate": True, "status": "RESEARCH_ONLY",
            "p_tradeable": 0.8, "p_profitable_btc_long": 0.7}


def test_queue_has_future_entry_and_no_overlap():
    c = db()
    observed = 25 * INTERVAL_MS + 120_000
    assert queue_signal(c, score(24 * INTERVAL_MS), 100., observed_ms=observed)
    row = c.execute("SELECT scheduled_entry_open_ms,status FROM paper_trades").fetchone()
    assert row == (26 * INTERVAL_MS, "QUEUED")
    assert not queue_signal(c, score(24 * INTERVAL_MS), 100., observed_ms=observed)
    assert summary(c)["queued"] == 1


def test_paper_trade_is_not_settled_before_future_candle():
    c = db()
    observed = 25 * INTERVAL_MS + 120_000
    assert queue_signal(c, score(24 * INTERVAL_MS), 100., observed_ms=observed)
    bars = pd.DataFrame([{"open": 50000., "high": 50100., "low": 49900., "close": 50050.}],
                        index=pd.Index([25 * INTERVAL_MS], name="open_time"))
    settle(c, bars)
    assert summary(c)["queued"] == 1
    bars.loc[26 * INTERVAL_MS] = [50000., 51000., 49900., 50900.]
    bars = bars.sort_index()
    settle(c, bars)
    p = summary(c)
    assert p["completed"] == 1
    assert p["wins"] == 1
    assert p["queued"] == 0
    assert p["open"] == 0


def test_stop_wins_same_bar_ambiguity():
    c = db()
    observed = 25 * INTERVAL_MS + 120_000
    assert queue_signal(c, score(24 * INTERVAL_MS), 100., observed_ms=observed)
    bars = pd.DataFrame([{"open": 50000., "high": 51000., "low": 49000., "close": 50000.}],
                        index=pd.Index([26 * INTERVAL_MS], name="open_time"))
    settle(c, bars)
    p=summary(c)
    assert p["losses"] == 1
    assert p["wins"] == 0


def test_reject_stale_signal():
    c=db()
    observed=30*INTERVAL_MS
    assert not queue_signal(c, score(24*INTERVAL_MS), 100., observed_ms=observed)
    assert summary(c)["completed"] == 0
