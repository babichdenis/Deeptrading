"""Добавляет encoding="utf-8" в файловые open() тестов, читающих артефакты.

Однократная правка: на Windows open() без кодировки берёт cp1251 и падает на
UTF-8 JSON. Имена вида `_open()` и режимы с "b" не трогаем.
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

CALL = re.compile(r"(?<![A-Za-z0-9_])open\(")


def patch_line(line: str) -> str:
    if "encoding=" in line or '"rb"' in line or "'rb'" in line or '"wb"' in line:
        return line
    out = line
    pos = 0
    while True:
        match = CALL.search(out, pos)
        if not match:
            return out
        start = match.end()
        depth = 1
        index = start
        while index < len(out) and depth:
            if out[index] == "(":
                depth += 1
            elif out[index] == ")":
                depth -= 1
            index += 1
        if depth:
            return out
        close = index - 1
        before = out[:close].rstrip()
        insert = 'encoding="utf-8"'
        if before.endswith(","):
            new = out[:close] + " " + insert + out[close:]
        else:
            new = out[:close] + ", " + insert + out[close:]
        out = new
        pos = close + len(insert) + 4


def main() -> int:
    total = 0
    for name in FILES:
        path = os.path.join(TESTS, name)
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        changed = 0
        new_lines = []
        for line in lines:
            updated = patch_line(line)
            if updated != line:
                changed += 1
            new_lines.append(updated)
        if changed:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.writelines(new_lines)
            print(f"{name}: строк изменено {changed}")
            total += changed
    print(f"итого {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
