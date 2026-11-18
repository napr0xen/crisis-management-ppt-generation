"""Settings read from `.env` / environment. Variable names: docs/development-guidelines.md § 8."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Only variables Phase 1 reads; later phases add theirs (development-guidelines § 8).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://doc_intel:doc_intel@127.0.0.1:5432/doc_intel"
    qdrant_url: str = "http://127.0.0.1:6333"
    storage_root: Path = Path("./data/storage")
    pipe_shared_secret: SecretStr
    signing_secret: SecretStr
    signed_url_ttl_s: int = 900
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
