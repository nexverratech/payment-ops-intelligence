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

    # Application login: reads and writes schema "ops" (investigation history, accounts,
    # sessions, audit log) and nothing else; cannot see payment data. Created by
    # `python -m payops.db.setup_app`. Required for sign-in: without it the API refuses
    # every protected request (fail closed); the CLI still runs without saving.
    app_db_user: str = Field(default="payops_app", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    app_db_password: SecretStr | None = None

    # --- Local LLM ---
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    llm_timeout_seconds: float = Field(default=180, gt=0, le=600)
    llm_max_calls: int = Field(default=4, ge=1, le=20)           # per investigation
    llm_context_tokens: int = Field(default=8192, ge=2048, le=131072)

    health_check_timeout_seconds: float = Field(default=3, gt=0, le=30)

    # --- Sign-in (Phase 7) ---
    session_idle_minutes: int = Field(default=30, ge=5, le=240)     # signed out after this long without a request
    session_max_hours: int = Field(default=8, ge=1, le=24)          # and at the latest this long after signing in
    login_max_failures: int = Field(default=5, ge=3, le=20)         # wrong passwords in a row before a lockout
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)
    password_min_length: int = Field(default=12, ge=10, le=64)
    max_request_bytes: int = Field(default=65_536, ge=4_096, le=1_048_576)   # larger request bodies are refused

    # --- Execution limits ---
    # Wall-clock budget for one investigation. When it runs out, the remaining expensive
    # steps are skipped and the user gets a labelled partial report instead of a hang.
    investigation_timeout_seconds: float = Field(default=600, ge=30, le=3600)
    # Investigations the API runs at the same time (they share one local GPU).
    max_concurrent_investigations: int = Field(default=2, ge=1, le=16)

    # --- Dashboard ---
    api_base_url: str = "http://127.0.0.1:8000"   # where the dashboard finds the API
    api_timeout_seconds: float = Field(default=300, gt=0, le=900)

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

    @property
    def stale_after_seconds(self) -> float:
        """A running investigation with no heartbeat for this long has died (crash, power cut).
        The longest single step is the report: up to 2 LLM attempts after the time budget check."""
        return self.investigation_timeout_seconds + 2 * self.llm_timeout_seconds + 60

    @property
    def history_enabled(self) -> bool:
        return self.app_db_password is not None and bool(self.app_db_password.get_secret_value())

    def database_url(self, readonly: bool = False, history: bool = False) -> URL:
        """Build the SQLAlchemy URL. Using URL.create() means special characters
        in the password are escaped correctly, and printing the URL hides the password.

        default        -> the owner account (schema changes, loading data)
        readonly=True  -> the read-only account (analytics, agents)
        history=True   -> the history account (investigation records and checkpoints only)
        """
        if history:
            if not self.history_enabled:
                raise ValueError("APP_DB_PASSWORD is not set, so investigation history is disabled")
            user, password = self.app_db_user, self.app_db_password
        elif readonly:
            user, password = self.readonly_db_user, self.readonly_db_password
        else:
            user, password = self.postgres_user, self.postgres_password
        return URL.create(
            drivername="postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),  # type: ignore[union-attr]
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once and reuse them (FastAPI calls this on every request)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
