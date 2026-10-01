"""Static guards for the hermetic test contour.

Аудит 2026-10-01, P0.2: marker migration была неполной — четыре модуля читали
backend/reports без маркера, из-за чего команда CI
`pytest -m "not artifact and not integration"` падала на чистом checkout.

Эти тесты ловят класс ошибки статически, до запуска pytest:
- тест, который открывает путь reports/, обязан иметь маркер artifact
  (или быть помечен integration, если зависит от живой БД);
- маркеры artifact/integration должны быть зарегистрированы в pytest.ini,
  иначе `-m` их не отфильтрует и будет warning о неизвестном маркере.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(BACKEND, "tests")
PYTEST_INI = os.path.join(BACKEND, "pytest.ini")

# Модули, которым reports/ достаётся из внешнего окружения (легаси-исключения
# должны быть явными и короткими, каждый — с причиной).
_REPORTS_EXEMPT: dict[str, str] = {}

# Признак реальной зависимости от локального каталога артефактов: конструирование
# пути (os.path.join/Path) с компонентом reports, либо литеральный путь reports/...
# НЕ являются зависимостью: HTTP-маршруты /api/v1/analysis/reports/..., имя
# self.reports_dir в tmp-фикстуре и упоминание слова в докстринге.
_REPORTS_PATH_RE = re.compile(
    r"""(?:os\.path\.join|Path)\s*\([^)]*["'][^"']*\breports\b"""
    r"""|["'](?!/api/)[^"'\n]*\breports/[^"'\n]*["']\s*[,)]"""
)
_MARKER_RE = re.compile(r"pytestmark\s*=|pytest\.mark\.(artifact|integration)")
_SELF = os.path.basename(__file__)


def _test_modules() -> list[str]:
    out = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if name.startswith("test_") and name.endswith(".py"):
            out.append(os.path.join(TESTS_DIR, name))
    return out


def test_marker_migration_complete():
    """Каждый тест, читающий reports/, обязан быть помечен artifact."""
    offenders: list[str] = []
    for path in _test_modules():
        name = os.path.basename(path)
        if name in _REPORTS_EXEMPT or name == _SELF:
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if not _REPORTS_PATH_RE.search(src):
            continue
        if _MARKER_RE.search(src):
            continue
        offenders.append(name)
    assert not offenders, (
        "тесты читают reports/ без маркера artifact — они упадут на чистом "
        f"checkout и сломают hermetic baseline: {offenders}"
    )


def test_markers_registered_in_pytest_ini():
    """Незарегистрированный маркер не фильтруется выражением -m в CI."""
    with open(PYTEST_INI, encoding="utf-8") as fh:
        ini = fh.read()
    for marker in ("artifact", "integration"):
        assert re.search(rf"^\s*{marker}\s*:", ini, re.MULTILINE), (
            f"маркер {marker} не зарегистрирован в pytest.ini"
        )


def test_ci_runs_hermetic_contour_only():
    """CI обязан исключать artifact+integration, иначе baseline не hermetic."""
    ci = os.path.join(os.path.dirname(BACKEND), ".github", "workflows", "ci.yml")
    with open(ci, encoding="utf-8") as fh:
        raw = fh.read()
    assert 'not artifact and not integration' in raw, (
        "CI не фильтрует artifact/integration — вернуть -m в test job"
    )


def test_frontend_build_job_present():
    """P0.3: frontend/tsc должен проверяться в CI, а не только локально."""
    ci = os.path.join(os.path.dirname(BACKEND), ".github", "workflows", "ci.yml")
    with open(ci, encoding="utf-8") as fh:
        raw = fh.read()
    assert "npm run build" in raw, "в CI нет frontend build job"
    assert re.search(r"^\s*frontend:\s*$", raw, re.MULTILINE), "нет отдельного job frontend"


def test_broker_sdk_pinned_and_isolated():
    """P0.1: private SDK зафиксирован и вынесен в отдельный requirements."""
    broker = os.path.join(BACKEND, "requirements-broker.txt")
    with open(broker, encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh]
    sdk = [ln for ln in lines if ln.startswith("t-tech-investments")]
    assert sdk, "t-tech-investments не найден в requirements-broker.txt"
    assert "==" in sdk[0], (
        f"версия private SDK не зафиксирована: {sdk[0]} — новый релиз может "
        "сломать baseline без коммита"
    )
    core = os.path.join(BACKEND, "requirements-core.txt")
    with open(core, encoding="utf-8") as fh:
        core_src = fh.read()
    assert "t-tech-investments" not in core_src, (
        "broker SDK не должен быть в requirements-core.txt"
    )


@pytest.mark.parametrize("name", ["test_indicator_state.py"])
def test_no_exact_float_equality(name):
    """P0.2: математический parity сравнивать через pytest.approx, не ==.

    Ловим только сравнения, где хотя бы одна сторона — вызов индикатора
    (ema/rsi/atr/...), потому что именно там всплывает накопленная погрешность
    порядка 1e-14. Сравнения строк/дат/логических флагов не интересуют.
    """
    path = os.path.join(TESTS_DIR, name)
    if not os.path.exists(path):
        pytest.skip(f"{name} нет")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    math_calls = {"ema", "rsi", "atr", "rsi_map", "atr_map", "sma", "macd",
                  "bollinger", "stochastic", "adx", "obv", "wma", "std"}
    exact: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for cmp_node in ast.walk(node):
            if not isinstance(cmp_node, ast.Compare):
                continue
            if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in cmp_node.ops):
                continue
            if any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in math_calls
                for sub in ast.walk(cmp_node)
            ):
                exact.append(cmp_node.lineno)
    assert not exact, (
        f"{name}: точное сравнение float на строках {exact} — заменить на "
        "pytest.approx(rel=..., abs=...)"
    )
