"""Settings read from `.env` / environment. Variable names: docs/development-guidelines.md § 8."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Only variables Phase 1 reads; later phases add theirs (development-guidelines § 8).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://doc_intel:doc_intel@127.0.0.1:5432/doc_intel"
    qdrant_url: str = "http://127.0.0.1:6333"
    # Set it with the same value in compose: Open WebUI shares the Docker network with Qdrant.
    qdrant_api_key: SecretStr | None = None
    storage_root: Path = Path("./data/storage")
    # the library's source of truth (ADR-017); a relative path resolves against the process cwd
    templates_dir: Path = Path("templates")
    # >= 32 chars: an empty SIGNING_SECRET would make every signed download link forgeable.
    pipe_shared_secret: SecretStr = Field(min_length=32)
    signing_secret: SecretStr = Field(min_length=32)
    signed_url_ttl_s: int = 900
    public_base_url: str = "http://127.0.0.1:8000"  # prefix for artifact download_url
    missing_marker: str = "[MISSING: {label}]"  # written for missing fields (FR-12)
    conflict_marker: str = "[CONFLICT: See Chat Summary]"
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
    # anthropic is planned (FR-17), not built: an unknown provider fails at startup.
    llm_provider: Literal["ollama", "openai_compat"] = "ollama"
    # ollama: native API root; openai_compat: the /v1 root, e.g. http://127.0.0.1:8080/v1
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:14b"
    llm_api_key: SecretStr = SecretStr("")
    # Prompts are sized from it (llm.gateway.prompt_tokens), any value >= 8192. Ollama also gets
    # it as num_ctx; for openai_compat it must match the server's context (it is not sent).
    llm_num_ctx: int = 32768
    llm_timeout_seconds: float = 120.0  # local 14B inference plus a cold model load
    # ollama `think` / openai_compat chat_template_kwargs.enable_thinking; off: at temperature 0
    # qwen3 reasoning can loop until the timeout
    llm_enable_thinking: bool = False
    # openai_compat only: send chat_template_kwargs (mlx_lm.server, LM Studio, vLLM need it to
    # switch Qwen3 reasoning off); strict OpenAI-compatible servers reject the field
    llm_chat_template_kwargs: bool = False
    # openai_compat only: reasoning-heavy calls think up to the budget, else a non-thinking pass
    llm_hybrid_thinking: bool = False
    llm_thinking_budget_tokens: int = Field(default=160, ge=1)  # reasoning tokens before the cut
    # mapping: a conflict, a verification failure or a high-score miss gets one more extraction
    # with thinking on, up to THINK_ESCALATION_MAX_TOKENS reasoning tokens (on top of the reply)
    think_on_escalation: bool = True
    think_escalation_max_tokens: int = 2048  # clamped to 512..4096
    # a `missing` field escalates only if a retrieved chunk scored at least this (Hit.score)
    think_escalation_score_threshold: float = 0.65
    # tokens of retrieved chunks (headers included) handed to the LLM per call
    retrieval_context_tokens: int = Field(default=4500, ge=512)
    qdrant_collection: str = "doc_chunks"  # tests use their own collection
    # 0 = no in-process worker (tests drive jobs directly). shortcut: max 1 worker, the 24 GB
    # RAM budget fits one Docling/LLM job at a time (ADR-010); revisit with more memory.
    job_concurrency: int = Field(default=1, ge=0, le=1)

    @field_validator("think_escalation_max_tokens")
    @classmethod
    def _clamp_think_tokens(cls, v: int) -> int:
        return min(max(v, 512), 4096)


@lru_cache
def get_settings() -> Settings:
    return Settings()
