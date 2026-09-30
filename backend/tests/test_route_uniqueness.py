"""Route uniqueness guard: в приложении не может быть двух роутеров
с одинаковым (path, method) на одном APIRouter.

Защищает от регрессии P0.5 (дважды зарегистрированный /api/v1/bot/heatmap:
вторая версия становилась недостижимой, разработчик чинил не ту).
"""
from __future__ import annotations

from collections import defaultdict

from app.main import app


def test_no_duplicate_route_paths() -> None:
    seen: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in app.routes:
        methods = getattr(r, "methods", None) or ()
        for m in methods:
            seen[(m, r.path)].append(getattr(r, "name", "?"))

    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"Дубли маршрутов: {dupes}"