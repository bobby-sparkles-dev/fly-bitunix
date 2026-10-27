# BTC Signal Lab: forward research protocol (v1)

This is a **paper-only research system**. No real orders, no auto-promotion, no
threshold rewriting after a lucky streak. The protocol was fixed before
substantial forward experimental evidence existed.

## Two different purposes

1. **Strict market + BTC-long forecast.** A frozen model version records a
   market tradeability score and BTC-long score. Its entry gate remains
   62% / 62% until a separate, predeclared study justifies a candidate update.
   Earlier historical labels use a **different spot bracket and cost model**.
2. **Exploratory simulated BTC long entries.** Twelve pre-registered cohorts:
   hourly reference, momentum breakout and dip rebound, each over 30, 60,
   120 and 240 minutes. This answers whether the simulation works and whether
   any predefined entry type is worth studying. It does not endorse a trade.

## Evidence that must be stored for EACH observed completed candle

- Immutable UTC candle timestamp and true observation timestamp
- Exchange/market-data venue and exact model ID
- Both score values and whether market context was complete
- All available decision-time feature measurements (including altcoins)
- Which strict gates and experimental strategies qualified or failed, and why
- Indicators missing due to data gaps
- Subsequent market price changes and adverse/favourable excursions at
  30m, 60m, 120m and 240m, measured **only after a future entry could occur**
- Model version, feature names and training/evaluation report saved durably

Do not backfill missing live predictions using today's model and claim they
were made in real time. The original score and inputs must be immutable;
future outcomes can be appended but never retroactively alter predictions.

## How to avoid being fooled by early wins

- Four time horizons from the **same underlying signal** do not count as four
  independent opportunities. Display the unique entry count separately.
- Test all twelve candidate strategies concurrently. Apply a familywise
  multiple-comparison adjustment to win-rate intervals.
- Estimate net returns after explicit fees and slippage assumptions and show
  a day-cluster bootstrap lower bound, with a minimum day count.
- The existing hourly reference is a negative control, not a recommendation.
  Compare the same holding horizons. Avoid changing thresholds based on
  the same sample being used to celebrate a performance improvement.
- Report how many UTC trading days generated the results. A handful of wins
  from one bounce cannot validate an edge.
- Never equate the price-response diagnostic with calibrated accuracy of the
  *different* original bracket-target probability model.

## Next evidence gate

A cohort may be labelled **candidate for NEW forward holdout**, never
"validated for trading", only after:

1. At least 200 settled, non-overlapping entries *within that cohort*.
2. At least 20 distinct UTC trading days.
3. Twelve-way-adjusted lower win-rate interval above the 55% research target.
4. Day-block-resampled net expectancy lower bound above zero.
5. Positive mean net return after assumed costs and a better result than its
   same-horizon hourly reference.

Even then it requires a **new, pre-committed later-time holdout**. No
automatic model reweighting or exchange execution is permitted.

## Source and execution limitations

The market feed is Binance.US **spot** BTCUSDT data, not Bitunix futures.
A futures-like fee assumption is a proxy; basis, funding, spreads, maker/taker
fills, market impact and leverage/liquidation are not comprehensively modelled.
The win/loss and net R numbers are virtual experiments.

## Operational acceptance checks

- New observations recorded every five minutes while the paid Render app runs.
- Missing/stale inputs prevent entries and are visible on the dashboard.
- Neon receives each forward score, paper trial, outcome probe, immutable
  decision snapshot and versioned model report.
- Restart preserves recorded original decision times and outcomes.
- Read-only `/api/research` and `/api/research/decisions` offer an audit trail.

Do not declare this evidence pipeline fully verified until successful deployed
PostgreSQL writes and restored records are demonstrated, not just tests passing.
