"""Уведомления и Telegram-бот оркестратора.

Два назначения:
1. Доставка сообщений владельцу в Telegram (через Cloudflare-прокси).
2. Приём входящих из Telegram: кнопки апрува (✅/❌) и переписка
   владельца с агентами (пересылается в их opencode-сессии, ответ —
   обратно в Telegram).

Режимы получения входящих (TELEGRAM_MODE):
  relay    — store-and-forward через Cloudflare Worker (работает в РФ, без VPN)
  polling  — долгий опрос напрямую (вне РФ)
  webhook  — локальный webhook + туннель (нестабильно в РФ, не рекомендуется)
"""
import asyncio
import json
import os
import re
import subprocess
import sys

from . import config


class Notifier:
    def __init__(self):
        self.bot = None
        self._loop = None
        self._last_category = "info"
        self._last_sent_key = None
        self._last_notified_key = None
        self.targets = self._load_targets()
        if config.TELEGRAM_BOT_TOKEN:
            try:
                from telegram import Bot
                bot_kwargs = {}
                if config.TELEGRAM_API_BASE:
                    bot_kwargs["base_url"] = config.TELEGRAM_API_BASE.rstrip("/") + "/bot"
                self.bot = Bot(token=config.TELEGRAM_BOT_TOKEN, **bot_kwargs)
            except Exception:
                self.bot = None

    # ---------- отправка владельцу ----------
    def _send_to_bridge(self, text, reply_markup):
        # Локальный REST/WS-мост оркестратора (опционален). Молча игнорируем,
        # если он не запущен — это не влияет на Telegram-канал.
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
        except Exception:
            pass

    def _send(self, text, reply_markup=None):
        self._send_to_bridge(text, reply_markup)
        if self.bot and config.TELEGRAM_CHAT_ID:
            try:
                loop = self._get_loop()
                fut = asyncio.run_coroutine_threadsafe(
                    self.bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID, text=text, reply_markup=reply_markup),
                    loop)
                fut.result(timeout=30)
                return True
            except Exception as e:
                print(f"[TG:ERR] {type(e).__name__}: {e}")
        print(f"[TG] {text}")
        return False

    def send(self, text, category="info"):
        self._last_category = category
        key = f"{category}|{text}"
        if getattr(self, "_last_sent_key", None) == key:
            return
        self._last_sent_key = key
        self._send(f"{_prefix(category)} {text}")

    # ---------- апрув владельца ----------
    def notify_decision_needed(self, run_id, question, options, document_path=None):
        key = f"{run_id}|{question}"
        if getattr(self, "_last_notified_key", None) == key:
            return
        self._last_notified_key = key
        text = f"⚠️ {question}\nrun_id={run_id}\nРешите кнопками ниже."
        reply_markup = _approval_kb(options)
        if document_path and os.path.exists(document_path):
            self._send_document_with_kb(document_path, text, reply_markup)
        else:
            self._send(text, reply_markup)

    def _send_document_with_kb(self, path, caption, reply_markup):
        if self.bot and config.TELEGRAM_CHAT_ID:
            try:
                from telegram import InputFile
                loop = self._get_loop()
                fut = asyncio.run_coroutine_threadsafe(
                    self.bot.send_document(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        document=InputFile(path), caption=caption,
                        reply_markup=reply_markup),
                    loop)
                fut.result(timeout=30)
                return
            except Exception as e:
                print(f"[TG:ERR] document: {type(e).__name__}: {e}")
        self._send(caption, reply_markup)

    def push_decision(self, choice, run_id=None):
        data = {"choice": choice, "run_id": run_id}
        with open(config.DECISIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        try:
            if os.path.exists(config.AGENT_PENDING_PATH):
                with open(config.AGENT_DECISION_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.remove(config.AGENT_PENDING_PATH)
        except Exception:
            pass

    def read_decision(self):
        if os.path.exists(config.DECISIONS_PATH):
            try:
                with open(config.DECISIONS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(config.DECISIONS_PATH)
                return data.get("choice")
            except (OSError, ValueError):
                pass
        return None

    def clear_decision_notify(self):
        self._last_notified_key = None

    # ---------- главное меню (выбор агента) ----------
    def _agent_names(self):
        from . import opencode_client
        return list(opencode_client.load_registry().keys())

    def _main_keyboard(self):
        from telegram import ReplyKeyboardMarkup, KeyboardButton
        names = self._agent_names() or ["executor", "architect"]
        rows = [[KeyboardButton(f"✏️ {n}")] for n in names]
        rows.append([KeyboardButton("✅ Апрув"), KeyboardButton("❌ Отклонить")])
        rows.append([KeyboardButton("ℹ️ Статус")])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    def _load_targets(self):
        try:
            with open(config.AGENT_TARGET_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_targets(self):
        try:
            with open(config.AGENT_TARGET_PATH, "w", encoding="utf-8") as f:
                json.dump(self.targets, f, ensure_ascii=False)
        except OSError:
            pass

    def _set_target(self, chat_id, role):
        self.targets[str(chat_id)] = role
        self._save_targets()

    def _target_for(self, chat_id):
        return self.targets.get(str(chat_id)) or os.getenv("DEFAULT_AGENT", "executor")

    # ---------- взаимодействие с агентами ----------
    def ask_agent(self, role, text):
        """Спросить агента (opencode-сессию) и вернуть текст ответа.

        Вопрос оборачивается TG_ORIGIN_WRAP, чтобы агент понимал: запрос
        пришёл из Telegram и отвечать надо лаконично (ответ уйдёт в Telegram).
        """
        from . import opencode_client
        client, sid = opencode_client.resolve_agent(role)
        if client is None or sid is None:
            return f"⚠️ Нет сессии для роли '{role}' (проверь agents_registry.json)."
        try:
            from . import agents as _agents
            resp = client.ask(sid, _agents.wrap_inbound(text, "telegram"))
        except Exception as e:
            return f"⚠️ Ошибка обращения к '{role}': {e}"
        return opencode_client._extract_text(resp)

    def save_document(self, doc, role_hint="agent"):
        """Сохранить присланный владельцем .md в папку reports/."""
        os.makedirs(config.REPORTS_DIR, exist_ok=True)
        import time
        fname = f"report_{role_hint}_{int(time.time())}.md"
        dst = os.path.join(config.REPORTS_DIR, fname)
        try:
            with open(doc, "rb") as f:
                data = f.read()
            with open(dst, "wb") as f:
                f.write(data)
        except Exception as e:
            return None, f"⚠️ не удалось сохранить файл: {e}"
        return dst, fname

    # ---------- процесс бота ----------
    def _get_loop(self):
        if self._loop is None or self._loop.is_closed():
            import threading
            self._loop = asyncio.new_event_loop()
            t = threading.Thread(target=self._loop.run_forever, daemon=True)
            t.start()
        return self._loop

    def _is_our_bot(self, pid):
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="], stderr=subprocess.DEVNULL
            ).decode(errors="ignore")
            return "orchestrator bot" in out or "orchestrator/__main__.py bot" in out
        except Exception:
            return False

    def start_bot(self):
        pidf = config.BOT_PID_PATH
        if os.path.exists(pidf):
            try:
                old = int(open(pidf).read().strip())
                os.kill(old, 0)
                if self._is_our_bot(old):
                    print(f"[TG] бот уже запущен (pid {old}); второй не стартую")
                    return
            except (OSError, ValueError):
                pass
        with open(pidf, "w") as f:
            f.write(str(os.getpid()))
        try:
            mode = (config.TELEGRAM_MODE or "polling").lower()
            if mode == "webhook":
                asyncio.run(self._start_bot_webhook())
            elif mode == "relay":
                asyncio.run(self._start_bot_relay())
            else:
                self._start_bot_polling()
        finally:
            try:
                if os.path.exists(pidf) and open(pidf).read().strip() == str(os.getpid()):
                    os.remove(pidf)
            except OSError:
                pass

    def _build_app(self):
        from telegram.ext import Application
        return Application.builder().bot(self.bot).build()

    def _register_handlers(self, app):
        from telegram.ext import CommandHandler, MessageHandler, filters, CallbackQueryHandler
        app.add_handler(CommandHandler("approve", self._cmd_approve))
        app.add_handler(CommandHandler("reject", self._cmd_reject))
        app.add_handler(CommandHandler("to", self._cmd_to))
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("agents", self._cmd_agents))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CallbackQueryHandler(self._cb))
        app.add_handler(MessageHandler(filters.Document.ALL, self._on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

    def _start_bot_polling(self):
        app = self._build_app()
        self._register_handlers(app)
        app.run_polling(timeout=10, poll_interval=1.0)

    async def _start_bot_webhook(self):
        app = self._build_app()
        self._register_handlers(app)
        # (опционально) поднимает локальный сервер + туннель — нестабильно в РФ
        port = int(getattr(config, "TELEGRAM_WEBHOOK_PORT", 8443))
        public_url = os.getenv("PUBLIC_URL", "")
        await app.bot.set_webhook(url=public_url.rstrip("/") + f"/{config.TELEGRAM_BOT_TOKEN}")
        await app.start()
        await app.updater.start_webhook(listen="127.0.0.1", port=port,
                                        url_path=config.TELEGRAM_BOT_TOKEN,
                                        webhook_url=public_url.rstrip("/") + f"/{config.TELEGRAM_BOT_TOKEN}")
        await app.updater.idle()

    async def _start_bot_relay(self):
        import httpx
        from telegram import Update
        base = (config.TELEGRAM_RELAY_BASE or "").rstrip("/")
        secret = config.TELEGRAM_RELAY_SECRET or "relay"
        if not base:
            print("[TG] relay режим требует TELEGRAM_RELAY_BASE")
            return
        webhook_url = f"{base}/webhook/{secret}"
        pull_url = f"{base}/pull/{secret}"

        app = self._build_app()
        self._register_handlers(app)
        await app.initialize()
        await app.start()
        try:
            try:
                await app.bot.set_webhook(
                    url=webhook_url,
                    allowed_updates=["message", "callback_query", "edited_message", "document"],
                    drop_pending_updates=True)
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
                                continue
                            try:
                                update = Update.de_json(json.loads(body), app.bot)
                                await app.process_update(update)
                            except Exception as e:
                                print(f"[TG] ошибка обработки апдейта: {e}")
                        elif r.status_code == 204:
                            await asyncio.sleep(1)
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

    # ---------- обработчики ----------
    async def _cb(self, u, c):
        if u.callback_query is None:
            return
        data = u.callback_query.data
        if data in ("approve", "reject"):
            try:
                with open(config.BOT_LOG_PATH, "a", encoding="utf-8") as lf:
                    lf.write(f"[cb] received {data}\n")
            except Exception:
                pass
            self.push_decision(data)
            try:
                if u.effective_message is not None:
                    await u.effective_message.reply_text(f"✅ {data} → decisions.json")
            except Exception:
                pass
        if u.callback_query is not None:
            try:
                await u.callback_query.answer()
            except Exception:
                pass

    async def _cmd_approve(self, u, c):
        self.push_decision("approve")
        await u.message.reply_text("✅ approve")

    async def _cmd_reject(self, u, c):
        self.push_decision("reject")
        await u.message.reply_text("❌ reject")

    async def _cmd_start(self, u, c):
        await u.message.reply_text(
            "Главное меню: выбери агента (✏️ …), чтобы писать ему. "
            "«✅ Апрув / ❌ Отклонить» — решение, «ℹ️ Статус» — состояние.",
            reply_markup=self._main_keyboard())

    async def _cmd_to(self, u, c):
        parts = (u.message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await u.message.reply_text("Использование: /to <agent> <вопрос>")
            return
        role, text = parts[1], parts[2]
        await u.message.reply_text("⏳ спрашиваю агента…")
        answer = self.ask_agent(role, text)
        await u.message.reply_text(answer[:4000])

    async def _cmd_status(self, u, c):
        from . import state as _st
        s = _st.load()
        await u.message.reply_text(f"state: {json.dumps(s, ensure_ascii=False)[:3000]}")

    async def _cmd_agents(self, u, c):
        from . import opencode_client
        reg = opencode_client.load_registry()
        await u.message.reply_text("Агенты: " + json.dumps(reg, ensure_ascii=False))

    async def _cmd_help(self, u, c):
        await u.message.reply_text(
            "/to <agent> <вопрос> — спросить агента (ответ в Telegram)\n"
            "/approve /reject — решение (если ждётся)\n"
            "/status — состояние\n/agents — реестр агентов\n"
            "Просто напиши текст — он уйдёт агенту по умолчанию.\n"
            "Пришли .md-документ с подписью — сохраню в reports/ и передам агенту.")

    async def _on_text(self, u, c):
        if u.effective_chat is None or str(u.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
            return
        text = (u.message.text or "").strip()
        if not text:
            return
        kb = self._main_keyboard()
        key = text
        # нормализуем: убираем ведущие эмодзи/спецсимволы (✏️ / ✅ / ❌ / ℹ️ …)
        norm = re.sub(r"^[^0-9A-Za-zА-Яа-я]+", "", key).strip()

        # кнопка выбора агента
        if norm in self._agent_names():
            self._set_target(u.effective_chat.id, norm)
            await u.message.reply_text(
                f"✍️ Теперь пишешь агенту «{norm}». Сообщения пойдут ему.", reply_markup=kb)
            return

        # кнопки управления
        if norm == "Апрув":
            self.push_decision("approve")
            await u.message.reply_text("✅ approve", reply_markup=kb)
            return
        if norm == "Отклонить":
            self.push_decision("reject")
            await u.message.reply_text("❌ reject", reply_markup=kb)
            return
        if norm == "Статус":
            from . import state as _st
            await u.message.reply_text(
                "Текущий адресат: " + self._target_for(u.effective_chat.id) +
                "\n" + json.dumps(_st.load(), ensure_ascii=False)[:2000], reply_markup=kb)
            return

        # маршрутизация: "@role вопрос" или текущий выбранный агент
        role = self._target_for(u.effective_chat.id)
        if text.startswith("@") and " " in text:
            head, rest = text[1:].split(maxsplit=1)
            role, text = head, rest
        await u.message.reply_text("⏳ пересылаю агенту…", reply_markup=kb)
        answer = self.ask_agent(role, text)
        await u.message.reply_text(answer[:4000], reply_markup=kb)

    async def _on_document(self, u, c):
        if u.effective_chat is None or str(u.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
            return
        doc = u.message.document
        if doc is None or not doc.file_name.endswith(".md"):
            await u.message.reply_text("Поддерживаются только .md-документы.")
            return
        # скачиваем файл во временное место
        from telegram import InputFile
        tf = os.path.join(config.REPORTS_DIR, f".tmp_{int(__import__('time').time())}.md")
        fobj = await doc.get_file()
        await fobj.download_to_drive(custom_path=tf)
        caption = (u.message.caption or "").strip()
        role = _default_role()
        if caption.startswith("@") and " " in caption:
            head, rest = caption[1:].split(maxsplit=1)
            role, caption = head, rest
        dst, fname = self.save_document(tf, role)
        try:
            os.remove(tf)
        except OSError:
            pass
        if dst is None:
            await u.message.reply_text(fname)
            return
        # передаём агенту содержимое файла + вопрос (помечаем как из Telegram)
        with open(dst, "r", encoding="utf-8") as f:
            content = f.read()
        payload = f"{caption}\n\n--- содержимое файла {fname} ---\n{content[:6000]}"
        answer = self.ask_agent(role, payload)
        await u.message.reply_text(f"📥 Сохранено в reports/{fname} и отправлено агенту '{role}'.\n\n{answer[:2000]}")


def _prefix(category):
    return {
        "done": "🟢", "new": "🆕", "decision": "⚠️",
        "signal": "📈", "info": "ℹ️",
    }.get(category, "ℹ️")


def _approval_kb(options):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    labels = {"approve": "✅ Approve", "reject": "❌ Reject"}
    buttons = [InlineKeyboardButton(labels.get(o, o), callback_data=o) for o in options]
    return InlineKeyboardMarkup([[b] for b in buttons])


def _default_role():
    # агент по умолчанию для простых сообщений владельца
    return os.getenv("DEFAULT_AGENT", config.SESSION_EXECUTOR and "executor" or "architect")


def ensure_bot_running():
    """Поднять единый процесс бота, если он ещё не крутится. Возвращает pid
    только если бот поднят здесь (вызвавший сам его и остановит)."""
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    pidfile = config.BOT_PID_PATH
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return None
        except (OSError, ValueError):
            try:
                os.remove(pidfile)
            except OSError:
                pass
    try:
        bot_mode = (
            "relay"
            if (config.TELEGRAM_RELAY_BASE and config.TELEGRAM_RELAY_SECRET)
            else "polling"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", "bot"],
            cwd=config.PROJECT_ROOT,
            stdout=open(config.BOT_LOG_PATH, "a"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "TELEGRAM_MODE": bot_mode},
        )
        return proc.pid
    except Exception as e:
        print(f"[TG] не удалось запустить бота: {e}")
        return None
