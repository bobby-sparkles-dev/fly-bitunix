"""Command line interface. No account keys or live order endpoints."""
from __future__ import annotations
import argparse
import json
from dataclasses import replace
from pathlib import Path
from .config import Settings
from .storage import connect


def args_parser():
    p = argparse.ArgumentParser(prog="btc-edge", description="Adaptive BTC-long research, PAPER ONLY")
    p.add_argument("--db", default="data/research.sqlite")
    p.add_argument("--models", default="models")
    sub = p.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Download closed spot candles from public Binance API")
    ingest.add_argument("--days", type=int, default=37)
    train = sub.add_parser("train", help="Fit two recency-weighted models and evaluate chronologically")
    train.add_argument("--days", type=int, default=30)
    train.add_argument("--test-days", type=int, default=7)
    train.add_argument("--horizon", type=int, default=6, help="Five-minute bars, default 6 = 30 minutes")
    sub.add_parser("score", help="Assess latest completed candle, NEVER execute a trade")
    youtube = sub.add_parser("youtube", help="Discover recent video metadata through a public channel feed")
    youtube.add_argument("--channel-id", required=True)
    transcript = sub.add_parser("parse-signal", help="Extract human-review candidate from an authorised text transcript")
    transcript.add_argument("--file", type=Path, required=True)
    transcript.add_argument("--source-url", required=True)
    transcript.add_argument("--published-at", required=True, help="ISO8601 with timezone")
    return p


def main():
    a = args_parser().parse_args()
    with connect(a.db) as db:
        if a.command == "ingest":
            from .market import ingest
            result = ingest(db, days=a.days)
        elif a.command in ("train", "score"):
            from .features import load_candles, build_features
            from .outcomes import build_outcomes
            from .learning import train, latest_score
            candles = load_candles(db, days=120)
            features = build_features(candles)
            if a.command == "train":
                settings = replace(Settings(), training_days=a.days, test_days=a.test_days,
                                   horizon_bars=a.horizon)
                outcomes = build_outcomes(candles["BTCUSDT"], features, settings)
                payload = train(features, outcomes, settings, a.models)
                result = {"model_id": payload["model_id"], "report": payload["report"],
                          "mode": "PAPER_ONLY"}
            else:
                result = latest_score(features, a.models)
        elif a.command == "youtube":
            from .traders import discover_videos
            result = discover_videos(db, a.channel_id)
        elif a.command == "parse-signal":
            from .traders import extract_review_candidate, save_candidate
            signal = extract_review_candidate(a.file.read_text(), a.source_url, a.published_at)
            save_candidate(db, signal)
            result = signal
        else:
            raise ValueError("Unknown command")
    print(json.dumps(result, indent=2, default=str))

if __name__ == "__main__":
    main()
