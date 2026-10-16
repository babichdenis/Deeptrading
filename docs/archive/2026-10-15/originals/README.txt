Как стартовать новую сессию
DeepSeek
В новую сессию достаточно отправить:
smb://MacBook Pro Den._smb._tcp.local/Dev/Deeptrading
text
Прочитай в таком порядке:

1. docs/agents/DEEPSEEK_EXECUTOR.md
2. docs/agents/RESEARCH_WORKFLOW.md
3. docs/PROJECT_STATE.md
4. [ссылка/путь на текущую задачу]

Подтверди:
- current baseline;
- effective capital;
- current run_id;
- что запрещено менять;
- что именно будет изменено в этой задаче.

Не пиши код, пока не покажешь короткий план и список файлов.
My3
text
Прочитай в таком порядке:

1. docs/agents/MY3_RESEARCH_LEAD.md
2. docs/agents/MY3_GLOBAL_DISCOVERY.md, если MODE=DISCOVERY
3. docs/agents/RESEARCH_WORKFLOW.md
4. docs/PROJECT_STATE.md
5. reports/{run_id}/manifest.json
6. reports/{run_id}/research_pack.json
7. последний отчёт DeepSeek.

MODE: AUDIT / DISCOVERY / EXPERIMENT_DESIGN.

Сначала верни:
- какой baseline ты видишь;
- что сейчас заблокировано;
- последний run и его статус;
- один разрешённый следующий шаг.
Не предлагай изменения до этого confirmation.
Главное правило
Новая сессия должна проходить так:

text
instructions
→ project state
→ specific artifacts
→ confirmation
→ task
А не:

text
новый чат
→ «сделай стратегию лучше»
Кто обновляет PROJECT_STATE.md
Лучше так:

DeepSeek обновляет только технический раздел:

text
tests, commits, artifacts, open bugs, status task
My3 готовит новый текст research status:

text
latest hypothesis, blocked directions, approved next experiment
Вы или мы подтверждаем итоговую версию.

Чтобы агент сам не переписал историю под красивый результат, добавить правило:

text
PROJECT_STATE.md изменяется только после завершённого run
или официально утверждённого experiment plan.
Коротко
Нет, им не нужно каждый раз пересказывать всю историю. Нужны:

text
Постоянные правила:
docs/agents/*.md

Текущий handoff:
docs/PROJECT_STATE.md

Доказательства:
reports/{run_id}/*

Конкретная задача:
docs/experiments/EXP-XXX.md
Тогда даже новый агент или новая модель сможет за 2–3 минуты восстановить контекст, не потеряв важные ограничения и не начав снова случайно менять quorum, cooldown или session policy.