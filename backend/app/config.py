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
    bot_test_log_persist: bool = False  # тест: писать логи реплея в bot_logs (BOT_TEST_LOG_PERSIST=1)
    bot_test_variant: str = ""   # вариант тестового набора: data/ensemble_config.test.<variant>.json
    universe_mode: str = ""      # research/test: v2_trend | v2_meanrev — универс через StrategyScreener (пусто = legacy)
    signal_trace: bool = False   # Signal Trace P0: JSONL-журнал сигналов (reports/signal_trace/<run>.jsonl)
    signal_trace_db: bool = False  # Signal Trace фаза 1: писать события ещё и в БД (signal_trace_runs/events)
    signal_trace_dir: str = ""   # переопределить каталог артефактов (пусто = backend/reports/signal_trace)
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    log_debug_engine: bool = False  # подробные debug-логи движка (hot path)
    api_token: str = ""  # Bearer-токен для write-эндпойнтов API (пусто = auth отключена)
    # CORS-домены через запятую. ПУСТО = legacy-поведение (разрешено всё + warning).
    # Для публичного/строгого режима задать явно, напр. http://localhost:5173.
    cors_origins: str = ""
    run_migrations_on_start: bool = False  # alembic upgrade head при старте приложения

    # --- Волатильная карусель (double-carousel) ---
    # Периодически ранжирует весь TQBR по дневной волатильности RNG% из MOEX ISS
    # и принудительно держит в юниверсе только top-N самых волатильных (остальные
    # снимает). Управляет метками eligible в БД — подхват/снятие делает обычный
    # hot-add/remove цикл бота (без рестарта).
    # ВЫКЛЮЧЕНА ПО УМОЛЧАНИЮ — включение требует осознанного решения.
    vol_carousel_enabled: bool = False          # мастер-переключатель
    vol_carousel_top_n: int = 20                # сколько самых волатильных держать
    vol_carousel_min_rng: float = 0.5           # мин. RNG% для добавления (порог)
    vol_carousel_hysteresis: int = 3            # сколько циклов «мимо top-N» держать перед снятием
    vol_carousel_cycle_sec: int = 300           # период цикла (5 мин)
    vol_carousel_lot_min: float = 1.0           # мин. стоимость лота (₽) — отсечка мусора
    vol_carousel_blacklist: str = ""            # тикеры-исключения через запятую

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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
