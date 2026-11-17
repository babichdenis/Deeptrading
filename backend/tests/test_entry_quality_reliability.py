import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location('entry_quality', Path(__file__).resolve().parents[1]/'scripts/entry_quality.py')
ENTRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRY)


class EntryQualityTests(unittest.TestCase):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def bar(self, minute, close=100, high=101, low=99):
        return SimpleNamespace(ts=self.start+timedelta(minutes=minute), open=close, high=high, low=low, close=close)

    def fixture(self, side='LONG'):
        bars = [self.bar(i*5) for i in range(25)]
        trade = {'trade_id': '1', 'figi': 'A', 'side': side,
                 'decision_time': (self.start+timedelta(minutes=120)).isoformat(),
                 'entry_time': (self.start+timedelta(minutes=122)).isoformat(),
                 'entry_price': '100', 'net_rub': '-1', 'gross_rub': '2'}
        minutes = [self.bar(i) for i in range(120, 125)]
        return trade, bars, minutes

    def test_ema_uses_all_prefix_and_no_future(self):
        values = [200]+[100]*39
        self.assertAlmostEqual(ENTRY.ema20_at(values, 39), 100+100*(19/21)**39)
        self.assertEqual(ENTRY.ema20_at(values+[99999], 39), ENTRY.ema20_at(values, 39))
        self.assertIsNone(ENTRY.ema20_at(values, 18))

    def test_open_bar_and_future_entry_price_do_not_change_features(self):
        trade, bars, minutes = self.fixture()
        before = ENTRY.analyze_trade(trade, bars, minutes)
        bars[-1] = self.bar(120, 900, 999, 1)
        trade['entry_price'] = '1000'
        after = ENTRY.analyze_trade(trade, bars, minutes)
        for field in ('trend_aligned', 'pullback_bps'):
            self.assertEqual(before[field], after[field])

    def test_pullback_uses_exactly_twenty_bars(self):
        trade, bars, minutes = self.fixture()
        bars[3] = self.bar(15, 100, 1000, 99)
        result = ENTRY.analyze_trade(trade, bars, minutes)
        self.assertAlmostEqual(result['pullback_bps'], 10000/101)

    def test_early_mae_starts_at_entry_including_entry_bar(self):
        trade, bars, minutes = self.fixture()
        minutes[0].low = 1
        minutes[2].low = 95
        result = ENTRY.analyze_trade(trade, bars, minutes)
        self.assertEqual(result['mae_early_bps'], 500)

    def test_gap_does_not_bridge_into_future_minutes(self):
        trade, bars, minutes = self.fixture()
        minutes[-1].ts += timedelta(minutes=1)
        result = ENTRY.analyze_trade(trade, bars, minutes)
        self.assertIsNone(result['mae_early_bps'])
        self.assertEqual(result['mae_early_status'], 'incomplete_window')

    def test_exit_inside_window_is_unknown(self):
        trade, bars, minutes = self.fixture()
        trade['exit_time'] = (self.start+timedelta(minutes=123)).isoformat()
        self.assertEqual(ENTRY.analyze_trade(trade, bars, minutes)['mae_early_status'], 'exit_before_window_end')

    def test_mae_cannot_be_negative_for_either_side(self):
        for side in ('LONG', 'SHORT'):
            trade, bars, minutes = self.fixture(side)
            price = 110 if side == 'LONG' else 90
            minutes = [self.bar(i, price, price+1, price-1) for i in range(122, 125)]
            self.assertEqual(ENTRY.analyze_trade(trade, bars, minutes)['mae_early_bps'], 0)

    def test_missing_history_keeps_row_unknown(self):
        trade, bars, minutes = self.fixture()
        result = ENTRY.analyze_trade(trade, bars[:3], minutes)
        self.assertIsNone(result['trend_aligned'])
        self.assertEqual(result['decision_features_status'], 'insufficient_history')

    def test_net_pf_and_missing_medians(self):
        rows = [{'net': value, 'gross': 10, 'mfe_r': None, 'mae_r': None, 'hold_bars': None} for value in (6, -3)]
        result = ENTRY.econ(rows)
        self.assertEqual(result['pf'], 2)
        self.assertEqual(result['win_rate'], .5)
        self.assertIsNone(result['median_mfe_r'])
        self.assertEqual(ENTRY.econ([]), {'count': 0})

    def test_invalid_trade_rejected(self):
        for field, value in (('side', 'OTHER'), ('entry_price', '0'), ('net_rub', 'nan'),
                             ('entry_time', '2026-07-01T00:00:00Z'), ('decision_time', '2026-07-01T02:00:00')):
            trade, bars, minutes = self.fixture()
            trade[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ENTRY.analyze_trade(trade, bars, minutes)

    def test_invalid_candles_rejected(self):
        with self.assertRaises(ValueError):
            ENTRY.validate_candles([self.bar(0), self.bar(0)])
        with self.assertRaises(ValueError):
            ENTRY.validate_candles([self.bar(0, high=1)])

    def test_main_empty_and_populated_outputs_without_database(self):
        import contextlib
        import io
        import csv
        import json
        import tempfile
        from unittest.mock import patch
        for populated in (False, True):
            with self.subTest(populated=populated), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trade, bars, minutes = self.fixture()
                trade['figi'] = ENTRY.FIGIS[0]
                with (root/'trades.csv').open('w') as output:
                    writer = csv.DictWriter(output, fieldnames=list(trade))
                    writer.writeheader()
                    if populated:
                        writer.writerow(trade)
                old = root/'entry_quality.json'
                old.write_text('historic')
                observed_ranges = []
                def load(figi, start, end):
                    observed_ranges.append((start, end))
                    return minutes
                modules = {
                    'app.services.research_pack': SimpleNamespace(_load_candles=load),
                    'app.services.ensemble': SimpleNamespace(resample=lambda candles, seconds: bars, TF_SECONDS={'5min': 300}),
                }
                with patch.dict('sys.modules', modules), patch.object(ENTRY, 'RUN_DIR', str(root)), contextlib.redirect_stdout(io.StringIO()):
                    ENTRY.main(['--output-dir', str(root/'new')])
                result = json.loads((root/'new/entry_quality.json').read_text())
                self.assertEqual(result['n_rows'], int(populated))
                self.assertLess(observed_ranges[0][0], ENTRY.T_FROM)
                self.assertGreater(observed_ranges[0][1], ENTRY.T_TO)
                self.assertTrue((root/'new/entry_quality.csv').read_text().startswith('trade_id,'))
                self.assertEqual(old.read_text(), 'historic')
                manifest = json.loads((root/'new/completion.json').read_text())
                self.assertEqual(manifest['outputs'], ENTRY.file_hashes(manifest['outputs']))
                if not populated:
                    self.assertIsNone(result['early_noise_median_bps'])
                    self.assertIsNone(result['pullback_q1_bps'])

    def test_existing_output_rejected_before_database_import(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ENTRY.main(['--output-dir', directory])


if __name__ == '__main__':
    unittest.main()
