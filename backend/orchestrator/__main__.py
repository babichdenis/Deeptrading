import os
import sys
import json
import time

# Минимальная версия — Python 3.6 (используются f-strings). Под Python 2
# пакет даже не импортируется нормально из-за синтаксиса, поэтому проверяем
# до импорта пакета и выдаём понятную ошибку вместо SyntaxError.
if sys.version_info[0] < 3:
    sys.stderr.write(
        "Orchestrator requires Python 3.6+ (f-strings used). "
        "You are running Python %s.\nRun with: python3 -m orchestrator\n" % sys.version.split()[0]
    )
    sys.exit(1)

from . import notifier, core, agents, state, config


def _read_arg(arg):
    if arg.startswith("@") and os.path.exists(arg[1:]):
        with open(arg[1:], "r", encoding="utf-8") as f:
            return f.read()
    return arg


def _cmd_dispatch(arg):
    text = _read_arg(arg) if arg else ""
    if not text:
        print("usage: python -m orchestrator dispatch \"@task.md\"  (или текст)")
        return
    path = agents.dispatch_executor_task(text)
    s = state.load()
    s["next_action"] = text[:200]
    s["next_action_owner"] = "executor"
    state.save(s)
    print(f"task written -> {path}")


def _cmd_task(arg):
    text = _read_arg(arg) if arg else ""
    if not text:
        print("usage: python -m orchestrator task \"текст задачи\"")
        return
    s = state.load()
    s["next_action"] = text
    s["next_action_owner"] = "architect"
    state.save(s)
    print("next_action set; daemon переключится на executor при следующем цикле.")


def _cmd_take(_arg):
    if not os.path.exists(config.EXECUTOR_TASK_PATH):
        print("(нет задачи для executor)")
        return
    with open(config.EXECUTOR_TASK_PATH) as f:
        print(f.read())


def _cmd_submit(arg):
    text = _read_arg(arg) if arg else ""
    if not text:
        print("usage: python -m orchestrator submit \"@answer.md\"  (или текст)")
        return
    agents.write_executor_response(text)
    notifier.send("📥 Ответ executor записан (executor_response.md).", category="done")
    print("response written -> executor_response.md")


def _cmd_status(_arg):
    s = state.load()
    print(json.dumps(s, ensure_ascii=False, indent=2))


def _cmd_oc_discover(_arg):
    from . import opencode_client

    servers = opencode_client.discover_opencode_servers(config.OPENCODE_SERVER_PASSWORD)
    print(f"Найдено opencode-серверов: {len(servers)}")
    for base in servers:
        print(f"\n=== {base} ===")
        client = opencode_client.OpenCodeClient(base, config.OPENCODE_SERVER_PASSWORD)
        try:
            sessions = client.sessions()
        except Exception as e:
            print(f"  (ошибка запроса: {e})")
            continue
        items = sessions.values() if isinstance(sessions, dict) else sessions
        for s in items:
            print(f"  id={s.get('id')}  title={s.get('title')!r}")
    print("\nРеестр агентов (agents_registry.json / config):")
    reg = opencode_client.load_registry()
    for role, ids in reg.items():
        print(f"  {role}: {ids}")


def _cmd_send(args):
    from . import opencode_client

    if not args or len(args) < 2:
        print('usage: python -m orchestrator send <target> "<message>"  (или @file)')
        return
    target = args[0]
    rest = args[1] if args[1].startswith("@") else " ".join(args[1:])
    text = _read_arg(rest)
    if not text:
        print("пустое сообщение")
        return
    client, sid = opencode_client.resolve_agent(target)
    if client is None or sid is None:
        print(f"⚠️ сессия для '{target}' не найдена (проверь agents_registry.json)")
        return
    client.wake(sid, text)
    print(f"→ отправлено в '{target}' ({sid}) — fire-and-forget")


def _cmd_ask(args):
    from . import opencode_client, agents

    if not args or len(args) < 2:
        print('usage: python -m orchestrator ask <target> "<question>"  (или @file)')
        return
    target = args[0]
    rest = args[1] if args[1].startswith("@") else " ".join(args[1:])
    text = _read_arg(rest)
    if not text:
        print("пустой вопрос")
        return
    client, sid = opencode_client.resolve_agent(target)
    if client is None or sid is None:
        print(f"⚠️ сессия для '{target}' не найдена (проверь agents_registry.json)")
        return
    resp = client.ask(sid, text)
    print(agents._extract_text(resp))


def _cmd_notify(args):
    """Одностороннее сообщение владельцу в Telegram (без ожидания ответа)."""
    text = " ".join(args) if args else ""
    text = _read_arg(text) if text.startswith("@") else text
    if not text:
        print('usage: python -m orchestrator notify "<сообщение владельцу>"  (или @file)')
        return
    notifier.send(text, category="info")
    print("[Telegram] сообщение владельцу отправлено")


def _cmd_request(args):
    """Запросить решение у владельца через Telegram. Блокируется до апрува.

    Публикует вопрос с кнопками ✅/❌ в Telegram и ждёт, пока владелец нажмёт
    /approve или /reject (либо кнопку). Возвращает 'approve' или 'reject' в stdout
    — агент видит ответ и продолжает работу. Использует изолированный канал
    agent_decision.json, чтобы не конфликтовать с демоном оркестратора.
    """
    text = " ".join(args) if args else ""
    text = _read_arg(text) if text.startswith("@") else text
    if not text:
        print('usage: python -m orchestrator request "<вопрос владельцу>"  (или @file)')
        return
    # пометить, что ждём агентского апрува (бот продублирует решение сюда)
    with open(config.AGENT_PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump({"question": text, "ts": time.time()}, f, ensure_ascii=False)
    # Поднять единый процесс бота, если он ещё не крутится: без polling-цикла
    # нажатия ✅/❌ в Telegram никто не получит. ensure_bot_running сам следит,
    # чтобы бот-процесс был ровно один (pid-файл) и возвращает pid, только если
    # бот поднят здесь — тогда мы его и остановим при выходе.
    bot_pid = None
    if config.TELEGRAM_BOT_TOKEN:
        bot_pid = notifier.ensure_bot_running()

    def _stop_bot():
        if bot_pid is not None:
            try:
                os.kill(bot_pid, 15)  # SIGTERM
            except OSError:
                pass

    notifier.notify_decision_needed("agent", text, ["approve", "reject"])
    print("[orchestrator] ожидание решения владельца в Telegram (Ctrl-C — отмена)...")
    try:
        while True:
            if os.path.exists(config.AGENT_DECISION_PATH):
                with open(config.AGENT_DECISION_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(config.AGENT_DECISION_PATH)
                print(data.get("choice"))
                return
            time.sleep(2)
    except KeyboardInterrupt:
        # убрать ожидание, если прервали
        try:
            os.remove(config.AGENT_PENDING_PATH)
        except OSError:
            pass
        print("отменено агентом")
    finally:
        _stop_bot()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "respond":
        choice = arg or "approve"
        notifier.push_decision(choice)
        print(f"decision '{choice}' queued -> decisions.json")
    elif cmd == "approve":
        notifier.push_decision("approve")
        print("approve queued")
    elif cmd == "reject":
        notifier.push_decision("reject")
        print("reject queued")
    elif cmd == "dispatch":
        _cmd_dispatch(arg)
    elif cmd == "task":
        _cmd_task(arg)
    elif cmd == "take":
        _cmd_take(arg)
    elif cmd == "submit":
        _cmd_submit(arg)
    elif cmd == "status":
        _cmd_status(arg)
    elif cmd == "oc-discover":
        _cmd_oc_discover(arg)
    elif cmd == "send":
        _cmd_send(sys.argv[2:])
    elif cmd == "ask":
        _cmd_ask(sys.argv[2:])
    elif cmd == "notify":
        _cmd_notify(sys.argv[2:])
    elif cmd == "request":
        _cmd_request(sys.argv[2:])
    elif cmd == "bot":
        notifier.Notifier().start_bot()
    else:
        core.main()
