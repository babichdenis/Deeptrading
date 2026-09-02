Ты работаешь в режиме GLOBAL_DISCOVERY.

Твоя задача:
искать мировой опыт, академические методы, GitHub-репозитории,
open-source библиотеки, успешные архитектурные паттерны
и методы исследования, которые потенциально применимы
к нашему торговому проекту.

Ты обязан исследовать три независимых горизонта:

A. INTRADAY MICRO / 5–60 минут
B. INTRADAY TREND / 30–240 минут
C. SWING / 2–5 торговых дней

Также исследуй cross-cutting направления:
- regime detection;
- market/index context, включая IMOEX;
- volume and liquidity;
- order-book / L2 / order-flow;
- execution and market impact;
- exit policy;
- position sizing;
- portfolio allocation;
- meta-labeling;
- walk-forward validation;
- multiple-testing control;
- risk overlays;
- cross-sectional ranking;
- correlation and sector exposure.

Источники:
- official GitHub repositories;
- официальная документация библиотек;
- академические papers / SSRN / NBER / arXiv;
- проверенные книги и reproducing repositories;
- биржевые или брокерские технические документы.

Запрещено:
- выдавать торговые сигналы BUY/SELL;
- заявлять, что метод обеспечит прибыль;
- рекомендовать live trading;
- переносить код из источника без адаптации;
- использовать oracle/future information как live feature;
- подбирать параметры на текущем OOS периоде;
- рекомендовать нейросеть в execution path;
- смешивать intraday и swing результаты в одну equity curve;
- предлагать любой метод без реалистичного transaction cost и execution analysis.

Для каждого найденного подхода подготовь запись:

{
  "id": "WORLD-H-XXX",
  "track": "intraday_micro|intraday_trend|swing_2_5d|cross_cutting",
  "method_name": "",
  "one_sentence_description": "",
  "source_type": "github|paper|book|official_docs",
  "source_title": "",
  "source_url": "",
  "source_relevance": "",
  "mechanism": "",
  "required_data": [],
  "available_now": [],
  "missing_data": [],
  "fit_to_current_architecture": "high|medium|low",
  "implementation_scope": "telemetry_only|shadow_research|new_strategy_family",
  "risk_of_overfit": "low|medium|high",
  "lookahead_risk": "",
  "cost_execution_risk": "",
  "minimum_validation_design": "",
  "expected_trade_frequency_effect": "",
  "prerequisites": [],
  "not_to_do": [],
  "priority": "P0|P1|P2",
  "status": "research_only|blocked_by_data|ready_for_design"
}

Нужно:
- найти 5–10 методов на каждый из трёх горизонтов;
- отдельно перечислить 5–10 cross-cutting методов;
- подтвердить каждый метод ссылкой;
- не выдумывать performance claims;
- отделять доказанный академический эффект от идеи реализации;
- указывать, применим ли метод к MOEX-акциям и имеющимся данным;
- отмечать, где нужны IMOEX, L2, volume, corporate actions,
  borrow availability, funding/overnight costs или earnings calendar.

В конце:
1. Составь Research Map.
2. Выбери не более трёх P0 направлений:
   одно intraday,
   одно intraday trend,
   одно swing.
3. Для каждого P0 создай pre-registration design,
   но НЕ создавай кодовую задачу.
4. Составь список данных, которые нужно сначала собрать.
5. Назови методы, которые сейчас опасно внедрять.