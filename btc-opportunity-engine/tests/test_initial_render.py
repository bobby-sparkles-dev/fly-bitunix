"""The main dashboard must show real saved values without browser JavaScript."""
import time
from btc_edge.initial_render import render_initial


def test_dashboard_renders_saved_market_and_svg_without_js():
    now=int(time.time()*1000)
    bars=[]
    for i in range(140):
        t=now-(140-i)*300_000
        p=80_000+i
        bars.append([t,p,p+10,p-10,p,20])
    market={"symbols":{"BTCUSDT":bars},"venue":"binance_us",
            "age_minutes":5.0,"stale":False}
    html=(
        '<strong id="price">—</strong><strong id="market-prob">—</strong>'
        '<div id="server-chart-fallback"><!--SERVER_STATIC_CHART--></div>'
        '<div id="saved-data-banner">Loading</div>'
    )
    saved=render_initial(html,{"phase":"RUNNING"},market)
    assert "$80,139" in saved
    assert "<svg " in saved
    assert "Saved Bitcoin close" in saved
    assert "MARKET" not in saved.upper() or "candles" in saved


def test_score_never_fabricated_when_no_model():
    html='<strong id="market-prob">—</strong>'
    rendered=render_initial(html,{"phase":"STARTING"},None,"DB restoring")
    assert 'id="market-prob">—' in rendered


def test_score_marked_old_if_the_model_is_stale():
    now=int(time.time()*1000)
    html='<strong id="market-prob">—</strong>'
    state={"phase":"RUNNING","score":{"timestamp_open_ms":now-3_600_000,
                                       "p_tradeable":0.74,"p_profitable_btc_long":0.20}}
    out=render_initial(html,state,None)
    assert "74.0% (old)" in out
