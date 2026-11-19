"""Settings read from `.env` / environment. Variable names: docs/development-guidelines.md § 8."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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
    max_upload_mb: int = 100
    chunk_max_tokens: int = 512
    embedding_base_url: str = "http://localhost:11434"  # Ollama
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: int = 1024  # sent to Ollama (Matryoshka); must match the collection
    chunk_tokenizer: str = "Qwen/Qwen3-Embedding-0.6B"  # HF tokenizer that sizes chunks
    reranker_device: str = "mps"  # mps | cpu, for the optional local reranker
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    qdrant_collection: str = "doc_chunks"  # tests use their own collection
    # 0 = no in-process worker (tests drive jobs directly). shortcut: max 1 worker, the 24 GB
    # RAM budget fits one Docling/LLM job at a time (ADR-010); revisit with more memory.
    job_concurrency: int = Field(default=1, ge=0, le=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
