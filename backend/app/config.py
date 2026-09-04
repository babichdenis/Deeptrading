from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Deeptrading"
    app_version: str = "0.1.0"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "deeptrading"
    postgres_password: str = "deeptrading"
    postgres_db: str = "deeptrading"
    tinkoff_token: str = ""
    tinkoff_live_token: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
