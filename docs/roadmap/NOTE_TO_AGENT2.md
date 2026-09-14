# Агенту №2 (на `.2`) — от агента на `.3`

## 1. Токен, который «просится» в логах

В `C:\Users\nadts\Dev\Deeptrading\backend\.env` строка **`tinkoff_token=` пустая**.
Поэтому всё, что ходит в T-Invest API (скачивание истории, live-фид), падает/просит токен.

**Но для ML токен не нужен вообще:**
- `scripts/ml_train_csv.py` — читает CSV (`uid;ts;open;close;high;low;volume;`) из файлов, сеть не трогает;
- `scripts/run_v4_mltest.py` — читает свечи из БД (PostgreSQL на `192.168.1.3`), T-Invest не трогает.

Запуск ML (без токена):
```bash
cd C:\Users\nadts\Dev\Deeptrading\backend
.venv\Scripts\python.exe scripts\ml_train_csv.py ^
    --csv-dir /tmp/hist --extra-dir /tmp/hist/2025 ^
    --train-from 2025-08-01 --train-to 2026-05-31 ^
    --val-from 2026-06-01 --val-to 2026-06-30 ^
    --oos-from 2026-07-01 --oos-to 2026-08-25
```
Если CSV-архивов на `.2` нет — их надо скачать скриптами `scripts/download_*.py`,
а вот им уже нужен `TINKOFF_TOKEN` (вписать в `.env`).

## 2. Что я сделал на `.3` (забрать себе: `git fetch central && git merge central/second`)

- `GET /api/v1/bot/state` — единый снапшот (позиции+цены+SL/TP+P&L+режим+алерты);
- `POST /api/v1/bot/positions/levels` `{"ticker":"SMLT","sl":304,"tp":292}` — ручные SL/TP
  (память + БД; при рестарте уровни читаются из БД);
- intrabar-проверка SL/TP между барами (`_intrabar_exit_loop`, `intrabar_check_sec=10`);
- `scripts/watch_positions.py` — мониторинг позиций + алерты;
- режим-гейт до ансамбля (не считаем ансамбль, если режим запрещён `trade_regimes`);
- IMOEX: автозагрузка 1м свечей (`app/bot/moex.py`, ISS index/SNDX);
- `docs/roadmap/HOWTO_STRATEGIES.md` — как менять стратегии и гонять ML.

## 3. UI

UI на `.2` (вкладка «📈 Статистика») я синхронизировал с `.3` — CSS-блок перенесён,
`bot.ts`/`api.ts`/`index.html` не трогал (они совпадали). Больше UI не трогаю без согласования.

## 4. Просьбы

- Не менять `data/ensemble_config.json` на `.3` (там боевой бот) — тесты на `.2`.
- Если правишь `bot.ts`/`style.css` — предупреди, я синхронизирую вручную (у нас разные ветки).
