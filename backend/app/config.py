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
    sandbox: str = ""
    bot_mode: str = "sandbox"  # sandbox | live | test
    sandbox_account: str = ""
    live_account: str = ""
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    log_debug_engine: bool = False  # подробные debug-логи движка (hot path)

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
