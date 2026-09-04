"""Уведомления оркестратора.

Основной канал: REST-мост в бэкенд (event_bus -> WebSocket фронту) — работает
без внешнего IP и Telegram.
Дополнительно: Telegram (если задан токен и прокси). Поллинг Telegram
обязан работать в главном потоке своего процесса (иначе падает
add_signal_handler), поэтому бот запускается отдельным процессом
`python3 -m orchestrator bot`, а не фоновым потоком внутри демона/agent.
"""
import asyncio
import json
import os
import subprocess
import sys

from . import config


class Notifier:
    def __init__(self):
        self.bot = None
        self._loop = None
        self._last_category = "info"
        self._last_notified_key = None
        if config.TELEGRAM_BOT_TOKEN:
            try:
                from telegram import Bot
                bot_kwargs = {}
                if config.TELEGRAM_API_BASE:
                    # PTB собирает URL как `base_url + token`; дефолтный base_url
                    # уже содержит `/bot`. Добавляем суффикс, чтобы прокси-воркер
                    # получал путь /bot<token>/method (иначе токен "склеивается"
                    # с хостом и даёт InvalidURL/Invalid port).
                    bot_kwargs["base_url"] = config.TELEGRAM_API_BASE.rstrip("/") + "/bot"
                self.bot = Bot(token=config.TELEGRAM_BOT_TOKEN, **bot_kwargs)
            except Exception:
                self.bot = None

    def _get_loop(self):
        if self._loop is None or self._loop.is_closed():
            import threading
            self._loop = asyncio.new_event_loop()
            t = threading.Thread(target=self._loop.run_forever, daemon=True)
            t.start()
        return self._loop

    def _send_tg_direct(self, text):
        import urllib.request
        token = config.TELEGRAM_BOT_TOKEN
        chat_id = config.TELEGRAM_CHAT_ID
        relay = config.TELEGRAM_API_BASE
        if not all([token, chat_id, relay]):
            return False
        try:
            url = relay.rstrip("/") + "/bot" + token + "/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": text}).encode()
            req = urllib.request.Request(url, data, {
                "Content-Type": "application/json",
                "X-Relay-Secret": getattr(config, "TELEGRAM_RELAY_SECRET", "") or "",
            })
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())
            return result.get("ok", False)
        except Exception as e:
            print("[TG:ERR] %s: %s" % (type(e).__name__, e))
            return False

    def _send(self, text, reply_markup=None):
        # 1) REST-мост в бэкенд (event_bus -> WS фронту)
        try:
            import urllib.request as _ur
            body = json.dumps({
                "text": text,
                "category": self._last_category,
                "reply_markup": (reply_markup.to_dict() if reply_markup else None),
            }).encode()
            req = _ur.Request(
                f"{config.ORCHESTRATOR_API_BASE}/api/v1/orchestrator/message", body,
                {"Content-Type": "application/json"}, method="POST")
            _ur.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[WS:ERR] {type(e).__name__}: {e}")
        # 2) Telegram (опционально)
        if self.bot and config.TELEGRAM_CHAT_ID:
            try:
                loop = self._get_loop()
                fut = asyncio.run_coroutine_threadsafe(
                    self.bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID, text=text, reply_markup=reply_markup
                    ),
                    loop,
                )
                fut.result(timeout=30)
                return True
            except Exception as e:
                print(f"[TG:ERR] {type(e).__name__}: {e} (fallback to console)")
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            try:
                if self._send_tg_direct(text):
                    return True
            except Exception:
                pass
        print(f"[TG] {text}")
        return False

    def send(self, text, category="info"):
        self._last_category = category
        prefix = {
            "done": "🟢", "new": "🆕", "decision": "⚠️",
            "signal": "📈", "info": "ℹ️",
        }.get(category, "ℹ️")
        if category != "decision":
            try:
                from . import state as _st
                if _st.load_control().get("muted"):
                    return
            except Exception:
                pass
        # дедуп: не шлём подряд одинаковые сообщения (анти-спам в цикле опроса)
        key = f"{category}|{text}"
        if getattr(self, "_last_sent_key", None) == key:
            return
        self._last_sent_key = key
        self._send(f"{prefix} {text}")

    def clear_decision_notify(self):
        self._last_notified_key = None

    def notify_signal(self, text):
        self.send(text, category="signal")

    def notify_decision_needed(self, run_id, question, options):
        # дедупликация: одно уведомление на одно pending_decision (анти-спам)
        key = f"{run_id}|{question}"
        if getattr(self, "_last_notified_key", None) == key:
            return
        self._last_notified_key = key
        text = (
            f"⚠️ {question}\nrun_id={run_id}\n"
            f"Решите: /approve или /reject (либо кнопки ниже)."
        )
        if self.bot and config.TELEGRAM_CHAT_ID:
            try:
                from telegram import (
                    InlineKeyboardButton,
                    InlineKeyboardMarkup,
                )
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Утвердить", callback_data="approve"),
                     InlineKeyboardButton("❌ Отклонить", callback_data="reject")]
                ])
                self._send(text, reply_markup=kb)
                return
            except Exception:
                pass
        self._send(text)

    def _build_app(self):
        from telegram.ext import (
            Application, CommandHandler, CallbackQueryHandler,
        )
        from . import state, notifier as nmod

        app = Application.builder().bot(self.bot).build()

        def fmt(s):
            return (
                f"📊 STATUS\n"
                f"last: {s['last_completed_by']} — {s['last_task']}\n"
                f"next_owner: {s['next_action_owner']}\n"
                f"experiment: {s['active_running_experiment']}\n"
                f"action: {s['next_action']}"
            )

        async def status_cmd(u, c):
            await u.message.reply_text(fmt(state.load()))

        async def chatid_cmd(u, c):
            await u.message.reply_text(f"chat_id: {u.message.chat.id}")

        async def pause_cmd(u, c):
            ctrl = state.load_control(); ctrl["paused"] = True
            state.save_control(ctrl)
            await u.message.reply_text("⏸ Пауза: диспетчер приостановлен.")

        async def resume_cmd(u, c):
            ctrl = state.load_control(); ctrl["paused"] = False
            state.save_control(ctrl)
            await u.message.reply_text("▶️ Возобновлено.")

        async def mute_cmd(u, c):
            ctrl = state.load_control(); ctrl["muted"] = True
            state.save_control(ctrl)
            await u.message.reply_text("🔇 Мьют включён.")

        async def unmute_cmd(u, c):
            ctrl = state.load_control(); ctrl["muted"] = False
            state.save_control(ctrl)
            await u.message.reply_text("🔊 Мьют выключен.")

        async def approve_cmd(u, c):
            nmod.push_decision("approve")
            await u.message.reply_text("✅ approve → decisions.json")

        async def reject_cmd(u, c):
            nmod.push_decision("reject")
            await u.message.reply_text("❌ reject → decisions.json")

        async def cb(u, c):
            if u.callback_query is None:
                return
            data = u.callback_query.data
            if data in ("approve", "reject"):
                try:
                    with open(config.BOT_LOG_PATH, "a", encoding="utf-8") as lf:
                        lf.write(f"[cb] received {data}\n")
                except Exception:
                    pass
                nmod.push_decision(data)
                if u.effective_message is not None:
                    try:
                        await u.effective_message.reply_text(f"✅ {data} → decisions.json")
                    except Exception:
                        pass
            if u.callback_query is not None:
                try:
                    await u.callback_query.answer()
                except Exception:
                    pass  # некритично: решение уже записано в push_decision

        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("chatid", chatid_cmd))
        app.add_handler(CommandHandler("pause", pause_cmd))
        app.add_handler(CommandHandler("resume", resume_cmd))
        app.add_handler(CommandHandler("mute", mute_cmd))
        app.add_handler(CommandHandler("unmute", unmute_cmd))
        app.add_handler(CommandHandler("approve", approve_cmd))
        app.add_handler(CommandHandler("reject", reject_cmd))
        app.add_handler(CallbackQueryHandler(cb))

        # Диагностический лог КАЖДОГО входящего апдейта (тип + id).
        from telegram.ext import MessageHandler, filters

        async def _log_update(u, c):
            try:
                with open(config.BOT_LOG_PATH, "a", encoding="utf-8") as lf:
                    kind = "cbq" if u.callback_query else ("msg" if u.message else "other")
                    lf.write(f"[upd] id={u.update_id} kind={kind}\n")
            except Exception:
                pass

        app.add_handler(MessageHandler(filters.ALL, _log_update), group=99)
        app.add_handler(CallbackQueryHandler(_log_update), group=99)
        return app

    def start_bot(self):
        # Запуск в ГЛАВНОМ потоке (команда `orchestrator bot`).
        mode = (config.TELEGRAM_MODE or "polling").lower()
        if mode == "webhook":
            self._start_bot_webhook()
        elif mode == "relay":
            asyncio.run(self._start_bot_relay())
        else:
            self._start_bot_polling()

    def _start_bot_polling(self):
        # run_polling ставит signal-обработчики — работает только в main thread.
        try:
            self._build_app().run_polling(timeout=10, poll_interval=1.0)
        except Exception as e:
            try:
                with open(config.BOT_LOG_PATH, "a", encoding="utf-8") as lf:
                    lf.write(f"[bot] run_polling error: {type(e).__name__}: {e}\n")
            except Exception:
                pass
            raise

    def _start_bot_relay(self):
        # Store-and-forward: Telegram -> воркер (KV) -> локальный бот забирает по pull.
        # Не требует публичного IP/туннеля; работает через стабильный outbound-HTTPS.
        import httpx
        from telegram import Update

        base = (config.TELEGRAM_RELAY_BASE or "").rstrip("/")
        secret = config.TELEGRAM_RELAY_SECRET
        if not base or not secret:
            print("[TG] relay режим требует TELEGRAM_RELAY_BASE и TELEGRAM_RELAY_SECRET")
            return
        webhook_url = f"{base}/webhook/{secret}"
        pull_url = f"{base}/pull/{secret}"

        async def _run():
            app = self._build_app()
            await app.initialize()
            await app.start()
            try:
                try:
                    await app.bot.set_webhook(
                        url=webhook_url,
                        allowed_updates=["message", "callback_query", "edited_message"],
                        drop_pending_updates=True,
                    )
                    print(f"[TG] webhook релея установлен: {webhook_url}")
                except Exception as e:
                    print(f"[TG] не удалось set_webhook релея: {e}")
                async with httpx.AsyncClient(timeout=httpx.Timeout(70.0)) as client:
                    while True:
                        try:
                            r = await client.get(pull_url)
                            if r.status_code == 200:
                                body = r.text
                                if not body:
                                    # пустое тело — KV вернул null (конкурентный
                                    # pull / eventual consistency). Считаем «нет апдейта».
                                    continue
                                try:
                                    update = Update.de_json(json.loads(body), app.bot)
                                    await app.process_update(update)
                                except Exception as e:
                                    print(f"[TG] ошибка обработки апдейта: {e}")
                            elif r.status_code == 204:
                                await asyncio.sleep(1)  # нет апдейтов — пауза, чтобы не долбить воркер
                            else:
                                print(f"[TG] pull вернул {r.status_code}")
                                await asyncio.sleep(2)
                        except Exception as e:
                            print(f"[TG] relay poll error: {e}")
                            await asyncio.sleep(2)
            finally:
                try:
                    await app.bot.delete_webhook()
                except Exception:
                    pass
                await app.stop()
                await app.shutdown()

        asyncio.run(_run())

    def _start_bot_webhook(self):
        import subprocess
        import time
        import re
        import urllib.request

        PORT = config.TELEGRAM_WEBHOOK_PORT
        path = "/tgbot"
        cf_log = "/tmp/cloudflared.log"
        try:
            with open(cf_log, "w", encoding="utf-8") as f:
                f.write("")
        except Exception:
            pass
        cf = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORT}",
             "--no-autoupdate", "--protocol", "http2"],
            stdout=open(cf_log, "a"),
            stderr=subprocess.STDOUT,
        )
        try:
            public_url = None
            for _ in range(40):
                try:
                    with open(cf_log, "r", encoding="utf-8") as f:
                        text = f.read()
                    m = re.search(r"https://[A-Za-z0-9\-]+\.trycloudflare\.com", text)
                    if m:
                        public_url = m.group(0)
                        break
                except Exception:
                    pass
                time.sleep(1)
            if not public_url:
                print("[TG] cloudflared-туннель не поднялся (нет сети?)")
                try:
                    with open(cf_log) as f:
                        print(f"[TG] cloudflared лог: {f.read()[-500:]}")
                except Exception:
                    pass
                return
            webhook_url = public_url.rstrip("/") + path
            print(f"[TG] туннель: {webhook_url}; регистрирую webhook в Telegram (ждём DNS)...")
            time.sleep(10)
            last_err = None
            for attempt in range(8):
                try:
                    app = self._build_app()
                    app.run_webhook(
                        listen="127.0.0.1", port=PORT, url_path=path,
                        webhook_url=webhook_url,
                        allowed_updates=["message", "callback_query", "edited_message"],
                        drop_pending_updates=True,
                    )
                    break
                except Exception as e:
                    last_err = e
                    print(f"[TG] run_webhook попытка {attempt+1}: {type(e).__name__}: {e}; повтор через 5с")
                    time.sleep(5)
            else:
                print(f"[TG] webhook-сервер не поднялся: {last_err}")
                return
        finally:
            try:
                cf.terminate()
            except Exception:
                pass


def ensure_bot_running():
    """Поднять единый процесс Telegram-бота, если он ещё не крутится.

    Бот всегда — отдельный процесс (главный поток), чтобы polling работал.
    И демон, и `orchestrator request` вызывают эту функцию; благодаря pid-файлу
    гарантируется ровно один бот-процесс на токен.

    Возвращает pid запущенного здесь бота, либо None, если бот уже был живым
    (его не нужно останавливать вызвавшему). Вызывающий (request) сам убивает
    бота при выходе, только если получил реальный pid — чтобы не гасить чужой
    (демоновский) бот.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    pidfile = config.BOT_PID_PATH
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # живой?
            return None
        except (OSError, ValueError):
            try:
                os.remove(pidfile)
            except OSError:
                pass
    try:
        # Relay-режим (store-and-forward через воркер) — единственный надёжный
        # путь инbound на этой машине (ngrok заблокирован, cloudflared-туннель
        # нестабилен). Если релей не настроен, откатываемся в polling.
        bot_mode = (
            "relay"
            if (config.TELEGRAM_RELAY_BASE and config.TELEGRAM_RELAY_SECRET)
            else "polling"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", "bot"],
            cwd=config.BACKEND_DIR,
            stdout=open(config.BOT_LOG_PATH, "a"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "TELEGRAM_MODE": bot_mode},
        )
        with open(pidfile, "w") as f:
            f.write(str(proc.pid))
        return proc.pid
    except Exception as e:
        print(f"[TG] не удалось запустить бота: {e}")
        return None


def push_decision(choice, run_id=None):
    data = {"choice": choice, "run_id": run_id}
    with open(config.DECISIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    # Если агент ждёт апрува через `orchestrator request`, продублировать решение
    # в изолированный канал agent_decision.json (помеченный agent_pending.json).
    try:
        if os.path.exists(config.AGENT_PENDING_PATH):
            with open(config.AGENT_DECISION_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.remove(config.AGENT_PENDING_PATH)
    except Exception:
        pass


def read_decision():
    # В режиме демона решение принимается только через decisions.json
    # (CLI `respond approve` / Telegram-бот / внешние агенты). Интерактивный
    # input() намеренно убран — в сервисе без TTY он блокирует процесс.
    if os.path.exists(config.DECISIONS_PATH):
        with open(config.DECISIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(config.DECISIONS_PATH)
        return data.get("choice")
    return None


_singleton = None


def _instance():
    global _singleton
    if _singleton is None:
        _singleton = Notifier()
    return _singleton


def send(text, category="info"):
    return _instance().send(text, category)


def notify_decision_needed(run_id, question, options):
    return _instance().notify_decision_needed(run_id, question, options)


def notify_signal(text):
    return _instance().notify_signal(text)
