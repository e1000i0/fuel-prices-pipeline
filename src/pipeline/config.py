"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str
    api_base_url: str
    api_timeout: int
    api_max_retries: int
    raw_dir: Path
    log_level: str

    @property
    def pg_conninfo(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user} password={self.pg_password}"
        )


def load_settings() -> Settings:
    load_dotenv()  # does not override variables already set in the environment
    env = os.environ.get
    return Settings(
        pg_host=env("POSTGRES_HOST", "127.0.0.1"),
        pg_port=int(env("POSTGRES_PORT", "5432")),
        pg_db=env("POSTGRES_DB", "fuel"),
        pg_user=env("POSTGRES_USER", "fuel"),
        pg_password=env("POSTGRES_PASSWORD", ""),
        api_base_url=env(
            "API_BASE_URL",
            "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes",
        ).rstrip("/"),
        api_timeout=int(env("API_TIMEOUT_SECONDS", "60")),
        api_max_retries=int(env("API_MAX_RETRIES", "3")),
        raw_dir=Path(env("RAW_DIR", "data/raw")),
        log_level=env("LOG_LEVEL", "INFO").upper(),
    )
