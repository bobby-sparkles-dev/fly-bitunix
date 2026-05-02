# BitUnix Fly.io Proxy

A 130-line Node proxy that forwards BitUnix REST calls from a BitUnix-friendly
region (Frankfurt) so Lovable Cloud's Cloudflare Workers stop getting 403'd.
Every request is HMAC-signed with `PROXY_SECRET` + timestamp + nonce, so a
leaked proxy URL is useless without the secret.

## Deploy

```bash
# 1. Install flyctl (https://fly.io/docs/flyctl/install/) and `fly auth login`.

cd fly/bitunix-proxy

# 2. Generate a strong secret (used on BOTH sides):
SECRET=$(openssl rand -hex 32)
echo "$SECRET"   # save this — you'll paste it into Lovable secrets too

# 3. Launch the app (pick a unique name, e.g. yourname-bitunix-proxy).
#    Say NO to Postgres, NO to Redis, YES to deploy now.
fly launch --no-deploy --copy-config --region fra

# 4. Set the shared secret on Fly:
fly secrets set PROXY_SECRET="$SECRET"

# 5. Deploy:
fly deploy

# 6. Grab the URL:
fly status   # → e.g. https://yourname-bitunix-proxy.fly.dev
```

## Wire it into Lovable Cloud

Add two server-side secrets in Lovable settings:

- `BITUNIX_PROXY_URL` = `https://yourname-bitunix-proxy.fly.dev`
- `BITUNIX_PROXY_SECRET` = the same `$SECRET` from step 2

Once both are set, every BitUnix REST call (signed account/positions calls AND
public market data) is automatically rewritten through the proxy. If either
env var is missing, the app falls back to direct calls — so nothing breaks
before deploy.

## Verify

```bash
# From your laptop:
curl -i https://yourname-bitunix-proxy.fly.dev/_health
# → 200 ok

# An unsigned request must be rejected:
curl -i https://yourname-bitunix-proxy.fly.dev/api/v1/futures/market/tickers
# → 401 missing auth headers
```

In the app: open Settings → BitUnix Phase 1 → "Test Connection". You should
see PUBLIC + SIGNED both succeed even if direct BitUnix calls were 403'ing
before.

## Cost

`shared-cpu-1x` / 256MB, single machine, always-on:
- ~$1.94/month compute
- + a few cents bandwidth (BitUnix calls are tiny JSON)

Effectively under $2.50/month. Auto-stop is disabled because cold starts
would add ~1s to every market-data poll.

## Security notes

- HMAC covers `timestamp.nonce.METHOD.path?query.sha256(body)` with a 30s
  skew window. Nonces are remembered for 60s to block replays.
- Rotate `PROXY_SECRET`: regenerate, `fly secrets set`, update the Lovable
  secret, redeploy. Old in-flight requests fail closed.
- The proxy is intentionally generic — it forwards any path under
  `fapi.bitunix.com`. The BitUnix API key/secret never touch Fly; they stay
  in Lovable Cloud and are signed there before the request leaves.
