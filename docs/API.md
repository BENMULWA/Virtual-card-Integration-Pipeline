# Customer-facing API

The API used by Mamlaka's own web app. For resellers see [PARTNER_API.md](PARTNER_API.md).

- Interactive reference (always current): `GET /docs` (Swagger) and `GET /openapi.json` on a running server.
- Base path: `/api/v1`. All bodies are JSON unless noted.

## Conventions

| | |
|---|---|
| **Auth** | `Authorization: Bearer <JWT>` from `register` or `login`. Tokens last `JWT_EXPIRE_MINUTES` (default 60). |
| **Ownership** | Every card, holder, wallet and top-up is scoped to the token's user. Asking for someone else's returns `404`. |
| **Idempotency** | Every operation that changes state (`cards/*`, `wallet/topup`, `wallet/withdraw`) needs an `Idempotency-Key` header, 8 to 64 characters. Repeating a request with the same key never repeats the action; it returns the stored state. A new key is a new action. |
| **Amounts** | Card amounts are USD strings with at most 2 decimals (`"10.00"`). Wallet amounts are whole KES. |
| **Errors** | `{"detail": "..."}`. `401` not signed in, `402` insufficient wallet balance, `403` not allowed, `404` not found, `409` wrong state, `422` invalid input, `429` rate limited, `503` feature disabled or provider unavailable. |
| **Provider outcomes** | `422` with `{"detail": {"code", "message"}}` means Wasabi refused. `202` with `"status": "UNKNOWN"` means the outcome is not confirmed: **retry with the same Idempotency-Key** to read its state. |

## Accounts

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/users/register` | Create an account; returns a token |
| `POST` | `/users/login` | Sign in |
| `POST` | `/users/verify-email` | Confirm an email with the emailed token |
| `POST` | `/users/resend-verification` | New link (always `200`, never reveals whether an account exists) |
| `GET` | `/users/me` | Current profile |
| `POST` | `/users/change-password` | Change password (current password required) |
| `POST` | `/users/logout` | Client-side only; tokens are stateless |
| `GET` | `/users/dev/verification-link` | **Development only**: `404` in production |

Limits: login 8 per 5 minutes, register 5 per hour, verification 10 per 10 minutes; 5 failed logins lock the account for 15 minutes.

## The application journey

`GET /journey` returns the whole application as an ordered list of seven steps (`ACCOUNT`, `VERIFY_EMAIL`, `KYC_DOCUMENTS`, `KYC_SUBMISSION`, `KYC_REVIEW`, `CARD_ISSUANCE`, `FUNDING`), each `completed`, `in_progress`, `pending` or `blocked`, with `currentStep`, `nextAction`, `progress`, the `holder` and the `card`.

`GET /journey/stream` is Server-Sent Events: it sends the journey immediately and again on every change.
`GET /journey/events/history` returns recent events for clients that cannot hold a stream open.

Browser `EventSource` cannot set headers, so the stream accepts the token as a query parameter:

```js
const stream = new EventSource(`/api/v1/journey/stream?token=${encodeURIComponent(token)}`);
stream.addEventListener("journey", (e) => render(JSON.parse(e.data).journey));
```

> The token appears in the URL, so it can end up in server logs. Do not log query strings on this route, and prefer a short-lived stream token before launch.

## KYC and cardholders

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/cards/kyc/upload` | One document (`multipart`: `file`, `kind` = `front` / `back` / `selfie`). JPG, PNG or PDF, max 2 MB. Returns a `fileId`. |
| `POST` | `/cards/kyc/upload-bulk` | Front, back and selfie in one request |
| `GET` | `/cards/kyc/documents` | The caller's uploaded documents and file ids |
| `POST` | `/cards/cardholder/register` | Register the cardholder (needs the three `fileId`s). Returns a `holderId`. |
| `PUT` | `/cards/cardholder/{holder_id}` | Correct and resubmit a **rejected** cardholder |
| `GET` | `/cards/cardholder/{holder_id}` | Status plus a `review` block. `?refresh=true` re-reads it from Wasabi. |
| `GET` | `/cards/cardholders` | The caller's cardholders |
| `POST` | `/cards/kyc/submit` | Escalate a stuck review with a work ticket |
| `GET` | `/cards/occupations`, `/cards/diagnostic/kenya-cities` | Reference data for the KYC form |

Wasabi's rules that most often cause a rejection: names, ID number and **date of issue must match the ID exactly**; `firstName` + `lastName` at most 32 characters; `address` and `town` accept letters, numbers, hyphens and spaces only; the contact **email must be unique** across the merchant.

Cardholder statuses: `wait_audit`, `under_review`, `pass_audit` (approved), `reject`.

## Cards

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/cards/supported-BINS/types` | Card types on offer, with limits and fees |
| `POST` | `/cards/create` | Issue a card for an approved holder. Body: `cardTypeId`, `holderId`, `amount`. The opening load plus the card fee is held from the wallet. |
| `GET` | `/cards/list-BINS` | The caller's cards (paginated) |
| `POST` | `/cards/info`, `/cards/balance` | Card status and live balance |
| `POST` | `/cards/deposit` | Load a card from the wallet |
| `POST` | `/cards/withdraw` | Move card funds back to the wallet |
| `POST` | `/cards/freeze`, `/cards/unfreeze`, `/cards/cancel` | Lock, unlock, permanently close |
| `POST` | `/cards/sensitive` | Full card number, expiry and CVV (see Known limitations) |

Card creation and money operations are gated by `LIVE_MONEY_ENABLED` and capped by `MAX_SINGLE_AMOUNT`. They finish asynchronously: the response is `PENDING` or `SUCCESS`, and the final state arrives by webhook or the reconcile job.

## Wallet and top-ups

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/wallet` | KES `available` and `held` |
| `GET` | `/wallet/quote?usd=10&kind=fund\|withdraw` | KES price of a card operation |
| `POST` | `/wallet/topup` | Start a top-up: `{ "phone", "amountKes", "provider": "mpesa\|airtel", "channel": "auto\|daraja\|gateway" }` |
| `GET` | `/wallet/topups/{id}` | Top-up status. `?refresh=true` asks the provider if the callback was missed. |
| `POST` | `/wallet/withdraw` | Send KES to the customer's **own registered phone** (needs `WALLET_WITHDRAW_ENABLED=true`) |
| `GET` | `/wallet/withdrawals/{reference}` | Withdrawal status |

`channel: "auto"` uses direct Safaricom Daraja when it is configured and the Mamlaka payments gateway otherwise. Airtel always uses the gateway. The wallet is credited only after the provider confirms the payment **and** the amount matches what was requested.

## Webhooks received

| Path | From | Authentication |
|---|---|---|
| `POST /webhooks/wasabi` | Wasabi | Optional RSA signature (`ENABLE_WEBHOOK_SIGNATURE_VALIDATION`). Payloads are treated as hints: state is re-read from Wasabi. Deduplicated on `X-WSB-REQUEST-ID`. |
| `POST /webhooks/mamlaka` | Mamlaka payments gateway | `X-Mamlaka-Signature: sha256=<HMAC-SHA256 of the raw body>` keyed with `AIRTEL_CALLBACK_SIGNING_SECRET`. Unsigned requests get `401`. |
| `POST /mpesa/callback/{secret}` | Safaricom | Secret in the path, plus confirmation through STK Query |

Wasabi categories handled: `card_holder`, `card_transaction`, `work`. Others are stored.

## Health

`GET /health` returns `{"status":"ok"}` and is public.

## Known limitations

- `POST /cards/sensitive` returns the full card number and CVV to any valid token. Add a step-up (email code plus authenticator) before launch.
- Customer routes use the JWT only; a suspended account keeps access until its token expires.
- The journey stream puts the JWT in the URL.
- Rate limiting and the live-update bus are in-process, so run one worker.
