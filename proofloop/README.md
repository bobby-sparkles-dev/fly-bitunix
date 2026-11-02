# Proofloop MVP

Isolated implementation of an opportunity discovery engine. **Outbound communications disabled** by design. FastAPI, PostgreSQL (Neon), Render, simple secured web dashboard.

## Run
Set `DATABASE_URL`, `DASHBOARD_PASSWORD`, `SESSION_SECRET` then `uvicorn proofloop.app:app --reload` (from repository root).

## Scope
- Ideas, discovery-mode idea creation, evidence, leads, experiments, conversation records stored in Postgres.
- Research queries against public Hacker News Algolia API, with links and quoted text; never treat them as paying demand.
- Slow-wave experiment planning and a hard outbound-off policy. No emails or forum posts are sent by this MVP.
- Only owner account can access data using a signed, HttpOnly session cookie.

## Planned next phase
Provider integrations (Gmail OAuth, approved forum APIs), inbound webhooks, AI classification, outbound eligibility rules, idempotent send queue, opt-outs, per-channel limits and periodic scheduler.
