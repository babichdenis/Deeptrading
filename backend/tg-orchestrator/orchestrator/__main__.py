"""CLI оркестратора.

Команды:
  bot                 запустить процесс Telegram-бота (релей/поллинг/вебхук)
  run                 цикл-демон: архитектор -> исполнитель -> апрув владельца
  dispatch <text>     поставить задачу и стартовать прогон
  task <n>            выполнить один шаг автомата (для отладки)
  send <role> <text>  fire-and-forget сообщение агенту
  ask  <role> <text>  спросить агента и вывести ответ
  notify <text>       сообщение владельцу в Telegram
  request "<вопрос>"  запросить апрув у владельца (возвращает approve/reject)
  approve / reject    записать решение (если владелец пишет из консоли)
  status              состояние
"""
import argparse
import json
import os
import sys
import time

from . import config
from . import agents as _agents
from . import notifier as _notif
from . import state as _st
from . import core as _core
from . import opencode_client as _oc


def cmd_bot(args):
    _notif.Notifier().start_bot()


def cmd_run(args):
    n = args.n or (_st.load().get("n", 1))
    while True:
        s = _st.load()
        s["n"] = n
        _core.run_once(n, s)
        _st.save(s)
        time.sleep(config.POLL_INTERVAL_SEC)


def cmd_dispatch(args):
    s = _st.load()
    s["task"] = args.text
    s["role"] = "architect"
    s["n"] = s.get("n", 1)
    _st.save(s)
    print("задача поставлена; запусти `orchestrator run`")


def cmd_task(args):
    s = _st.load()
    s = _core.run_once(args.n, s)
    _st.save(s)
    print(json.dumps(s, ensure_ascii=False))


def cmd_send(args):
    client, sid = _oc.resolve_agent(args.role)
    if not client or not sid:
        print(f"нет сессии для {args.role}")
        return
    from . import agents as _a
    source = f"agent:{args.frm}" if args.frm else "orchestrator"
    client.send_message(sid, _a.wrap_inbound(args.text, source))
    print("отправлено")


def cmd_ask(args):
    client, sid = _oc.resolve_agent(args.role)
    if not client or not sid:
        print(f"нет сессии для {args.role}")
        return
    from . import agents as _a
    source = f"agent:{args.frm}" if args.frm else "orchestrator"
    resp = client.ask(sid, _a.wrap_inbound(args.text, source))
    print(_oc._extract_text(resp))


def cmd_notify(args):
    _notif.ensure_bot_running()
    _notif.Notifier().send(args.text, category="info")


def cmd_request(args):
    """Запросить апрув у владельца; блокируется до решения кнопкой."""
    n = _notif.Notifier()
    _notif.ensure_bot_running()
    doc = None
    question = args.text
    # если текст указывает на файл — прикрепим документ
    if question.startswith("@"):
        path = question[1:].strip()
        if os.path.exists(path):
            doc = path
            question = os.path.basename(path)
    run_id = f"req-{int(time.time())}"
    decision = _wait_decision(run_id, question, ["approve", "reject"], doc)
    print(decision or "timeout")


def _wait_decision(run_id, question, options, document_path=None, timeout=600):
    n = _notif.Notifier()
    n.notify_decision_needed(run_id, question, options, document_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = n.read_decision()
        if d:
            n.clear_decision_notify()
            return d
        time.sleep(config.POLL_INTERVAL_SEC)
    return None


def cmd_approve(args):
    _notif.Notifier().push_decision("approve")


def cmd_reject(args):
    _notif.Notifier().push_decision("reject")


def cmd_status(args):
    print(json.dumps(_st.load(), ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(prog="orchestrator")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("bot").set_defaults(func=cmd_bot)

    rp = sub.add_parser("run")
    rp.add_argument("--n", type=int, default=None)
    rp.set_defaults(func=cmd_run)

    dp = sub.add_parser("dispatch")
    dp.add_argument("text")
    dp.set_defaults(func=cmd_dispatch)

    tp = sub.add_parser("task")
    tp.add_argument("n", type=int)
    tp.set_defaults(func=cmd_task)

    sp = sub.add_parser("send")
    sp.add_argument("role")
    sp.add_argument("text")
    sp.add_argument("--from", dest="frm", default=None, help="роль отправителя (другой агент)")
    sp.set_defaults(func=cmd_send)

    ap = sub.add_parser("ask")
    ap.add_argument("role")
    ap.add_argument("text")
    ap.add_argument("--from", dest="frm", default=None, help="роль отправителя (другой агент)")
    ap.set_defaults(func=cmd_ask)

    np = sub.add_parser("notify")
    np.add_argument("text")
    np.set_defaults(func=cmd_notify)

    rq = sub.add_parser("request")
    rq.add_argument("text")
    rq.set_defaults(func=cmd_request)

    sub.add_parser("approve").set_defaults(func=cmd_approve)
    sub.add_parser("reject").set_defaults(func=cmd_reject)
    sub.add_parser("status").set_defaults(func=cmd_status)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
