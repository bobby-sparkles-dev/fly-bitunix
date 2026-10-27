"""Annotate each logged research decision with outcomes observed later.

No future observation is used to decide/record an entry. For missing bars the
horizon remains pending; it is never assigned an invented win or loss.
"""
import json
import sqlite3
import time
from datetime import datetime,timezone

from .config import INTERVAL_MS, Settings
from .shadow_paper import HORIZONS

def mature(db, btc, now_ms=None):
    """Settle outcomes that begin AFTER each logged prediction.

    Previous historical results using the next candle after the scored bar
    remain labelled legacy and are not rewritten or silently mixed with new
    executable-after-observation results.
    """
    from .score_probes import install as install_score_probes, settle as settle_score_probes
    from .research_log import attach_matured_outcomes
    # Some callers build a lightweight research-only database. Initialise
    # the forward probe table before looking for pending outcomes.
    install_score_probes(db)
    settle_score_probes(db,btc,now_ms=now_ms)
    return attach_matured_outcomes(db,now_ms=now_ms)
