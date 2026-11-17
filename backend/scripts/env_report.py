"""Инвентаризация ML-окружения .8 для обзора кандидатов (ARCH-04, exploratory)."""
import importlib
import sys

PKGS = ["sklearn", "numpy", "pandas", "xgboost", "lightgbm", "torch", "onnxruntime", "scipy", "joblib"]

print(f"python {sys.version.split()[0]}")
for name in PKGS:
    try:
        m = importlib.import_module(name)
        print(f"{name:12s} {getattr(m, '__version__', '?')}")
    except Exception as exc:
        print(f"{name:12s} НЕТ ({type(exc).__name__})")
