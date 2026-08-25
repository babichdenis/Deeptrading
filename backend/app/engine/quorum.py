from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def merge_quorum(
    member_runs: list[tuple[str, list[dict]]],
    quorum: int,
) -> tuple[list[dict], dict]:
    """Единственный механизм кворума (same-bar, window_bars=0).

    member_runs: [(strategy_id, signals)], signals — dict с ключами 'ts', 'side'.
    Возвращает (merged_signals, funnel). Используется ВЕЗДЕ: склад, Lab, бот.
    """
    votes: dict[Any, dict] = defaultdict(lambda: {"BUY": [], "SELL": []})
    funnel: dict = {}
    for strategy_id, signals in member_runs:
        buys = sum(1 for s in signals if s["side"] == "BUY")
        sells = sum(1 for s in signals if s["side"] == "SELL")
        funnel[f"{strategy_id}:raw"] = {"BUY": buys, "SELL": sells}
        for s in signals:
            votes[s["ts"]][s["side"]].append(strategy_id)

    merged: list[dict] = []
    for ts in sorted(votes):
        buys = votes[ts]["BUY"]
        sells = votes[ts]["SELL"]
        buy_n, sell_n = len(buys), len(sells)
        if buy_n >= quorum and buy_n > sell_n:
            side, n_votes, members_for, opposition = "BUY", buy_n, buys, sells
        elif sell_n >= quorum and sell_n > buy_n:
            side, n_votes, members_for, opposition = "SELL", sell_n, sells, buys
        else:
            continue
        merged.append(
            {
                "ts": ts,
                "side": side,
                "reason": f"quorum_{n_votes}of{len(member_runs)}",
                "features": {
                    "votes": n_votes,
                    "buy_votes": buy_n,
                    "sell_votes": sell_n,
                    "members_for": members_for,
                    "opposition": opposition,
                    "window_bars": 0,
                },
            }
        )
    funnel["quorum"] = {
        "k": quorum,
        "signals": len(merged),
        "BUY": sum(1 for m in merged if m["side"] == "BUY"),
        "SELL": sum(1 for m in merged if m["side"] == "SELL"),
    }
    return merged, funnel
