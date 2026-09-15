"""IMOEX guard — защита новых входов от всплесков индекса MOEX.

Идея: пока индекс IMOEX резко двигается (всплеск), акции входят против него
(например, SHORT при взлёте индекса) и такие входы системно убыточны.
Guard считает ход индекса за окно `window_min` минут по 1м свечам:

    pts = close(now) - close(now - window_min)
    pct = pts / close(now - window_min) * 100

- Активация (всплеск): |pct| >= on_pct или (on_points > 0 и |pts| >= on_points).
- Пока активен UP-всплеск — новые входы SELL против индекса запрещены;
  DOWN-всплеск — запрещены BUY. Входы ПО направлению индекса разрешены.
- Стабилизация (release): ход затухает ниже off_pct (и |pts| ниже off_points),
  но не раньше, чем через min_block_min минут после активации.
- Смена знака всплеска — мгновенная переактивация в противоположную сторону.

Существующие позиции не трогаются: они выходят по своим сигналам/SL/TP.

Модуль чистый (без БД/сети) — тестируется юнит-тестами.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class ImoexGuardState:
    active: int = 0  # +1 = всплеск вверх, -1 = вниз, 0 = нет
    since: datetime | None = None  # когда активирован текущий всплеск
    activated_move: float = 0.0  # pts на момент активации
    move: float = 0.0  # последний ход, пункты
    pct: float = 0.0  # последний ход, %
    updated: datetime | None = None
    activations: int = 0
    releases: int = 0
    blocks: int = 0  # сколько входов заблокировано (инкрементит runtime)


def move_at(
    buf: list[tuple[datetime, float]],
    now: datetime,
    window_min: int,
    tol_min: float = 2.0,
) -> tuple[float, float, datetime, datetime] | None:
    """Ход индекса за окно. `buf` — [(ts, close)], отсортирован по ts.

    Возвращает (pts, pct, ts_now, ts_ref) или None, если данных мало.
    Берём последний бар <= now и последний бар <= now - window_min.
    """
    if not buf:
        return None
    i = len(buf) - 1
    while i >= 0 and buf[i][0] > now:
        i -= 1
    if i < 0:
        return None
    ref_deadline = now - timedelta(minutes=window_min)
    j = i
    while j >= 0 and buf[j][0] > ref_deadline:
        j -= 1
    if j < 0:
        return None
    span = (buf[i][0] - buf[j][0]).total_seconds() / 60.0
    if span < window_min - tol_min:
        return None
    pts = buf[i][1] - buf[j][1]
    pct = (pts / buf[j][1] * 100.0) if buf[j][1] else 0.0
    return pts, pct, buf[i][0], buf[j][0]


def block_for(
    state: ImoexGuardState | None,
    side: str,
    beta: float | None = None,
    min_beta: float = 0.0,
    chase_pct: float = 0.0,
) -> str | None:
    """Причина блокировки входа, или None.

    - UP-всплеск: блок SELL; DOWN: блок BUY (контр-входы против индекса).
    - `min_beta > 0` — блокировать только бумаги с beta >= min_beta (остальным
      контр-входы разрешены — mean-reversion по слабозависимым).
    - `chase_pct > 0` — второй уровень: при |ходе| >= chase_pct блокировать и входы
      ПО индексу («догон» — forward-доходность после экстремума отрицательна).
    """
    if state is None or not state.active:
        return None
    if min_beta > 0 and (beta is None or float(beta) < min_beta):
        return None
    _side = str(side).upper()
    if state.active > 0:
        if _side == "SELL":
            return f"IMOEX UP {state.pct:+.2f}% ({state.move:+.1f}п)"
        if chase_pct > 0 and state.pct >= chase_pct and _side == "BUY":
            return f"IMOEX UP chase {state.pct:+.2f}% ({state.move:+.1f}п)"
    else:
        if _side == "BUY":
            return f"IMOEX DOWN {state.pct:+.2f}% ({state.move:+.1f}п)"
        if chase_pct > 0 and state.pct <= -chase_pct and _side == "SELL":
            return f"IMOEX DOWN chase {state.pct:+.2f}% ({state.move:+.1f}п)"
    return None


def step(
    state: ImoexGuardState,
    pts: float,
    pct: float,
    now: datetime,
    on_pct: float = 0.8,
    off_pct: float | None = None,
    on_points: float = 0.0,
    off_points: float = 0.0,
    min_block_min: float = 5.0,
) -> str | None:
    """Обновить состояние guard'а по текущему ходу индекса.

    Возвращает событие: "activate_up" | "activate_down" | "release" | None.
    """
    if off_pct is None:
        off_pct = on_pct * 0.5
    state.move = float(pts)
    state.pct = float(pct)
    state.updated = now

    up = pct >= on_pct or (on_points > 0 and pts >= on_points)
    dn = pct <= -on_pct or (on_points > 0 and pts <= -on_points)

    if state.active == 0:
        if up:
            state.active = 1
            state.since = now
            state.activated_move = float(pts)
            state.activations += 1
            return "activate_up"
        if dn:
            state.active = -1
            state.since = now
            state.activated_move = float(pts)
            state.activations += 1
            return "activate_down"
        return None

    # Активен UP-всплеск.
    if state.active > 0:
        if dn:
            state.active = -1
            state.since = now
            state.activated_move = float(pts)
            state.activations += 1
            return "activate_down"
        calm = pct <= off_pct and (off_points <= 0 or pts <= off_points)
        elapsed_ok = (now - (state.since or now)) >= timedelta(minutes=min_block_min)
        if calm and elapsed_ok:
            state.active = 0
            state.releases += 1
            return "release"
        return None

    # Активен DOWN-всплеск.
    if up:
        state.active = 1
        state.since = now
        state.activated_move = float(pts)
        state.activations += 1
        return "activate_up"
    calm = pct >= -off_pct and (off_points <= 0 or pts >= -off_points)
    elapsed_ok = (now - (state.since or now)) >= timedelta(minutes=min_block_min)
    if calm and elapsed_ok:
        state.active = 0
        state.releases += 1
        return "release"
    return None
