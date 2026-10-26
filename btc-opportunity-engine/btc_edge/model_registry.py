"""Immutable, inspectable registry of model versions used by BTC forecasts.

Each fitted model includes hyperparameters, feature coefficients, validation
metrics and the exact UTC fit time. Models are *never* rewritten after a later
backtest. This supports separating live performance of each model version.
"""
from __future__ import annotations
import json
from pathlib import Path

from .market import PROVIDER


DDL="""CREATE TABLE IF NOT EXISTS btc_model_versions_v1 (
    venue TEXT NOT NULL,
    model_id TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    settings JSONB NOT NULL,
    feature_schema JSONB NOT NULL,
    validation_report JSONB NOT NULL,
    full_model JSONB NOT NULL,
    first_saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (venue,model_id)
)"""


def store(model_file: Path) -> bool:
    if not model_file.is_file():
        return False
    from .durable_ledger import enabled,connect_pg
    if not enabled():
        return False
    from psycopg.types.json import Jsonb
    data=json.loads(model_file.read_text())
    columns=data.get("models",{}).get("long_win",{}).get("features",[])
    with connect_pg() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""
              INSERT INTO btc_model_versions_v1 (
                venue,model_id,trained_at,settings,feature_schema,
                validation_report,full_model)
              VALUES (%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(venue,model_id) DO NOTHING
            """,(PROVIDER,data["model_id"],data["trained_at"],
                 Jsonb(data["settings"]),Jsonb(columns),
                 Jsonb(data["report"]),Jsonb(data)))
        conn.commit()
    return True
