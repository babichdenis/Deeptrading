# TASK: H-077 v2 — allocation INVERSE-vol (size-up high-vol) + risk-budget

**От:** My3 · **Дата:** 2026-08-29 · **Контекст:** владелец указал, что раз уж high-vol несёт весь edge
(INSIGHT-002: H1 high-vol 11.68 ₽/сделку vs low-vol 0.56 ₽, p≈0), то капитал надо КОНЦЕНТРИРОВАТЬ в
high-vol, а не сжимать его (как в H-077 v1 vol-target, что уронило net на 22–30%).
**Статус:** SHADOW / pre-deployment (не меняет baseline, не деплоит — только симуляция). HARD CONSTRAINT
«не sizing-change до OOS» относится к деплою; здесь shadow-оценка.

**Методология (как в v1, `batch_block_E.py`):** day-capacity модель; trades = July+H1 (n=3481); disjoint;
last-closed bar; H-059 block-bootstrap MC. total 30k и 50k.

**ТРИ схемы сайзинга (сравнить с baseline fixed 10k и с v1 vol-target):**
- **A) baseline** — fixed 10k/поз (reference, из v1).
- **B) vol-tilt DISCRETE** — per-trade вес w: high-vol (ATR14>rolling median 20д) w=2.0, low-vol w=0.3;
  dry-powder 10%; суммарный нотионал ≤ total (без плеча).
- **C) vol-tilt CONTINUOUS** — w_i ∝ N-ATR_i (норм. волатильность, H-075), отмасштаб. так что median w=1;
  dry-powder 5%; суммарный нотионал ≤ total.
- **D) vol-tilt + RISK-BUDGET** — как C, НО доп. потолок: сумма position-$risk (size_i × ATR_i) ≤ 25% от
  capital одновременно (бордер на DD). Это отвечает на риск-оговорку владельца.

**Метрики (для каждой схемы и total):** n_taken / n_missed, portfolio_net, PF, max_DD, %idle,
max_funded_names, opportunity_cost_net, H-059 MC (rule vs baseline net%, rule_dd_better_p).
Плюс сравнение net/DD с v1 vol-target (ожидаем: net ВЫШЕ baseline, DD выше чем у v1 но в рамке budget D).

**Вопросы к ответу:**
1. Даёт ли vol-tilt (B/C) net ВЫШЕ baseline fixed (и на сколько %)?
2. Контролирует ли risk-budget D просадку при этом (max_DD в допустимых рамках vs baseline)?
3. Решает ли это concentration-ловушку (max_funded_names=5 при 30k) без потери net?

**Деливерабл:** `reports/batch_block_E2.json` + краткий `.md`. Не деплоить без отдельного OOS-прогона.
