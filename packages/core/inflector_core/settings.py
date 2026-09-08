"""Application configuration shared by local processes."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings with safe local development defaults."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://inflector:inflector@localhost:5432/inflector"
    cors_origins: str = "http://localhost:3000"

    @property
    def allowed_origins(self) -> list[str]:
        """Return configured API origins without empty entries."""

        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return one settings object per process."""

    return Settings()
