import os
from functools import lru_cache
from pathlib import Path

try:  # backend/.env (git-ignored) holds deployment configuration for the demo
    from dotenv import load_dotenv

    if os.getenv("ACELO_SKIP_DOTENV") != "1":  # tests run isolated from a developer's .env
        load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:  # pragma: no cover - python-dotenv is in requirements
    pass


class Settings:
    """
    Runtime configuration, read from environment variables.
    Nothing customer-specific lives here — this is deployment-level config only.
    """

    APP_ENV: str = os.getenv("APP_ENV", "development")  # "development" | "production"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./hema.db")

    # Symmetric key used to encrypt connection secrets at rest. Fallback dev key ensures smooth local startup.
    _DEV_ENCRYPTION_KEY = "u8T_18P3jWz6V0eX7sQ3p7x_Z4a2c1b0_d9e8f7g6h5="
    SECRET_ENCRYPTION_KEY: str = os.getenv("SECRET_ENCRYPTION_KEY", _DEV_ENCRYPTION_KEY)

    @property
    def uses_dev_encryption_key(self) -> bool:
        """
        True when the built-in development key is in use.

        It is published in this file, so anything encrypted with it is readable
        by anyone with the source. Harmless locally; in production it must be
        replaced, and startup says so loudly rather than failing silently.
        """
        return self.SECRET_ENCRYPTION_KEY == self._DEV_ENCRYPTION_KEY

    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173").split(",") if o.strip()
    ]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
