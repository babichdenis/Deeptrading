# Task: Ensemble vote behavior at oracle entry/exit (shadow) — B5b

## Goal

Измерить, как вёл себя ансамбль на точках входа/выхода оракула (July 2026):
для КАЖДОЙ из 7 функций зафиксировать направление голоса, силу голоса в момент
решения и на коротком горизонте вперёд/назад (1 / 2 / 3 / 5 бар).

Это data-collection (read-only). Не менять:
- EngineRunner / entry logic / exit policy / quorum / cooldown / session
- CostModel / sizing / ML
- фактические trade results

Использовать только canonical baseline (July 2026) + point-in-time переоценку функций.

## Data

Вход:
- trades.csv (July 2026, 517 сделок): entry_time, exit_time, side, exit_type ∈ {target, stop_loss, signal_exit}
- dataset_v1: intent_vote_features.csv, vote_shadow_scores.csv (per-intent vote features на decision-баре)
- движок: определения 7 функций для point-in-time переоценки на любом баре
- IMOEX + per-FIGI 1m и 5m свечи (point-in-time, без look-ahead)

## Method

### 1. Извлечение голоса функции

Для функции f и бара b определить:
```text
vote_direction(f, b) ∈ {long, short, neutral}
vote_strength(f, b)   ∈ ℝ  (нормированная магнитуда голоса функции f на баре b)
```
- На decision-баре (t0 входа / tE выхода): ground-truth из intent_vote_features.csv / vote_shadow_scores.csv.
- На forward/backward барах: переоценить f на point-in-time состоянии (без утечки) — см. Tests.

### 2. Точка входа «5+1» (для 5m таймфрейма)

Сигнал стратегии — 5m, исполнение — next 1m open. Уточнить входную точку:
```text
t0        = 5m signal-бар (decision)
t0_exec   = следующий 1m open (фактическое исполнение)
```
Зафиксировать vote_direction/strength на t0 И на t0_exec (1m бар исполнения).

### 3. Окна вокруг точек оракула

Для КАЖДОЙ сделки и КАЖДОЙ функции f:

**На входе** (reference = t0, 5m бары):
```text
at t0,        t0+1, t0+2, t0+3, t0+5   → vote_direction(f), vote_strength(f)
плюс at t0_exec (1m)                    → vote_direction(f), vote_strength(f)
```

**На выходе** (reference = tE, 5m бары):
```text
at tE-5, tE-3, tE-1, tE                → vote_direction(f), vote_strength(f)
```
(назад — чтобы увидеть, предвещали ли голоса выход; особенно signal_exit).

### 4. Агрегации (по всем 517 сделкам)

- Распределение vote_direction каждой функции на t0 vs исход сделки (win/loss) и vs exit_type.
- Динамика vote_strength winning-направления функции от t0 до t0+5: растёт ли у winners vs падает у losers?
- Консенсус на входе: число согласных функций + средняя сила → vs исход (win/loss, net/trade).
- На выходе: росла ли сила «exit-функций» (напр. signal_exit / противонаправленные) за tE-5..tE перед
  signal_exit-выходами (vs target/stop)?
- Корреляция vote_strength на t0 с последующим MFE/MAE.

### 5. Сравнение с B5

B5 смотрит на КОНТЕКСТ рынка (IMOEX trend regime). B5b смотрит на ВНУТРЕННЕЕ состояние
ансамбля (голоса функций). Вместе дают «что видел рынок» + «что видел ансамбль» на точках решений.

## Output

- reports/{run_id}/entry_exit_vote_behavior.json
  - per-function direction distribution на t0 / tE
  - consensus-strength vs outcome (win/loss, net/trade)
  - strength-dynamics t0..t0+5 (winners vs losers)
  - signal_exit foreshadow check (tE-5..tE)
- reports/{run_id}/entry_exit_vote_audit.csv
  - строки: trade_id, figi, side, exit_type, function, window(rel_bar), vote_direction, vote_strength, outcome(net)
- reports/{run_id}/entry_exit_vote_methodology.md
  - определение vote_strength, point-in-time checks, окна, ограничения

## Tests

- все vote_strength / vote_direction для бара b используют ТОЛЬКО состояние ≤ b (без look-ahead)
- t0_exec строго после t0 (next 1m open)
- no oracle data в output
- no EngineRunner decision changes
- end-of-data handling корректна

## Conclusion

Одно из:
- SUPPORTED_FOR_FUTURE_EXPERIMENT
- INCONCLUSIVE
- REJECTED
