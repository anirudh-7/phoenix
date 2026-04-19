"""Runtime configuration.

Source of truth for anything read from the environment. Import `settings`
from here; never read os.environ directly elsewhere.

Loading order (pydantic-settings default):
  1. Environment variables
  2. .env file in the repo root
  3. Defaults defined on the Settings class

The .env file is loaded from the repo root (two directories up from this file).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://phoenix:phoenix@localhost:5432/phoenix",
        description="Async SQLAlchemy URL for the app.",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg://phoenix:phoenix@localhost:5432/phoenix",
        description="Sync SQLAlchemy URL for Alembic migrations.",
    )

    # Secrets
    phoenix_app_key: str = Field(
        default="dev-key-do-not-use-in-real-life",
        description="Application key for column-level encryption (OAuth tokens).",
    )

    # Model defaults
    phoenix_default_model: str = Field(default="claude-sonnet-4-6")

    # Observability
    log_level: str = Field(default="INFO")

    # Policy
    policy_path: Path = Field(default=REPO_ROOT / "infra" / "policy.yaml")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use this in FastAPI dependencies."""
    return Settings()


settings = get_settings()
