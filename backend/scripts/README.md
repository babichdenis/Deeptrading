# backend/scripts/ — конвенция (2026-10-01)

Каталог содержит **289 скриптов**: воспроизводимые research-пайплайны,
эксплуатационные утилиты и их результаты. Разделение на
`app/` (production) / `research/` / `tools/` из аудита
(`docs/roadmap/PROJECT_AUDIT_2026-09-30.md`, P1.4) **ещё не выполнено** —
это большой рефакторинг, см. «Что нельзя делать механически» ниже.

## Что уже сделано (P1.4)

- Временный мусор вынесен из git и добавлен в `.gitignore`:
  `tmp_*.py`, `_tmp_*.py`, `*.jsonl.old`, `*.jsonl.bak`.
  Файлы на диске не удалены — только перестали отслеживаться.
- `backend/reports/` (749 MB) — в git отслеживается **один** файл;
  остальное локальное и untracked. Это правильное состояние:
  большие артефакты прогонов не в репозитории.

## Почему `results_*.json` остаются рядом со скриптами

Файлы `results_optuna_params.json`, `results_optuna_votes.json`,
`results_optuna_regime.json`, `results_blind_test.json`,
`results_mc_champion.json`, `results_wf.json` — **path-coupled**: скрипты
читают и перезаписывают их через `os.path.join(HERE, "<name>.json")`
(`optuna_params_sim.py:506`, `wf_sim.py:65`, `mc_champion.py:35-36`,
`blind_test.py:433`, `optuna_votes_sim.py:577`, `optuna_regime_sim.py:904`).

Перенос в подкаталог без правки путей **сломает пайплайны** (они перезапишут
файл в старом месте, и «золотые» параметры разъедутся). Размеры небольшие
(4–150 KB) — по духу аудита это «короткий summary в git», что допустимо.

## Правило для новых скриптов

- Новый research-пайплайн кладётся в `backend/research/<topic>/`,
  результаты — рядом с ним или в `artifacts/` (вне git).
- Утилиты эксплуатации — в `backend/tools/`.
- Скрипт, который пишет большой JSON дамп, обязан писать его **вне git**
  (в `artifacts/` или в `backend/reports/`, untracked).
- Реестр скриптов ведётся в `docs/roadmap/SCRIPTS_INDEX.md` — при добавлении
  нового файла обновлять его.

## Чего делать нельзя механически

Массовый `git mv` скриптов в `research/`/`tools/` сломает:

1. `docs/roadmap/SCRIPTS_INDEX.md` — реестр ссылается на ~180 путей;
2. внутренние импорты вида `from <скрипт> import ...` и `sys.path`-хаки;
3. `docs/results/*.md`, `chat.md`, `main_plan.md` — ссылки на скрипты в отчётах.

Такой перенос — отдельная задача с characterization-тестами, а не уборка.