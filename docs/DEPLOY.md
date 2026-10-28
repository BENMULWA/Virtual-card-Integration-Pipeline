# Deployment

How to host the service and connect Wasabi, the payments gateway and resellers to it.

## 1. Pick a host name

Give the API a host of its own, for example `cards.mamlakapsp.com`. `payments.mamlakapsp.com` is the payments gateway and does not serve these routes. Create a DNS `A` record to the server and get a TLS certificate (certbot or similar). Everything below assumes `https://cards.mamlakapsp.com`.

## 2. What you need on the server

- Docker and Docker Compose, and nginx (or another TLS-terminating proxy).
- Outbound HTTPS to Wasabi, MongoDB Atlas, the payments gateway, Safaricom (if used) and your mail provider.
- The server's **public IP** in the MongoDB Atlas allowlist, and given to Wasabi (see section 6).

## 3. Files

```
Virtual-card-Integration-Pipeline/
  .env                 production settings (never committed)
  app/keys/private.pem   your RSA private key (signs requests to Wasabi)
  app/keys/public.pem    your RSA public key (given to Wasabi)
  app/keys/wsbpublic.pem Wasabi's public key (verifies Wasabi's signatures)
```

The keys are mounted read-only into the container and are never baked into the image. The container runs as uid 10001, so give that user the files and nobody else: `sudo chown 10001:10001 app/keys/*.pem && chmod 600 app/keys/*.pem`.

## 4. Start it

```bash
docker compose up -d --build
docker compose logs -f api
```

`docker-compose.yml` runs two containers from one image:

| Service | Command | Purpose |
|---|---|---|
| `api` | `uvicorn app.main:app` on `127.0.0.1:8000` | The API. Only nginx can reach it. |
| `reconcile` | `python -m app.jobs.reconcile` every 180 s | Recovers missed webhooks and callbacks, settles operations, retries reseller events, checks the treasury and the ledger. **It must always run**: Wasabi stops retrying a webhook after about 37 hours. |

Keep to a **single** `uvicorn` worker. Rate limits and live updates are in-process.

If host port 8000 is already used by another service, set `API_PORT=8010` (or any free port) in `.env` and use the same port in the nginx `proxy_pass` lines.

## 5. nginx

Copy `deploy/nginx-cards.conf` into `/etc/nginx/snippets/mamlaka-cards.conf` and include it inside the TLS `server` block for your cards host:

```nginx
server {
    listen 443 ssl;
    server_name cards.mamlakapsp.com;
    # ssl_certificate ... (certbot)
    include snippets/mamlaka-cards.conf;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

The snippet exposes `/health`, `/api/v1/webhooks/`, `/api/v1/mpesa/callback/`, `/api/v1/cards/`, `/api/v1/wallet/` and `/api/v1/partner/`, and sets `X-Forwarded-For` to the real client address (overwriting, never appending, so clients cannot spoof it). Access to customer routes is controlled by the user's token, not by IP.

## 6. Connect Wasabi

Give Wasabi:

1. **Webhook URL:** `https://cards.mamlakapsp.com/api/v1/webhooks/wasabi`
2. **Your server's public IP** to allowlist for API calls. Production API calls from an unlisted IP can be refused.
3. Your **public key** (`public.pem`).

Ask them to confirm the **webhook signature algorithm**. Until it is confirmed, set `ENABLE_WEBHOOK_SIGNATURE_VALIDATION=false`. That is safe: webhook bodies are only hints and every handler re-reads the real state from Wasabi, and the reconcile job catches anything missed.

## 7. Connect the payments gateway (top-ups)

Set `AIRTEL_CALLBACK_BASE_URL=https://cards.mamlakapsp.com`. The service tells the gateway to call back at `/api/v1/webhooks/mamlaka`. Callbacks are authenticated by an HMAC over the raw body (`X-Mamlaka-Signature`), keyed with `AIRTEL_CALLBACK_SIGNING_SECRET`; ask the gateway operator for a signing secret that belongs to **your own merchant**.

## 8. Environment reference

Copy `.env.example` to `.env`. The settings that matter most:

| Setting | Meaning |
|---|---|
| `APP_ENV` | `production` forces the dev-only features off (test token, dev email preview, auto-verify). |
| `MONGO_URL`, `DB_NAME` | MongoDB. Use a **production** cluster, not a development or backup one. |
| `JWT_SECRET` | At least 32 characters. Generate with `openssl rand -hex 32`. The app refuses to start with a weak one. |
| `CORS_ORIGINS` | Comma-separated origins allowed to call the API from a browser. |
| `WASABI_BASE_URL`, `WASABI_API_KEY` | Wasabi credentials. |
| `RSA_PRIVATE_KEY_PATH`, `RSA_PUBLIC_KEY_PATH`, `WASABI_PUBLIC_KEY_PATH` | Key locations inside the container (compose sets `app/keys/...`). |
| `LIVE_MONEY_ENABLED` | Must be `true` before card creation, deposits and withdrawals work. |
| `MAX_SINGLE_AMOUNT` | Per-operation cap in USD (default 500). |
| `FX_KES_PER_USD`, `FX_SPREAD_PCT` | The KES to USD rate and your margin. Funding is off while the rate is empty. No feed is wired in: keep it current. |
| `ALLOWED_CARD_TYPE_IDS` | Card types that may be registered and issued (default `111078`). |
| `ENABLE_WEBHOOK_SIGNATURE_VALIDATION` | See section 6. |
| `TRUST_PROXY_HEADERS` | `true` behind nginx (compose sets it) so rate limits use the real client IP. |
| `APP_PUBLIC_URL`, `MAIL_API_URL`, `MAIL_API_KEY`, `MAIL_FROM` | Email verification. A mail provider must be configured; the payload format must match the provider. |
| `AIRTEL_API_BASE_URL`, `AIRTEL_API_USERNAME`, `AIRTEL_API_PASSWORD`, `AIRTEL_CALLBACK_BASE_URL`, `AIRTEL_CALLBACK_SIGNING_SECRET` | Payments gateway (M-Pesa and Airtel top-ups). |
| `MPESA_*` | Optional direct Safaricom Daraja. When these are empty, M-Pesa top-ups go through the gateway. |
| `WALLET_WITHDRAW_ENABLED`, `WITHDRAW_MIN_KES`, `WITHDRAW_MAX_KES` | Wallet to phone withdrawals (off by default: it moves real money out). |
| `TREASURY_ALERT_BELOW_USD`, `TREASURY_RESERVE_USD`, `TREASURY_CURRENCIES`, `ALERT_WEBHOOK_URL` | Wasabi balance monitoring (see [OPERATIONS.md](OPERATIONS.md)). |

## 9. Verify

```bash
curl https://cards.mamlakapsp.com/health                      # {"status":"ok",...}
curl -i https://cards.mamlakapsp.com/api/v1/partner/balance   # 401: routes are live, a key is needed
```

## 10. Production checklist

- [ ] `APP_ENV=production`, `DEBUG=false`; no `DEV_AUTO_VERIFY_EMAIL`.
- [ ] A strong, new `JWT_SECRET`; a production MongoDB cluster with backups on.
- [ ] Keys present, mode `600`, not in git or the image.
- [ ] A working mail provider (test a real verification email).
- [ ] Wasabi webhook URL set; your server IP allowlisted; webhook signature algorithm confirmed.
- [ ] The reconcile container is running (`docker compose ps`).
- [ ] The Wasabi merchant balance is funded; `ALERT_WEBHOOK_URL` points at a channel someone reads.
- [ ] `FX_KES_PER_USD` set and reviewed.
- [ ] Customer-facing known limitations in [API.md](API.md) reviewed.
- [ ] Reseller API keys issued fresh over a private channel; the old test keys rotated.
