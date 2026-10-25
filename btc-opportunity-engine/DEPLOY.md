# Deploy BTC Signal Lab without affecting BitUnix

## Research-only deployment

This service **never sends live orders**. It serves the preview on `/` and the research state on `/api/state`.

1. Create a new Railway service from the GitHub repo `bobby-sparkles-dev/fly-bitunix` **using branch `feat/btc-opportunity-engine-mvp`**.
2. Set its **Root Directory** to `/btc-opportunity-engine`. It must NOT use the repository-root BitUnix proxy Dockerfile.
3. Attach a Railway persistent Volume mounted at **`/data`** so SQLite observations and JSON models survive redeploys.
4. Set `DATA_DIR=/data`, generate a public domain, and deploy. The service runs from its own Dockerfile with `python -m btc_edge.service`.
5. Open `https://<your-railway-domain>/api/health` (check service, not research readiness) and `/api/state` (look for `phase=RUNNING`, recent `updated_at`, and non-stale `score`).
6. Allow initial public-market backfill and fit to finish. If `phase=ERROR`, read `last_error` and service logs, notably possible regional blocking of Binance data API.

## What runs

- The web server starts immediately and the daemon research worker runs a cycle approximately every 5 minutes.
- First run imports 37 days of public BTC + ten altcoin five-minute bars; later cycles refresh two days and deduplicate.
- Research models retrain at most every six hours (and at process start). Both have a 30-day chronological window and seven-day holdout.
- Live predictions are written **once per candle**, independently of retraining. Matched 30-minute paper outcomes are added only when the outcome window has matured.
- Scores always remain **paper/research only**; even a passing historical gate does NOT authorize trading. The 55% goal has not been proven.

## Limitations

- Until a Railway service with a persistent volume is deployed, GitHub code and the raw.githack static page do NOT constitute a running backend.
- This is a *single-process* service; do not scale horizontally with SQLite. A persistent volume is essential. Background collection pauses during container downtime. Container startup with a large data history can briefly delay the first prediction.
- Binance availability depends on the deployment region and is not guaranteed; fail visibly rather than display synthetic scores.
- YouTube caption acquisition and automatically following traders are not active services yet. There are no broker credentials, signal-triggered orders, or trading endpoints.
- This project remains in a feature branch separate from the production BitUnix proxy.
