"""Вставляет в модульные фикстуры проверку наличия артефакта.

Перед `with open(os.path.join(BASE, "file"), ...)` добавляет путь и skip,
заменяя выражение в open() на переменную. Однократная правка.
"""
from __future__ import annotations

import os
import re

TESTS = os.path.dirname(os.path.abspath(__file__))
FILES = (
    "test_imoex_trend_attribution.py", "test_imoex_stop_fill.py",
    "test_imoex_stop_concept.py", "test_imoex_context_attribution.py",
    "test_exp002b.py", "test_entry_quality.py", "test_entry_exit_vote_behavior.py",
    "test_entry_confluence.py", "test_vote_attribution.py",
    "test_macro_regime_attribution.py", "test_time_of_day_attribution.py",
)

WITH = re.compile(
    r'^(?P<indent>[ \t]+)with open\((?P<expr>os\.path\.join\((?P<base>[A-Z_]+), (?P<file>"[^"]+"))\)'
    r'(?P<rest>.*)\) as (?P<var>\w+):\s*$'
)


def patch(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    out: list[str] = []
    changed = 0
    for line in lines:
        match = WITH.match(line.rstrip("\n"))
        if not match:
            out.append(line)
            continue
        indent = match.group("indent")
        guard = (
            f'{indent}path = os.path.join({match.group("base")}, {match.group("file")})\n'
            f'{indent}if not os.path.exists(path):\n'
            f'{indent}    pytest.skip(f"нет research-артефакта {{path}}")\n'
        )
        replacement = (
            f'{indent}with open(path{match.group("rest")}) as {match.group("var")}:\n'
        )
        out.append(guard)
        out.append(replacement)
        changed += 1
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(out)
    return changed


def main() -> int:
    total = 0
    for name in FILES:
        path = os.path.join(TESTS, name)
        count = patch(path)
        if count:
            print(f"{name}: фикстур защищено {count}")
            total += count
    print(f"итого {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
