# =========================================================
# main.py - Application entry point
# =========================================================

from fastapi import FastAPI
from dotenv import load_dotenv

from app.cards.routeendpoint import router as cards_router
from app.webhooks.router import router as webhook_router

from app.core.security import create_access_token

load_dotenv()


# =========================================================
# App initialization
# =========================================================
app = FastAPI(
    title="Wasabi Cards API Integration",
    version="1.0.0",
    description="Virtual Card Infrastructure for Mamlaka"
)


# =========================================================
# Routers
# =========================================================
app.include_router(cards_router,prefix="/api/v1/cards",tags=["Cards"])

app.include_router(webhook_router,prefix="/api/v1/webhooks",tags=["Webhooks"])


# =========================================================
# Health Check
# =========================================================
@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "Mamlaka Wasabi API"
    }


# =========================================================
# JWT TEST TOKEN GENERATOR
# =========================================================
@app.post("/token", tags=["Authentication"])
async def generate_token():

    payload = {
        "sub": "testuser",
        "role": "admin"
    }

    token = create_access_token(payload)

    return {
        "access_token": token,
        "token_type": "bearer"
    }



    #=========================================================
    # DEBUG 
    #=========================================================
    