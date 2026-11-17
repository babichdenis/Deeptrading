import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import importlib.util

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/macro_regime_attribution_v3.py"
SPEC = importlib.util.spec_from_file_location("macro_v3_reliability", SCRIPT)
MACRO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MACRO)


class MacroReliabilityTests(unittest.TestCase):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def bars(self, count=70):
        return [(self.start + timedelta(minutes=5*i), 100+i, 102+i, 99+i, 101+i, 1)
                for i in range(count)]

    def test_adx_trend_golden_and_future_invariance(self):
        bars = self.bars()
        prefix = MACRO.regime_series(bars[:60])
        future = bars[:60] + [(b[0], 1, 10000, 1, 9999, 1) for b in bars[60:]]
        full = MACRO.regime_series(future)
        for first, second in zip(prefix, full):
            self.assertEqual(first, second[:60])
        self.assertAlmostEqual(prefix[2][-1], 100)
        self.assertEqual(MACRO.regime_B(59, *prefix), 'up_trend')

    def test_flat_adx_golden(self):
        bars = [(b[0], 100, 100, 100, 100, 0) for b in self.bars()]
        series = MACRO.regime_series(bars)
        self.assertEqual(series[2][-1], 0)
        self.assertEqual(MACRO.regime_B(59, *series), 'flat')

    def test_unknown_indicator_is_not_flat(self):
        for value in (None, float('nan'), float('inf')):
            for column in range(3):
                series = [[100.0]*60, [99.0]*60, [40.0]*60]
                series[column][-1] = value
                self.assertIsNone(MACRO.regime_B(59, *series))

    def test_price_equal_ema_has_no_direction(self):
        self.assertEqual(MACRO.regime_B(59, [100]*60, [100]*60, [40]*60), 'flat')

    def test_closed_boundary_and_freshness(self):
        bars = self.bars(60)
        timestamps = [b[0] for b in bars]
        series = MACRO.regime_series(bars)
        close = timestamps[-1] + timedelta(minutes=5)
        self.assertEqual(MACRO.last_closed_bar(timestamps, close), 59)
        self.assertEqual(MACRO.last_closed_bar(timestamps, close-timedelta(microseconds=1)), 58)
        self.assertEqual(MACRO.regime_at(timestamps, close, series, 0), ('up_trend', 'known'))
        self.assertEqual(MACRO.regime_at(timestamps, close+timedelta(seconds=1), series, 0), (None, 'stale'))
        self.assertEqual(MACRO.regime_at(timestamps, self.start, series), (None, 'missing'))
        self.assertEqual(MACRO.regime_at(timestamps, timestamps[1], series), (None, 'unavailable'))

    def test_invalid_clock_and_interval(self):
        for interval in (0, -1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                MACRO.last_closed_bar([], self.start, interval)
        with self.assertRaises(ValueError):
            MACRO.last_closed_bar([], self.start.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            MACRO.last_closed_bar([self.start.replace(tzinfo=None)], self.start)
        with self.assertRaises(ValueError):
            MACRO.regime_at([], self.start, ([], [], []), -1)

    def load_trades(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'trades.csv'
            path.write_text(content, encoding='utf-8-sig')
            return MACRO.load_trades(path)

    def test_trade_time_normalized_and_bom_supported(self):
        rows = self.load_trades('figi,decision_time,net_rub,gross_rub\nA,2026-01-01T03:00:00+03:00,1,2\n')
        self.assertEqual(rows[0]['decision_time'], self.start.isoformat())

    def test_invalid_trade_rows(self):
        header = 'figi,decision_time,net_rub,gross_rub\n'
        for row in ('A,2026-01-01T00:00:00,1,2', 'A,2026-01-01T00:00:00Z,nan,2',
                    'A,2026-01-01T00:00:00Z,1,inf', 'A,2026-01-01T00:00:00Z,1',
                    ',2026-01-01T00:00:00Z,1,2', 'A,2026-01-01T00:00:00Z,1,2,3'):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.load_trades(header + row)

    def test_trade_schema_and_duplicate_ids(self):
        for content in ('figi\nA', 'figi,figi,decision_time,net_rub,gross_rub\n',
                        'trade_id,figi,decision_time,net_rub,gross_rub\n' +
                        '1,A,2026-01-01T00:00:00Z,1,2\n'*2):
            with self.assertRaises(ValueError):
                self.load_trades(content)

    def run_fixture(self, root):
        rows = ''.join(f'{b[0].isoformat()};{b[2]};{b[3]};{b[1]};{b[4]};1\n' for b in self.bars())
        for name in MACRO.FACTORS_CSV.values():
            (root/name).write_text(rows, encoding='utf-8-sig')
        trades = root/'trades.csv'
        trades.write_text('figi,decision_time,net_rub,gross_rub\n'
                          'BBG004S683W7,2026-01-01T05:50:00Z,1,2\n'
                          'UNKNOWN,2026-01-01T05:50:00Z,1,2\n')
        return ['--output-dir', str(root/'run'), '--data-dir', str(root), '--source', 'csv',
                '--july-trades', str(trades), '--h1-trades', str(trades), '--max-factor-age-minutes', '0']

    def test_cli_coverage_provenance_and_completion_without_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.run_fixture(root)
            with patch.object(MACRO, 'REPORTS', directory), contextlib.redirect_stdout(io.StringIO()):
                MACRO.main(args)
            result = json.loads((root/'run/macro_regime_attribution_v3.json').read_text())
            self.assertEqual(result['coverage']['July_2026']['unmapped_trades'], 1)
            self.assertEqual(result['coverage']['July_2026']['factors']['IMOEX']['known'], 1)
            self.assertEqual(result['factor_ranges']['BRENT']['count'], 70)
            self.assertEqual(len(result['input_hashes']), 5)
            self.assertEqual(len(result['code_hashes']), 5)
            self.assertTrue(result['freshness_checked'])
            self.assertIn('H1_2026', result['bonferroni']['by_window'])
            completion = json.loads((root/'run/completion.json').read_text())
            self.assertEqual(completion['outputs'], MACRO.file_hashes(completion['outputs']))

    def test_mutating_input_prevents_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.run_fixture(root)
            original = MACRO.load_trades
            def mutate(path):
                result = original(path)
                with Path(path).open('a') as output:
                    output.write('\n')
                return result
            with patch.object(MACRO, 'load_trades', side_effect=mutate), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'input files changed'):
                    MACRO.main(args)
            self.assertFalse((root/'run').exists())

    def test_output_failure_has_no_completion_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.run_fixture(root)
            with (patch.object(MACRO, 'REPORTS', directory),
                  patch.object(MACRO.os, 'fsync', side_effect=OSError('disk error')),
                  contextlib.redirect_stdout(io.StringIO())):
                with self.assertRaises(OSError):
                    MACRO.main(args)
            self.assertFalse((root/'run/completion.json').exists())


if __name__ == '__main__':
    unittest.main()
