"""Environment -> typed settings. The single place env vars are read."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # --- Ollama -----------------------------------------------------------
    # Service name, not localhost: inside the app container localhost is the app.
    ollama_url: str = field(default_factory=lambda: _str("OLLAMA_URL", "http://ollama:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _str("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    ollama_timeout: int = field(default_factory=lambda: _int("OLLAMA_TIMEOUT", 180))

    # --- Database ---------------------------------------------------------
    dialect_name: str = field(default_factory=lambda: _str("DB_DIALECT", "postgres"))
    admin_dsn: str = field(default_factory=lambda: _str("ADMIN_DATABASE_URL"))
    readonly_dsn: str = field(default_factory=lambda: _str("READONLY_DATABASE_URL"))

    # --- Guardrails -------------------------------------------------------
    max_rows: int = field(default_factory=lambda: _int("MAX_ROWS", 200))
    statement_timeout_ms: int = field(default_factory=lambda: _int("STATEMENT_TIMEOUT_MS", 8000))

    # --- Seed -------------------------------------------------------------
    seed_random_seed: int = field(default_factory=lambda: _int("SEED_RANDOM_SEED", 20260101))
    seed_days: int = field(default_factory=lambda: _int("SEED_DAYS", 60))
    seed_end_date: str = field(default_factory=lambda: _str("SEED_END_DATE"))

    # --- Paths ------------------------------------------------------------
    schema_path: Path = field(default_factory=lambda: APP_DIR / "schema.yaml")
    fewshot_dir: Path = field(default_factory=lambda: APP_DIR / "fewshot")
    static_dir: Path = field(default_factory=lambda: APP_DIR / "static")

    def seed_anchor_date(self) -> date:
        """End of the generated history window.

        Blank SEED_END_DATE anchors to today, so 'yesterday' and 'last week'
        always have data in a demo. Set it to pin the calendar and get
        byte-identical rows on every machine.
        """
        if self.seed_end_date:
            return date.fromisoformat(self.seed_end_date)
        return date.today()

    def require_db(self) -> None:
        missing = [
            name
            for name, value in (
                ("ADMIN_DATABASE_URL", self.admin_dsn),
                ("READONLY_DATABASE_URL", self.readonly_dsn),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Did you copy .env.example to .env?"
            )


settings = Settings()
