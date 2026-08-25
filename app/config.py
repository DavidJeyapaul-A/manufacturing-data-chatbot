"""Environment -> typed settings. The single place env vars are read."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

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
    # The DSNs are DERIVED from these discrete parts, which are the very same
    # variables the db container uses to create the roles. Writing the password
    # once means the credential the app presents and the credential the role was
    # created with cannot drift apart — a mismatch shows up as an opaque
    # "password authentication failed", which is a miserable thing to debug.
    dialect_name: str = field(default_factory=lambda: _str("DB_DIALECT", "postgres"))
    db_name: str = field(default_factory=lambda: _str("POSTGRES_DB", "mfg"))
    db_host: str = field(default_factory=lambda: _str("DB_HOST", "db"))
    db_port: int = field(default_factory=lambda: _int("DB_PORT", 5432))

    admin_user: str = field(default_factory=lambda: _str("POSTGRES_ADMIN_USER", "mfg_admin"))
    admin_password: str = field(default_factory=lambda: _str("POSTGRES_ADMIN_PASSWORD"))
    readonly_user: str = field(default_factory=lambda: _str("APP_READONLY_USER", "readonly_user"))
    readonly_password: str = field(default_factory=lambda: _str("APP_READONLY_PASSWORD"))

    #: Optional escape hatches, for pointing at a database this compose file did
    #: not create. Leave blank and the DSN is built from the parts above.
    admin_url_override: str = field(default_factory=lambda: _str("ADMIN_DATABASE_URL"))
    readonly_url_override: str = field(default_factory=lambda: _str("READONLY_DATABASE_URL"))

    def _dsn(self, user: str, password: str) -> str:
        """Build a DSN, percent-encoding the credentials.

        Without the encoding a password containing '@', ':' or '/' produces a
        URL that parses into the wrong pieces and fails to authenticate.
        """
        return (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def admin_dsn(self) -> str:
        return self.admin_url_override or self._dsn(self.admin_user, self.admin_password)

    @property
    def readonly_dsn(self) -> str:
        return self.readonly_url_override or self._dsn(self.readonly_user, self.readonly_password)

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
        """Fail early and specifically, rather than at connect time."""
        missing = [
            name
            for name, value in (
                ("POSTGRES_ADMIN_USER", self.admin_user),
                ("POSTGRES_ADMIN_PASSWORD", self.admin_password),
                ("APP_READONLY_USER", self.readonly_user),
                ("APP_READONLY_PASSWORD", self.readonly_password),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Did you copy .env.example to .env?"
            )
        self._check_override_agrees(
            "ADMIN_DATABASE_URL", self.admin_url_override,
            "POSTGRES_ADMIN_USER", self.admin_user,
            "POSTGRES_ADMIN_PASSWORD", self.admin_password,
        )
        self._check_override_agrees(
            "READONLY_DATABASE_URL", self.readonly_url_override,
            "APP_READONLY_USER", self.readonly_user,
            "APP_READONLY_PASSWORD", self.readonly_password,
        )

    @staticmethod
    def _check_override_agrees(
        url_var: str, url: str,
        user_var: str, user: str,
        password_var: str, password: str,
    ) -> None:
        """A URL override that disagrees with the discrete vars is always a bug.

        The db container creates the role from the DISCRETE variables, so if the
        override carries a different credential the app authenticates with one
        password against a role created with another. Say so plainly instead of
        letting Postgres report 'password authentication failed'.
        """
        if not url:
            return
        parts = urlsplit(url)
        url_user = unquote(parts.username or "")
        url_password = unquote(parts.password or "")
        problems = []
        if url_user != user:
            problems.append(f"user {url_user!r} but {user_var}={user!r}")
        if url_password != password:
            problems.append(f"a different password than {password_var}")
        if problems:
            raise RuntimeError(
                f"{url_var} disagrees with the discrete database variables: "
                + "; ".join(problems)
                + f". The database roles are created from {user_var}/{password_var}, "
                f"so this mismatch would fail authentication. Either delete {url_var} "
                f"from .env (it is optional — the DSN is built from the parts) or "
                f"make it match."
            )


settings = Settings()
