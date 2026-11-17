import importlib.util
import json
import contextlib
import io
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/macro_regime_attribution_v3.py"
SPEC = importlib.util.spec_from_file_location("macro_v3_validation", SCRIPT)
MACRO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MACRO)


class MacroValidationTests(unittest.TestCase):
    def load_rows(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "brent_5m.csv").write_text(rows)
            with patch.object(MACRO, "DATA", directory):
                return MACRO.load_full_csv("BRENT")

    def test_valid_utc_conversion_and_legacy_trailing_separator(self):
        bars = self.load_rows("2026-01-01T03:00:00+03:00;12;9;10;11;0;\n\n")
        self.assertEqual(bars[0][0].hour, 0)
        self.assertEqual(bars[0][0].tzinfo, timezone.utc)
        self.assertEqual(bars[0][1:], (10, 12, 9, 11, 0))

    def test_invalid_rows_fail_explicitly(self):
        for row in (
            "2026-01-01T00:00:00;12;9;10;11;1",
            "2026-01-01T00:00:00Z;nan;9;10;11;1",
            "2026-01-01T00:00:00Z;10;9;10;11;1",
            "2026-01-01T00:00:00Z;12;9;10;11;-1",
            "2026-01-01T00:00:00Z;12;9",
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.load_rows(row)

    def test_duplicate_and_reversed_timestamps_are_rejected(self):
        first = "2026-01-01T00:05:00Z;12;9;10;11;1\n"
        for timestamp in ("00:05", "00:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.load_rows(first + f"2026-01-01T{timestamp}:00Z;12;9;10;11;1\n")

    def test_profit_factor_and_win_rate_include_costs(self):
        result = MACRO.econ([
            {"net": 6, "gross": 10, "win": True},
            {"net": -3, "gross": 1, "win": True},
        ])
        self.assertEqual(result["pf"], 2)
        self.assertEqual(result["win_rate"], 0.5)
        self.assertEqual(result["net"], 3)

    def test_empty_and_no_losses(self):
        self.assertEqual(MACRO.econ([]), {"count": 0})
        self.assertIsNone(MACRO.econ([{"net": 2}])["pf"])

    def test_permutation_probability_has_nonzero_monte_carlo_floor(self):
        result = MACRO.perm_test([10] * 30, [0] * 30, n_perm=19, seed=42)
        self.assertEqual(result["p_one_sided"], 1 / 20)
        self.assertEqual(result["p_two_sided"], 1 / 20)
        self.assertEqual(result["p_estimator"], "plus_one")

    def test_permutation_ties_and_repeatability(self):
        result = MACRO.perm_test([1] * 30, [1] * 30, n_perm=19)
        self.assertEqual(result["p_one_sided"], 1)
        self.assertEqual(result["p_two_sided"], 1)
        values = list(range(30))
        self.assertEqual(
            MACRO.perm_test(values, values, n_perm=19),
            MACRO.perm_test(values, values, n_perm=19),
        )
        self.assertIsNone(MACRO.perm_test([1] * 29, [1] * 30))

    def test_permutation_rejects_invalid_samples_and_budget(self):
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                MACRO.perm_test([1] * 30, [1] * 30, n_perm=count)
        for values in ([float("nan")], [float("inf")], [[1, 2]]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MACRO.perm_test(values, [1] * 30)

    def test_main_writes_new_run_and_preserves_original_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_report = root / "macro_regime_attribution_v3.json"
            old_report.write_text('{"historic": true}', encoding="utf-8")
            source = root / "h058_significance_testing.json"
            original = '{"h058": {"1c": "historic"}, "other": "сохранить"}'
            source.write_text(original, encoding="utf-8")
            for name in MACRO.FACTORS_CSV.values():
                (root / name).write_text("")
            trades = root / "trades.csv"
            trades.write_text("figi,decision_time,net_rub,gross_rub\n")
            destination = root / "new_run"
            arguments = ["--output-dir", str(destination), "--data-dir", directory,
                         "--july-trades", str(trades), "--h1-trades", str(trades)]
            with (
                patch.object(MACRO, "REPORTS", directory),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                MACRO.main(arguments)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    MACRO.main(arguments)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual(old_report.read_text(), '{"historic": true}')
            result = json.loads((destination / "macro_regime_attribution_v3.json").read_text())
            updated = json.loads((destination / source.name).read_text(encoding="utf-8"))
            self.assertEqual(result["bonferroni"]["n_tests"], 4)
            self.assertEqual(updated["other"], "сохранить")
            self.assertIsInstance(updated["h058"]["1c"], dict)


if __name__ == "__main__":
    unittest.main()
