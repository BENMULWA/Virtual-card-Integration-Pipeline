# Mamlaka Virtual Cards - Partner API

Base URL: `https://payments.mamlakapsp.com/api/v1/partner`

## Credentials you receive (from Mamlaka, over a secure channel)

| Credential | Use |
|---|---|
| **API key** (`mk_live_...`) | Send on every request as `X-API-Key: <key>`. Server-side only; never ship it in a mobile or browser app. Shown once. |
| **Webhook signing secret** (`whsec_...`) | Verify the signature on every webhook we send you. |

Give Mamlaka an HTTPS **webhook URL** for your server. Keep a prepaid **float** with Mamlaka: card issuance and card deposits are debited from it.

## Conventions
- Every `POST` / `PUT` needs an `Idempotency-Key` header (8-64 chars). Retrying with the same key never repeats the operation.
- `customers/{id}` is **your** customer id (`externalId`). It is only ever resolved inside your own account.
- Amounts: card amounts are USD strings (`"10.00"`). Your float is in KES.
- Errors: `401` bad key, `402` float too low, `403` not allowed for your account, `404` unknown customer, `422` invalid input, `429` rate limit (600 requests/minute).

## Flow

1. `POST /customers` `{ "externalId", "fullName", "email", "phone" }` - idempotent on `externalId`.
2. KYC - upload each image, then submit the details:
   - `POST /customers/{id}/kyc/documents` (multipart: `file`, `kind` = `front` | `back` | `selfie`; JPG/PNG/PDF, max 2 MB) returns `fileId`.
   - `POST /customers/{id}/kyc` with the cardholder details and the `fileId`s (`idFrontId`, `idBackId`, `idHoldId`).
   - Wait for the `kyc.status_changed` webhook (or poll `GET /customers/{id}/kyc?refresh=true`).
   - If rejected, correct and resubmit with `PUT /customers/{id}/kyc/{holderId}`.
   - One approval per person; the same cardholder can hold more cards.
3. `GET /balance` - your float. `GET /rates` - your rate card (KES per USD, card fees). `GET /quote?kind=issue|deposit&amount=&cardTypeId=` - the exact KES an operation will take from your float.
4. `POST /customers/{id}/cards` `{ "cardTypeId", "holderId", "amount" }` - amount is the opening load in USD (the card type sets a minimum, e.g. 10). Fees are added; the total is debited from your float.
5. `GET /customers/{id}/cards`, `POST /customers/{id}/cards/freeze|unfreeze` `{ "cardNo" }`, `POST /customers/{id}/cards/deposit` `{ "cardNo", "amount" }`.

## Webhooks we send you

`POST <your webhook URL>` with a JSON body and these headers:

- `X-Mamlaka-Signature: sha256=<hex>` - HMAC-SHA256 of the **raw body** keyed with your signing secret. Reject anything that does not match.
- `X-Mamlaka-Event-Id` - same as `id` in the body.

```json
{"id":"...","type":"kyc.status_changed","createdAt":"2026-10-27T12:00:00Z",
 "data":{"customerId":"u-1","holderId":900,"status":"pass_audit","approved":true,"reason":null}}
```

Events: `kyc.status_changed` (`status`: `wait_audit`, `under_review`, `pass_audit`, `reject`), `card.updated`.
Answer with any `2xx` to acknowledge. Anything else is retried (1 min, 5 min, 30 min, 2 h, 6 h, then daily) and delivery is **at least once**: dedupe on `id`.

```python
import hmac, hashlib
def valid(raw_body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)
```

## Not in this version
Card number / CVV reveal, withdrawals back to your float, and per-customer wallet top-ups.
