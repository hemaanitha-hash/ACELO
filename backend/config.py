import os
from functools import lru_cache


class Settings:
    """
    Runtime configuration, read from environment variables.
    Nothing customer-specific lives here — this is deployment-level config only.
    """

    APP_ENV: str = os.getenv("APP_ENV", "development")  # "development" | "production"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./hema.db")

    # Symmetric key used to encrypt connection secrets at rest. Fallback dev key ensures smooth local startup.
    SECRET_ENCRYPTION_KEY: str = os.getenv(
        "SECRET_ENCRYPTION_KEY", "u8T_18P3jWz6V0eX7sQ3p7x_Z4a2c1b0_d9e8f7g6h5="
    )

    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173").split(",") if o.strip()
    ]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
