from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: str = "dev"
    frontend_id: str = "frontend1"
    backend_id: str = "api1"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_ssl: bool = False
    redis_password: str | None = None

    key_vault_url: str | None = None
    oauth_client_id_secret_name: str = "oauth-client-id"
    oauth_client_secret_secret_name: str = "oauth-client-secret"

    oauth_token_url: str = ""
    oauth_scope: str = ""

    token_cache_enabled: bool = True
    token_ttl_safety_buffer_seconds: int = 60

    class Config:
        env_file = ".env"


settings = Settings()
