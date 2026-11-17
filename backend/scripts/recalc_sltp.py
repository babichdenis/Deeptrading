"""Пересчёт испорченных SL/TP в paper_positions по штатной ATR-политике.

Позиции, открытые 15.11.2026 01:56–02:04, получили stop_pct=50.0 из конфига
(проценты на месте доли): ASTR sl=10873.71 при 213.17, SBER sl=-13552.42 и т.д.
Уровни неисполнимые, то есть позиции без защиты. Скрипт пересчитывает их тем же
путём, каким бот считает стоп при входе (AtrStopPolicy на 5м барах, множитель
initial_sl_atr, rr из atr_risk_reward), и пишет пары было/стало в отчёт.

Это НЕ clamp: значение не поджимается к границе, а вычисляется заново политикой
от фактической цены входа. Уже нормальные уровни (< 50% от входа) не трогаются.

Запуск:
  .venv\\Scripts\\python.exe scripts\\recalc_sltp.py            # только показать
  .venv\\Scripts\\python.exe scripts\\recalc_sltp.py --apply    # записать в БД
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.engine.exits import AtrStopPolicy  # noqa: E402
from app.engine.models import Candle, Side  # noqa: E402
from app.models.candle import Candle as CandleRow  # noqa: E402
from app.models.paper import PaperPosition  # noqa: E402
from app.services.ensemble import resample  # noqa: E402

ATR_PERIOD = 14
ATR_MULT = 4.0
RISK_REWARD = 4.0
MAX_OFF_SHARE = 0.5  # дальше половины входа — уровень точно битый


async def bars_for(figi: str, limit: int = 1500) -> list[Candle]:
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(CandleRow).where(CandleRow.figi == figi, CandleRow.interval == 1)
            .order_by(CandleRow.ts.desc()).limit(limit)
        )).scalars().all()
    rows = list(reversed(rows))
    if not rows:
        return []
    return [Candle(ts=r.ts, open=float(r.open), high=float(r.high),
                   low=float(r.low), close=float(r.close), volume=int(r.volume or 0))
            for r in rows]


async def main(apply: bool) -> None:
    lines: list[str] = []
    async with SessionLocal() as db:
        poss = (await db.execute(select(PaperPosition).order_by(PaperPosition.id))).scalars().all()
        for p in poss:
            entry = float(p.entry_price)
            side = Side.BUY if str(p.side).upper() in ("BUY", "LONG") else Side.SELL
            cur_sl = float(p.stop_loss) if p.stop_loss is not None else None
            cur_tp = float(p.take_profit) if p.take_profit is not None else None
            off = abs(cur_sl - entry) if cur_sl is not None else 0.0
            if cur_sl is not None and off <= entry * MAX_OFF_SHARE:
                lines.append(f"{p.ticker}: уровни в норме, не трогаю (sl={cur_sl:.2f})")
                continue
            bars = await bars_for(p.figi)
            bars5 = resample(bars, 300) if bars else []
            if not bars5:
                lines.append(f"{p.ticker}: нет баров для пересчёта, пропуск")
                continue
            pol = AtrStopPolicy(period=ATR_PERIOD, multiplier=ATR_MULT, risk_reward=RISK_REWARD)
            plan = pol.plan_entry(side, entry, bars5)
            lines.append(
                f"{p.ticker} {p.side} entry={entry:.2f}: "
                f"SL {cur_sl:.2f} -> {plan.stop_loss:.2f}, "
                f"TP {cur_tp if cur_tp is None else round(cur_tp, 2)} -> "
                f"{None if plan.take_profit is None else round(plan.take_profit, 2)}")
            if apply:
                p.stop_loss = round(float(plan.stop_loss), 6)
                p.take_profit = round(float(plan.take_profit), 6) if plan.take_profit is not None else None
        if apply:
            await db.commit()
            lines.append("ЗАПИСАНО в paper_positions")
        else:
            lines.append("режим просмотра (--apply не задан)")
    text = "\n".join(lines)
    print(text)
    out = Path(__file__).resolve().parents[1] / "reports" / "affected" / "sltp_20261115" / "recalc.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Пересчёт SL/TP после дефекта stop_pct=50.0\n\n" + text + "\n", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
