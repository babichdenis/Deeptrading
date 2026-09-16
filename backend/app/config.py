from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Deeptrading"
    app_version: str = "0.1.0"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "deeptrading"
    db_pool_size: int = 10          # размер пула соединений (для тестов на .2 — 3)
    db_max_overflow: int = 20
    postgres_password: str = "deeptrading"
    postgres_db: str = "deeptrading"
    tinkoff_token: str = ""
    tinkoff_live_token: str = ""
    sandbox: str = ""
    bot_mode: str = "sandbox"  # sandbox | live | test
    sandbox_account: str = ""
    live_account: str = ""
    bot_test_name: str = ""
    bot_test_start: str = ""
    bot_test_end: str = ""
    bot_test_pace: str = "fast"  # fast | wall (wall = поминутно, реальное время)
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    log_debug_engine: bool = False  # подробные debug-логи движка (hot path)

    @field_validator("log_debug_engine", mode="before")
    @classmethod
    def _empty_bool(cls, v):
        # Пустая строка в .env (LOG_DEBUG_ENGINE=) — это False, а не ошибка парсинга.
        if isinstance(v, str) and v.strip() == "":
            return False
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _strip_level(cls, v):
        return v.strip() if isinstance(v, str) else v

    @property
    def feed_token(self) -> str:
        return self.tinkoff_live_token or self.tinkoff_token

    def get_token(self, mode: str | None = None) -> str:
        """Токен для режима ('sandbox' | 'live'). По умолчанию bot_mode."""
        mode = mode or self.bot_mode
        if mode == "live":
            return self.tinkoff_live_token or self.tinkoff_token
        return self.sandbox or self.tinkoff_token

    def get_account(self, mode: str | None = None) -> str:
        """Account id для режима ('sandbox' | 'live')."""
        mode = mode or self.bot_mode
        if mode == "live":
            return self.live_account
        return self.sandbox_account

    def get_target(self, mode: str | None = None) -> str | None:
        """gRPC endpoint target. None = production (live)."""
        mode = mode or self.bot_mode
        if mode == "live":
            return None
        return "sandbox-invest-public-api.tbank.ru"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
