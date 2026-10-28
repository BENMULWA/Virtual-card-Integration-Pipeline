# Mamlaka Virtual Cards - Integration Guide

Issue virtual Mastercards to your customers through one API. Mamlaka handles KYC submission, card issuance and card operations. You keep the customer relationship.

**Base URL:** `https://payments.mamlakapsp.com/api/v1/partner`

## 1. Authorization
- Mamlaka gives you two secrets: an **API key** (`mk_live_...`) and a **webhook secret** (`whsec_...`).
- Send the key on every request: `X-API-Key: mk_live_...`.
- Call the API from your **server only**. Never put the key in a mobile app or browser.
- A wrong or missing key returns `401`. Limit: 600 requests per minute.
- Lost or leaked key? Ask Mamlaka to regenerate it; the old one stops working immediately.

```bash
curl https://payments.mamlakapsp.com/api/v1/partner/balance -H "X-API-Key: $MAMLAKA_API_KEY"
```

## 2. Rules for every call
- Every `POST` and `PUT` needs an `Idempotency-Key` header (8-64 characters). Repeating a request with the same key never repeats the action. Use one key per business action and reuse it when you retry.
- A customer is identified by **your own id** (`externalId`). You never handle Mamlaka ids for customers.
- Errors: `401` bad key, `402` float too low, `403` not enabled for you, `404` unknown customer, `422` invalid input, `429` too many requests.

## 3. The customer journey

**Step 1 - Fund your float.** Cards are paid for from your prepaid float (KES), held with Mamlaka. Check it with `GET /balance`. A $10 card costs about 1,554 KES (10 USD load + 2 USD card fee at the current rate).

**Step 2 - Create the customer.**
`POST /customers` with `externalId`, `fullName`, `email`, `phone`. Repeating the same `externalId` returns the existing customer.

**Step 3 - Upload the KYC documents.**
`POST /customers/{externalId}/kyc/documents` (multipart): `file` and `kind` = `front`, `back` or `selfie`.
- JPG, PNG or PDF, max 2 MB each. The selfie is the customer holding their ID.
- Each upload returns a `fileId`. Keep the three ids.

**Step 4 - Submit KYC.**
`POST /customers/{externalId}/kyc` with the cardholder details:
`cardTypeId`, `areaCode`, `mobile`, `email`, `firstName`, `lastName`, `birthday`, `nationality`, `country`, `town`, `address`, `postCode`, `gender` (`M`/`F`), `idType` (`GOVERNMENT_ISSUED_ID_CARD`, `DLN`, `PASSPORT`, `HK_HKID`), `idNumber`, `issueDate`, `idNoExpiryDate`, `idFrontId`, `idBackId` (not needed for a passport), `idHoldId`.
- `cardTypeId` for the Mastercard Virtual (USD) card is `111078`.
- Dates are `YYYY-MM-DD`. Names, ID number and **date of issue must match the ID exactly**: the most common rejection is a wrong date of issue or a missing middle name.
- `firstName` + `lastName` together are at most 32 characters. Put a middle name into `firstName`.
- `address` and `town`: letters, numbers, hyphens and spaces only.
- **The email must be unique**: an email already registered for a cardholder is rejected ("Email already exists").
- Wasabi, the card processor, sends the customer a verification code at this email.

**Step 5 - Wait for the decision (usually a few hours).**
- You receive a `kyc.status_changed` webhook, or poll `GET /customers/{externalId}/kyc?refresh=true`.
- Statuses: `wait_audit` / `under_review` (waiting), `pass_audit` (approved), `reject` (see `reason`).
- KYC is **once per person**: the same approved customer can hold more cards.

**Step 6 - If rejected, fix and resubmit.**
`PUT /customers/{externalId}/kyc/{holderId}` with the corrected details. Only a rejected application can be updated.

**Step 7 - Issue the card.**
`POST /customers/{externalId}/cards` with `cardTypeId`, `holderId` (from the KYC response) and `amount`.
- `amount` is the opening load in USD (string, e.g. `"10"`). The minimum is **10 USD**, set by the card issuer. The opening load is part of creating the card; a card cannot be created empty.
- The total (load + card fee) is taken from your float. If the float is too low you get `402` and nothing is sent.
- The card starts as `pending` and becomes active shortly after (`card.updated` webhook).

**Step 8 - Manage cards.**
- `GET /customers/{externalId}/cards` - the customer's cards.
- `POST .../cards/deposit` `{cardNo, amount}` - add funds, from your float.
- `POST .../cards/freeze` and `.../cards/unfreeze` `{cardNo}`.

## 4. Webhooks (Mamlaka to you)
- Give Mamlaka an HTTPS endpoint on your server. Mamlaka sends `POST` with a JSON body:
  `{"id","type","createdAt","data":{...}}`.
- Events: `kyc.status_changed` (`data`: `customerId`, `holderId`, `status`, `approved`, `reason`) and `card.updated` (`customerId`, `cardNo`).
- **Verify the signature.** Header `X-Mamlaka-Signature: sha256=<hex>` is the HMAC-SHA256 of the **raw body**, keyed with your webhook secret. Reject anything that does not match.
- Reply `2xx` to acknowledge. Other answers are retried (1 min, 5 min, 30 min, 2 h, 6 h, then daily).
- Delivery is **at least once**: use `id` to ignore repeats.

```python
import hmac, hashlib
def valid(raw_body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)
```

## 5. Good practice
- Save the `externalId` to `holderId` link from the KYC response; you need it to issue cards.
- Retry only with the **same** `Idempotency-Key`. A new key means a new action.
- Do not depend on webhooks alone: poll `GET .../kyc?refresh=true` for customers waiting too long.
- Not available in this version: viewing card number/CVV through the API, withdrawals back to your float, and per-customer wallet top-ups.
