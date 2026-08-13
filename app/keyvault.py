import logging
from functools import lru_cache

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache
def _client() -> SecretClient:
    if not settings.key_vault_url:
        raise RuntimeError("KEY_VAULT_URL is not configured")
    credential = DefaultAzureCredential()
    return SecretClient(vault_url=settings.key_vault_url, credential=credential)


def get_secret(name: str) -> str:
    logger.info("Retrieving secret from Key Vault: %s", name)
    return _client().get_secret(name).value
