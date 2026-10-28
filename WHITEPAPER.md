# Mamlaka Virtual Cards

## Technical Whitepaper: process flows, reseller model and settlement

**Version:** 2.0
**Date:** 2026-10-28
**Card provider:** WasabiCard
**Status:** Implemented and tested end to end in a development environment. Section 11 lists what must still be closed before real customers. This is not a statement of production readiness.

Version 1.0 (2026-10-05) was a review of an early skeleton. Most of its findings have since been fixed; Section 11 records which, and which remain.

---

## 1. Executive summary

Mamlaka Virtual Cards is a backend service that issues and manages **virtual Mastercards** through WasabiCard's merchant API. It serves two kinds of caller:

- **Customers** of Mamlaka's own application, who register, pass KYC, top up a KES wallet with M-Pesa and get a card.
- **Resellers** (businesses such as Comet and Jasiri), who use a **Partner API** to offer cards to their own customers, paid for from a prepaid float.

The service is a FastAPI application backed by MongoDB. It validates requests, enforces ownership, records every money-affecting operation before it calls a provider, keeps a ledger of wallet and float movements, signs requests to Wasabi, and reconciles asynchronous outcomes from webhooks and a background worker.

The central design rule is that **money is held first, the provider is called once, and the hold is then captured or released**. An unknown outcome (a timeout) never refunds: it stays held until it is read back from the provider.

## 2. Participants

| Party | Role |
|---|---|
| **Customer** | Uses Mamlaka's web app (a separate repository). |
| **Reseller** | A business that integrates the Partner API for its own customers. Holds a prepaid float with Mamlaka. |
| **Mamlaka Virtual Cards service** | This repository. Enforces rules, keeps ledgers, talks to the providers. |
| **WasabiCard** | The card program provider: cardholder review, card issuance, card operations. Holds Mamlaka's USD merchant balance. |
| **Mamlaka payments gateway** | Collects KES from phones (M-Pesa, Airtel) and pays KES out. A separate system run by Mamlaka PSP. |
| **Safaricom Daraja** | Optional direct M-Pesa integration. |
| **Operations and compliance** | Credit floats, fund the Wasabi balance, review stuck items, hold the production credentials. |

## 3. System architecture

```mermaid
flowchart TB
    subgraph CALL["Callers"]
        CU["Customer web app"]
        RS["Reseller servers: Comet, Jasiri"]
    end

    subgraph SVC["Mamlaka Virtual Cards service (FastAPI)"]
        API["Customer API"]
        PAPI["Partner API"]
        ENG["Operation engine<br/>hold, call once, settle"]
        LED["Ledger and wallets"]
        HOOK["Webhook receivers"]
        REC["Reconcile worker<br/>and treasury check"]
        OUT["Reseller event outbox"]
    end

    DB[("MongoDB")]

    subgraph EXT["External systems"]
        WSB["WasabiCard API"]
        GW["Mamlaka payments gateway"]
        SAF["Safaricom Daraja"]
        RWH["Reseller webhook endpoints"]
    end

    CU --> API
    RS --> PAPI
    API --> ENG
    PAPI --> ENG
    ENG --> LED
    ENG --> DB
    LED --> DB
    ENG -->|"signed requests"| WSB
    API -->|"STK push and payouts"| GW
    API -.->|"optional"| SAF
    REC --> ENG
    REC -->|"read USD balance"| WSB
    REC --> OUT
    OUT -->|"signed events"| RWH
    WSB -->|"webhooks"| HOOK
    GW -->|"signed callbacks"| HOOK
    SAF -->|"callback"| HOOK
    HOOK --> ENG
```

| Component | Responsibility | Code |
|---|---|---|
| Operation engine | Records intent, holds funds, calls Wasabi once, settles | `app/cards/service.py`, `app/transactions/orchestrator.py` |
| Ledger | Idempotent credit, reserve, capture, release on integer minor units | `app/wallet/ledger.py` |
| Partner layer | Reseller accounts, API keys, customers, floats, events | `app/partners/` |
| Gateway client | STK collections and payouts, signed callbacks | `app/mobilemoney/` |
| Treasury | Real Wasabi USD against reseller floats | `app/core/treasury.py` |
| Reconcile worker | Recovers anything a webhook missed | `app/jobs/reconcile.py` |

## 4. Customer journey

The customer-facing application tracks one ordered journey of seven steps, exposed at `GET /api/v1/journey` and streamed over Server-Sent Events.

```mermaid
flowchart TD
    A["1. Register"] --> B["2. Verify email"]
    B --> C["3. Upload ID front, back and selfie"]
    C --> D["4. Submit identity details<br/>cardholder registration"]
    D --> E{"5. Wasabi review"}
    E -->|"approved: pass_audit"| F["6. Top up wallet and request card"]
    E -->|"rejected"| G["Correct details and resubmit<br/>PUT cardholder"]
    G --> E
    E -->|"stuck"| H["Escalate with a work ticket"]
    H --> E
    F --> I{"Wallet covers the price?"}
    I -->|"no"| J["Top up with mobile money"]
    J --> I
    I -->|"yes"| K["Card issued<br/>pending, then active"]
    K --> L["7. Use and manage the card<br/>load, balance, freeze, details"]
```

Wasabi's cardholder statuses are `wait_audit`, `under_review`, `pass_audit` (approved) and `reject`. Approval is Wasabi's decision: Mamlaka can only observe it, escalate, or help the customer resubmit. KYC is **once per person**: an approved cardholder can hold several cards.

Rules that cause most rejections: names, ID number and the **date of issue must match the ID exactly**; first plus last name at most 32 characters; address and town accept letters, numbers, hyphens and spaces only; the contact email must be unique across the whole merchant account.

## 5. Operations: the idempotent lifecycle

Every action that touches Wasabi (create holder, create card, deposit, withdraw, freeze, cancel) follows one lifecycle.

```mermaid
stateDiagram-v2
    [*] --> INITIATED: intent recorded, funds held
    INITIATED --> PENDING: Wasabi accepted
    INITIATED --> SUCCESS: Wasabi finished at once
    INITIATED --> FAILED: Wasabi refused
    INITIATED --> UNKNOWN: timeout or server error
    UNKNOWN --> PENDING: read back, still processing
    UNKNOWN --> SUCCESS: read back, done
    UNKNOWN --> FAILED: read back, refused
    PENDING --> SUCCESS: webhook or reconcile
    PENDING --> FAILED: webhook or reconcile
    SUCCESS --> [*]: hold captured, money spent
    FAILED --> [*]: hold released, money returned
```

How funds behave in each state:

| State | Wallet or float |
|---|---|
| `INITIATED`, `PENDING`, `UNKNOWN` | **Held** (moved from `available` to `held`). Nothing is lost or released. |
| `SUCCESS` | Held amount **captured** (spent). |
| `FAILED` | Held amount **released** (returned). |

Guarantees:

- **Idempotent.** The Wasabi order number is derived from the user, the operation and a client `Idempotency-Key`. A repeated request returns the stored state and never charges or sends twice.
- **Exactly once.** Every ledger change has a unique `entry_key` (`<reference>:<kind>`). A hold is captured **or** released, never both, however many times a callback replays.
- **Unknown is not failed.** A timeout, a 5xx or an unparsable reply means the provider may have processed it. The operation stays held until a webhook or the reconcile worker reads the real state.
- **Final states never move backwards.** A late `PENDING` cannot overwrite a recorded `SUCCESS`.

### 5.1 Issuing a card (customer paying from the wallet)

```mermaid
sequenceDiagram
    actor C as Customer
    participant A as Mamlaka API
    participant L as Ledger
    participant W as WasabiCard
    participant R as Reconcile worker

    C->>A: Create card, with Idempotency-Key
    A->>A: Check ownership, holder approved, card type allowed
    A->>A: Treasury pre-flight, Wasabi USD covers it
    A->>L: Hold KES price from the wallet
    alt insufficient balance
        L-->>A: refused
        A-->>C: 402 top up first
    else held
        A->>W: Signed createCard
        alt Wasabi refuses
            W-->>A: error
            A->>L: Release hold
            A-->>C: Failed, wallet unchanged
        else accepted
            W-->>A: wait_process
            A-->>C: Processing
            W->>A: Webhook, card transaction
            A->>W: Read the authoritative state
            A->>L: Capture hold, card active
            A-->>C: Card ready
        end
    end
    R->>W: If no webhook arrived, read the state
    R->>L: Capture or release
```

## 6. Money, ledgers and settlement

### 6.1 Three separate balances

The most important thing to understand is that **three different pools of money exist**, owned by different parties.

```mermaid
flowchart TB
    subgraph P1["Pool 1: customer money"]
        CW["Customer wallet<br/>KES ledger in this service"]
    end
    subgraph P2["Pool 2: reseller prepayment"]
        FL["Reseller float<br/>KES ledger, credited by an admin"]
    end
    subgraph P3["Pool 3: real USD"]
        WB["Wasabi merchant balance<br/>USD, held by Wasabi"]
    end

    MM["Mobile money<br/>M-Pesa, Airtel"] -->|"top-up credits"| CW
    CW -->|"card operation: hold then capture"| OP["Card operation"]
    RP["Reseller pays Mamlaka<br/>bank or M-Pesa"] -->|"admin credits with a reference"| FL
    FL -->|"partner card operation: hold then capture"| OP
    OP -->|"Wasabi deducts load plus card fee"| WB
    FU["Mamlaka funds Wasabi<br/>crypto deposit"] --> WB
    WB --> TR{"Treasury check<br/>real USD covers floats?"}
    FL --> TR
```

- A **customer wallet** holds KES the customer has paid in. It is a ledger number backed by money in the payments gateway account.
- A **reseller float** is a ledger number that says "this reseller has prepaid Mamlaka this much". It is **not cash**: it is only a promise unless real USD backs it.
- The **Wasabi merchant balance** is the only place real USD is deducted when a card is created. Mamlaka funds it directly with Wasabi.

### 6.2 Settlement cycle with a reseller

```mermaid
sequenceDiagram
    actor U as Reseller's customer
    participant R as Reseller
    participant M as Mamlaka Virtual Cards
    participant W as WasabiCard
    actor Ops as Mamlaka admin

    U->>R: Pays KES to the reseller
    R->>Ops: Reseller prepays Mamlaka (bank or M-Pesa), with a payment reference
    Ops->>M: Credit float: credit-float slug amount reference
    Note over M: The same reference twice is a no-op, so a repeat cannot double-credit
    R->>M: Issue card for customer (Partner API)
    M->>M: Hold cost from the float
    M->>W: createCard
    W-->>M: Success, Wasabi deducts load plus fee in USD
    M->>M: Capture hold, float reduced
    M-->>R: Event operation.updated SUCCESS
    Ops->>W: Keep the Wasabi USD balance funded
    M->>M: Treasury check compares Wasabi USD with all floats
```

### 6.3 Pricing

A card operation has a **cost**, set by Mamlaka from the live Wasabi catalog: the opening load plus the card fee, converted from USD to KES at the configured rate (`FX_KES_PER_USD`). A reseller prices its own customer from that cost.

```mermaid
flowchart LR
    Q["Reseller asks GET /quote<br/>kind, amount, cardTypeId"] --> COST["Mamlaka cost in KES<br/>(load + card fee) x rate"]
    COST --> MARGIN["Reseller adds its own margin<br/>e.g. 2 percent"]
    MARGIN --> CHG["Charge to the reseller's customer"]
    COST -.->|"debited from the reseller float"| FLT["Float"]
    CHG -.->|"collected by the reseller"| REV["Reseller revenue"]
```

Worked example (rate 129.50, a $10 card with a $2 card fee): cost = (10 + 2) x 129.50 = **KES 1,554.00** from the float. With a 2 percent margin the reseller charges its customer **KES 1,585.08** and keeps KES 31.08. Quoting from Mamlaka's own cost means a rate change on Mamlaka's side can never leave the reseller out of pocket.

### 6.4 Treasury: keeping the pools honest

```mermaid
flowchart TD
    S["Every 15 minutes, or on demand"] --> R["Read Wasabi available USD"]
    R --> K{"Readable?"}
    K -->|"no"| U["Unknown: keep the last status, never alert, never block"]
    K -->|"yes"| F["Sum all reseller floats in KES<br/>convert to USD at the rate"]
    F --> A{"Wasabi USD below floats?"}
    A -->|"yes"| UF["UNDERFUNDED<br/>prepaid more than we hold"]
    A -->|"no"| B{"Below the alert threshold?"}
    B -->|"yes"| LO["LOW"]
    B -->|"no"| OK["OK"]
    UF --> AL["Alert: now, then every 6 hours"]
    LO --> AL
    OK --> RC["If it was bad, alert once: recovered"]
```

The same check runs as a **pre-flight** on every issuance and card deposit: if the real Wasabi balance cannot cover the request (plus a configured reserve), it is refused cleanly before anything is held, and the caller is told only that the service is temporarily unavailable. If Wasabi cannot be asked, requests are not blocked: Wasabi's own refusal is still handled and refunded.

## 7. Top-ups and withdrawals

Customers fund the KES wallet through the Mamlaka payments gateway (M-Pesa and Airtel) or, optionally, direct Safaricom Daraja. The wallet is credited only after the provider confirms payment **and** the confirmed amount equals the amount requested.

```mermaid
sequenceDiagram
    actor C as Customer
    participant A as Mamlaka API
    participant G as Payments gateway
    participant P as Customer's phone
    participant L as Ledger
    participant R as Reconcile worker

    C->>A: Top up, phone and amount, with Idempotency-Key
    A->>A: Record the top-up as PENDING
    A->>G: Collect, externalId and callbackUrl
    G->>P: STK prompt
    P-->>G: Customer approves with a PIN
    G->>A: Signed callback, X-Mamlaka-Signature
    A->>A: Verify HMAC over the raw body, reject if it fails
    A->>A: Check reference and amount match the request
    A->>L: Credit the wallet once, unique entry key
    A-->>C: Wallet credited
    R->>G: If no callback arrived, ask the gateway for the status
    R->>L: Credit once, same entry key, so no double credit
```

Withdrawals to a phone follow the same lifecycle as card operations: the KES is **held first**, the gateway is called once, a confirmed payout captures the hold, a failure releases it, and an unknown outcome keeps it held. Withdrawals can only go to the phone number registered on the account and are off by default (`WALLET_WITHDRAW_ENABLED`), because they move real money out.

## 8. The reseller (partner) model

A reseller is a **tenant**. Its customers are stored as `<tenant_id>:<externalId>`, so every wallet, cardholder, card and operation is isolated per reseller by the same ownership checks the first-party app uses. A reseller can never read or act on another reseller's customers: the answer is simply "not found".

### 8.1 Onboarding a reseller

```mermaid
sequenceDiagram
    actor Ops as Mamlaka admin
    participant S as Admin script
    participant DB as MongoDB
    actor R as Reseller developer

    Ops->>S: create-tenant name, slug, webhook URL
    S->>DB: Store tenant with SHA-256 hash of the API key
    S-->>Ops: API key and webhook secret, shown once
    Ops->>R: Send both over a private channel, with the integration guide
    Ops->>S: credit-float slug, amount, payment reference
    S->>DB: Ledger credit, idempotent on the reference
    R->>S: Webhook URL provided later
    Ops->>S: set-webhook slug, url
    R->>DB: First call GET /balance with X-API-Key
    Note over R,DB: 200 and the float balance means authorization works
```

The API key is a 256-bit random value, shown once and stored only as a hash. A lost or leaked key is replaced with `rotate-key`; the old key stops working immediately.

### 8.2 A reseller's customer, end to end

```mermaid
sequenceDiagram
    actor U as Reseller's customer
    participant R as Reseller server
    participant P as Partner API
    participant W as WasabiCard

    R->>P: POST /customers, externalId, name, email, phone
    R->>P: POST /customers/id/kyc/documents, front, back, selfie
    P->>W: Upload files, returns file ids
    R->>P: POST /customers/id/kyc, details and file ids
    P->>W: Register cardholder
    W-->>P: holderId, status wait_audit
    Note over W: Wasabi reviews, usually a few hours
    W->>P: Webhook, holder status changed
    P->>W: Read the authoritative status
    P->>R: Signed event kyc.status_changed
    alt rejected
        R->>P: PUT /customers/id/kyc/holderId, corrected details
    else approved
        R->>P: GET /quote, kind issue
        R->>P: POST /customers/id/cards, amount, Idempotency-Key
        P->>P: Hold the cost from the reseller float
        P->>W: createCard
        W->>P: Webhook, card transaction
        P->>R: Signed event operation.updated SUCCESS
        R-->>U: Card ready
        R->>P: POST /customers/id/cards/details, after re-authenticating the user
        P-->>R: Number, expiry, CVV, never stored or logged
    end
```

### 8.3 Events sent to a reseller

Events are stored first, then delivered, so a reseller that is briefly down still receives everything. Delivery is **at least once**: a reseller must ignore repeats using the event `id`.

```mermaid
flowchart TD
    E["Event created<br/>kyc.status_changed, card.updated, operation.updated"] --> ST["Stored as PENDING"]
    ST --> SD["POST to the reseller webhook URL<br/>header X-Mamlaka-Signature, HMAC-SHA256 of the body"]
    SD --> Q{"2xx answer?"}
    Q -->|"yes"| DONE["DELIVERED"]
    Q -->|"no"| N{"Attempts left?"}
    N -->|"yes"| BO["Wait: 1 min, 5 min, 30 min, 2 h, 6 h, then daily"]
    BO --> SD
    N -->|"no"| FAIL["FAILED, logged for an operator"]
    ST --> NU{"No webhook URL set?"}
    NU -->|"yes"| SK["SKIPPED, not retried"]
```

### 8.4 A worked example: Jasiri

Jasiri is a retail app that adds virtual cards as a funding channel under **Deposits**. It is a reseller that also keeps its own customer wallets.

```mermaid
sequenceDiagram
    actor U as Jasiri user
    participant J as Jasiri backend
    participant M as Mamlaka Partner API
    participant W as WasabiCard

    U->>J: Open Deposits then Virtual Card
    J->>M: GET /quote, kind issue
    M-->>J: Cost in KES
    J->>J: Price = cost plus Jasiri margin, shown to the user
    U->>J: Confirm with email code and authenticator code
    J->>J: Debit the user's KES wallet first, keyed by the operation id
    J->>M: POST cards, Idempotency-Key = operation id
    alt refused
        M-->>J: error
        J->>J: Refund the wallet once
    else timeout
        Note over J: Keep the money held. Settle later by replaying the same key
    else accepted
        M->>W: createCard
        W-->>M: success
        M->>J: Signed event operation.updated
        J->>J: Mark the charge complete, money stays spent
    end
    J-->>U: Card page with balance, Load, Details, Freeze
```

Jasiri's wallet debit is its own money rule: Jasiri charges the user first, calls Mamlaka with the operation id as the idempotency key, and refunds exactly once on a definitive refusal.

## 9. Reliability: reconciliation and the webhook trust model

**Webhooks are hints, not truth.** A callback only says *which* holder, card or operation changed. Every handler re-reads the authoritative state from Wasabi's API, so a forged callback can at worst cause an extra read. Callbacks are deduplicated on Wasabi's `X-WSB-REQUEST-ID`; gateway callbacks and reseller events are authenticated with an HMAC over the raw body.

The reconcile worker covers everything a webhook missed. Wasabi stops retrying a webhook after about 37 hours, so the worker must always run.

```mermaid
flowchart TD
    T["Every 3 minutes"] --> O1["1. Wasabi operations still in flight<br/>read back and settle"]
    O1 --> O2["2. Finished operations whose wallet effect<br/>was never applied: apply it"]
    O2 --> O3["3. Top-ups with no callback<br/>ask the provider, credit once"]
    O3 --> O4["4. Withdrawals with no callback<br/>ask the gateway, capture or release"]
    O4 --> O5["5. Reseller KYC still in review<br/>announce changes, retry undelivered events"]
    O5 --> O6["6. Treasury check<br/>throttled to 15 minutes"]
    O6 --> O7["7. Every wallet counter equals its ledger?<br/>log LEDGER MISMATCH if not"]
```

## 10. Security model

| Area | Control |
|---|---|
| Customer authentication | JWT (HS256); bcrypt (cost 12) passwords with a strength policy; lockout after 5 failed logins; per-endpoint rate limits; no account enumeration |
| Ownership | Every card, holder, wallet, top-up and operation is filtered by the token's user, or by the reseller's tenant |
| Reseller authentication | `X-API-Key`, stored only as a SHA-256 hash, shown once, rotatable; 600 requests per minute per reseller |
| Requests to Wasabi | RSA SHA-256 signature over the exact serialized bytes sent |
| Inbound webhooks | Wasabi: re-read state, deduplicate. Gateway and reseller events: HMAC-SHA256 over the raw body, constant-time compare |
| Card data | Full number and CVV are never stored or logged. The reseller reveal endpoint audits who revealed which card (never the values) and is `Cache-Control: no-store` |
| Secrets | `.env` and `app/keys/*.pem` are git-ignored and mounted at runtime, never baked into the image |
| Production switches | The test token, dev email preview and auto-verify are forced off when `APP_ENV=production` |
| Documents | KYC images are validated for type (JPG, PNG, PDF), magic bytes and 2 MB size, and sent to Wasabi. Identity values are not kept in general-purpose records |

## 11. Status: what is done, and what must still close

### 11.1 Resolved since version 1.0

| v1.0 finding | Now |
|---|---|
| Wrong field names (`cardId`), missing `merchantOrderNo` | Contract aligned: `cardNo`, `merchantOrderNo` on every mutation |
| Float and integer amount handling | Decimal-safe amounts; integer minor units in the ledger |
| No ownership or real users | Real accounts, ownership enforced on every route, uploads authenticated; test token off in production |
| Callbacks not verified or reconciled | Deduplicated, treated as hints, plus a reconcile worker |
| Secrets in the image, payload logging | `.dockerignore` fixed, secrets injected at runtime, bodies not logged |
| No idempotency or reconciliation | Intent recorded first, deterministic references, unknown outcomes held |
| Holder and card data not persisted | Holders, cards and operations persisted and linked to owners |
| No wallet or ledger | Wallet ledger with holds, M-Pesa and gateway top-ups, withdrawals |
| No UI | A user app exists in a separate repository |
| No reseller support | Partner API, tenants, floats, signed events, admin script |

### 11.2 Open before real customers

These are known and not yet fixed:

1. **Ledger atomicity.** A wallet counter and its ledger entry are written in two steps. A crash between them can leave a hold with no ledger entry. The reconcile worker detects a mismatch but does not repair it. Use MongoDB transactions.
2. **Customer money routes do not re-check the account.** They authenticate the token only, so a suspended or unverified user keeps access until the token expires.
3. **Card details on the customer route.** `POST /cards/sensitive` needs a step-up (email code plus authenticator), as the reseller side already does.
4. **STK push to any number.** A top-up can be started for a phone that does not belong to the account, and money endpoints are not rate-limited.
5. **Automatic failure of unseen operations.** After 30 minutes without a record at Wasabi, an operation is marked failed and its hold released. If Wasabi's listing lags, a card could be paid for twice. Such cases should go to manual review.
6. **Wasabi response signatures are not verified**, and the webhook signature algorithm is unconfirmed. Today state is re-read through Wasabi's API instead.
7. **No password reset**, and the email sender's payload matches no real provider.
8. **No FX feed.** The KES to USD rate is set by hand and can go stale.
9. **No sandbox for resellers.** Partners integrate against live Wasabi unless a simulated processor is built.
10. **Card-data scope.** Revealing full card details puts the revealing system in PCI DSS scope. Prefer a hosted reveal.
11. **Settlement is manual.** A reseller's payment and its float credit are matched by an administrator; per-reseller fees and automatic settlement are not built.
12. **Compliance.** Holding customer balances, the data-protection duties around KYC, and the agreement with the card provider are contractual and licensing matters, not code.
13. **Observability.** There are logs and treasury alerts but no metrics dashboard.

## 12. Live rollout boundary

Production activation is an operational change, not an environment-variable swap. Before release, confirm with Wasabi:

- The live API host, merchant account, card-type access and enabled operations.
- The production API key and the merchant public key that matches the mounted private key.
- Production egress IP allowlisting and the public HTTPS webhook URL.
- The webhook signing algorithm, and the enabled callback categories.

Keep sandbox and production credentials, keys, databases and callback URLs separate. Fund the Wasabi merchant balance before the first card, and use the smallest permitted amounts for live smoke tests. Never run a test with real funds just to discover a payload or status.

## 13. Testing

The automated suite (`python -m pytest -q`) covers signing and canonicalisation, ownership isolation, idempotent operations, hold, capture and release, unknown outcomes, callback deduplication and signatures, the gateway and M-Pesa top-ups, withdrawals, the Partner API (including isolation between resellers and signed event delivery), the treasury pre-flight and alerts. It uses in-memory fakes and never reaches Wasabi, MongoDB or M-Pesa.

Live verification is manual and staged: read-only calls first, then one minimal mutation at a time, reconciled against Wasabi's portal and the local records.

## 14. References

- [README](README.md), [API](docs/API.md), [Deployment](docs/DEPLOY.md), [Operations](docs/OPERATIONS.md)
- [Reseller integration guide](docs/PARTNER_INTEGRATION_GUIDE.md) and [Partner API reference](docs/PARTNER_API.md)
- [WasabiCard Quick start](https://wsb.gitbook.io/wasabicard-doc/guides/quickstart.md)
- [WasabiCard Card API](https://wsb.gitbook.io/wasabicard-doc/api/card/cards.md)
- [WasabiCard Cardholder API](https://wsb.gitbook.io/wasabicard-doc/api/card/card-holder.md)
- [WasabiCard Account API](https://wsb.gitbook.io/wasabicard-doc/api/account.md)
- [WasabiCard Webhooks](https://wsb.gitbook.io/wasabicard-doc/api/webhooks.md)
