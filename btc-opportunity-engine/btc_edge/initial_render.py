"""Server-rendered dashboard data so the page stays useful without JavaScript.

Only display completed saved candles. Never fabricate prices, freshness or score
accuracy. Browser JS may progressively enhance these initial readings.
"""
from __future__ import annotations
from datetime import datetime, timezone
from html import escape
from math import log, sqrt, isfinite
import re
from typing import Any


def _label_time(value: str | None) -> str:
    if not value:
        return "Not available"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc).strftime("%d %b %H:%M UTC")
    except ValueError:
        return "Not available"


def _percent(value: Any) -> str:
    if not isinstance(value, (int, float)) or not isfinite(float(value)):
        return "—"
    return f"{100 * value:.1f}%"


def _update(html: str, element_id: str, content: str) -> str:
    pattern = (
        r'(<(?P<tag>[a-z][a-z0-9-]*)\b[^>]*\bid="'
        + re.escape(element_id)
        + r'"[^>]*>)[^<]*(</(?P=tag)>)'
    )
    # Substitute through callable: no regex expansion of escaped characters.
    return re.sub(pattern, lambda match: match.group(1) + escape(content)
                  + match.group(3), html, count=1)


def _static_chart(bars: list) -> str:
    """Small SVG from genuinely stored BTC closing prices, no JS required."""
    if len(bars) < 20:
        return "<p>Not enough saved BTC candles to show a chart.</p>"
    closes = [float(row[4]) for row in bars[-140:] if float(row[4]) > 0]
    if len(closes) < 20:
        return "<p>Saved chart unavailable.</p>"
    lo, hi = min(closes), max(closes)
    spread = max(hi - lo, hi * 0.0001)
    points = []
    for index, close in enumerate(closes):
        x = 18 + 764 * index / max(1, len(closes) - 1)
        y = 18 + 184 * (hi - close) / spread
        points.append(f"{x:.1f},{y:.1f}")
    path = "M" + " L".join(points)
    return ('<svg viewBox="0 0 800 220" role="img" '
            'aria-label="Saved Bitcoin closing prices, not a live trading signal" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<rect width="800" height="220" fill="#fafbf7"/>'
            '<path d="' + path + '" fill="none" stroke="#249f82" '
            'stroke-width="3" stroke-linejoin="round"/>'
            '<text x="20" y="214" font-size="13" fill="#485969">'
            + f"Saved Bitcoin close: {closes[-1]:,.0f} USDT; not live"
            + '</text></svg>')


def render_initial(template: str, state: dict, market: dict | None = None,
                   market_error: str | None = None) -> str:
    values: dict[str, str] = {}
    server_now = datetime.now(timezone.utc)
    phase = str(state.get("phase") or "STARTING")
    values["engine-plain-state"] = phase
    values["phase-label"] = f"{phase} · server-rendered"
    values["plain-message"] = str(state.get("message") or "Research service initialising")
    values["refresh-result"] = "Page loaded at " + server_now.strftime("%H:%M:%S UTC") + (
        ". The full-page Reload link works even when JavaScript fails."
    )

    if market:
        bars = market.get("symbols", {}).get("BTCUSDT") or []
        if bars:
            close = float(bars[-1][4])
            values["price"] = f"${close:,.0f}"
            values["price-status"] = "Last completed saved candle (not necessarily live)"
            age = market.get("age_minutes")
            age_text = f"{age:.1f}" if isinstance(age, (float, int)) else "unknown"
            venue = market.get("venue", "unknown")
            status = "outdated" if market.get("stale") else "recent"
            values["data-quality"] = f"Saved candles {status}"
            values["feed-status"] = f"BACKEND SAVED · {venue}"
            values["saved-data-banner"] = (
                f"Saved {venue} BTC candles loaded on the server. Last completed candle "
                f"{age_text} minutes old ({status}). "
                "The dashboard will refresh in your browser if JavaScript works."
            )
            values["chart-note"] = (
                f"Saved {venue} BTC candles available · latest {age_text} minutes old."
                " The visual chart requires JavaScript, but figures above do not."
            )
        if len(bars) >= 25:
            candles = bars[-13:]
            path = sum(abs(float(candles[i][4]) - float(candles[i-1][4])) for i in range(1, 13))
            net = abs(float(candles[-1][4])-float(candles[0][4]))
            values["efficiency"] = f"{100 * net / path:.0f}%" if path else "0%"
            current = float(candles[-1][4])
            prev = float(candles[0][4])
            pct = 100 * (current / prev - 1) if prev else 0
            values["change"] = f"{pct:+.2f}%"
            recent = sum(float(x[5]) * float(x[4]) for x in bars[-12:])
            older = sum(float(x[5]) * float(x[4]) for x in bars[-24:-12])
            values["volume"] = f"{recent/older:.2f}×" if older else "—"
            changes = [log(float(candles[i][4]) / float(candles[i-1][4]))
                       for i in range(1, 13)]
            avg = sum(changes)/len(changes)
            stdev = sqrt(sum((x-avg)**2 for x in changes)/len(changes))
            values["volatility"] = f"{stdev*100:.3f}%"
            alt = market.get("symbols", {})
            alts = [x for k,x in alt.items() if k != "BTCUSDT" and len(x) >= 13]
            if alts:
                ups = sum(1 for x in alts if float(x[-1][4]) > float(x[-13][4]))
                values["breadth"] = f"{100*ups/len(alts):.0f}%"
    else:
        values["saved-data-banner"] = (
            "Stored market data could not be read on the server. "
            + (market_error or "The database may be restoring.")
            + " Research history is managed independently in PostgreSQL."
        )
        values["data-quality"] = "Saved data unavailable"
        values["feed-status"] = "BACKEND DATA UNAVAILABLE"

    score = state.get("score")
    if isinstance(score, dict):
        at = score.get("timestamp_open_ms")
        stale = True
        try:
            age_minutes = (server_now.timestamp() * 1000 - (int(at) + 300000))/60000
            stale = not (0 <= age_minutes <= 15)
        except (ValueError, TypeError):
            pass
        fresh = not stale and phase not in {"ERROR","RATE_LIMITED","STALE_DATA"}
        tail = "" if fresh else " (old)"
        values["market-prob"] = _percent(score.get("p_tradeable")) + tail
        values["long-prob"] = _percent(score.get("p_profitable_btc_long")) + tail
        values["model-reliability"] = "Unverified"
        values["last-trained"] = "Trained " + _label_time(state.get("last_training_at"))
        values["score"] = f"{round(float(score.get('p_tradeable', 0))*100)}/100" + tail
        if not fresh:
            values["saved-data-banner"] = (
                values.get("saved-data-banner", "")+
                " Research scores are old or paused; not trading signals."
            )

    ledger = state.get("paper_trades") or {}
    if isinstance(ledger, dict):
        values["paper-queued"] = str(ledger.get("queued",0))
        values["paper-open"] = str(ledger.get("open",0))
        values["paper-closed"] = str(ledger.get("completed",0))
        values["paper-winloss"] = str(ledger.get("wins",0))+" / "+str(ledger.get("losses",0))
        rate = ledger.get("win_rate")
        values["paper-winrate"] = _percent(rate)
        net = ledger.get("total_net_r")
        if isinstance(net,(int,float)):
            values["paper-net"] = f"{net:+.2f} R"

    if market and market.get("symbols", {}).get("BTCUSDT"):
        template = template.replace("<!--SERVER_STATIC_CHART-->",
                                    _static_chart(market["symbols"]["BTCUSDT"]))
    else:
        template = template.replace("<!--SERVER_STATIC_CHART-->",
                                    "<p>Saved chart data is not available yet.</p>")
    for key,value in values.items():
        template = _update(template,key,str(value))
    # Replace one legacy bare score node. Its class exists even without an id.
    if "score" in values:
        template = re.sub(r'(<div class="score">)[^<]*(</div>)',
                          lambda m: m.group(1) + escape(values["score"]) + m.group(2),
                          template,count=1)
    return template
