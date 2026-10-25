# BTC Opportunity Engine (research MVP)

**A Bitcoin-only, long-only, research-and-paper-trading system. There is no order execution code, no exchange credentials and no investment recommendation.**

The objective is to see whether a rolling model trained on *recent* BTC and broader crypto-market observations can identify favourable conditions **before** tradable BTC long opportunities occur. An initial aspirational metric is **55% or higher winning paper trades**, with positive expectancy after costs and better risk-adjusted performance than simple baselines. No such performance has been demonstrated yet.

This project is currently an isolated subproject; do not deploy it as the existing BitUnix proxy service.

## What is implemented

- Download completed Binance **spot** 5-minute OHLCV/taker-buy-volume candles for BTC and ten large-cap altcoins via **public**, authentication-free endpoint, with time pagination and backoff.
- Store immutable point-in-time market observations in SQLite (upserts used only to recover duplicate/repeated ingestion).
- Calculate backward-looking BTC trend efficiency, returns, volatility, ATR, volume surprise, taker buy ratio, range position and correlated altcoin breadth / momentum / relative strength / volume features.
- Label future BTC tradeability and simulate **BTC longs** from the following candle open. Realistic modelling assumptions: entry/exit slippage, both-side fees, ATR-aware stops, 1.25R targets, time exits, conservative stop-first handling when a candle touches both barriers, no overlapping accepted positions.
- Train **separate** recency-weighted regularised logistic models for BTC tradeability and BTC long profitability. The standard 30-day window contains a seven-day chronological holdout; predictions whose label window crosses the boundary are purged.
- Compare combined-model paper-trade candidates with a fixed BTC momentum baseline and compare long-probability forecast error against a BTC-only feature model and publish net R, drawdown, win rate, Brier scores, sample sizes and model metadata. Predictions use held-out data only for evaluation; no test-set threshold optimisation.
- Store model coefficients in inspectable **JSON** (no pickle loading). The command-line score refuses stale market readings and never creates real orders.
- Discover video metadata via YouTube's public channel Atom feed, and extract a **human-review-only** signal from an **authorised supplied transcript** when it contains *one* explicit, unambiguous `BTC LONG entry: ... stop: ... target: ...` line. No automated third-party caption scraping, and no signal-based execution.

## Quick start

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'

# Run from the project folder; needs internet access to public Binance endpoint.
btc-edge ingest --days 37
btc-edge train --days 30 --test-days 7 --horizon 6
btc-edge score
pytest -q
```

If an exchange is inaccessible from your network, ingestion fails explicitly. No synthetic live results are substituted. Add a compliant alternative market-data adapter before proceeding.

For alternate file locations:

```bash
btc-edge --db data/mydata.sqlite --models models train --days 30
```

Monitor a channel's publicly available *metadata*:

```bash
btc-edge youtube --channel-id UCxxxxxxxxxxxxxxxxxxxxxx
```

To parse a source-authorised transcript, create a text file containing one explicit template such as:

```text
BTC LONG entry: 72500 stop: 72100 target: 73500
```

Then:

```bash
btc-edge parse-signal --file transcript.txt \
  --source-url 'https://www.youtube.com/watch?v=EXAMPLE' \
  --published-at '2026-10-25T10:00:00+00:00'
```

It saves a `REVIEW_REQUIRED` candidate with provenance. Do **not** use placeholder levels as trades. Extracting complex commentary, resolving uncertain conditionals and linking price to the current market will require additional development and independent verification.

## Scoring and safeguards

- `p_tradeable`: modelled probability that the **next** 30-minute BTC path meets a predefined movement and efficiency criterion; not the chance that a trade wins.
- `p_profitable_btc_long`: modelled likelihood that a specified bracket-style BTC **spot** long entered next candle produces a positive net return; this is not necessarily calibrated to an absolute probability yet.
- Both scores above 0.62 form a **candidate**. A research gate additionally requires at least 30 non-overlapping held-out trades, 55% net wins, positive net mean R and a better mean R than the fixed momentum benchmark.
- Even a passing research gate **only** permits `PAPER_CANDIDATE`, never a live order. `RESEARCH_ONLY` indicates inadequate validation; `STALE_DATA` means do not interpret the readings as current. No thresholds are chosen on the test sample.
- Realistic production paper trading still requires forward timestamped signal persistence, spread and market-impact modelling, data-quality monitoring, honest opportunity accounting, sufficient independent outcomes (preferably 200+), further regime testing, and a separate human-approved live integration.

**Limitations:** Kline highs/lows do not reveal which intrabar event happened first, so stops win ties; OHLCV can't model queue priority or actual fill probability. 30 days of five-minute candles contains many correlated observations, not thousands of independent trades. The logistic models are a deliberately modest baseline, not an established edge. Performance can change or disappear abruptly. Ingestion may be delayed, and Binance data represents that venue only. YouTube caption downloading via the official API generally requires video-editing permission; metadata feeds alone are not transcripts. The system has not been deployed to a server or scheduled.

## Design and follow-on milestones

1. Verify source coverage, latency and exchange availability; persist first live observations and ensure closed-candle freshness.
2. Log forward predictions with immutable timestamps and replay exactly as observed, including skipped candidate signals.
3. Test 14/30/60-day training windows and alternative half-lives, using purged walk-forward cross-validation and never tuning to the final holdout.
4. Add reliability diagrams, model-regime monitoring, volatility/liquidity checks, and evidence-based uncertainty thresholds.
5. Obtain **authorised** transcripts or structured real-time trader signals; timestamp publication, discovery, analysis and executable price independently. Reject ambiguous/late/counterfactual signals. Compare trader-only, market-only and combined filters at equal capital/risk with matched latency and fees.
6. Build a small visual dashboard and alert service after research verifies meaningful benefit. Keep long-only spot paper trading by default.

Data source: [Binance public market-data endpoint](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md). Caption permission restrictions: [YouTube Captions API](https://developers.google.com/youtube/v3/docs/captions/download).
