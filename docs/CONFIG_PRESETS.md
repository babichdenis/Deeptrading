# CONFIG_PRESETS — «ветки» конфигураций (харнесс ↔ replay ↔ live)

> Один JSON = полная конфигурация теста. Мгновенно превращается в harness-спеку, в нагрузку
> `/bot/mode` (проектный replay) и (позже) в live-настройки. Аналитика однозначно видит,
> ЧТО тестировалось и с какими параметрами — через сайдкар пресета.

## Зачем

- **Не искать параметры по env/.env/сейвам.** Всё в одном файле: роботы, период, ТФ, сессии,
  деньги/маржа, SL/TP, bias/entry, кворум, overnight, гейты, режимы, издержки.
- **Мгновенно переставлять**: тот же пресет гоняем в харнессе и запускаем в replay одной командой.
- **Однозначная идентификация в аналитике**: тест создаётся с именем `<preset.id> <timestamp>`
  (перезапуск = новый тест, история сохраняется), а рядом кладётся сайдкар
  `backend/reports/presets/<test_name>.json` — аналитика показывает настройки тегами.

## Схема

```json
{
  "preset":  {"id": "mtf-rsi-v1", "name": "RSI MTF (1h bias → 10m)", "created": "2026-09-30",
              "notes": "..."},
  "harness": {                        // что и как гоняем (bt_ose_sweep spec v1)
    "robots": [{"robot": "rsi_mtf_hub", "params": {}}],
    "timeframe": "10min",
    "universe": ["SBER", "..."],
    "period": ["2026-09-01", "2026-09-24"],
    "costs": {"commission": 0.0005, "slippage_bps": 2.0, "qty": 1, "capital": 100000},
    "exits": ["x07"],
    "filters": {"min_trades": 10, "min_net_pnl": 0},
    "wf": {"iterations": 4, "percent_oos": 25, "top_k": 2, "rank_by": "net_pnl",
           "robustness": {"delta_pct": 20, "period_days": 10}}
  },
  "runtime": {                        // чем бы торговал replay/live
    "sessions": ["morning", "day", "evening"],
    "overnight": false,
    "money": {"initial_cash": 100000, "qty_per_trade": 1, "pos_pct": 0.4, "max_positions": 5},
    "exits": {"sl_mode": "atr", "atr_period": 14, "initial_sl_atr": 4.0,
              "trail_activation_comm_mult": null},
    "entry": {"quorum": null, "confirm_flip": 2, "cooldown_bars": 15,
              "gates": ["imoex_guard", "confirm_flip", "loss_streak", "rank_queue"]},
    "bias": {"enabled": false, "tf": "hour", "period": 50},
    "regimes": "all"
  },
  "targets": {"replay": {"engine": "rsi_mtf_hub", "interval": "10min", "pace": "fast"}}
}
```

## Команды (`scripts/preset.py`)

```
.venv/bin/python scripts/preset.py tags    configs/presets/mtf-rsi-v1.json
.venv/bin/python scripts/preset.py to-spec configs/presets/mtf-rsi-v1.json   # спеку в configs/ose_runs/
.venv/bin/python scripts/preset.py replay  configs/presets/mtf-rsi-v1.json --start
```

- `tags` — человекочитаемая сводка (то, что должно быть тегами в аналитике).
- `to-spec` — harness-спека для `bt_ose_sweep.py real|wf`.
- `replay` — payload для `/bot/mode` + сайдкар; `--start` сразу запускает проектный тест
  (имя `<id> <YYYYMMDD-HHMM>` — новый тест на каждый запуск).

## Интерфейс с аналитикой (контракт)

1. **Имя теста:** `<preset.id> <YYYYMMDD-HHMM>` — уникально на каждый запуск.
2. **Сайдкар:** `backend/reports/presets/<test_name с заменой недопустимых символов>.json` —
   `{"preset": {...}, "payload": {...}, "created_utc": "..."}`.
3. Аналитика (см. `docs/ANALYTICS_TAB.md`, «Этап 2») читает сайдкар и рисует теги:
   роботы+параметры / период / ТФ / сессии / SL/TP / капитал / маржа-лимиты / bias / entry /
   кворум-голоса / overnight / гейты / режимы / издержки.
4. Нет сайдкара — теги деградируют до того, что удаётся вытащить из `bot_logs`/payload.

## Роадмап

- [x] v1: схема, `tags`, `to-spec`, `replay` (+сайдкар, уникальные имена).
- [ ] Применение `runtime`-блока одной кнопкой: расширить `/bot/mode` полем `preset`
      (env `TEST_PRESET`) и whitelist-применение полей в `apply_test_overrides` (сессии,
      overnight, деньги, SL/TP, гейты, bias/кворум) — сейчас payload несёт только
      engine/interval/params, остальное живёт в сейве/envi.
- [ ] `preset.py live` — выдача/применение live-настроек (отдельный осторожный этап).
- [ ] Пре-флайт валидация пресета (схема + сверка с карточками каталога).
