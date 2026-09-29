"""CandleHub — единый владелец свечных серий (Фаза C, порт CandleManager из OsEngine).

Референс: docs/osengine/PORT_NOTES_CANDLEHUB.md. Ключевые решения порта:

- Конвенция времени: ts свечи = время ЗАКРЫТИЯ бара (как в таблице candles
  и в T-Invest). Свеча с ts=T покрывает интервал (T - tf, T].
- Вход — ТОЛЬКО закрытые 1m-свечи (live-feed / реплей / БД). Все старшие ТФ
  строятся внутри hub из 1m: одна точка склейки, одна конвенция — никаких
  параллельных resample-функций с разной семантикой (как было в ensemble.py
  против candle_cache.py). Ресемпл на ходу делает ТОЛЬКО этот модуль.
- Ряд хранит только ЗАКРЫТЫЕ бары; формирующийся бар (partial) живёт отдельно
  и в snapshot() не попадает — стратегии видят завершённую историю
  (детерминизм, golden-тесты).
- Бакеты считаются от глобальной UTC-сетки (кратно tf). Пустые бакеты не
  существуют, поэтому свечи разных сессий не склеиваются через клиринг/ночь.
- Бакет считается ЗАКРЫТЫМ, когда приходит 1m-свеча с ts == правой границе
  бакета (последняя возможная минута) — не нужно ждать первую свечу следующего
  бакета. Для ТФ, чья граница не совпадает с последней минутой торгов
  (например, day: граница 00:00 UTC, а сессия кончается 20:50 UTC), ряд
  закрывается явно через finalize().
- ТФ от 1min до day — см. TF_SECONDS. figi для движка — просто строка:
  индексы (IMOEX), сырье, валюты живут в тех же сериях, отдельного
  «сигнального» контура в движке нет и не нужно.

v2 (2026-09-26) — движок учится жить с неидеальными данными:

- validate_candle(): каждая свеча проверяется (OHLC-согласованность, цены > 0,
  без NaN/inf, volume >= 0); битые отбрасываются с событием on_rejected и
  счётчиком — ошибочная свеча не может испортить ряд.
- Источники с приоритетом (SOURCE_PRIORITY: live > rest > db, настраивается
  через CandleHub(source_priority=...)): та же минута от более доверенного
  источника ЗАМЕНЯЕТ принятую (replace), от равного/худшего — игнорируется.
- Опоздавшие минуты (ts < последней) для 1m-ряда — это дыры: вставляются на
  своё место (insert), производные ТФ целиком перестраиваются из 1m-истории
  (rebuild) — коррекция задним числом не оставляет расхождений между ТФ.
- gap_report(): дыры 1m-ряда — что докачивать; календарь сессий передаётся
  снаружи (is_trading_minute), движок ничего не знает о расписаниях.

ГРАНИЦА РОЛЕЙ (решение 2026-09-26): CandleHub — ЧИСТЫЙ ДВИЖОК свечей —
приём/валидация/дедуп/приоритет источников/склейка ТФ/гэп-отчёт/seed.
БЕЗ сети, БД и подписок. Подписки тикеров (live add/remove), исполнение
докачки по gap_report, сигнальные фиды (IMOEX → нефть/золото/валюты) —
ОТДЕЛЬНАЯ роль оркестратора ПОВЕРХ движка (аналог CandleManager vs
ServerMaster/коннекторы в OsEngine). Движок наращивается без колупания
оркестратора и наоборот.

События (аналог CandleFinishedEvent / CandleUpdateEvent в OsEngine):
    series.on_closed.append(cb)    # бар финализирован: cb(candle)
    series.on_updated.append(cb)   # формирующийся бар обновлён: cb(candle)
    series.on_rejected.append(cb)  # свеча забракована: cb(candle, source, reason)
    series.on_rebuilt.append(cb)   # история перестроена: cb() — перечитай snapshot()

build_tf() — чистая функция сборки ТФ из списка 1m-свечей (БД, реплей,
исследовательские скрипты); использует ту же агрегацию, что и живые серии;
битые свечи молча отбрасываются валидатором. Живой кэш сознательно не
дублируем: сами серии в hub и есть кэш.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from app.engine.models import Candle

TF_SECONDS: dict[str, int] = {
    "1min": 60,
    "5min": 300,
    "10min": 600,
    "15min": 900,
    "30min": 1800,
    "hour": 3600,
    "2h": 7200,
    "4h": 14400,
    "day": 86400,
}

EventListener = Callable[[Candle], None]
RejectListener = Callable[[Candle, str, str], None]
RebuiltListener = Callable[[], None]

# Приоритет источников: меньше ранг — больше доверия. Одна и та же минута от
# более доверенного источника ЗАМЕНЯЕТ принятую (при отличии значений);
# от равного/худшего — игнорируется. Настраивается извне:
# CandleHub(source_priority={...}) — оркестратор может поставить, например,
# MOEX ISS выше live-стрима для индексов.
SOURCE_PRIORITY: dict[str, int] = {"live": 0, "rest": 1, "db": 2}
_UNKNOWN_SOURCE_RANK = 1000


def source_rank(source: str, priority: dict[str, int] | None = None) -> int:
    """Ранг источника: меньше — довереннее. Неизвестный — ниже всех известных."""
    return (priority if priority is not None else SOURCE_PRIORITY).get(
        str(source), _UNKNOWN_SOURCE_RANK
    )


def validate_candle(c: Candle, *, rel_eps: float = 1e-9) -> str | None:
    """Проверка свечи на «ошибочность». None = здорова, иначе код причины.

    - "nan": любое из OHLCV не конечно (NaN/inf — мусор парсинга/брокера);
    - "bad_price": цена <= 0 (open/high/low/close);
    - "bad_volume": volume < 0;
    - "bad_range": high < max(open, close) или low > min(open, close)
      (с допуском rel_eps — float-шум округления не рождает ложные отказы).

    Нулевой объём и плоская свеча (o==h==l==c) — ЗАКОННЫЕ данные, не ошибки.
    """
    vals = (c.open, c.high, c.low, c.close, c.volume)
    if any(not math.isfinite(float(v)) for v in vals):
        return "nan"
    if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
        return "bad_price"
    if c.volume < 0:
        return "bad_volume"
    eps = rel_eps * max(1.0, abs(c.high), abs(c.low))
    if c.high + eps < max(c.open, c.close) or c.low - eps > min(c.open, c.close):
        return "bad_range"
    if c.high + eps < c.low:
        return "bad_range"
    return None


def tf_to_seconds(tf: str | int) -> int:
    """'5min' | 300 → 300. Неизвестная строка или tf < 60s — ValueError."""
    if isinstance(tf, bool):
        raise ValueError(f"bad timeframe: {tf!r}")
    if isinstance(tf, int):
        if tf < 60:
            raise ValueError(f"tf must be >= 60 seconds, got {tf}")
        return tf
    if tf not in TF_SECONDS:
        raise ValueError(f"unknown timeframe {tf!r}; known: {sorted(TF_SECONDS)}")
    return TF_SECONDS[tf]


def _unix(ts: datetime) -> int:
    """Unix-секунды; naive-время трактуется как UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())


def bucket_close(ts: datetime, tf_seconds: int) -> datetime:
    """Правая граница (close) tf-бакета, которому принадлежит свеча с close=ts.

    ceil-семантика: ts, кратный tf, сам является границей СВОЕГО бакета —
    минута, закрывающаяся в 10:05, входит в 5m-бар с close 10:05
    (интервал (10:00, 10:05]). Совпадает с resample_from_1m в candle_cache.
    MSK = UTC+3 без перехода на летнее время, поэтому UTC-сетка бакетов
    совпадает с настенной Москвой.
    """
    u = _unix(ts)
    bc = (u + tf_seconds - 1) // tf_seconds * tf_seconds
    return datetime.fromtimestamp(bc, tz=timezone.utc)


class CandleSeries:
    """Ряд закрытых свечей одного (figi, tf) + формирующийся бар.

    Хранение: deque закрытых баров (авто-обрезка головы по maxlen) + индекс
    _pos (ts -> абсолютная позиция) для поиска по ts и карта источников
    _src (ts -> source) для решений о приоритете. Формирующийся бар (partial)
    живёт отдельно и в историю не попадает. Подписки: on_closed / on_updated /
    on_rejected / on_rebuilt.

    Поддержка неидеального входа (v2):
    - битая свеча (validate_candle) — reject: счётчик + on_rejected;
    - та же минута от источника выше классом (и с другими значениями) —
      replace; только для 1min-ряда — производные перестраивает CandleHub;
    - ts меньше последнего — дыра: вставка на своё место (insert), тоже
      только 1min; для tf>60 опоздавшие не поддерживаются — CandleHub
      в этом случае делает rebuild из 1m-истории.
    """

    def __init__(self, figi: str, tf_seconds: int, maxlen: int = 5000,
                 source_priority: dict[str, int] | None = None):
        if tf_seconds < 60:
            raise ValueError(f"tf_seconds must be >= 60, got {tf_seconds}")
        self.figi = figi
        self.tf_seconds = tf_seconds
        self.maxlen = maxlen
        self._priority = source_priority
        self._closed: deque[Candle] = deque()
        self._start = 0                      # абсолютная позиция _closed[0]
        self._pos: dict[datetime, int] = {}   # ts -> абсолютная позиция
        self._src: dict[datetime, str] = {}   # ts -> источник последней записи
        self._partial: Candle | None = None
        self._last_ts: datetime | None = None  # close последней принятой 1m
        self._n_skipped = 0                   # дубликаты + опоздавшие + дропы
        self._n_rejected = 0                  # отброшено валидатором
        self._n_rebuilds = 0                  # сколько раз ряд перестраивался
        self.on_closed: list[EventListener] = []
        self.on_updated: list[EventListener] = []
        self.on_rejected: list[RejectListener] = []
        self.on_rebuilt: list[RebuiltListener] = []

    # --- чтение -------------------------------------------------------
    def __len__(self) -> int:
        return len(self._closed)

    @property
    def last(self) -> Candle | None:
        """Последний ЗАКРЫТЫЙ бар (partial не входит)."""
        return self._closed[-1] if self._closed else None

    @property
    def partial(self) -> Candle | None:
        """Формирующийся (незакрытый) tf-бар, если есть."""
        return self._partial

    @property
    def skipped(self) -> int:
        """Сколько баров отброшено как дубликаты/опоздавшие/не поместившиеся."""
        return self._n_skipped

    @property
    def rejected(self) -> int:
        """Сколько свечей забраковано валидатором."""
        return self._n_rejected

    @property
    def rebuilds(self) -> int:
        """Сколько раз ряд целиком перестраивался (коррекция истории)."""
        return self._n_rebuilds

    def snapshot(self, n: int | None = None) -> list[Candle]:
        """Копия последних n закрытых баров (хронологический порядок)."""
        if n is None or n >= len(self._closed):
            return list(self._closed)
        if n <= 0:
            return []
        return list(self._closed)[-n:]

    # --- запись ---------------------------------------------------------
    def push_1m(self, candle: Candle, *, source: str = "live",
                fire: bool = True) -> str:
        """Принять закрытую 1m-свечу от источника source.

        Возвращает действие:
          "append"  — новая минута по порядку (быстрый путь, без пересборок);
          "replace" — та же минута от более доверенного источника заменила
                      принятую (значения отличались);
          "insert"  — опоздавшая минута вставлена на своё место (дыра);
          "dup"     — дубль/опоздавшая худшего класса/совпавшая по значениям:
                      проигнорирована;
          "reject"  — свеча забракована валидатором (наркоз на ряд не влияет).

        tf=60: свеча сразу уходит в историю. tf>60: копится в partial, пока не
        придёт минута, закрывающаяся на границе бакета (ts == bucket_close) —
        тогда бар финализируется немедленно; либо пока не начнётся следующий
        бакет (гэп внутри кадра → короткий бар).
        """
        reason = validate_candle(candle)
        if reason is not None:
            self._n_rejected += 1
            self._emit_rejected(candle, source, reason)
            return "reject"

        ts = candle.ts
        # та же минута уже в истории: замена, если источник довереннее
        # и значения отличались (совпавшая — тихий дубликат, без пересборки)
        if ts in self._pos:
            if (self.tf_seconds <= 60
                    and self._closed[self._pos[ts] - self._start] != candle
                    and source_rank(source, self._priority)
                    < source_rank(self._src.get(ts, "live"), self._priority)):
                self._replace_closed(ts, candle, source)
                if fire:
                    self._emit_rebuilt()
                return "replace"
            self._n_skipped += 1
            return "dup"

        if self._last_ts is not None:
            if ts == self._last_ts:      # повтор последней принятой минуты
                self._n_skipped += 1
                return "dup"
            if ts < self._last_ts:       # опоздавшая минута
                if self.tf_seconds <= 60:
                    if self._insert_closed(ts, candle, source):
                        if fire:
                            self._emit_rebuilt()
                        return "insert"
                    return "dup"
                # производный ряд: опоздавшие не поддерживаются — их вставляет
                # CandleHub через rebuild из 1m-истории (см. ingest_1m)
                self._n_skipped += 1
                return "dup"

        self._last_ts = ts
        if self.tf_seconds <= 60:
            self._append_closed(candle, source=source, fire=fire)
            return "append"

        bts = bucket_close(ts, self.tf_seconds)
        p = self._partial
        if p is None or bts > p.ts:
            # новый бакет: предыдущий (если был) закрываем как есть
            if p is not None:
                self._append_closed(p, source=source, fire=fire)
            self._partial = Candle(
                ts=bts, open=candle.open, high=candle.high,
                low=candle.low, close=candle.close, volume=candle.volume,
            )
            if fire:
                self._emit_updated(self._partial)
        elif bts == p.ts:
            merged = Candle(
                ts=p.ts, open=p.open, high=max(p.high, candle.high),
                low=min(p.low, candle.low), close=candle.close,
                volume=p.volume + candle.volume,
            )
            self._partial = merged
            if fire:
                self._emit_updated(merged)
        else:
            # bts < p.ts невозможно при монотонном входе (проверка ts выше) —
            # защита от кривых данных.
            self._n_skipped += 1
            return "dup"

        if ts == bts:
            # пришла последняя возможная минута бакета — бар полон, закрываем
            self._finalize_partial(fire=fire)
        return "append"

    def merge_1m(self, candles: Iterable[Candle], *, source: str = "db",
                 notify: bool = True) -> bool:
        """Батч-загрузка 1m-свечей (докачка дыр / прогрев / коррекция) за один проход.

        Только для 1min-ряда (производные ТФ перестраивает CandleHub — см.
        seed_1m). Свечи валидируются; внутри батча при дубле ts побеждает
        поздний. Та же минута от более доверенного источника (с отличными
        значениями) заменяет принятую; равный/худший класс — игнор (счётчик).
        Возвращает True, если история изменилась. on_closed не стреляет;
        при изменении и notify=True — on_rebuilt.
        """
        if self.tf_seconds != 60:
            raise ValueError(
                "merge_1m — только для 1min-ряда; производные ТФ "
                "перестраивает CandleHub (rebuild из 1m-истории)"
            )
        batch: dict[datetime, Candle] = {}
        for c in candles:
            reason = validate_candle(c)
            if reason is not None:
                self._n_rejected += 1
                self._emit_rejected(c, source, reason)
                continue
            batch[c.ts] = c
        if not batch:
            return False

        merged: dict[datetime, tuple[Candle, str]] = {
            c.ts: (c, self._src.get(c.ts, "live")) for c in self._closed
        }
        changed = False
        for ts, c in batch.items():
            old = merged.get(ts)
            if old is None:
                merged[ts] = (c, source)
                changed = True
            elif (old[0] != c
                    and source_rank(source, self._priority)
                    < source_rank(old[1], self._priority)):
                merged[ts] = (c, source)
                changed = True
            else:
                self._n_skipped += 1
        if not changed:
            return False

        items = sorted(merged.items(), key=lambda kv: kv[0])
        if len(items) > self.maxlen:
            items = items[-self.maxlen:]
        self._closed = deque(c for _ts, (c, _s) in items)
        self._start = 0
        self._pos = {ts: j for j, (ts, _kv) in enumerate(items)}
        self._src = {ts: s for ts, (_c, s) in items}
        self._last_ts = items[-1][0]
        if notify:
            self._emit_rebuilt()
        return True

    def rebuild_from_1m(self, candles: Iterable[Candle], *,
                        notify: bool = True) -> None:
        """Полная перестройка ряда из 1m-истории (после replace/insert/merge).

        Тихая для on_closed/on_updated (история пересобрана целиком, а не
        поштучно); в конце при notify=True — on_rebuilt: «перечитай snapshot()».
        """
        self._closed = deque()
        self._start = 0
        self._pos = {}
        self._src = {}
        self._partial = None
        self._last_ts = None
        self._n_rebuilds += 1
        for c in candles:
            self.push_1m(c, fire=False)
        if notify:
            self._emit_rebuilt()

    def finalize(self) -> Candle | None:
        """Принудительно закрыть partial (EOD-флаш, конец реплея, обрыв данных).

        Бар уходит в историю с меткой закрытия своего бакета, даже если в нём
        меньше минут, чем в полном кадре: по времени бакет закончился,
        плотность — забота потребителя (см. _filter_sparse_signals в ensemble).
        Обязателен для ТФ, чья граница не совпадает с последней минутой торгов
        (например, day: граница 00:00 UTC, торги кончаются раньше).
        """
        return self._finalize_partial(fire=True)

    def seed(self, candles: Iterable[Candle], *, fire: bool = False) -> None:
        """Загрузить историю 1m-свечей по одной (прогрев рядов, build_tf)."""
        for c in candles:
            self.push_1m(c, fire=fire)

    # --- внутреннее ------------------------------------------------------
    def _register(self, ts: datetime, source: str, idx: int | None = None) -> None:
        # idx=None: регистрируется ПОСЛЕДНИЙ элемент _closed — индекс len-1
        # (фикс 2026-09-26: раньше писали _start+len, т.е. позицию СЛЕДУЮЩЕГО
        # элемента — off-by-one, replace по _pos перезаписывал соседний бар).
        self._pos[ts] = (self._start + len(self._closed) - 1) if idx is None \
            else self._start + idx
        self._src[ts] = source

    def _evict_left(self) -> None:
        ev = self._closed.popleft()
        self._pos.pop(ev.ts, None)
        self._src.pop(ev.ts, None)
        self._start += 1

    def _append_closed(self, candle: Candle, *, source: str, fire: bool) -> None:
        if len(self._closed) >= self.maxlen:
            self._evict_left()
        self._closed.append(candle)
        self._register(candle.ts, source)
        if fire:
            self._emit_closed(candle)

    def _replace_closed(self, ts: datetime, candle: Candle, source: str) -> None:
        idx = self._pos[ts] - self._start
        self._closed[idx] = candle
        self._src[ts] = source

    def _insert_closed(self, ts: datetime, candle: Candle, source: str) -> bool:
        """Вставить опоздавшую свечу на позицию по ts (заполнение дыры).

        False — вставка невозможна (история полна и свеча старше всего).
        """
        idx = len(self._closed)
        while idx > 0 and self._closed[idx - 1].ts > ts:
            idx -= 1
        if len(self._closed) >= self.maxlen and idx == 0:
            # история полна, а свеча старше всего хранимого — места нет
            self._n_skipped += 1
            return False
        # абсолютные позиции хвоста сдвигаются на +1
        for j in range(idx, len(self._closed)):
            self._pos[self._closed[j].ts] += 1
        self._closed.insert(idx, candle)
        self._register(ts, source, idx=idx)
        while len(self._closed) > self.maxlen:
            self._evict_left()
        return True

    def _finalize_partial(self, *, fire: bool) -> Candle | None:
        p, self._partial = self._partial, None
        if p is not None:
            self._append_closed(p, source="live", fire=fire)
        return p

    def _emit_closed(self, c: Candle) -> None:
        for cb in self.on_closed:
            cb(c)

    def _emit_updated(self, c: Candle) -> None:
        for cb in self.on_updated:
            cb(c)

    def _emit_rejected(self, c: Candle, source: str, reason: str) -> None:
        for cb in self.on_rejected:
            cb(c, source, reason)

    def _emit_rebuilt(self) -> None:
        for cb in self.on_rebuilt:
            cb()


def build_tf(
    candles: Iterable[Candle],
    tf: str | int,
    *,
    include_partial: bool = False,
) -> list[Candle]:
    """Собрать старший ТФ из последовательности закрытых 1m-свечей.

    Чистая функция поверх той же агрегации, что и живые серии CandleHub —
    одна семантика для БД, реплея и live. Вход — в хронологическом порядке
    (дубликаты/опоздавшие пропускаются, битые свечи бракуются валидатором).

    include_partial=False (умолчание): хвостовой неполный бакет НЕ эмитится —
    по списку неизвестно, закрыт ли он (без look-ahead и полусвечей в
    бэктестах). True — эмитить и его (UI, отчёты, добивка истории).
    Бакет, закрытый по времени (пришла минута границы), эмитится всегда.
    """
    s = CandleSeries("<build_tf>", tf_to_seconds(tf))
    s.seed(candles)
    if include_partial:
        s.finalize()
    return s.snapshot()


class CandleHub:
    """Единый владелец свечных серий (figi × tf) — порт CandleManager (OsEngine).

    Отличия от источника: серии ленивые (создаются по первому обращению
    series(figi, tf)), вход только 1m, старшие ТФ — производные.
    Режим-агностичность: hub не знает, откуда свечи (live-feed, реплей, БД) —
    режимы = разные источники (source) и разные писатели в hub, торговая
    логика одна.

    Движок ЧИСТЫЙ (см. модульный докстринг): сеть/БД/подписки — вне его;
    оркестратор поверх кормит ingest_1m/seed_1m, читает gap_report() и
    развешивает события серий. 1m-ряд каждого figi ведётся всегда (создаётся
    при первом ингесте) — источник правды по свечам фиги.
    """

    def __init__(self, maxlen: int = 5000,
                 source_priority: dict[str, int] | None = None):
        self.maxlen = maxlen
        self.source_priority = source_priority
        self._series: dict[tuple[str, int], CandleSeries] = {}

    # --- доступ к рядам ---------------------------------------------------
    def series(self, figi: str, tf: str | int) -> CandleSeries:
        """Ряд figi×tf; создаётся при первом обращении и дальше поддерживается.

        Ряд, созданный ПОСЛЕ пришедших свечей, задним числом не достраивается —
        прогревайте через seed_1m() (см. test_series_late_subscription_needs_seed).
        """
        tf_sec = tf_to_seconds(tf)
        key = (figi, tf_sec)
        s = self._series.get(key)
        if s is None:
            s = CandleSeries(figi, tf_sec, maxlen=self.maxlen,
                             source_priority=self.source_priority)
            self._series[key] = s
        return s

    def keys(self) -> list[tuple[str, int]]:
        """Активные ряды: [(figi, tf_seconds)] — для логов."""
        return sorted(self._series)

    def stats(self) -> dict[tuple[str, int], dict[str, object]]:
        """{(figi, tf_sec): {closed, partial, skipped, rejected, rebuilds}}.

        Мониторинг/тесты: skipped — дубликаты и проигнорированные опоздавшие,
        rejected — брак валидатора, rebuilds — пересборки истории.
        """
        return {
            key: {
                "closed": len(s),
                "partial": s.partial is not None,
                "skipped": s.skipped,
                "rejected": s.rejected,
                "rebuilds": s.rebuilds,
            }
            for key, s in sorted(self._series.items())
        }

    # --- поток данных --------------------------------------------------------
    def ingest_1m(self, figi: str, candle: Candle, *, source: str = "live",
                  fire: bool = True) -> str:
        """Принять закрытую 1m-свечу инструмента figi от источника source.

        Обычная минута ("append") сразу агрегируется во все активные старшие
        ТФ figi. Замена/вставка ("replace"/"insert") перестраивают производные
        ряды из 1m-истории целиком — коррекция задним числом не оставляет
        расхождений между ТФ. "dup"/"reject" ряды не трогают.
        Возвращает действие (см. CandleSeries.push_1m).
        """
        s1 = self._series.get((figi, 60)) or self.series(figi, "1min")
        action = s1.push_1m(candle, source=source, fire=fire)
        if action in ("replace", "insert"):
            self._rebuild_derived(figi, notify=fire)
        elif action == "append":
            for t in sorted(t for f, t in self._series if f == figi and t > 60):
                self._series[(figi, t)].push_1m(candle, source=source, fire=fire)
        return action

    def seed_1m(self, figi: str, candles: Iterable[Candle], *,
                source: str = "db", fire: bool = False) -> None:
        """Батч: докачка дыр / прогрев истории / коррекция из источника source.

        Один merge в 1m-ряд (валидация, приоритет источников, дедуп) и одна
        перестройка производных рядов figi — батч любой длины стоит
        O(история), а не O(батч × история). Производные строятся и когда ряд
        холодный (подписка позже данных), и когда merge что-то изменил.
        Идемпотентен: повторный seed тем же источником — no-op.
        fire=True — прокинуть on_rebuilt (докачка в live-режиме).
        """
        s1 = self._series.get((figi, 60)) or self.series(figi, "1min")
        changed = s1.merge_1m(candles, source=source, notify=fire)
        if changed or self._has_cold_derived(figi):
            self._rebuild_derived(figi, notify=fire)

    def gap_report(
        self, figi: str,
        *,
        is_trading_minute: Callable[[datetime], bool] | None = None,
    ) -> list[tuple[datetime, datetime]]:
        """Дыры 1m-истории figi — что докачивать: [(первая_минута, последняя_минута)].

        Минута = close-time отсутствующей 1m-свечи (интервал (m-60, m]).
        Дыры ищутся строго МЕЖДУ первой и последней закрытой свечой ряда:
        что было до первой свечи — движку неизвестно (глубину истории и
        стартовую докачку задаёт оркестратор).

        is_trading_minute — календарь сессий от вызывающего (движок ничего не
        знает о расписаниях): False → минута не считается дырой (клиринг,
        ночь, выходной). Без календаря отчёт «сырой»: любая отсутствующая
        минута — дыра. Диапазон = подряд идущие отсутствующие минуты;
        неторговые минуты внутри диапазона не разрывают его (докачка
        диапазоном; если внутри нет ни одной торгуемой минуты, REST просто
        вернёт пусто). С календарём стоимость O(длина интервала истории).
        """
        s = self._series.get((figi, 60))
        if s is None:
            return []
        hist = s.snapshot()
        if len(hist) < 2:
            return []
        out: list[tuple[datetime, datetime]] = []
        cur: list[datetime] | None = None

        def _flush() -> None:
            nonlocal cur
            if cur is not None:
                out.append((cur[0], cur[1]))
                cur = None

        for a, b in zip(hist, hist[1:]):
            if (b.ts - a.ts).total_seconds() <= 60:
                continue
            if is_trading_minute is None:
                out.append((a.ts + timedelta(seconds=60),
                            b.ts - timedelta(seconds=60)))
                continue
            m = a.ts + timedelta(seconds=60)
            while m < b.ts:
                if is_trading_minute(m):
                    if cur is None:
                        cur = [m, m]
                    elif (m - cur[1]).total_seconds() <= 60:
                        cur[1] = m
                    else:
                        _flush()
                        cur = [m, m]
                m += timedelta(seconds=60)
        _flush()
        return out

    def finalize(self, figi: str | None = None,
                 tf: str | int | None = None) -> list[Candle]:
        """Принудительно закрыть partial-бары (EOD-флаш, конец реплея).

        figi=None — все инструменты; tf=None — все ТФ инструмента.
        Возвращает закрытые бары (для лога/аудита).
        """
        tf_sec = tf_to_seconds(tf) if tf is not None else None
        out: list[Candle] = []
        for (f, t), s in sorted(self._series.items()):
            if figi is not None and f != figi:
                continue
            if tf_sec is not None and t != tf_sec:
                continue
            c = s.finalize()
            if c is not None:
                out.append(c)
        return out

    # --- внутреннее ------------------------------------------------------
    def _has_cold_derived(self, figi: str) -> bool:
        """Есть ли у figi производные ряды, которые ещё не строились."""
        return any(
            len(s) == 0 and s.partial is None
            for (f, t), s in self._series.items() if f == figi and t > 60
        )

    def _rebuild_derived(self, figi: str, *, notify: bool = True) -> None:
        """Перестроить все старшие ТФ figi из 1m-истории (тихо, затем on_rebuilt)."""
        s1 = self._series.get((figi, 60))
        if s1 is None:
            return
        hist = s1.snapshot()
        for t in sorted(t for f, t in self._series if f == figi and t > 60):
            self._series[(figi, t)].rebuild_from_1m(hist, notify=notify)
