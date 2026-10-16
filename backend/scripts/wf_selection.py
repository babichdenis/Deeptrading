"""Чистая логика отбора WF-кандидатов (задача A2, ARCH 2026-10-07; A2-C1).

Обязательные метрики trades и net_pnl проверяются на конечность независимо
от выбранного rank_by. None в необязательных метриках (profit_factor без
потерь, expectancy без сделок) не блокирует допуск и остаётся решением
фильтров; документированная семантика PF=None при отсутствии потерь
сохранена. Пустой passed не заменяется fallback-топом: если кандидатов
нет — selected пуст и статус NO_ELIGIBLE_CANDIDATES. Покрытие ticker×exit
сверяется по точному множеству ключей с кратностью (missing/duplicate/
unexpected/worker error). Недопущенные окна хранят metrics=None.

Дополнения A2-C1: дополнительные числа raw (max_dd_pct, commission,
slippage, turnover) проверяются при наличии (присутствующее нечисло —
повреждение), отсутствие/None — UNKNOWN; aggregate_rows не подменяет
неизвестное нулём (агрегат поля — null, если оно неизвестно хотя бы в
одной строке) и отмечает переполнение сумм; невалидный ранг (None/NaN/
±inf/bool) отклоняется с явной причиной missing_rank:<field>; тексты
ошибок воркера санитизируются (печатные символы, лимит длины).

Ревизия 4 (A2-C1 ревизия 3): ошибки воркера публикуются только как
безопасные классы (worker_error_code, без текста сообщения); суммы —
math.fsum (результат не зависит от порядка raw); конечность производных
win_rate/expectancy проверяется после деления; identity_problems и
coverage_breakdown дают структурированную оценку повреждений (не-dict /
без strategy / неизвестные стратегии, ключи и поля повреждений);
неизвестные стратегии и записи без атрибуции делают неполной всю фазу —
тот же контракт в IS, OOS и robustness.
"""

from __future__ import annotations

import math
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

__all__ = [
    "RankFieldError",
    "RANK_FIELDS",
    "REQUIRED_METRICS",
    "OOS_REQUIRED_METRICS",
    "RAW_REQUIRED_FIELDS",
    "STATUS_OK",
    "STATUS_ELIGIBLE",
    "STATUS_NO_ELIGIBLE_CANDIDATES",
    "STATUS_INCOMPLETE",
    "STATUS_WORKER_ERRORS",
    "is_number",
    "metrics_complete",
    "rank_value",
    "select_top",
    "eligible_labels",
    "select_is",
    "oos_rows",
    "build_oos_matrix",
    "raw_row_problem",
    "coverage_reasons",
    "coverage_breakdown",
    "identity_problems",
    "aggregate_rows",
    "sanitize_text",
    "worker_error_code",
    "WORKER_ERROR_CLASSES",
    "RAW_OPTIONAL_FIELDS",
]

STATUS_OK = "OK"
STATUS_ELIGIBLE = "ELIGIBLE"
STATUS_NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_WORKER_ERRORS = "WORKER_ERRORS"

RANK_FIELDS = frozenset(
    {
        "net_pnl",
        "profit_factor",
        "win_rate",
        "trades",
        "expectancy",
        "sharpe",
        "sqn",
        "recovery_factor",
    }
)

REQUIRED_METRICS = ("trades", "net_pnl")
OOS_REQUIRED_METRICS = REQUIRED_METRICS
RAW_REQUIRED_FIELDS = ("trades", "wins", "gw", "gl", "net")
# Дополнительные числа raw (A2-C1): отсутствие/None = UNKNOWN (не
# повреждение), присутствующее нечисло/NaN/±inf/bool = повреждение строки.
RAW_OPTIONAL_FIELDS = ("max_dd_pct", "commission", "slippage", "turnover")

_FILTER_METRIC_FIELDS = {
    "min_trades": "trades",
    "min_profit_factor": "profit_factor",
    "min_net_pnl": "net_pnl",
    "max_drawdown_pct": "max_drawdown_pct",
    "min_win_rate": "win_rate",
}


class RankFieldError(ValueError):
    """Поле ранжирования не входит в RANK_FIELDS."""


def is_number(value: Any) -> bool:
    """True только для конечных int/float (bool числом не считается)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def metrics_complete(metrics: Any, required: Sequence[str] = REQUIRED_METRICS) -> bool:
    """Обязательные метрики присутствуют и конечны; None в прочих не мешает."""
    if not isinstance(metrics, dict) or not metrics:
        return False
    return all(is_number(metrics.get(field)) for field in required)


def rank_value(metrics: Any, field: str) -> Optional[float]:
    """Значение поля ранга или None, если оно невалидно (нет/None/NaN/inf/не число)."""
    if field not in RANK_FIELDS:
        raise RankFieldError(f"unsupported rank field: {field!r}")
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(field)
    if not is_number(value):
        return None
    return value


def select_top(
    passed: Sequence[str],
    by_label: Dict[str, Any],
    rank_by: str,
    top_k: int,
) -> Tuple[List[str], str, List[Dict[str, Any]]]:
    """Ранжирует passed по rank_by (desc), tie-break — label asc.

    Возвращает (selected, status, rejected). Кандидаты с невалидным
    значением ранга уходят в rejected с причиной invalid_rank:<field>.
    «Топ без фильтра» не допускается: пустой/полностью невалидный passed
    даёт STATUS_NO_ELIGIBLE_CANDIDATES и пустой selected.
    """
    scored: List[Tuple[float, str]] = []
    rejected: List[Dict[str, Any]] = []
    for label in passed:
        value = rank_value(by_label.get(label), rank_by)
        if value is None:
            rejected.append({"label": label, "reason": f"invalid_rank:{rank_by}"})
            continue
        scored.append((value, label))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    k = max(int(top_k or 0), 0)
    selected = [label for _, label in scored[:k]]
    status = STATUS_ELIGIBLE if selected else STATUS_NO_ELIGIBLE_CANDIDATES
    return selected, status, rejected


def _filter_fields_problem(metrics: Dict[str, Any],
                           filters: Optional[Dict[str, Any]]) -> str:
    for key, field in _FILTER_METRIC_FIELDS.items():
        if key not in (filters or {}):
            continue
        value = metrics.get(field)
        if is_number(value):
            continue
        if key == "min_profit_factor" and value is None:
            net = metrics.get("net_pnl")
            if is_number(net) and net > 0:
                continue
        return f"incomplete_metrics:{field}"
    return ""


def eligible_labels(
    by_label: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
    apply_filters: Callable[[Dict[str, Any], Optional[Dict[str, Any]]], Dict[str, Any]],
    rank_by: str,
    incomplete: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Кандидаты, прошедшие completeness, поля фильтров/ранга и фильтры.

    ``incomplete`` — карта label → причина (неполный набор ticker×exit,
    worker error): такие кандидаты не допускаются и попадают в rejected.
    """
    incomplete = dict(incomplete or {})
    passed: List[str] = []
    rejected: List[Dict[str, Any]] = []
    for label in sorted(by_label):
        if label in incomplete:
            rejected.append({"label": label, "reason": incomplete.pop(label)})
            continue
        metrics = by_label.get(label) or {}
        if not metrics_complete(metrics):
            rejected.append({"label": label, "reason": "incomplete_metrics"})
            continue
        problem = _filter_fields_problem(metrics, filters)
        if problem:
            rejected.append({"label": label, "reason": problem})
            continue
        if rank_value(metrics, rank_by) is None:
            # None/NaN/±inf/bool/строка в поле ранга — кандидат не ранжируется
            # (явная причина; для PF=None/expectancy=None это случай формул).
            rejected.append({"label": label, "reason": f"missing_rank:{rank_by}"})
            continue
        verdict = apply_filters(metrics, filters) or {}
        if not verdict.get("passed", False):
            rejected.append(
                {
                    "label": label,
                    "reason": "filtered_out",
                    "checks": list(verdict.get("checks") or []),
                }
            )
            continue
        passed.append(label)
    for label, reason in sorted(incomplete.items()):
        rejected.append({"label": label, "reason": reason})
    return passed, rejected


def select_is(
    by_label: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
    apply_filters: Callable[..., Dict[str, Any]],
    rank_by: str,
    top_k: int,
    incomplete: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Полный цикл in-sample отбора: eligibility -> ранжирование -> top_k."""
    passed, rejected = eligible_labels(by_label, filters, apply_filters, rank_by,
                                       incomplete=incomplete)
    selected, status, rank_rejected = select_top(passed, by_label, rank_by, top_k)
    rejected.extend(rank_rejected)
    return {
        "passed": passed,
        "selected": selected,
        "status": status,
        "rejected": rejected,
    }


def sanitize_text(value: Any, limit: int = 80) -> str:
    """Однострочный текст для причин: только печатные символы, лимит длины.

    Ошибки воркера не сериализуются дословно: управляющие символы и
    переводы строк схлопываются, причина остаётся короткой диагностикой,
    а не произвольным дампом (A2-C1: без секретов/мусора из ошибок).
    """
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:limit]


WORKER_ERROR_CLASSES = (
    "TimeoutError", "ConnectionError", "KeyError", "ValueError",
    "TypeError", "ZeroDivisionError", "OSError", "RuntimeError",
    "IndexError", "ArithmeticError",
)


def worker_error_code(value: Any) -> str:
    """Безопасный класс ошибки воркера вместо текста сообщения (A2-C1 р4).

    Публикуются только имена из WORKER_ERROR_CLASSES (совпадение по началу
    санитизированного текста, как у repr(exception)); всё остальное —
    родовой 'exception'. Само сообщение (токены, пути, секреты) не попадает
    в причину, phases и stdout.
    """
    text = sanitize_text(value)
    for cls in WORKER_ERROR_CLASSES:
        if text == cls or text.startswith(cls + ":") or text.startswith(cls + "("):
            return cls
    return "exception"


def _fsum_safe(values: List[Any], field: str, problems: List[str]) -> Any:
    """Точное суммирование без зависимости от порядка (math.fsum).

    fsum накапливает точно и округляет один раз: сумма не зависит от
    порядка raw (контракт перестановок). Переполнение промежуточной
    суммы — OverflowError → overflow:<field>, агрегат None.
    """
    if not values:
        return 0
    try:
        total = math.fsum(values)
    except OverflowError:
        problems.append(f"overflow:{field}")
        return None
    if not math.isfinite(total):
        problems.append(f"overflow:{field}")
        return None
    return total


def aggregate_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Агрегат валидных raw-строк одного label.

    Набор ключей прежнего _metrics_from_rows плюс служебный "_problem"
    (вызывающая сторона вырезает его и подмешивает в причины неполноты).
    Правила A2-C1: обязательные числа суммируются точно (math.fsum) —
    результат не зависит от порядка raw; дополнительное число, неизвестное
    хотя бы в одной строке, даёт агрегат None — нулевая подстановка
    запрещена; max_drawdown_pct — максимум известных значений;
    profit_factor=None при gl==0 и expectancy=None при trades==0 — случаи
    формул, не повреждение; переполнение суммы/частного конечных входов
    помечается overflow:<field> (включая производные win_rate/expectancy/
    profit_factor), сам агрегат становится None (неконечный результат
    не допускается).
    """
    problems: List[str] = []
    columns: Dict[str, List[Any]] = {
        f: [] for f in RAW_REQUIRED_FIELDS + RAW_OPTIONAL_FIELDS}
    for row in rows:
        for f in columns:
            columns[f].append(row.get(f))
    sums: Dict[str, Any] = {}
    for f in columns:
        col = columns[f]
        if f in RAW_OPTIONAL_FIELDS:
            if any(not is_number(v) for v in col):
                sums[f] = None  # unknown хотя бы в одном прогоне → агрегат null
                continue
            if f == "max_dd_pct":
                sums[f] = max(col) if col else None
                continue
        sums[f] = _fsum_safe(col, f, problems)
    n, wins, net = sums["trades"], sums["wins"], sums["net"]
    gw, gl = sums["gw"], sums["gl"]
    # Производные числа: конечность проверяется ПОСЛЕ деления/умножения
    # (A2-C1 р4): конечные входы могут дать inf в частном — это повреждение
    # агрегата (overflow:<метрика>), а не значение.
    if is_number(wins) and is_number(n) and n:
        wr_raw = wins / n * 100
        if math.isfinite(wr_raw):
            win_rate: Any = round(wr_raw, 2)
        else:
            problems.append("overflow:win_rate")
            win_rate = None
    elif is_number(n) and n == 0:
        win_rate = 0.0
    else:
        win_rate = None  # trades/wins неизвестны из-за переполнения
    if is_number(gl) and gl > 0 and is_number(gw):
        pf: Any = round(gw / gl, 2)
        if not math.isfinite(pf):
            problems.append("overflow:profit_factor")
            pf = None
    else:
        pf = None  # убытков не было (gl==0) — документированная семантика
    if is_number(net) and is_number(n) and n:
        exp_raw = net / n
        if math.isfinite(exp_raw):
            expectancy: Any = round(exp_raw, 3)
        else:
            problems.append("overflow:expectancy")
            expectancy = None
    else:
        expectancy = None  # нет сделок или неизвестно из-за переполнения
    return {
        "trades": n, "wins": wins, "win_rate": win_rate,
        "gross_win": gw, "gross_loss": gl, "net_pnl": net,
        "profit_factor": pf, "expectancy": expectancy,
        "max_drawdown_pct": sums["max_dd_pct"],
        "commission": sums["commission"], "slippage": sums["slippage"],
        "turnover": sums["turnover"],
        "_problem": ";".join(problems),
    }


def raw_row_problem(row: Any, fields: Sequence[str] = RAW_REQUIRED_FIELDS) -> str:
    """Причина непригодности raw-строки прогона ('' = строка валидна).

    Обязательные поля (trades, wins, gw, gl, net): missing/None/нечисло —
    повреждение. Дополнительные (max_dd_pct, commission, slippage,
    turnover): missing/None — UNKNOWN (валидно), присутствующее нечисло/
    NaN/±inf/bool — повреждение. Ключ error означает ошибку воркера:
    публикуется безопасный класс (worker_error_code), а не текст сообщения
    (A2-C1 р4).
    """
    if not isinstance(row, dict):
        return "invalid_raw:row"
    if "error" in row:
        return f"worker_error:{worker_error_code(row.get('error', ''))}"
    for field in fields:
        if not is_number(row.get(field)):
            return f"invalid_raw:{field}"
    for field in RAW_OPTIONAL_FIELDS:
        value = row.get(field)
        if value is not None and not is_number(value):
            return f"invalid_raw:{field}"
    return ""


def _key_json(key: Tuple[Any, Any]) -> List[str]:
    """(exit, ticker) как безопасная JSON-пара: печатные строки с лимитом."""
    e, t = key
    return [sanitize_text(e, 80), sanitize_text(t, 80)]


def identity_problems(raw: Sequence[Any], known_labels: Iterable[str]) -> Dict[str, Any]:
    """Записи raw без атрибуции к ожидаемым прогонам — до арифметики.

    non_dict — не dict; unlabeled — dict без непустой строковой strategy;
    unknown_labels — стратегии не из known_labels (с образцом ключей).
    Такие строки в агрегирование не допускаются и (вместе с повреждениями
    покрытия) делают фазу неполной (A2-C1 р4).
    """
    known = set(known_labels)
    non_dict = unlabeled = 0
    unknown_set: set = set()
    unknown_rows: List[Dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            non_dict += 1
            continue
        strategy = row.get("strategy")
        if not isinstance(strategy, str) or not strategy:
            unlabeled += 1
            continue
        if strategy not in known:
            unknown_set.add(strategy)
            if len(unknown_rows) < 5:
                unknown_rows.append({
                    "strategy": sanitize_text(strategy, 80),
                    "key": _key_json((row.get("exit"), row.get("ticker"))),
                })
    return {
        "non_dict": non_dict,
        "unlabeled": unlabeled,
        "unknown_labels": sorted(unknown_set),
        "unknown_rows": unknown_rows,
    }


def coverage_breakdown(
    raw: Sequence[Any],
    labels: Iterable[str],
    expected_keys: Iterable[Tuple[Any, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Структурированные повреждения покрытия по label: классы с ключами/полями.

    Возвращает {label: {"missing"/"duplicates"/"unexpected" — списки
    [exit, ticker]; "worker_errors": [{"key", "code"}]; "invalid":
    [{"key", "field"}]}} только для повреждённых label. Структура хранится
    в phases, чтобы детализация (какое поле, какой ключ) не терялась
    вместе со строковой причиной (A2-C1 р4).
    """
    expected = set(expected_keys)
    known = list(dict.fromkeys(labels))
    buckets: Dict[str, List[Dict[str, Any]]] = {label: [] for label in known}
    for row in raw:
        if isinstance(row, dict) and row.get("strategy") in known:
            buckets[row["strategy"]].append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for label in known:
        seen: Dict[Tuple[Any, Any], int] = {}
        worker_errors: List[Dict[str, Any]] = []
        invalid: List[Dict[str, Any]] = []
        for row in buckets[label]:
            problem = raw_row_problem(row)
            key = (row.get("exit"), row.get("ticker"))
            if not problem:
                seen[key] = seen.get(key, 0) + 1
            elif problem.startswith("worker_error"):
                worker_errors.append({"key": _key_json(key),
                                      "code": problem.split(":", 1)[1]})
            else:
                invalid.append({"key": _key_json(key),
                                "field": problem.split(":", 1)[1]})
        missing = sorted(expected - set(seen), key=str)
        unexpected = sorted(set(seen) - expected, key=str)
        duplicates = sorted((k for k, c in seen.items() if c > 1), key=str)
        if not (missing or unexpected or duplicates or worker_errors or invalid):
            continue
        out[label] = {
            "missing": [_key_json(k) for k in missing],
            "duplicates": [_key_json(k) for k in duplicates],
            "unexpected": [_key_json(k) for k in unexpected],
            "worker_errors": worker_errors,
            "invalid": invalid,
        }
    return out


def coverage_reasons(
    raw: Sequence[Any],
    labels: Iterable[str],
    expected_keys: Iterable[Tuple[Any, Any]],
    breakdown: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Неполнота покрытия ticker×exit по каждому label ('' нет = полное).

    Причина собирается из структурированного coverage_breakdown (готовый
    можно передать, чтобы не считать дважды): количество по классам плюс
    классы ошибок воркера (worker_codes=…) и поля повреждений
    (invalid_fields=…) — детализация сопровождает причину (A2-C1 р4).
    """
    if breakdown is None:
        breakdown = coverage_breakdown(raw, labels, expected_keys)
    out: Dict[str, str] = {}
    for label, bd in breakdown.items():
        parts: List[str] = []
        if bd["missing"]:
            parts.append(f"missing={len(bd['missing'])}")
        if bd["duplicates"]:
            parts.append(f"duplicate={len(bd['duplicates'])}")
        if bd["unexpected"]:
            parts.append(f"unexpected={len(bd['unexpected'])}")
        if bd["worker_errors"]:
            codes = sorted({w["code"] for w in bd["worker_errors"]})
            parts.append(f"worker_errors={len(bd['worker_errors'])}")
            parts.append("worker_codes=" + ",".join(codes))
        if bd["invalid"]:
            fields = sorted({i["field"] for i in bd["invalid"]})
            parts.append(f"invalid_raw={len(bd['invalid'])}")
            parts.append("invalid_fields=" + ",".join(fields))
        sample = bd["missing"] + bd["duplicates"] + bd["unexpected"]
        if sample:
            parts.append("keys=" + ",".join(f"{e}@{t}" for e, t in sample[:5]))
        out[label] = "incomplete_runs " + " ".join(parts)
    return out


def oos_rows(
    selected: Sequence[str],
    by_label: Dict[str, Any],
    errors: Optional[Iterable[str]] = None,
    required: Sequence[str] = OOS_REQUIRED_METRICS,
    incomplete: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Строки OOS-результатов: label, status, reason, metrics.

    Приоритет статусов: worker error > incomplete > OK. Обязательные
    метрики должны быть конечными числами. Для недопущенных окон
    metrics=None (частичные/нулевые агрегаты не публикуются; диагностика
    остаётся в phases).
    """
    error_set = set(errors or ())
    incomplete = dict(incomplete or {})
    rows: List[Dict[str, Any]] = []
    for label in selected:
        metrics = by_label.get(label)
        if not isinstance(metrics, dict):
            metrics = {}
        if label in error_set:
            status = STATUS_WORKER_ERRORS
            reason = incomplete.get(label) or "worker_error"
        elif label in incomplete:
            status = STATUS_INCOMPLETE
            reason = incomplete[label]
        else:
            missing = [m for m in required if not is_number(metrics.get(m))]
            if missing:
                status = STATUS_INCOMPLETE
                reason = "incomplete_metrics:" + ",".join(missing)
            else:
                status = STATUS_OK
                reason = ""
        rows.append({
            "label": label,
            "status": status,
            "reason": reason,
            "metrics": dict(metrics) if status == STATUS_OK else None,
        })
    return rows


def build_oos_matrix(
    selected_per_iter: Iterable[Any],
    by_label: Dict[str, Any],
    errors: Optional[Iterable[str]] = None,
    required: Sequence[str] = OOS_REQUIRED_METRICS,
    incomplete: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """OOS-матрица: по записи на WF-итерацию со строками из oos_rows()."""
    matrix: List[Dict[str, Any]] = []
    for item in selected_per_iter:
        if isinstance(item, dict):
            entry = {k: v for k, v in item.items() if k != "selected"}
            labels = list(item.get("selected") or [])
        else:
            entry = {}
            labels = list(item or [])
        entry["selected"] = labels
        entry["rows"] = oos_rows(labels, by_label, errors=errors, required=required,
                                 incomplete=incomplete)
        matrix.append(entry)
    return matrix
