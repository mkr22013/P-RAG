"""
config.py
────────────────────────────────────────────────────────────────────────────
Environment-aware configuration loader — Python equivalent of .NET Program.cs
AddEnvironmentVariables() + AddAzureKeyVault().

Load order (later values override earlier):
    1. .env.{environment}     — non-sensitive environment-specific config
    2. .env                   — local dev overrides (gitignored, never deployed)
    3. OS environment vars    — secrets injected by Azure pipeline from Key Vault

Usage:
    from config import settings

    db_url = settings.POSTGRES_DSN
    model  = settings.OLLAMA_MODEL

Environment is controlled by APP_ENV variable:
    APP_ENV=development   → loads .env.development
    APP_ENV=staging       → loads .env.staging
    APP_ENV=production    → loads .env.production
    (default: development)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Determine environment ─────────────────────────────────────────────────────
APP_ENV = os.getenv("APP_ENV", "development").lower()
BASE_DIR = Path(__file__).resolve().parent

# ── Load order: env file first, then OS env vars override ────────────────────
# .env.{environment} — non-sensitive environment-specific config (staging/prod)
# .env               — local dev values (gitignored, personal machine only)
# OS env vars        — secrets injected by Azure pipeline from Key Vault (always win)
env_file = BASE_DIR / f".env.{APP_ENV}"
if env_file.exists():
    load_dotenv(env_file, override=False)

# .env — local overrides, only exists on developer machines
local_env = BASE_DIR / ".env"
if local_env.exists():
    load_dotenv(local_env, override=False)

print(f"[config] Environment: {APP_ENV}")


# ── Settings ──────────────────────────────────────────────────────────────────
class Settings:
    """
    Typed settings class — single source of truth for all config.
    Secrets (marked with *) come from Azure Key Vault via pipeline injection,
    never from files in staging/production.
    """

    # ── App ───────────────────────────────────────────────────────────────────
    APP_ENV: str = APP_ENV

    # ── LLM ──────────────────────────────────────────────────────────────────
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1")

    # ── Auth0 * ───────────────────────────────────────────────────────────────
    AUTH0_DOMAIN: str = os.getenv("AUTH0_DOMAIN", "")
    AUTH0_AUDIENCE: str = os.getenv("AUTH0_AUDIENCE", "")
    AUTH0_ALGORITHMS: str = os.getenv("AUTH0_ALGORITHMS", "RS256")
    AUTH_EXCLUDED_PATHS: str = os.getenv("AUTH_EXCLUDED_PATHS", "/health")

    # ── PostgreSQL * ──────────────────────────────────────────────────────────
    POSTGRES_DSN: str = os.getenv("POSTGRES_DSN", "")

    # ── Azure Blob Storage * ──────────────────────────────────────────────────
    AZURE_BLOB_CONNECTION_STRING: str = os.getenv("AZURE_BLOB_CONNECTION_STRING", "")
    AZURE_BLOB_PDF_CONTAINER: str = os.getenv(
        "AZURE_BLOB_PDF_CONTAINER", "insurance-pdfs"
    )
    AZURE_BLOB_INDEX_CONTAINER: str = os.getenv(
        "AZURE_BLOB_INDEX_CONTAINER", "insurance-indices"
    )

    # ── Redis * ───────────────────────────────────────────────────────────────
    REDIS_CONNECTION_STRING: str = os.getenv("REDIS_CONNECTION_STRING", "")

    # ── Service Bus * ─────────────────────────────────────────────────────────
    AZURE_SERVICE_BUS_CONNECTION_STRING: str = os.getenv(
        "AZURE_SERVICE_BUS_CONNECTION_STRING", ""
    )
    AZURE_SERVICE_BUS_QUEUE_NAME: str = os.getenv(
        "AZURE_SERVICE_BUS_QUEUE_NAME", "cache-invalidation"
    )

    # ── Member Info API ───────────────────────────────────────────────────────
    MEMBER_INFO_API_URL: str = os.getenv("MEMBER_INFO_API_URL", "")
    MEMBER_API_CONNECT_TIMEOUT: float = float(
        os.getenv("MEMBER_API_CONNECT_TIMEOUT", "3.0")
    )
    MEMBER_API_READ_TIMEOUT: float = float(os.getenv("MEMBER_API_READ_TIMEOUT", "10.0"))
    MEMBER_API_MAX_RETRIES: int = int(os.getenv("MEMBER_API_MAX_RETRIES", "3"))
    MEMBER_API_RETRY_MIN_WAIT: float = float(
        os.getenv("MEMBER_API_RETRY_MIN_WAIT", "0.5")
    )
    MEMBER_API_RETRY_MAX_WAIT: float = float(
        os.getenv("MEMBER_API_RETRY_MAX_WAIT", "5.0")
    )

    @property
    def is_production(self) -> bool:
        return bool(self.AZURE_BLOB_CONNECTION_STRING)

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


settings = Settings()
