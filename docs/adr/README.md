# ADR — Architecture Decision Records (канон решений)

Каждое принятое архитектурное решение — отдельный файл `NNNN-краткое-имя.md`
с шаблоном: **Контекст → Решение → Альтернативы → Следствия**.

Зачем: `MEMORY.md` хранит статус и договорённости, `ROADMAP.md` — план,
а **почему** архитектура именно такая — здесь. Иначе решения теряются в чате.

Индекс решений — внизу этого файла.

Правила:
- Решение не переписывается задним числом: новое решение = новый ADR со ссылкой
  на заменённое (`Supersedes: NNNN`).
- Правку вносит владелец либо архитектор (агент `architect`).

## Индекс

| ADR | Дата | Решение | Статус |
|---|---|---|---|
| [0001](0001-hermetic-baseline-ci.md) | 2026-10-01 | Hermetic baseline для CI: тесты разделены на hermetic / `artifact` / `integration`; CI гоняет только hermetic (инвариант — 0 failed) | принято |
| [0002](0002-timeframe-semantics.md) | 2026-10-01 | Семантика таймфреймов: метка = начало бакета; видимость по закрытию `bar.ts + TF <= as_of`; forming-бар недоступен | принято |
| [0003](0003-regime-contract.md) | 2026-10-02 | Regime Contract: оси direction/trend_strength/volatility/structure + confidence, forward-таргеты по смыслу оси, hysteresis после state model, legacy adapter на 5 строк `trade_regimes` | предложено |
