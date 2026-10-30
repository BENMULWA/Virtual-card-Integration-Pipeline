# Mamlaka Virtual Cards

A FastAPI service that issues and manages **virtual Mastercards** through the WasabiCard API, for two kinds of callers:

- **Customers** of Mamlaka's own app: sign up, complete KYC, top up a KES wallet (M-Pesa), get a card.
- **Resellers** (businesses such as Comet and Jasiri): the **Partner API** lets them offer cards to *their* customers, paid from a prepaid float.

Cards are issued by Wasabi's issuing bank. This service handles KYC submission, wallets and ledgers, pricing, settlement, webhooks and reconciliation.

| | |
|---|---|
| Card product | Mastercard, virtual, USD (card type `111078`, BIN `524013`) |
| Stack | Python 3.10, FastAPI, MongoDB (Atlas), Docker |
| Companion repo | the user web app: `virtual-card-User-interface-` |

## Documentation

| Read this | If you want to |
|---|---|
| [docs/API.md](docs/API.md) | Call the customer-facing API (users, journey, cards, wallet, webhooks) |
| [docs/PARTNER_INTEGRATION_GUIDE.md](docs/PARTNER_INTEGRATION_GUIDE.md) | Integrate as a **reseller**, step by step |
| [docs/PARTNER_API.md](docs/PARTNER_API.md) | Look up a reseller endpoint or webhook |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Host it: Docker, nginx, environment, Wasabi webhook setup |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Run it: reconcile job, treasury checks, pricing and margins, reseller admin, incidents |
| `/docs` on a running server | Interactive Swagger UI (generated from the code) |
| [WHITEPAPER.md](WHITEPAPER.md) | Background and design rationale |

## How money moves

Three different balances exist. Mixing them up is the most common mistake.

```
Customer pays KES (M-Pesa / Airtel)
        |
        v
 Customer wallet  (KES ledger in this service)        Reseller's customer pays the RESELLER instead;
        |  card charge                                  the reseller prepays us:
        v                                                      |
 Card operation  <------------------------------------  Reseller FLOAT (KES ledger, credited by an admin)
        |
        v
 Wasabi merchant balance  (real USD at Wasabi)  <- what Wasabi actually deducts for every card
```

- A **float** is a ledger number, not cash. It is only a promise unless real USD backs it at Wasabi.
- `app/core/treasury.py` compares the real Wasabi USD with what resellers have prepaid, refuses issuance it cannot cover, and alerts when the balance runs low (see [docs/OPERATIONS.md](docs/OPERATIONS.md)).
- **Pricing.** Our cost of a usable dollar is the USDT price divided by what reaches Wasabi after its 1.5% deposit fee
  (`129.50 / 0.985 = 131.47 KES`). Our margin goes on top: `FX_SPREAD_PCT` for our own customers, a per-reseller
  margin (default `PARTNER_MARGIN_PCT`) for resellers, who add their own on top again. Wasabi's card fees are treated
  as cost. `python tests/resellers_keys.py pricing` prints the rate card; resellers read theirs at
  `GET /api/v1/partner/rates`. Details: [docs/OPERATIONS.md](docs/OPERATIONS.md#pricing-and-margins).
- Every card operation is idempotent: the wallet or float is **held first**, the provider is called once, then the hold is captured (success) or released (failure). An unknown outcome never refunds; it is settled later by webhook or the reconcile job.

## Architecture

```
            Customers (web app)                    Resellers (their servers)
                    |                                          |
        /api/v1/users, /journey, /cards, /wallet        /api/v1/partner/*   (X-API-Key)
                    \                                         /
                     +---------------  FastAPI  ------------+
                     |   users . journey . cards . wallet . partners . mobilemoney . mpesa
                     |   orchestrator (idempotent operations)  .  ledger  .  treasury
                     +--------------------------------------------------------------+
                        |                |                 |                   |
                    MongoDB          Wasabi API       Mamlaka payments      Daraja (M-Pesa)
                  (ledger, ops)    (signed requests)  gateway (top-ups,    (optional, direct)
                                        |              payouts)
                              Wasabi webhooks  -> POST /api/v1/webhooks/wasabi
                              Gateway callbacks -> POST /api/v1/webhooks/mamlaka
                              Events to resellers <- signed, retried (see docs/PARTNER_API.md)
```

Key design points:

1. **Signed outbound requests.** Payloads are normalised, sorted into a canonical string and signed with our RSA private key (SHA-256, Base64) in `X-WSB-SIGNATURE`.
2. **Two-phase issuance.** The card type requires KYC (`needCardHolder`): register a cardholder, wait for Wasabi's approval, then create the card with the `holderId`. Approval is Wasabi's decision, not ours.
3. **Webhooks are hints, not truth.** Callbacks only say *what changed*. The handlers re-read the authoritative state from Wasabi, so a forged callback can at worst trigger a read.
4. **Reconciliation.** A worker (`python -m app.jobs.reconcile`, every 3 minutes) recovers missed webhooks, settles operations, retries reseller events and checks that wallet counters equal the ledger.
5. **Live journey.** `app/journey/state.py` folds the user, KYC files, cardholder, card and top-ups into one ordered journey, pushed over Server-Sent Events.

## Repository layout

```
app/
  cards/            Wasabi client, card/cardholder service, customer card routes
  partners/         Reseller (tenant) layer: API keys, customers, events, admin CLI
  mobilemoney/      Mamlaka payments gateway client: STK top-ups and payouts
  mpesa/            Direct Safaricom Daraja STK push
  wallet/           KES wallet ledger, pricing, wallet routes
  transactions/     Idempotent provider-operation lifecycle
  users/  journey/  Accounts, email verification, the application journey (SSE)
  webhooks/         Wasabi and gateway callbacks
  jobs/             Reconcile worker
  core/             Config, database, security, rate limiting, treasury, mail
tests/              pytest suite (no network, no real database)
deploy/             nginx configuration
docs/               Developer documentation
postman/            Postman collections
Dockerfile  docker-compose.yml  requirements*.txt  pytest.ini  .env.example
```

## Quick start (local)

```bash
git clone https://github.com/BENMULWA/Virtual-card-Integration-Pipeline.git
cd Virtual-card-Integration-Pipeline

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env            # then fill it in; see docs/DEPLOY.md for every setting
mkdir -p app/keys               # private.pem, public.pem (yours) and wsbpublic.pem (Wasabi's)

uvicorn app.main:app --port 8001 --reload     # http://localhost:8001/docs
```

Run the tests (they use in-memory fakes and never reach Wasabi, Mongo or M-Pesa):

```bash
python -m pytest -q
```

> **One worker only.** The rate limiter and the live-update bus are in-process. Do not run `--workers N` until they are moved to Redis.

## The customer journey

| # | Step | The customer | API |
|---|------|--------------|-----|
| 1 | `ACCOUNT` | Registers | `POST /api/v1/users/register` |
| 2 | `VERIFY_EMAIL` | Clicks the emailed link | `POST /api/v1/users/verify-email` |
| 3 | `KYC_DOCUMENTS` | Uploads ID front, back and a selfie | `POST /api/v1/cards/kyc/upload` |
| 4 | `KYC_SUBMISSION` | Submits identity details | `POST /api/v1/cards/cardholder/register` |
| 5 | `KYC_REVIEW` | Waits for Wasabi's decision | webhook / `GET /api/v1/journey` |
| 6 | `CARD_ISSUANCE` | Requests the card | `POST /api/v1/cards/create` |
| 7 | `FUNDING` | Tops up the wallet, loads the card | `POST /api/v1/wallet/topup` then `POST /api/v1/cards/deposit` |

`GET /api/v1/journey` returns all seven steps with their status; `GET /api/v1/journey/stream` pushes updates live. Details and the browser client are in [docs/API.md](docs/API.md).

## Security at a glance

- Passwords: bcrypt (cost 12), strong-password policy, account lockout after 5 failures, per-endpoint rate limits.
- Accounts cannot be enumerated: unknown email and wrong password look identical.
- JWT (HS256) on every customer route; every card, wallet and holder query is scoped to the token's user.
- Reseller API keys are stored only as SHA-256 hashes and shown once.
- Gateway and reseller webhooks are authenticated with HMAC-SHA256 over the raw body.
- Card numbers and CVVs are never stored or logged. The reseller reveal endpoint records who revealed which card (never the values) and tells resellers to re-authenticate the user first. The customer route `POST /api/v1/cards/sensitive` currently relies on the JWT alone; add a step-up before launch (see "Known limitations" in [docs/API.md](docs/API.md)).
- Secrets (`.env`, `app/keys/*.pem`) are git-ignored and mounted at runtime, never baked into the image.

## Running in production

Live at `https://virtual.cards.mamlakapsp.com` (Docker on AWS, nginx and certbot in front). Wasabi's webhook URL is
`https://virtual.cards.mamlakapsp.com/api/v1/webhooks/wasabi`. Wasabi only accepts API calls from allowlisted IPs,
so calls from a laptop on another network get `403 Not authorized`; test through the server.

```bash
cd ~/virtual-cards
git pull
docker compose up -d --build          # api + reconcile worker
docker compose ps                     # both Up; api (healthy)
docker compose logs -f api reconcile
```

The host port comes from `API_PORT` in `.env` (8010 on the current server, where 8000 is taken). Full setup:
[docs/DEPLOY.md](docs/DEPLOY.md).

## Related

- User web app: https://github.com/BENMULWA/virtual-card-User-interface-
- Reseller documentation: [docs/PARTNER_INTEGRATION_GUIDE.md](docs/PARTNER_INTEGRATION_GUIDE.md)
