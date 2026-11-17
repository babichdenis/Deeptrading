import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

SPEC = importlib.util.spec_from_file_location('significance', Path(__file__).resolve().parents[1]/'scripts/h058_h059_significance.py')
STATS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATS)


class SignificanceTests(unittest.TestCase):
    def test_permutation_is_independent_of_call_order(self):
        args = (list(range(60)), [0]*30+[1]*30)
        first = STATS.perm_test(*args, n_perm=19)
        STATS.perm_test([1]*60, args[1], n_perm=31)
        self.assertEqual(first, STATS.perm_test(*args, n_perm=19))
        self.assertEqual(first['p_two_sided'], 1/20)

    def test_invalid_permutation_inputs(self):
        for nets, groups in (([1], []), ([1], [2]), ([1], [.5]), ([np.nan], [0]), ([[1]], [0])):
            with self.assertRaises(ValueError):
                STATS.perm_test(nets, groups)
        for count in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                STATS.perm_test([], [], n_perm=count)

    def test_two_sided_decision_and_null_interval(self):
        result = STATS.perm_test([1]*60, [0]*30+[1]*30, n_perm=19)
        self.assertEqual(result['status'], 'not_significant')
        self.assertEqual(result['alternative'], 'two-sided')
        self.assertEqual(result['null_interval95'], [0, 0])
        self.assertNotIn('ci95', result)

    def test_one_sided_p_cannot_override_two_sided_decision(self):
        group = np.array([0]*30+[1]*30)
        permutations = [group]*3 + [1-group]*3 + [np.array([0, 1]*30)]*93
        with patch.object(STATS.np.random, 'default_rng') as factory:
            factory.return_value.permutation.side_effect = permutations
            result = STATS.perm_test([0]*30+[1]*30, group, n_perm=99)
        self.assertEqual(result['p_one_sided'], .04)
        self.assertEqual(result['p_two_sided'], .07)
        self.assertEqual(result['status'], 'not_significant')

    def test_bonferroni_rejects_truncation_and_duplicate_names(self):
        for results, names in (([], []), ([{}], []), ([{}, {}], ['a', 'a'])):
            with self.assertRaises(ValueError):
                STATS.bonf_adjust(results, names)
        result, count = STATS.bonf_adjust([{'p_two_sided': .02}, {'p_two_sided': None}], ['a', 'b'])
        self.assertEqual(count, 2)
        self.assertTrue(result['a']['significant_bonf'])
        self.assertIsNone(result['b']['significant_bonf'])

    def test_drawdown_starts_at_initial_capital(self):
        for values, expected in (([-5], 5), ([-5, -3], 8), ([5, -3, -4, 2], 7), ([], 0)):
            self.assertEqual(STATS.max_drawdown(values), expected)

    def test_monte_carlo_empty_and_invalid(self):
        self.assertEqual(STATS.monte_carlo([])['status'], 'insufficient_n')
        for values in ([np.inf], [np.nan], [[1]]):
            with self.assertRaises(ValueError):
                STATS.monte_carlo(values)
        with self.assertRaises(ValueError):
            STATS.monte_carlo([1], n_mc=0)

    def test_no_losses_pf_is_explicit_and_json_finite(self):
        result = STATS.monte_carlo([1, 2], n_mc=20)
        self.assertIsNone(result['actual']['pf'])
        self.assertEqual(result['pf_distribution']['undefined'], 20)
        self.assertIsNone(result['percentiles']['pf']['p50'])
        json.dumps(result, allow_nan=False)

    def test_monte_carlo_golden_and_repeatability(self):
        result = STATS.monte_carlo([-5], n_mc=10)
        self.assertEqual(result['actual'], {'net': -5, 'max_dd': 5, 'pf': 0})
        self.assertEqual(result['percentiles']['max_dd']['p95'], 5)
        self.assertEqual(result, STATS.monte_carlo([-5], n_mc=10))
        self.assertFalse(result['dependence_preserved'])

    def test_quorum_unknown_and_one_are_excluded(self):
        frame = pd.DataFrame({'net': [1]*5, 'quorum': [1, 2, 3, np.nan, np.inf]})
        result = STATS.compare_feature(frame, 'quorum', lambda x: x >= 3, lambda x: x == 2)
        self.assertEqual((result['n0'], result['n1']), (1, 1))
        self.assertEqual(result['coverage'], {'total': 5, 'selected': 2, 'unknown': 2, 'outside_groups': 1})

    def test_function_unknown_and_negative_are_not_absence(self):
        frame = pd.DataFrame({'net': [1]*4, 'fn': [0, 1, -1, np.nan]})
        result = STATS.compare_feature(frame, 'fn', lambda x: x == 1, lambda x: x == 0)
        self.assertEqual((result['n0'], result['n1']), (1, 1))

    def test_main_produces_new_complete_run_with_four_macro_tests(self):
        import contextlib
        import io
        import sys
        sys.path.insert(0, str(Path(STATS.__file__).parent))
        import macro_regime_attribution_v3 as macro
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root/'july'
            (run/'vote_attribution_dataset_v1').mkdir(parents=True)
            (root/'57b6244ee3eb').mkdir()
            pd.DataFrame({'net': [-5], 'pullback_bps': [100]}).to_csv(root/'entry_quality.csv', index=False)
            features = {'net': [-5], 'quorum_count': [2]}
            for name in ('bollinger_reclaim', 'donchian_breakout', 'macd_cross', 'pullback_ema',
                         'range_compression_breakout', 'rsi_reversal', 'vwap_reclaim'):
                features['fn_'+name] = [0]
            pd.DataFrame(features).to_csv(run/'vote_attribution_dataset_v1/intent_vote_features.csv', index=False)
            trade = 'figi,decision_time,net_rub,gross_rub\nBBG004S683W7,2026-01-01T00:00:00Z,-5,-4\n'
            (run/'trades.csv').write_text(trade)
            (root/'57b6244ee3eb/trades.csv').write_text(trade)
            old = root/'h058_significance_testing.json'
            old.write_text('historic')
            with (patch.object(STATS, 'REPORTS', directory), patch.object(STATS, 'RUN', str(run)),
                  patch.object(macro, 'load_full_csv', return_value=[]), contextlib.redirect_stdout(io.StringIO())):
                STATS.main(['--output-dir', str(root/'new')])
            result = json.loads((root/'new/h058_significance_testing.json').read_text())['h058']
            self.assertEqual(result['1c_n_tests'], 4)
            self.assertEqual(result['1d_n_tests'], 7)
            self.assertEqual(result['2_monte_carlo']['july_2026_517']['actual']['max_dd'], 5)
            manifest = json.loads((root/'new/completion.json').read_text())
            self.assertEqual(manifest['outputs'], macro.file_hashes(manifest['outputs']))
            self.assertEqual(old.read_text(), 'historic')

    def test_output_required_and_existing_directory_refused_before_reads(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory, patch.object(STATS.pd, 'read_csv') as reader:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                STATS.main(['--output-dir', directory])
            reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
