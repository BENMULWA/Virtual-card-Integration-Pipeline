# =========================================================
# main.py - Application entry point
# =========================================================

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.cards import wasabi_client
from app.cards.routeendpoint import router as cards_router
from app.cards.wasabi_client import WasabiError, WasabiUnknownOutcome
from app.core.config import CORS_ORIGINS, ENABLE_TEST_TOKEN
from app.core.database import ensure_indexes
from app.core.security import create_access_token, validate_jwt_config
from app.journey.router import router as journey_router
from app.mobilemoney import client as gateway_client
from app.mpesa import client as mpesa_client
from app.mpesa.router import router as mpesa_router
from app.partners.router import router as partner_router
from app.users.router import router as users_router
from app.wallet.router import router as wallet_router
from app.webhooks.router import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_jwt_config()
    await ensure_indexes()
    yield
    await wasabi_client.close_client()
    await mpesa_client.close_client()
    await gateway_client.close_client()


app = FastAPI(
    title="Wasabi Cards API Integration",
    version="1.2.0",
    description="Virtual Card Infrastructure for Mamlaka",
    lifespan=lifespan,
)

# The user UI runs on its own origin and calls this API from the browser.
# Without this every request from the UI would be blocked by the same-origin policy.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],
    )
    logging.getLogger("main").info("CORS enabled for: %s", ", ".join(CORS_ORIGINS))

app.include_router(users_router, prefix="/api/v1/users", tags=["Users"])
app.include_router(journey_router, prefix="/api/v1/journey", tags=["Journey"])
app.include_router(cards_router, prefix="/api/v1/cards", tags=["Cards"])
app.include_router(webhook_router, prefix="/api/v1/webhooks", tags=["Webhooks"])
app.include_router(wallet_router, prefix="/api/v1/wallet", tags=["Wallet"])
app.include_router(mpesa_router, prefix="/api/v1/mpesa", tags=["M-Pesa"])
app.include_router(partner_router, prefix="/api/v1/partner", tags=["Partner API"])


# =========================================================
# Provider error mapping
# =========================================================
@app.exception_handler(WasabiError)
async def wasabi_error_handler(request: Request, exc: WasabiError):
    # Definitive failure reported by Wasabi.
    return JSONResponse(status_code=422, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(WasabiUnknownOutcome)
async def wasabi_unknown_handler(request: Request, exc: WasabiUnknownOutcome):
    # The operation may or may not have happened. Retry with the SAME Idempotency-Key to read its state.
    return JSONResponse(
        status_code=202,
        content={"status": "UNKNOWN",
                 "detail": "Outcome not confirmed. Retry with the same Idempotency-Key to check its state."},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Mamlaka Wasabi API"}


# =========================================================
# TEST TOKEN - only exists when ENABLE_TEST_TOKEN=true AND APP_ENV is not production
# =========================================================
if ENABLE_TEST_TOKEN:
    @app.post("/token", tags=["Authentication"])
    async def generate_token():
        return {"access_token": create_access_token({"sub": "testuser"}), "token_type": "bearer"}
