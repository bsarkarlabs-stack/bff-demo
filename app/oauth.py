import logging

import httpx

from app.config import settings
from app.keyvault import get_secret
from app.token_cache import get_cached_token, store_token

logger = logging.getLogger(__name__)


async def get_access_token() -> str:
    cached = get_cached_token()
    if cached:
        return cached

    logger.info("Token cache miss, requesting new token from OAuth provider")

    client_id = get_secret(settings.oauth_client_id_secret_name)
    client_secret = get_secret(settings.oauth_client_secret_secret_name)

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            settings.oauth_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": settings.oauth_scope,
            },
        )
        response.raise_for_status()
        payload = response.json()

    access_token = payload["access_token"]
    expires_in = payload.get("expires_in", 3600)

    logger.info("Token generation success, expires_in=%s", expires_in)
    store_token(access_token, expires_in)

    return access_token
