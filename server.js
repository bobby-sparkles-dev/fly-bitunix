// Tiny BitUnix geo-unblock proxy.
// Forwards any path to https://fapi.bitunix.com from a Fly.io region (fra)
// that BitUnix actually answers from. Authed by HMAC(timestamp, nonce, method,
// upstream-path-and-query, sha256(body)) using a shared secret.
//
// Required env: PROXY_SECRET (must match BITUNIX_PROXY_SECRET on the app).
// Optional env: PORT (default 8080), MAX_SKEW_MS (default 30000).
//
// Pure Node.js, zero dependencies — runs on the official `node:20-alpine` image.

import http from "node:http";
import crypto from "node:crypto";

const UPSTREAM = "https://fapi.bitunix.com";
const PORT = Number(process.env.PORT ?? 8080);
const MAX_SKEW_MS = Number(process.env.MAX_SKEW_MS ?? 30_000);
const SECRET = process.env.PROXY_SECRET;

if (!SECRET) {
  console.error("PROXY_SECRET is required");
  process.exit(1);
}

// Replay protection: remember nonces for slightly longer than MAX_SKEW_MS.
const seenNonces = new Map(); // nonce -> expiresAt
function rememberNonce(n) {
  const now = Date.now();
  // Cheap GC.
  if (seenNonces.size > 5_000) {
    for (const [k, exp] of seenNonces) if (exp < now) seenNonces.delete(k);
  }
  seenNonces.set(n, now + MAX_SKEW_MS * 2);
}
function nonceSeen(n) {
  const exp = seenNonces.get(n);
  if (!exp) return false;
  if (exp < Date.now()) { seenNonces.delete(n); return false; }
  return true;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function timingSafeEqHex(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  try {
    return crypto.timingSafeEqual(Buffer.from(a, "hex"), Buffer.from(b, "hex"));
  } catch {
    return false;
  }
}

const server = http.createServer(async (req, res) => {
  // Health check (no auth) — for Fly health probes.
  if (req.url === "/_health") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("ok");
    return;
  }

  try {
    const ts = req.headers["x-proxy-ts"];
    const nonce = req.headers["x-proxy-nonce"];
    const sig = req.headers["x-proxy-sig"];

    if (typeof ts !== "string" || typeof nonce !== "string" || typeof sig !== "string") {
      res.writeHead(401).end("missing auth headers");
      return;
    }
    const tsNum = Number(ts);
    if (!Number.isFinite(tsNum) || Math.abs(Date.now() - tsNum) > MAX_SKEW_MS) {
      res.writeHead(401).end("stale timestamp");
      return;
    }
    if (nonceSeen(nonce)) {
      res.writeHead(401).end("replayed nonce");
      return;
    }

    const body = await readBody(req);
    const bodyStr = body.length ? body.toString("utf8") : "";
    const bodyHash = crypto.createHash("sha256").update(bodyStr).digest("hex");
    const method = (req.method ?? "GET").toUpperCase();
    const pathAndQuery = req.url ?? "/";

    const payload = `${ts}.${nonce}.${method}.${pathAndQuery}.${bodyHash}`;
    const expected = crypto.createHmac("sha256", SECRET).update(payload).digest("hex");
    if (!timingSafeEqHex(sig, expected)) {
      res.writeHead(401).end("bad signature");
      return;
    }
    rememberNonce(nonce);

    // Forward to BitUnix. Strip hop-by-hop and proxy-auth headers.
    const fwdHeaders = {};
    for (const [k, v] of Object.entries(req.headers)) {
      const lk = k.toLowerCase();
      if (lk.startsWith("x-proxy-")) continue;
      if (["host", "connection", "content-length", "accept-encoding"].includes(lk)) continue;
      if (Array.isArray(v)) fwdHeaders[k] = v.join(", ");
      else if (typeof v === "string") fwdHeaders[k] = v;
    }
    if (!fwdHeaders["user-agent"]) {
      fwdHeaders["user-agent"] = "Mozilla/5.0 (compatible; SniperBot/1.0)";
    }

    const upstreamUrl = `${UPSTREAM}${pathAndQuery}`;
    const upstreamRes = await fetch(upstreamUrl, {
      method,
      headers: fwdHeaders,
      body: method === "GET" || method === "HEAD" ? undefined : bodyStr,
    });

    const buf = Buffer.from(await upstreamRes.arrayBuffer());
    const respHeaders = {};
    upstreamRes.headers.forEach((value, key) => {
      const lk = key.toLowerCase();
      if (["content-encoding", "transfer-encoding", "connection"].includes(lk)) return;
      respHeaders[key] = value;
    });
    res.writeHead(upstreamRes.status, respHeaders);
    res.end(buf);
  } catch (err) {
    console.error("[proxy] error", err);
    if (!res.headersSent) res.writeHead(502, { "Content-Type": "text/plain" });
    res.end(`upstream error: ${(err && err.message) || "unknown"}`);
  }
});

server.listen(PORT, () => {
  console.log(`[bitunix-proxy] listening on :${PORT}, upstream=${UPSTREAM}`);
});
