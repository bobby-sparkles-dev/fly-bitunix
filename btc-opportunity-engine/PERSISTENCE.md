# Reliable BTC Signal Lab: persistence and scheduling

## Current state
- A free Render web service hosts the dashboard and a Python background loop. Free web service can sleep.
- **No persistent database has been provisioned**. Render returned HTTP 402 while creating the smallest paid PostgreSQL database: payment method required.
- The repository includes an optional PostgreSQL **checkpoint bridge**, not a native relational migration. Set server-side `DATABASE_URL` once a provider is connected. The bridge stores a consistent, integrity-checked snapshot of the complete SQLite data set after each successful cycle. On restart, it restores before the worker starts.
- If no `DATABASE_URL` is configured, the original temporary SQLite mode remains active. The dashboard must not claim durable storage.

## Next hosting configuration

Prefer a dedicated durable PostgreSQL service, then migrate source tables (candles, live_forecasts, paper_trades, model version registry, system events) directly to PostgreSQL. The checkpoint bridge is an interim measure to protect data immediately.

1. Connect Neon (free tier subject to vendor limits), or add a payment method to Render. Never expose or commit a DB URL.
2. Inject `DATABASE_URL` into the Render service, using internal networking when supported.
3. Confirm a checkpoint was written, restart the service deliberately, and verify restored counts and model metadata against pre-restart figures.
4. Migrate to PostgreSQL-native tables and use transactions and idempotent writes.
5. Split processing into a worker running at five-minute UTC boundaries with a separate scheduled retrain. Keep the dashboard as read-only web service.
6. Confirm scheduler costs before creating Render Cron, or use a GitHub Actions schedule as a no-cost research prototype after durable storage is connected.
7. Compare incremental candle collection checkpoint results with exchange last-closed-candle times; alert on staleness, request throttling and gaps.
8. Preserve forward-only paper trade history and out-of-sample metrics separately. Retraining does not automatically improve performance; measure model drift and only promote objectively better candidates.

## Safety
No exchange order execution code. Model tradeability scores are exploratory and not calibrated probabilities. Targets of 55% wins and positive net expectancy are not established.
