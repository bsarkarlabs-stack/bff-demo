import logging

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.logging_config import configure_logging
from app.oauth import get_access_token
from app.redis_client import get_redis

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="BFF Auth Service")


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Application startup, environment=%s", settings.environment)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready() -> dict:
    try:
        get_redis().ping()
    except Exception:
        logger.exception("Redis connectivity error")
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return {"status": "ready"}


@app.get("/token")
async def token() -> dict:
    logger.info("Authentication request received, environment=%s", settings.environment)
    try:
        access_token = await get_access_token()
    except Exception:
        logger.exception("Token generation failure")
        raise HTTPException(status_code=502, detail="Failed to obtain token")
    return {"access_token": access_token}
