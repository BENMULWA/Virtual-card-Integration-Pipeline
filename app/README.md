# 💳 Mamlaka Virtual Cards Integration Service

An advanced, containerised FastAPI proxy infrastructure orchestrating corporate-level card issuance, wallet ledgers, and compliance screening workflows with the **WasabiCard Gateway API Stack (V2)**.

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

---

## 🚦 Endpoint Routing Map

### 📦 1. Core Common Integration Utilities
* `POST /api/v1/cards/supported-BINS/types` - Retrieves supported card profiles and operational constraints.
* `GET /api/v1/cards/diagnostic/kenya-cities` - Queries approved geographic destination tokens (Nairobi Code: `KE-047-001`).

### 📁 2. Compliance Document Management
* `POST /api/v1/cards/kyc/upload` - Processes `multipart/form-data` binary streams (ID front, back, selfie) to secure unique compliance file references.

### 👤 3. Cardholder Registry
* `POST /api/v1/cards/cardholder/register` - Registers full KYC compliance details (Phase 1) to generate a numeric `holderId`.

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