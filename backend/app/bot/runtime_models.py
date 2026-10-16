from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class BotConfig:
    strategy_id: str = "rsi_reversal"
    params: dict = field(default_factory=dict)
    interval_name: str = "5min"
    top_n: int = 40
    qty_per_trade: int = 1
    stop_pct: float = 0.01
    target_pct: float = 0.02
    sl_mode: str = "atr"  # "atr" or "fixed"
    atr_period: int = 14
    atr_multiplier: float = 4.0
    atr_risk_reward: float = 4.0
    # --- Trailing stop ---
    initial_sl_atr: float = 4.0  # стандартный SL = 4.0×ATR (шире, чтобы сигнальные/volume-выходы успевали)
    trail_activation_comm_mult: float | None = None  # None = трейлинг ОТКЛЮЧЁН (только SL/TP+сигналы)
    # Инфо-трейлинг: если реальный выключен (трейлинг=None) но multi задан (напр. 4.0),
    # виртуально считаем, где бы сработал трейл, и пишем в карточку сделки (SL/TP не трогаем).
    trail_info_activation_comm_mult: float | None = None
    trail_distance_atr: float = 2.5  # базовая дистанция трейлинга за ценой = 2.5×ATR
    # --- Динамический трейлинг ---
    trail_compress_r: float = 1.0    # сжатие дистанции по прибыли (в R): чем больше плюс, тем теснее
    trail_min_factor: float = 0.6    # минимальный множитель сжатия (2.5 → 1.5×ATR)
    trail_min_atr: float = 1.5       # минимальная дистанция (в ATR)
    trail_vol_boost: float = 0.3     # влияние объёма (высокий → шире, низкий → теснее)
    allow_short: bool = False
    long_allowed: bool = True
    short_allowed: bool = False
    initial_cash: float = 10_000.0
    daily_loss_limit: float = 1000.0
    mode: str = "paper"  # paper | sandbox | live
    use_ensemble: bool = False
    ensemble_capital: float = 2000.0
    pos_pct: float = 0.40  # доля equity на одну позицию (слот), 0.4 = 40%
    max_positions: int = 5  # максимум одновременных позиций (0 = без лимита)
    reconcile_enabled: bool = True  # сверка позиций/кэша с брокером (только в торговое время)
    max_exposure_pct: float = 1.0  # свои деньги в позициях <= X от equity (1.0 = 100%, 0 = без лимита)
    max_short_share: float = 0.7  # макс. доля SHORT среди позиций (0 = без лимита)
    # --- Портфельные лимиты (в деньгах) ---
    max_net_exposure_pct: float = 0.5   # |net notional| <= X equity (0=выкл)
    max_sector_pct: float = 0.35        # notional сектора <= X equity (0=выкл)
    max_sector_positions: int = 0       # макс. позиций в одном секторе-кластере (0=выкл)
    beta_filter_enabled: bool = False    # лимит позиций по бета-группе (low/mid/high)
    confirmed_cluster_enabled: bool = False  # лимит позиций в подтверждённых кластерах
    max_margin_use_pct: float = 0.8     # starting_margin <= X equity (0=выкл)
    max_stress_loss_pct: float = 0.10   # убыток при ±5% IMOEX <= X equity (0=выкл)
    queue_enabled: bool = True           # очередь кандидатов: топ-1 по силе входит с бустом
    queue_ttl_min: int = 30              # время жизни кандидата в очереди (мин)
    queue_interval_sec: int = 120        # период проверки очереди (сек)
    queue_min_turnover: float = 300_000.0  # мин. дневной оборот тикера (₽) — иначе вето illiquid
    rank_enabled: bool = True            # ранжирование тикеров: разведка → топ-N по прошлому net
    rank_top_n: int = 10                 # сколько тикеров торгуем после разведки
    rank_explore: int = 10               # пробных сделок каждому тикеру
    rank_min_hist: int = 5               # мин. история для попадания в рейтинг
    queue_adv_multiple: float = 200.0    # слот ≤ 1/N дневного оборота (ликвидность под размер)
    top_sizing: str = "multiply"         # режим размера для топ-1 кандидата (divide|multiply)
    top_relax_caps: bool = True          # топ-1: net до 100% и сектор без лимита (стресс/маржа жёсткие)
    queue_history_veto: bool = True      # вето на явно токсичную историю (n≥20, net<−50₽, WR<25%)
    top_boost: float = 2.0               # множитель слота для топ-1 кандидата
    dd_reduce1_pct: float = 0.05         # просадка от пика equity → закрыть 50% позиций
    dd_reduce2_pct: float = 0.10         # просадка от пика equity → закрыть 80% позиций
    balance_min_positions: int = 3  # баланс L/S включается при >= N позиций
    ensemble_quorum: int = 2
    ensemble_session: str = "main"
    ensemble_entry_tf: str = "5min"  # ТФ свечей входа (micro_breakout): 1min | 5min | 10min | 15min
    ensemble_entry_from_setups: bool = True  # True: сторона из ансамблей; False: из micro_breakout
    ensemble_direction_sid: str = ""  # Путь 2: направление только от этой стратегии (пусто = общий режим)
    # --- Тройное подтверждение входа на 1м свечах ---
    entry_confirm_closes: int = 3  # N 1м-закрытий строго по направлению (BUY: каждое выше предыдущего)
    entry_confirm_closes_sides: list = field(default_factory=lambda: ["BUY", "SELL"])  # к каким сторонам
    # --- HOLD после серии убытков ---
    loss_streak_hold: bool = True       # пауза входов после серии убытков
    loss_streak_n: int = 2              # сколько убытков подряд
    loss_streak_hold_min: float = 60.0  # длительность паузы, минут
    loss_streak_scope: str = "ticker"   # ticker | global
    # --- Оверрайд SL/TP (0 = брать per-ticker optuna) ---
    ensemble_sl_mult: float = 0.0
    ensemble_rr: float = 0.0
    sessions: list = field(default_factory=lambda: ["day"])
    leverage: float = 1.0
    # --- Commission & slippage (live parity with backtest) ---
    commission_rate: float = 0.0005  # 0.05% per trade (T-Invest, parity с движком)
    slippage_bps: float = 2.0  # 2 bps adverse slippage
    # --- Opposite-hold / confirm_flip ---
    confirm_flip: int = 3  # N встречных сигналов перед закрытием (0=отключено)
    invert_signals: bool = False  # ЭКСПЕРИМЕНТ: инвертировать сторону входа (проверка "обратной" логики)
    # --- Re-entry cooldown ---
    reentry_cooldown_bars: int = 30  # баров между выходом и повторным входом (0=отключено)
    # --- Overnight ---
    overnight: bool = False  # по умолчанию закрывать на конец торгового дня; True = держать через ночь
    eod_close_min_before: int = 10  # закрывать за N минут до конца последней сессии (overnight=False)
    # Выходные: закрыться до ухода (владелец 02.10) — в выходные только лимитные
    # заявки и тонкий объём, позиция не должна их пережить (и висеть до понедельника).
    weekend_flat: bool = True
    # --- Bias-exit: закрывать позицию, если bias_by_state сменил знак против позиции ---
    # (по умолчанию ВЫКЛ: эталонный бэктест держит до EOD; опция — для экспериментов)
    bias_exit_enabled: bool = False
    daily_bias: bool = True          # дневной MACD-bias: блокировать входы против дневного направления
    daily_bias_mode: str = "veto"    # veto | info
    mtf_align: bool = False          # H1 MACD должен совпадать с дневным bias (подтверждение)
    ensemble_require_member: str = ""  # обязательный голос кворума (напр. "macd_cross")
    momentum_short: bool = False     # режим «моментум-шорт»: утром шорт низ-K по N-дневному падению
    momentum_n: int = 63             # окно моментума (торговых дней)
    momentum_k: int = 3              # сколько имён шортить
    momentum_stop_pct: float = 0.03  # внутридневной стоп (доля от входа)
    momentum_entry_time: str = "10:30"  # время входа (МСК)
    momentum_max_lev: float = 2.0    # кап плеча для моментум-входа
    momentum_only: bool = False      # только моментум: сигналы ансамбля игнорируются
    momentum_side: str = "short"     # направление моментума: short | long | both
    mtf_trigger: bool = False        # M5 MACD гистограмма должна разворачиваться в сторону входа
    # --- Детерминированные TF-гейты входа (правила AI-гейта, зашиты в движок) ---
    entry_h1_align: bool = True         # 7) H1 MACD должен подтверждать сторону входа
    entry_tf_conflict: bool = True      # 6) daily bias и H1 не должны противоречить
    entry_last_hour_block: bool = True  # 16) не входить в последний час сессии
    # --- Veto по накопленному движению (heatmap-часы из 1м) ---
    entry_hm_veto: bool = False         # вход против накопленного движения за окно → reject
    hm_veto_window_h: int = 24          # окно в часах (закрытые часовые бары)
    hm_veto_thr_pct: float = 3.0        # базовый порог |накопленного %|
    hm_veto_mode: str = "veto"          # veto | info
    # --- Margin ---
    use_margin: bool = True  # использовать маржинальное кредитование
    margin_sessions: list = field(default_factory=lambda: ["day"])  # сессии с маржой: morning/day/evening
    margin_leverage: float = 0.0  # потолок плеча: 0 = Max (как одобрит брокер), иначе 2/3/4/5...
    margin_sizing: str = "divide"  # divide: позиция=бюджет (свои=бюджет/плечо); multiply: позиция=бюджет×плечо
    # --- Торговые режимы (regime gate): в каких режимах рынка разрешены входы ---
    trade_regimes: list = field(default_factory=lambda: ["NEUTRAL", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "RANGE"])
    trend_alignment: bool = True  # не входить против тренда (TREND_UP→BUY, TREND_DOWN→SELL)
    max_margin_pct: float = 80.0  # макс % от доступного маржинального лимита на сделку
    # --- Replay (V2): тест на исторических данных через те же runtime-пути, что и live ---
    feed: str = "live"        # live | replay (источник свечей; реплей = БД)
    replay_start: str = ""    # ISO UTC datetime начала окна реплея (обязателен при feed=replay)
    replay_end: str = ""      # ISO UTC datetime конца окна (пусто = до конца данных)
    replay_pace: str = "fast" # fast (макс. скорость) | wall (в реальном времени по барам)
    test_name: str = ""       # имя теста (режим test): сделки реплея помечаются им
    replay_log_persist: bool = False  # тест: писать логи в БД bot_logs (без флага — только ring/UI)
    intrabar_check_sec: float = 10.0  # период intrabar-проверки SL/TP по последней цене (0=выкл)
    # --- IMOEX guard: запрет новых входов против всплеска индекса MOEX ---
    imoex_guard: bool = True          # вкл/выкл защиту
    imoex_spike_pct: float = 0.8      # порог хода индекса за окно, % (активация)
    imoex_spike_points: float = 20.0  # порог хода индекса за окно, пункты (0=выкл)
    imoex_spike_window_min: int = 20  # окно расчёта хода индекса, минут
    imoex_release_frac: float = 0.5   # релиз, когда ход затух до N от порога
    imoex_min_block_min: float = 5.0  # мин. длительность блока, минут
    imoex_refresh_sec: float = 60.0   # период догрузки 1м свечей IMOEX (live), сек
    imoex_guard_min_beta: float = 0.0  # 0 = блокировать все; >0 = только бумаги с beta >= порога
    imoex_chase_block_pct: float = 1.5  # второй уровень: при |ходе| >= N% блокировать и входы ПО индексу (0=выкл)
    # --- AI-гейт подтверждения входов (approval gate) ---
    ai_approval: bool = False              # True = новые входы ждут подтверждения ИИ/человека
    ai_approval_timeout_sec: float = 45.0  # сколько ждать решение, сек
    ai_approval_default: str = "approve"   # approve | reject — что делать по таймауту
    ai_reject_cooldown_min: float = 5.0   # пауза входов по тикеру после отклонения ИИ, мин (0=выкл)
    # --- Стакан/ликвидность: гейт для ВСЕХ входов (движок + AI), первый после ансамбля ---
    entry_ob_imbalance_max: float = 0.3  # блок входа против потока стакана сильнее X (0=выкл)
    entry_ob_spread_max: float = 25.0    # блок входа при спреде > X б.п. (0=выкл)
    entry_min_turnover: float = 0.0      # мин. дневной оборот тикера, ₽ (0=выкл)
    entry_volatility_max_mult: float = 3.0  # блок если ATR% > X × медианы (0=выкл)
    entry_news_blackout: bool = True     # блок входа по свежей негативной новости
    entry_news_blackout_min: int = 60    # окно новостей для стоп-блока, минут
    # --- AI-ордера (AI-трейдер): чейзинг и потолки SL/TP ---
    ai_chase_pct: float = 3.0         # блок входа после хода >X% за день без отката (0=выкл)
    ai_sl_max_pct: float = 0.03       # потолок SL для AI-ордера, доля (0=без потолка)
    ai_tp_max_pct: float = 0.08       # потолок TP для AI-ордера, доля (0=без потолка)


@dataclass
class BotOrder:
    id: str
    figi: str
    ticker: str
    action: str
    side: str
    qty: int
    meta: dict | None = None
    status: str = "SUBMITTED"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled_at: datetime | None = None
    price: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "figi": self.figi,
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "qty": self.qty,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "price": round(self.price, 6) if self.price is not None else None,
        }
