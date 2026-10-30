# Operations

Running the service day to day: the reconcile job, the treasury check, reseller administration and what to do when something is stuck.

## The reconcile job

`python -m app.jobs.reconcile` runs every 3 minutes (the `reconcile` container). Each pass:

1. Reads back every Wasabi operation still in flight and settles it (a missed webhook).
2. Applies the wallet effect of finished operations whose settlement never happened (a crash between steps).
3. Settles top-ups whose callback never arrived (Daraja STK Query, or the gateway's status endpoint).
4. Settles wallet-to-phone withdrawals whose callback never arrived.
5. Re-reads reseller KYC still under review and announces any change; retries undelivered reseller webhooks.
6. Checks the treasury (throttled to every 15 minutes).
7. Verifies that every wallet's counters equal its ledger; a mismatch is logged as `LEDGER MISMATCH`.

Check that it is alive: `docker compose ps` and `docker compose logs --tail 50 reconcile`. It prints a line of counters per pass.

## Treasury: real USD vs what resellers prepaid

Every card operation spends the **Wasabi merchant balance** (USD). A reseller's float is a KES ledger entry that only promises payment, so the two must be watched together.

```bash
.venv/bin/python tests/resellers_keys.py treasury
```

| Status | Meaning | Action |
|---|---|---|
| `OK` | Wasabi USD is above the alert threshold and covers the floats | none |
| `LOW` | Wasabi USD is below `TREASURY_ALERT_BELOW_USD` | Top up the Wasabi merchant wallet |
| `UNDERFUNDED` | Resellers have prepaid more (in USD) than Wasabi holds | Top up Wasabi, or stop crediting floats |
| unknown | Wasabi could not be read | Nothing is blocked; check connectivity |

What the service does on its own:

- **Pre-flight:** issuance and card deposits are refused cleanly (`503`, nothing held, your balance never revealed) when the Wasabi balance cannot cover them plus `TREASURY_RESERVE_USD`. A replay of an operation already started is never blocked. If Wasabi cannot be asked, requests are **not** blocked; Wasabi's own refusal is still handled and refunded.
- **Alerts:** when the status gets worse, then every 6 hours while it stays bad, and once on recovery. Logged as errors; also posted to `ALERT_WEBHOOK_URL` (a Slack-style `{"text": ...}` webhook) when set.

You top up the Wasabi merchant wallet from the Wasabi dashboard (the merchant wallet is funded with crypto deposits per coin and chain).

## Pricing and margins

Every card price is built the same way:

```
our cost of 1 USD = FX_KES_PER_USD / (1 - WASABI_DEPOSIT_FEE_PCT/100)      e.g. 129.50 / 0.985 = 131.47
cost of an operation = USD amount (+ Wasabi's card fee and any Wasabi load fee) x our cost of 1 USD
price = cost x (1 + margin)       margin = FX_SPREAD_PCT (own app) | the reseller's margin (default PARTNER_MARGIN_PCT)
```

Wasabi's own fees are always treated as **cost**; your margin is added on top, never replaced by them.

```bash
.venv/bin/python tests/resellers_keys.py pricing                     # the rate card, per reseller
.venv/bin/python tests/resellers_keys.py set-margin --slug jasiri --pct 1.0
.venv/bin/python tests/resellers_keys.py earnings --days 30          # margin kept on settled operations
```

Change `FX_KES_PER_USD` whenever your USDT price moves (then `docker compose up -d`). Resellers see their rate at `GET /api/v1/partner/rates` and the exact charge at `GET /api/v1/partner/quote`. Operations priced before margins were recorded do not appear in `earnings`.

## Administering resellers

All reseller administration is one script, run on the server from the project folder:

```bash
.venv/bin/python tests/resellers_keys.py          # interactive menu
```

| Menu | Direct command | Purpose |
|---|---|---|
| 1 | `create-tenant --name Comet --slug comet [--webhook-url https://...] [--card-types 111078] [--max-amount 100]` | Create a reseller and print its **API key** and **webhook secret** (shown once) |
| 2 | `rotate-key --slug comet` | New API key; the old one stops working immediately |
| 3 | `credit-float --slug comet --amount-kes 1600 --ref BANK-REF-123` | Credit the reseller's prepaid float |
| 4 | `balance --slug comet` | Show the float |
| 5 | `list` | List resellers |
| 6 | `set-webhook --slug comet --url https://...` | Set or change where we send their events |
| 7 | `treasury` | The treasury report above |
| 8 | `pricing` | Your cost of a dollar and the rate each reseller pays |
| 9 | `set-margin --slug comet --pct 1.5` | Your margin on one reseller (blank resets to `PARTNER_MARGIN_PCT`) |
| 10 | `earnings --days 30` | Margin kept on settled card operations |
| 11 | `statement --slug comet` | Every movement on the float (top-ups, holds, charges, refunds) with the running balance |

Notes:

- **Slugs** are lowercase letters, digits and `-` (for example `commet-wallet`).
- **Webhook URLs** must be `https://` (plain `http://localhost` is allowed for local testing). Until a URL is set, events for that reseller are skipped, not retried.
- **Crediting a float:** use the bank or M-Pesa reference of the reseller's payment. The same reference twice does nothing, so a repeated command cannot double-credit. Only credit what Wasabi can cover.
- **Key rotation:** rotate when a key was shared in a chat or email, when someone with access leaves, or on a schedule. Send the new key over a private channel. The API key is never stored, only its hash.
- The reseller's webhook secret is stored on the reseller record in the `tenants` collection.

## When something is stuck

| Symptom | Cause | What to do |
|---|---|---|
| A card is `PENDING` for a long time | Wasabi has not confirmed; the webhook was missed | Wait for the reconcile pass; `GET /api/v1/cards/info` or the reseller's `GET /operations/{reference}?refresh=true` re-reads it |
| An operation or top-up is `REVIEW` | The outcome could not be confirmed (no provider reference after 30 minutes, an amount mismatch, or it stayed pending for 24 hours) | A person checks it with the provider, then settles it by hand. The money stays held; nothing is refunded or kept automatically |
| `LEDGER MISMATCH` in the logs | A crash left a wallet counter and the ledger out of step | Compare `wallets` with `ledger` for that owner; do not edit balances without recording a ledger entry |
| Issuance returns `503` "temporarily unavailable" | The treasury pre-flight (Wasabi USD too low) or Wasabi is down | Run the treasury report; top up Wasabi |
| Every Wasabi webhook returns `401` | Signature checking is on but the algorithm is not what we assume | Set `ENABLE_WEBHOOK_SIGNATURE_VALIDATION=false` until Wasabi confirms; reconcile covers the gap |
| Top-ups never credit | The gateway callback cannot reach `/api/v1/webhooks/mamlaka`, or the signing secret is wrong | Check `AIRTEL_CALLBACK_BASE_URL`, the secret and the nginx route; `GET /wallet/topups/{id}?refresh=true` recovers a single top-up |
| The server stops answering after a code change in development | `uvicorn --reload` is waiting for an open streaming connection to close | Restart it; add `--timeout-graceful-shutdown 5` in development |

## Backups and secrets

- MongoDB holds the ledger, wallets, operations and reseller records. Keep Atlas backups on and test a restore.
- Back up `app/keys/*.pem` somewhere private. Losing `private.pem` means Wasabi needs a new public key.
- Never commit `.env` or the keys. Rotate any secret that has been pasted into a chat or ticket.
