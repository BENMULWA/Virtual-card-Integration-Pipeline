# 💳 Mamlaka Virtual Cards Integration Service

An advanced, containerised FastAPI proxy infrastructure orchestrating corporate-level card issuance, wallet ledgers, and compliance screening workflows with the **WasabiCard Gateway API Stack (V2)**.

---

## 👤 The Customer Journey (end to end)

A customer applies for a virtual card in **seven steps**. The API exposes the whole
journey as one ordered list, and pushes live updates as the application progresses -
the frontend never has to guess what state the application is in.

| # | Step | Customer does | API |
|---|------|---------------|-----|
| 1 | `ACCOUNT` | Registers with email, phone and password | `POST /api/v1/users/register` |
| 2 | `VERIFY_EMAIL` | Clicks the link we email | `POST /api/v1/users/verify-email` |
| 3 | `KYC_DOCUMENTS` | Uploads ID front, ID back and a selfie | `POST /api/v1/cards/kyc/upload` (×3) |
| 4 | `KYC_SUBMISSION` | Submits their identity details to Wasabi | `POST /api/v1/cards/cardholder/register` |
| 5 | `KYC_REVIEW` | Waits - **Wasabi's compliance team approves**, not us | *(webhook / polling)* |
| 6 | `CARD_ISSUANCE` | Requests the card; we issue and load it | `POST /api/v1/cards/create` |
| 7 | `FUNDING` | Tops up with M-Pesa, then deposits onto the card | `POST /api/v1/wallet/topup` → `POST /api/v1/cards/deposit` |

### Reading the journey

```http
GET /api/v1/journey
Authorization: Bearer <token>
```

```json
{
  "currentStep": "KYC_REVIEW",
  "currentTitle": "Wait for approval",
  "nextAction": "POST /api/v1/cards/kyc/submit  (only if the review is stuck)",
  "progress": 57,
  "completedSteps": 4,
  "totalSteps": 7,
  "summary": "Your KYC is with Wasabi's compliance team. We will notify you when it is approved.",
  "steps": [
    { "key": "ACCOUNT",        "status": "completed",   "title": "Create your account", ... },
    { "key": "VERIFY_EMAIL",   "status": "completed",   "title": "Verify your email", ... },
    { "key": "KYC_DOCUMENTS",  "status": "completed",   "title": "Upload your ID documents", ... },
    { "key": "KYC_SUBMISSION", "status": "completed",   "title": "Submit your KYC details", ... },
    { "key": "KYC_REVIEW",     "status": "in_progress", "title": "Wait for approval", ... },
    { "key": "CARD_ISSUANCE",  "status": "pending",     "title": "Get your virtual card", ... },
    { "key": "FUNDING",        "status": "pending",     "title": "Fund your card", ... }
  ],
  "holder": { "holderId": 124024, "status": "wait_audit", "statusFlowLocation": "admin" },
  "card": null,
  "funded": false
}
```

Step statuses: `completed` · `in_progress` · `pending` · `blocked` (e.g. a rejected KYC).

### Live updates (no polling)

```http
GET /api/v1/journey/stream
Authorization: Bearer <token>
Accept: text/event-stream
```

The stream sends the current journey immediately, then re-sends it every time the
application changes - a Wasabi webhook arrives, the cardholder is approved, the
browser updates itself.

```
event: journey
data: {"reason": "kyc_status_changed", "journey": { ... }}
```

Browser client:

```js
const stream = new EventSource(`/api/v1/journey/stream`, {
  headers: { Authorization: `Bearer ${token}` }   // or use a cookie
});
stream.addEventListener("journey", (e) => {
  const { journey } = JSON.parse(e.data);
  renderJourney(journey);
});
```

> **Single-worker note:** the event bus is in-process. A multi-worker uvicorn
> (`--workers N`) would give each worker its own bus, so a webhook handled by one
> worker would not reach a client on another. Put Redis pub/sub behind the same
> interface in `app/journey/events.py` before scaling out.

---

## 🔐 Accounts & Security

| Feature | Detail |
|---|---|
| Password hashing | bcrypt, cost 12 (`app/core/passwords.py`) |
| Password policy | 10+ chars, mixed case, a number, a symbol |
| Account lockout | 5 failed logins → 15 minute lock |
| Rate limiting | login 8/5min, register 5/hour, verify 10/10min |
| Enumeration safety | Unknown email and wrong password are indistinguishable; `resend-verification` always returns 200 |
| Email verification | Single-use token, SHA-256 at rest, 24 h expiry, consumed on use |
| Tokens | JWT HS256, configurable TTL (`JWT_EXPIRE_MINUTES`), `jti` for future revocation |
| Scoping | Every card, wallet and holder query is filtered by the JWT subject |

### Auth endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/users/register` | Create an account, returns a token |
| `POST` | `/api/v1/users/login` | Sign in |
| `POST` | `/api/v1/users/verify-email` | Confirm an email address |
| `POST` | `/api/v1/users/resend-verification` | New verification link |
| `GET` | `/api/v1/users/me` | Current profile |
| `POST` | `/api/v1/users/change-password` | Change password |
| `GET` | `/api/v1/users/dev/verification-link` | **Dev only** - 404 in production |

---

## 🏗️ System Architecture & Core Design

This system acts as a high-security proxy layer between our consumer clients and the WasabiCard core payment network. It implements a strict **B2C (Business-to-Consumer) card management model**.


######  ARCHTECTURE DESIGN

### 🧠 The Core Engine Workflows 

1. **Outbound Signed Transmission**: Outgoing requests undergo strict schema normalization. Fields are filtered by `clean_payload` and sorted alphabetically into a canonical string. The system signs this payload using our **RSA SHA-256 Private Key (`private.pem`)**, outputs a **Base64** token, and appends it to the `X-WSB-SIGNATURE` request headers.
2. **Dual-Phase B2C Issuance Chain**: Because our MasterCard BIN profile (`539578`) enforces KYC controls (`needCardHolder = true`), card issuance requires two steps:
   * **Phase 1**: Register user compliance vectors to acquire a platform `holderId`.
   * **Phase 2**: Pass the numeric `holderId` into the card creation router to build and fund the virtual card.
3. **Manual Audit Fallback**: If automated checks flag an identity account profile as `wait_audit`, a manual **Work Order Activation Ticket** maps the uploaded file identifiers to clear the compliance queue.
4. **Asymmetric Webhook Mirroring**: The system exposes a public **ngrok** tunnel wrapper mapping inbound server updates directly to a local MongoDB data instance to track transaction status changes.
5. **Live Journey Projection**: A single state machine (`app/journey/state.py`) folds the user record, uploaded KYC files, Wasabi cardholder, Wasabi card and M-Pesa top-ups into one ordered journey, published over SSE whenever a webhook moves the application forward.

---

## 🚦 Endpoint Routing Map

### 👤 0. Accounts & Journey (the customer-facing layer)
* `POST /api/v1/users/register` - Creates an account and returns an access token.
* `POST /api/v1/users/login` - Signs in with email and password.
* `POST /api/v1/users/verify-email` - Confirms an email address with a single-use token.
* `POST /api/v1/users/resend-verification` - Issues a new verification link (always 200, never leaks account existence).
* `GET  /api/v1/users/me` - The signed-in user's profile.
* `POST /api/v1/users/change-password` - Changes the password (current password required).
* `GET  /api/v1/journey` - The full ordered application journey with per-step status, next action and progress.
* `GET  /api/v1/journey/stream` - Server-Sent Events: live journey updates as Wasabi moves the application forward.
* `GET  /api/v1/journey/events/history` - The webhook events that affected this customer's application.

### 📦 1. Core Common Integration Utilities
* `POST /api/v1/cards/supported-BINS/types` - Retrieves supported card profiles and operational constraints.
* `GET /api/v1/cards/diagnostic/kenya-cities` - Queries approved geographic destination tokens (Nairobi Code: `KE-047-001`).

### 📁 2. Compliance Document Management
* `POST /api/v1/cards/kyc/upload` - Processes `multipart/form-data` binary streams (ID front, back, selfie) to secure unique compliance file references. Pass `kind=front|back|selfie` so the journey tracker knows which slot the file fills.

### 👤 3. Cardholder Registry
* `POST /api/v1/cards/cardholder/register` - Registers full KYC compliance details (Phase 1) to generate a numeric `holderId`.
* `PUT  /api/v1/cards/cardholder/{holder_id}` - Corrects and resubmits a **rejected** cardholder (Wasabi only accepts updates when `status=reject`).
* `GET  /api/v1/cards/cardholder/{holder_id}` - Holder status plus a `review` block naming who is approving it.
* `GET  /api/v1/cards/cardholders` - The caller's own cardholders.

### 🛠️ 4. Manual Ticketing Workspace
* `POST /api/v1/cards/work-order/submit` - Submits manual activation tickets to unblock pending audit queues (`tradeStatus: processing`).

### 💳 5. Virtual Card Lifecycle
* `POST /api/v1/cards/create` - Assembles the `holderId`, BIN properties, and funding criteria to manufacture the card (Phase 2).
* `GET /api/v1/cards/list-BINS` - Paginated transaction logs checking local wallet parameters.
* `POST /api/v1/cards/info` - Pulls status metadata arrays.
* `POST /api/v1/cards/sensitive` - Decrypts true 16-digit card components.
* `POST /api/v1/cards/balance` - Live accounting checks.

### 💸 6. Ledgers & Funding
* `POST /api/v1/cards/deposit` - Moves capital from our treasury wallet into the target virtual card.
* `POST /api/v1/cards/withdraw` - Recovers funds from a card back into our core vault.

### 🔒 7. Security & Risk Management
* `POST /api/v1/cards/freeze` / `/unfreeze` - Instantly locks or unlocks transaction card pipelines.
* `POST /api/v1/cards/cancel` - Permanently destroys active asset lines.

### 📬 8. Callback Events
* `POST /api/v1/cards/wasabi` - Public webhook endpoint capturing lifecycle mutations asynchronously.

---

## 🔐 Security Protocols & Environment Masking

The project implements a **Git Shield Strategy** via `.gitignore` to ensure production secrets and private cryptographic keys are completely masked before code is pushed to GitHub:

* `app/.env` - Protects database URLs, JWT encryption seeds, and gateway API profiles.
* `app/keys/private.pem` - Private RSA key used to sign requests. Must never leave the container environment.
* `app/keys/public.pem` / `wsbpublic.pem` - Identity verification keys.

---

## 🐳 Docker Deployment & Containerization

The application is fully containerised to isolate dependencies and simplify cloud infrastructure deployments.

### 1. File Context Management
* **`Dockerfile`**: Sets up a Python runtime, compiles cryptographic binaries, sets the `PYTHONPATH=/code` module search index, and exposes port `8000`.
* **`.dockerignore`**: Excludes local virtual environments (`.venv/`) and cache artifacts to keep the image compact.

### 2. Standard Container Commands

To compile the image context from scratch:
```bash
docker build --no-cache -t mamlaka-cards-service .
```

To run the container instance securely (injecting local configuration secrets and mounting key folders via external volume bindings without baking them into the shared image):
```bash
docker run -d \
  --name virtual-cards-app \
  -p 8000:8000 \
  --env-file app/.env \
  -v \$(pwd)/app/keys:/code/app/keys \
  mamlaka-cards-service
```

To trace active background processing logs:
```bash
docker logs virtual-cards-app
```