"""Выгрузка датасета для пилота NanoJev: те же бары и признаки, что в обзоре
быстрых моделей, но в CSV — чтобы research-окружение не зависело от БД и
боевого venv.

Запуск в боевом venv:
    .venv\\Scripts\\python.exe scripts\\export_research_ds.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from research_fast_models import FEATURES, HORIZON, THRESHOLD, build_dataset, load_frames

OUT = Path(__file__).resolve().parent.parent / "research" / "data"


async def main() -> None:
    frames = await load_frames()
    if not frames:
        raise SystemExit("нет данных")
    data = build_dataset(frames)
    OUT.mkdir(parents=True, exist_ok=True)
    cols = FEATURES + ["y", "fwd", "ticker"]
    path = OUT / "nanojev_ds.csv"
    data[cols].to_csv(path, index_label="ts")
    print(f"rows={len(data)} features={len(FEATURES)} horizon={HORIZON} thr={THRESHOLD}")
    print(f"saved {path} ({path.stat().st_size / 1024 / 1024:.1f} MB)")
    print("class shares:", data["y"].value_counts(normalize=True).round(3).to_dict())
    print("period:", data.index.min(), "-", data.index.max())


if __name__ == "__main__":
    asyncio.run(main())
