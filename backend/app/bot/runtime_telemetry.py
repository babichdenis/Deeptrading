from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.bot.runtime_diagnostics import audit_swallow as _audit_swallow


class RuntimeTelemetryMixin:
    # ── часовой контекст ─────────────────────────────────────────────────────
    # Расчёт живёт в app.engine.hourly_context (архитектор), проводка и журнал —
    # в app.bot.hourly_runtime. Здесь только вход из рабочего цикла.

    HOURLY_MARKET_ID = "BBG00KDWPPW2"

    def _hourly_service(self):
        service = getattr(self, "_hourly_service_obj", None)
        if service is None:
            from app.bot.hourly_runtime import HourlyContextService, HourlyJournal

            service = HourlyContextService(
                market_id=str(getattr(self.config, "hourly_market_id", "") or self.HOURLY_MARKET_ID),
                journal=HourlyJournal(getattr(self.config, "hourly_journal_path", None)),
                clock=self._hourly_clock,
            )
            self._hourly_service_obj = service
        return service

    def _hourly_clock(self) -> datetime:
        """Часы цикла: в replay — время ленты, в live — системное."""
        now = self._bot_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    async def _hourly_universe(self, db) -> tuple[str, ...]:
        """Бумаги текущей вселенной бота, а не весь каталог инструментов.

        Часовой срез считается ровно по тем бумагам, которыми бот работает:
        подстановка всей таблицы instruments раздувала срез чужими бумагами
        и прятала факт пустой вселенной.
        """
        figis = tuple(
            str(u["figi"]) for u in (getattr(self, "universe", None) or [])
            if isinstance(u, dict) and u.get("figi")
        )
        return figis

    async def hourly_context_refresh(self) -> dict:
        """Собрать снимок часа один раз и вернуть его статус. Заявок не делает."""
        service = self._hourly_service()
        now = self._hourly_clock()
        async with self._database_session() as db:
            figis = await self._hourly_universe(db)
            try:
                await service.ensure(db=db, now=now, figis=figis)
            except Exception as exc:
                _audit_swallow("hourly_context_refresh@L35", exc)  # audit silent-except
                return {"state": "ERROR", "reason": str(exc)}
        return service.status(now=now)

    def hourly_context_status(self) -> dict:
        """Статус часового контекста без сборки: что реально доступно сейчас."""
        return self._hourly_service().status(now=self._hourly_clock())

    def hourly_context_restore(self) -> bool:
        """После перезапуска поднять только неистёкший снимок из журнала."""
        service = self._hourly_service()
        restored = service.restore(now=self._hourly_clock())
        return restored is not None

    async def sector_meta(self) -> dict:
        """{ticker: {sector, beta}} из instruments (кэш 10 мин)."""
        import time as _t
        if self._sector_meta and (_t.monotonic() - self._sector_meta_ts) < 600:
            return self._sector_meta
        try:
            from sqlalchemy import text as _text
            async with self._database_session() as db:
                rows = (await db.execute(_text(
                    "SELECT ticker, figi, coalesce(sector,'other'), coalesce(imoex_beta,0), "
                    "coalesce(dlong,0), coalesce(dshort,0) "
                    "FROM instruments WHERE sector IS NOT NULL OR imoex_beta IS NOT NULL"
                ))).all()
            _m: dict[str, dict] = {}
            for r in rows:
                _v = {"sector": str(r[2]), "beta": float(r[3] or 0.0),
                      "dlong": float(r[4] or 0.0), "dshort": float(r[5] or 0.0)}
                _m[str(r[0]).upper()] = _v
                if r[1]:
                    _m[str(r[1])] = _v
            self._sector_meta = _m
            self._sector_meta_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('sector_meta@L663', _sw_e)  # audit silent-except
            pass
        return self._sector_meta


    async def portfolio_snapshot(self, ttl: float = 10.0) -> dict:
        """Сводка портфеля: экспозиции/сектора/маржа/стресс (кэш ttl сек)."""
        import time as _t
        from app.bot.portfolio import snapshot as _snap
        if getattr(self.config, "feed", "live") != "replay" and self._pf_cache and (_t.monotonic() - self._pf_cache[0]) < ttl:
            return self._pf_cache[1]
        equity = 0.0
        positions: list[dict] = []
        margin: dict = {}
        try:
            equity = float(await self.broker.equity() or 0.0)
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L678', _sw_e)  # audit silent-except
            raise
        try:
            for p in (await self.broker.positions()):
                bb = self.tcs_to_bbg.get(getattr(p, "figi", ""), getattr(p, "figi", ""))
                buf = self.buffers.get(bb)
                last = float(buf[-1].close) if buf else float(getattr(p, "entry_price", 0) or 0)
                positions.append({
                    "ticker": str(getattr(p, "ticker", "") or self.tickers.get(bb, "")).upper(),
                    "figi": bb,
                    "side": "LONG" if str(getattr(p, "side", "")).upper() in ("LONG", "BUY") else "SHORT",
                    "qty": abs(float(getattr(p, "qty", 0) or 0)),
                    "entry": float(getattr(p, "entry_price", 0) or 0),
                    "last": last,
                })
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L693', _sw_e)  # audit silent-except
            raise
        _ma_fn = getattr(self.broker, "margin_attributes", None)
        if _ma_fn is not None:
            try:
                margin = await _ma_fn() or {}
            except Exception as _sw_e:
                _audit_swallow('portfolio_snapshot@L699', _sw_e)  # audit silent-except
                margin = {}
        meta = await self.sector_meta()
        snap = _snap(equity, positions, meta, margin)
        snap["skip_counts"] = self.get_no_trade_stats()
        try:
            await self.trade_history()
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L706', _sw_e)  # audit silent-except
            pass
        try:
            _mtfm = await self.mtf_macd_map()
            snap["mtf"] = {k: {"h1": (v.get("h1") or {}).get("side"),
                               "m5": (v.get("m5") or {}).get("side"),
                               "m5_trend": (v.get("m5") or {}).get("trend")}
                           for k, v in list(_mtfm.items())[:40]}
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L714', _sw_e)  # audit silent-except
            snap["mtf"] = {}
        try:
            _dbm = await self.daily_bias_map()
            snap["daily_bias"] = {k: v.get("bias") for k, v in list(_dbm.items())[:40]}
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L719', _sw_e)  # audit silent-except
            snap["daily_bias"] = {}
        try:
            snap["regime"] = await self.market_regime()
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L723', _sw_e)  # audit silent-except
            snap["regime"] = {}
        snap["queue"] = sorted(
            [{"ticker": c.get("ticker"), "side": c.get("side"), "score": c.get("score"),
              "factors": c.get("factors"), "veto": c.get("veto"), "hist": c.get("hist"),
              "why": c.get("why"), "ts": c.get("ts")} for c in self._cand_queue.values()],
            key=lambda x: x.get("score") or 0, reverse=True)[:10]
        try:
            _ranked = sorted(((k, float(v.get("net") or 0.0)) for k, v in (self._hist_cache or {}).items()
                              if int(v.get("n") or 0) >= int(getattr(self.config, "rank_min_hist", 5) or 0)),
                             key=lambda kv: -kv[1])
            snap["rank"] = {
                "enabled": bool(getattr(self.config, "rank_enabled", True)),
                "top_n": int(getattr(self.config, "rank_top_n", 10) or 0),
                "explore": int(getattr(self.config, "rank_explore", 10) or 0),
                "top": [k for k, _ in _ranked[:int(getattr(self.config, "rank_top_n", 10) or 0)]],
            }
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L740', _sw_e)  # audit silent-except
            snap["rank"] = {}
        snap["dd"] = {"peak": round(self._equity_peak, 2),
                      "dd_pct": round(max(0.0, (self._equity_peak - equity) / self._equity_peak), 4)
                      if self._equity_peak > 0 else 0.0,
                      "level_done": self._dd_level_done}
        self._pf_cache = (_t.monotonic(), snap)
        return snap


    async def market_regime(self, ttl: float = 30.0) -> dict:
        """Режим рынка: bear/bull/neutral/reversal по IMOEX (20м/60м/день) + breadth."""
        import time as _t
        if getattr(self.config, "feed", "live") != "replay" and self._regime_cache and (_t.monotonic() - self._regime_cache[0]) < ttl:
            return self._regime_cache[1]
        now = self._bot_now()
        last_v = self._imoex_buf[-1][1] if self._imoex_buf else None

        def _pct(mins: int) -> float | None:
            if last_v is None:
                return None
            target = now - timedelta(minutes=mins)
            ref = next((v for t, v in reversed(self._imoex_buf) if t <= target), None)
            return ((last_v - ref) / ref * 100) if ref else None

        p20, p60 = _pct(20), _pct(60)
        pday = None
        try:
            from zoneinfo import ZoneInfo as _ZI
            _msk = _ZI("Europe/Moscow")
            _day = now.astimezone(_msk).date()
            _dv = [v for t, v in self._imoex_buf if t.astimezone(_msk).date() == _day]
            if _dv and last_v:
                pday = (last_v - _dv[0]) / _dv[0] * 100
        except Exception as _sw_e:
            _audit_swallow('_pct@L773', _sw_e)  # audit silent-except
            pass
        br_up = None
        try:
            from app.api.routes.screener import market_breadth
            br_up = (market_breadth() or {}).get("up_pct")
        except Exception as _sw_e:
            _audit_swallow('_pct@L779', _sw_e)  # audit silent-except
            br_up = None
        st = "neutral"
        if p20 is not None and p60 is not None:
            if p60 <= -0.3 and p20 <= 0.1 and (br_up is None or br_up <= 50):
                st = "bear"
            elif p60 >= 0.3 and p20 >= -0.1 and (br_up is None or br_up >= 50):
                st = "bull"
            if (p60 <= -0.3 and p20 >= 0.25) or (p60 >= 0.3 and p20 <= -0.25):
                st = "reversal"
        out = {"state": st,
               "pct_20m": round(p20, 3) if p20 is not None else None,
               "pct_60m": round(p60, 3) if p60 is not None else None,
               "pct_day": round(pday, 3) if pday is not None else None,
               "breadth_up_pct": br_up,
               "ts": datetime.now(timezone.utc).isoformat()}
        self._regime_cache = (_t.monotonic(), out)
        return out


    async def trade_history(self) -> dict:
        import time as _t
        from sqlalchemy import select, text
        from app.models.sandbox_trade import SandboxTrade
        from app.bot.runtime import _account_scope_or_raise

        mode = getattr(self, "broker_mode", None)
        if mode not in ("live", "sandbox", "paper"):
            raise ValueError("trade history requires a known broker mode")
        replay = str(getattr(self.config, "feed", "")) == "replay"
        as_of = self._bot_now()
        if as_of.utcoffset() is None:
            raise ValueError("trade history requires an aware decision time")
        try:
            account = _account_scope_or_raise(self)
        except ValueError:
            self._hist_cache = {}
            self._hist_scope = None
            self._hist_ts = 0.0
            raise
        test_name = (getattr(self.config, "test_name", "") or None) if mode == "paper" else None
        scope = (mode, account, test_name, as_of if replay else None)
        if (getattr(self, "_hist_scope", None) == scope
                and (_t.monotonic() - self._hist_ts) < 600):
            return self._hist_cache
        query = select(SandboxTrade.ticker, SandboxTrade.net_pnl).where(
            SandboxTrade.mode == mode,
            SandboxTrade.broker_account_id == account,
            SandboxTrade.net_pnl.is_not(None),
            SandboxTrade.exit_time.is_not(None),
            SandboxTrade.exit_time <= as_of,
        ).order_by(SandboxTrade.exit_time.desc(), SandboxTrade.id.desc())
        if mode == "paper":
            query = query.where(SandboxTrade.test_name == test_name)
        try:
            async with self._database_session() as db:
                rows = (await db.execute(query)).all()
                uni = [] if replay else (await db.execute(text(
                    "SELECT ticker, avg_daily_turnover FROM universe"
                ))).all()
            out = {}
            for ticker, pnl in rows:
                tk = str(ticker or "").upper()
                if not tk:
                    continue
                value = float(pnl)
                record = out.setdefault(tk, {"n": 0, "wins": 0, "net": 0.0, "last5": []})
                record["n"] += 1
                record["net"] += value
                if value > 0:
                    record["wins"] += 1
                if len(record["last5"]) < 5:
                    record["last5"].append(1 if value > 0 else 0)
            for record in out.values():
                record["wr"] = record["wins"] / record["n"]
                record["wr5"] = sum(record["last5"]) / len(record["last5"])
                record.pop("last5")
            if not replay:
                self._turnover_cache = {str(row[0] or "").upper(): float(row[1] or 0) for row in uni}
                try:
                    from app.api.routes.screener import fetch_tqbr_market
                    quotes = await asyncio.to_thread(fetch_tqbr_market)
                    for ticker, row in (quotes or {}).items():
                        value = float(row.get("turnover") or 0)
                        if value > 0:
                            self._turnover_cache[str(ticker).upper()] = value
                except Exception as error:
                    _audit_swallow("trade_history:turnover", error)
            self._hist_cache = out
            self._hist_scope = scope
            self._hist_ts = _t.monotonic()
            return out
        except Exception:
            self._hist_cache = {}
            self._hist_scope = None
            self._hist_ts = 0.0
            raise

    async def daily_bias_map(self, ttl: float = 900.0) -> dict[str, dict]:
        """Дневной MACD-bias по тикерам (кэш 15 мин): {ticker: {bias, hist, bars}}.

        В реплее кэш отключён и берётся срез на момент реплея (иначе look-ahead).
        """
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if (not _replay and self._daily_bias_cache
                and (_t.monotonic() - self._daily_bias_ts) < ttl):
            return self._daily_bias_cache
        _cutoff = self._bot_now()
        from app.bot.daily_bias import bias_from_closes as _bfc
        out: dict[str, dict] = {}
        try:
            from sqlalchemy import text as _text
            async with self._database_session() as db:
                _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
                if not _figs:
                    return self._daily_bias_cache
                # Дневные бары (interval=24, качаются scripts/download_daily_candles.py)
                rows = (await db.execute(_text(
                    "SELECT figi, close FROM candles WHERE interval = 24 "
                    "AND figi = ANY(:fs) AND ts <= :cut ORDER BY figi, ts"
                ), {"fs": _figs, "cut": _cutoff})).all()
                if not rows:
                    # fallback: собираем дневные закрытия из 5м свечей
                    rows = (await db.execute(_text(
                        "SELECT figi, close FROM ("
                        "  SELECT figi, (ts AT TIME ZONE 'Europe/Moscow')::date AS d, ts, close,"
                        "         row_number() OVER (PARTITION BY figi,"
                        "                            (ts AT TIME ZONE 'Europe/Moscow')::date"
                        "                            ORDER BY ts DESC) AS rn"
                        "  FROM candles WHERE interval = 5 AND figi = ANY(:fs)"
                        "        AND ts <= :cut AND ts > :cut - interval '90 days'"
                        ") t WHERE rn = 1 ORDER BY figi, d"
                    ), {"fs": _figs, "cut": _cutoff})).all()
            by_figi: dict[str, list[float]] = {}
            for figi, close in rows:
                by_figi.setdefault(str(figi), []).append(float(close or 0.0))
            for figi, closes in by_figi.items():
                tk = str(self.tickers.get(figi, "") or "").upper()
                if not tk:
                    continue
                out[tk] = _bfc(closes)
            self._daily_bias_cache = out
            self._daily_bias_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('daily_bias_map@L894', _sw_e)  # audit silent-except
            pass
        return self._daily_bias_cache


    async def mtf_macd_map(self, ttl: float = 900.0) -> dict[str, dict]:
        """M5 и H1 MACD по тикерам (кэш 15 мин): {ticker: {m5: {...}, h1: {...}}}."""
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if not _replay and self._mtf_cache and (_t.monotonic() - self._mtf_ts) < ttl:
            return self._mtf_cache
        _cutoff = self._bot_now()
        from app.bot.daily_bias import macd_state as _ms
        out: dict[str, dict] = {}
        try:
            from sqlalchemy import text as _text
            _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if not _figs:
                return self._mtf_cache
            async with self._database_session() as db:
                rows = (await db.execute(_text(
                    "SELECT figi, ts, close FROM candles WHERE interval = 5 "
                    "AND figi = ANY(:fs) AND ts <= :cut "
                    "AND ts > :cut - interval '20 days' ORDER BY figi, ts"
                ), {"fs": _figs, "cut": _cutoff})).all()
            by_figi: dict[str, list[tuple]] = {}
            for figi, ts, close in rows:
                by_figi.setdefault(str(figi), []).append((ts, float(close or 0.0)))
            for figi, seq in by_figi.items():
                tk = str(self.tickers.get(figi, "") or "").upper()
                if not tk:
                    continue
                m5 = _ms([c for _ts, c in seq])
                # H1: последнее закрытие каждого часового бакета
                h1: list[float] = []
                bucket = None
                last_close = None
                for ts, c in seq:
                    b = ts.replace(minute=0, second=0, microsecond=0)
                    if bucket is not None and b != bucket and last_close is not None:
                        h1.append(last_close)
                    bucket, last_close = b, c
                if last_close is not None:
                    h1.append(last_close)
                out[tk] = {"m5": m5, "h1": _ms(h1)}
            self._mtf_cache = out
            self._mtf_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('mtf_macd_map@L940', _sw_e)  # audit silent-except
            pass
        return self._mtf_cache


    async def hm_map(self, window_h: int = 24, ttl: float = 180.0) -> dict[str, tuple]:
        """Накопленное движение за window_h часов по тикерам (heatmap-карта).

        Возвращает {ticker: (pct, dur)}:
          pct — % изменения цены за окно (часовые бары, закрытия из 1м свечей БД);
          dur — длительность тренда: сколько последних часов подряд цена шла в ту же
                сторону, что суммарное смещение за окна (для прогрессивного veto).
        """
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if not _replay and self._hm_cache and (_t.monotonic() - self._hm_ts) < ttl:
            return self._hm_cache.get(window_h) or {}
        _cut = self._bot_now()
        _frm = _cut - timedelta(hours=max(1, int(window_h)))
        out: dict[str, tuple] = {}
        try:
            from sqlalchemy import text as _text
            _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if _figs:
                async with self._database_session() as db:
                    rows = (await db.execute(_text(
                        "SELECT figi, date_trunc('hour', ts) AS h, "
                        "(array_agg(close ORDER BY ts DESC))[1] AS close "
                        "FROM candles WHERE interval = 1 AND figi = ANY(:fs) "
                        "AND ts >= :frm AND ts <= :cut "
                        "GROUP BY figi, h ORDER BY figi, h"
                    ), {"fs": _figs, "frm": _frm, "cut": _cut})).all()
                by: dict[str, list[float]] = {}
                for figi, _h, close in rows:
                    by.setdefault(str(figi), []).append(float(close or 0.0))
                for figi, closes in by.items():
                    tk = str(self.tickers.get(figi, "") or "").upper()
                    if not tk or len(closes) < 2:
                        continue
                    c0, c1 = closes[0], closes[-1]
                    if c0 <= 0:
                        continue
                    pct = round((c1 / c0 - 1.0) * 100.0, 3)
                    dur = 0
                    _sign = 1 if pct >= 0 else -1
                    for i in range(len(closes) - 1, 0, -1):
                        _ch = closes[i] - closes[i - 1]
                        if (_ch >= 0 and _sign >= 0) or (_ch < 0 and _sign < 0):
                            dur += 1
                        else:
                            break
                    out[tk] = (pct, dur)
                self._hm_cache = {}
                self._hm_cache[window_h] = out
                self._hm_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('hm_map', _sw_e)  # audit silent-except
            pass
        return out


    async def momentum_map(self, ttl: float = 1800.0) -> dict[str, float]:
        """{ticker: изменение за N дней, %} по дневным барам (кэш 30 мин)."""
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if not _replay and self._momentum_cache and (_t.monotonic() - self._momentum_ts) < ttl:
            return self._momentum_cache
        _n = max(2, int(getattr(self.config, "momentum_n", 63) or 63))
        _cutoff = self._bot_now()
        out: dict[str, float] = {}
        try:
            from sqlalchemy import text as _text
            _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if not _figs:
                return self._momentum_cache
            async with self._database_session() as db:
                rows = (await db.execute(_text(
                    "SELECT figi, close FROM candles WHERE interval = 24 "
                    "AND figi = ANY(:fs) AND ts <= :cut ORDER BY figi, ts"
                ), {"fs": _figs, "cut": _cutoff})).all()
            by: dict[str, list[float]] = {}
            for figi, close in rows:
                by.setdefault(str(figi), []).append(float(close or 0.0))
            for figi, closes in by.items():
                tk = str(self.tickers.get(figi, "") or "").upper()
                if not tk or len(closes) < _n + 1:
                    continue
                c0, c1 = closes[-_n - 1], closes[-1]
                if c0 > 0:
                    out[tk] = round((c1 / c0 - 1.0) * 100.0, 1)
            self._momentum_cache = out
            self._momentum_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('momentum_map@L975', _sw_e)  # audit silent-except
            pass
        return self._momentum_cache
