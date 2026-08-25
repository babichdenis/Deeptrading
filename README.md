# Deeptrading

Анализ рынка акций (Т-Банк / Т-Инвестиции) и основа для торгового робота.

Стек: **Python 3.11 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · T-Invest SDK** +
**Vite · TypeScript · lightweight-charts**

## Структура

```
backend/               FastAPI-приложение
├── app/
│   ├── main.py              # точка входа
│   ├── config.py            # настройки (.env)
│   ├── database.py          # async engine, сессии
│   ├── models/              # ORM: Instrument, Candle
│   ├── schemas/             # Pydantic-схемы
│   ├── services/
│   │   ├── tinvest.py       # загрузка данных с биржи
│   │   └── indicators.py    # SMA, EMA, MACD, RSI
│   └── api/routes/          # instruments, candles, analysis
├── tests/
├── requirements.txt
└── .env

frontend/              Vite + TypeScript
└── src/
    ├── main.ts              # график (свечи + SMA + MACD)
    ├── api.ts               # клиент API
    └── style.css            # тёмная тема в стиле TradingView

docker-compose.yml     PostgreSQL (можно запускать на отдельной машине)
```

## Запуск бэкенда

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

В `backend/.env`:

```
POSTGRES_HOST=<ip машины с БД>
TINKOFF_TOKEN=<токен Т-Инвестиций>
```

Swagger: http://localhost:8000/docs

## Запуск фронтенда

```bash
cd frontend
npm install
npm run dev
```

Если бэкенд не на той же машине:

```bash
BACKEND_URL=http://<ip-backend>:8000 npm run dev
```

UI: http://localhost:5173

## API

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/instruments/sync` | Загрузить список акций с биржи |
| GET | `/api/instruments?search=` | Список акций (поиск, пагинация) |
| POST | `/api/candles/{figi}/sync` | Скачать историю свечей (`interval_name`, `days`) |
| GET | `/api/candles/{figi}` | Свечи из БД |
| GET | `/api/analysis/{figi}` | Свечи + SMA20 + MACD(12,26,9) |

Интервалы: `1min, 5min, 10min, 15min, hour, 2h, 4h, day, week, month`

## Дорожная карта

- [x] Загрузка инструментов и истории свечей
- [x] График с таймфреймами, SMA, MACD
- [ ] Поиск тикера в UI (сейчас захардкожен SBER)
- [ ] Больше индикаторов: RSI, Bollinger, объёмные профили
- [ ] Статистика доходностей, корреляции
- [ ] Backtest движок
- [ ] Торговый робот (сигналы + выставление заявок через T-Invest API)
