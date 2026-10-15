"""Application configuration.

All settings come from environment variables or the `.env` file. Nothing
secret is hard-coded. Pydantic validates every value at startup, so a typo
(e.g. a port of "54x2") stops the app immediately with a clear message
instead of failing later in a confusing way.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from payops.netpolicy import is_loopback_host, require_local_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds values meant for Docker; ignore those here
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Database ---
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "payops"
    postgres_user: str = "payops_admin"
    postgres_password: SecretStr  # required: no default on purpose

    # Read-only login used for all analytics and by the agents (created by the loader).
    readonly_db_user: str = Field(default="payops_readonly", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    readonly_db_password: SecretStr  # required

    # --- Local LLM ---
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    llm_timeout_seconds: float = Field(default=60, gt=0, le=600)

    health_check_timeout_seconds: float = Field(default=3, gt=0, le=30)

    # --- Dashboard ---
    api_base_url: str = "http://127.0.0.1:8000"   # where the dashboard finds the API
    api_timeout_seconds: float = Field(default=120, gt=0, le=600)

    @field_validator("postgres_host")
    @classmethod
    def _db_must_be_local(cls, value: str) -> str:
        if not is_loopback_host(value):
            raise ValueError(f"POSTGRES_HOST '{value}' is not local; use 127.0.0.1 or localhost")
        return value

    @field_validator("ollama_base_url", "api_base_url")
    @classmethod
    def _urls_must_be_local(cls, value: str) -> str:
        return require_local_url(value).rstrip("/")

    @field_validator("ollama_model")
    @classmethod
    def _no_cloud_models(cls, value: str) -> str:
        # Ollama names its hosted models with a "-cloud" suffix (e.g. "gpt-oss:120b-cloud").
        if value.endswith("-cloud") or ":cloud" in value:
            raise ValueError(f"OLLAMA_MODEL '{value}' is an Ollama cloud model; use a locally pulled model")
        return value

    def database_url(self, readonly: bool = False) -> URL:
        """Build the SQLAlchemy URL. Using URL.create() means special characters
        in the password are escaped correctly, and printing the URL hides the password.

        readonly=False -> the owner account (schema changes, loading data)
        readonly=True  -> the read-only account (analytics, agents)
        """
        user, password = (
            (self.readonly_db_user, self.readonly_db_password)
            if readonly
            else (self.postgres_user, self.postgres_password)
        )
        return URL.create(
            drivername="postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once and reuse them (FastAPI calls this on every request)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
