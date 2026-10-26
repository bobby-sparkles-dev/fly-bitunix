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

def mature(db, btc, limit=1000):
    if btc.empty:
        return 0
    last=int(btc.index.max())
    rows=db.execute("""SELECT candle_open_ms,feature_json,future_json
        FROM research_decisions_v1
        WHERE candle_open_ms >= ? ORDER BY candle_open_ms DESC LIMIT ?""",
        (last-4*86_400_000,limit)).fetchall()
    modified=0
    settings=Settings()
    for instant, raw_features, raw_future in rows:
        instant=int(instant)
        if instant not in btc.index:
            continue
        prior=json.loads(raw_future or "{}")
        features=json.loads(raw_features)
        atr=features.get("atr_at_decision")
        changed=False
        for horizon in HORIZONS:
            key=str(horizon*5)
            if key in prior or instant+horizon*INTERVAL_MS>last:
                continue
            indices=[instant+i*INTERVAL_MS for i in range(1,horizon+1)]
            if any(i not in btc.index for i in indices):
                continue
            future=btc.loc[indices]
            entry=float(future["open"].iloc[0])
            close=float(future["close"].iloc[-1])
            if entry<=0:
                continue
            result={"entry_open_ms":indices[0],"end_open_ms":indices[-1],
                    "return_pct":round(100*(close/entry-1),5),
                    "max_favourable_pct":round(100*(float(future["high"].max())/entry-1),5),
                    "max_adverse_pct":round(100*(float(future["low"].min())/entry-1),5)}
            if horizon==6 and atr is not None and atr>0:
                slip=settings.slippage_bps_per_side/10000
                fee=settings.fee_bps_per_side/10000
                fill=entry*(1+slip)
                risk=max(settings.stop_atr_multiple*atr,fill*settings.min_stop_pct)
                stop=fill-risk
                target=fill+settings.reward_to_risk*risk
                exit_raw=close
                reason="TIME"
                for _,bar in future.iterrows():
                    if float(bar["low"])<=stop:
                        exit_raw=min(stop,float(bar["open"]))
                        reason="STOP"
                        break
                    if float(bar["high"])>=target:
                        exit_raw=target
                        reason="TARGET"
                        break
                exit_fill=exit_raw*(1-slip)
                net_r=(exit_fill-fill-fee*(fill+exit_fill))/risk
                result["strict_net_r"]=round(net_r,6)
                result["strict_win"]=net_r>0
                result["strict_exit_reason"]=reason
                closes=[float(btc.loc[instant,"close"])]
                closes.extend(float(x) for x in future["close"])
                travel=sum(abs(b-a) for a,b in zip(closes[:-1],closes[1:]))
                eff=abs(closes[-1]-closes[0])/travel if travel>0 else 0.0
                rnge=float(future["high"].max()-future["low"].min())
                result["market_tradeable"]=eff>=0.4 and rnge>=.8*atr
            prior[key]=result
            changed=True
        if changed:
            observed=datetime.now(timezone.utc).isoformat()
            db.execute("""UPDATE research_decisions_v1 SET
                future_json=?,last_evaluated_at=? WHERE candle_open_ms=?""",
                (json.dumps(prior,allow_nan=False),observed,instant))
            modified+=1
    if modified:
        db.commit()
    return modified
