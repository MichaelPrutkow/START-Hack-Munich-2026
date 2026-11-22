"""Offline replay and integration tests; no runner or API requests."""
import copy
import json
import unittest
from unittest.mock import patch
from claim_to_fame import parameter_grid as G
from claim_to_fame import feedback as F
from tests.test_feedback import fixture, NOW
from tests import test_feedback as fixtures


class GridTests(unittest.TestCase):
    def prepared(self, gid=1, **kwargs):
        rec, data = fixture(gid, **kwargs)
        lines, final = F._reconcile(rec, data, 'us')
        return rec, data, lines, final

    def test_actual_pair_exact_and_all_grid_pairs_present(self):
        row = self.prepared()
        before = copy.deepcopy(row)
        result = G.evaluate([row], [1], 'us')
        self.assertEqual(len(result['ranking']), len(G.ALPHAS)*len(G.SIGMAS))
        actual = next(r for r in result['ranking'] if r['a_mult'] == .85 and r['sigma'] == .35)
        self.assertEqual(actual['delta_lower'], 0)
        self.assertEqual(actual['delta_upper'], 0)
        self.assertEqual(row, before)
        json.dumps(result, allow_nan=False)

    def test_every_interval_contains_known_synthetic_counterfactual(self):
        row = self.prepared()
        result = G.evaluate([row], [1], 'us')
        opponents = [(100, 150), (120, 150), (0, 0)]
        true_t = 110
        for candidate in result['ranking']:
            a, b = G.replay(row[0], 1, candidate['a_mult'], candidate['sigma'])
            income = sum(min(a, 4*true_t+100) if a <= other_b else
                         1.5*a if a <= true_t else 0 for _, other_b in opponents)
            paid = sum(min(other_a, 4*true_t+100) if other_a <= b else
                       1.5*other_a if other_a <= true_t else 0 for other_a, _ in opponents)
            self.assertLessEqual(candidate['lower'], income-paid+.001)
            if candidate['upper'] is not None:
                self.assertGreaterEqual(candidate['upper'], income-paid-.001)

    def test_fine_grid_reuses_axis_prices_and_includes_used_pair(self):
        row = self.prepared()
        # Retain a historical off-grid pair as a comparison even if its baseline
        # no longer reproduces this fixture's submitted prices.
        row[0]['est']['unused'] = {'1': {'a_mult': .855, 'sigma': .345}}
        with patch.object(G, 'replay', wraps=G.replay) as replay:
            result = G.evaluate([row], [1], 'us')
        pairs = {(r['a_mult'], r['sigma']) for r in result['ranking']}
        self.assertEqual(len(pairs), 7382)
        self.assertIn((.86, .36), pairs)
        self.assertIn((.5, .2), pairs)
        self.assertIn((1.1, 1.4), pairs)
        self.assertIn((.855, .345), pairs)
        self.assertLess(replay.call_count, 200)
        self.assertEqual(sum('games' in r for r in result['ranking']), 10)

    def test_axis_separation_matches_direct_replay_with_adjustments(self):
        rec = self.prepared()[0]
        est = rec['est']['fast']['1']
        rec['recall'] = {'mode': '2', 'lines': {'1': {'cap_hi': 63}}}
        for fields in ({'p': .8, 'cap': 70, 'bias': 1.1, 'log_spread': .3,
                        'uncertainty_applied': True}, {'stated': 40}, {'p': .5}):
            est.update(fields)
            for a in (.5, .86, 1.1):
                for sigma in (.2, .36, 1.4):
                    self.assertEqual(G.replay(rec, 1, a, sigma),
                                     (G.replay(rec, 1, a, .2)[0],
                                      G.replay(rec, 1, .5, sigma)[1]))

    def test_missing_estimate_stays_actual_for_all_candidates(self):
        row = self.prepared()
        row[0]['est'] = {}
        result = G.evaluate([row], [1], 'us')
        self.assertIsNone(result['best'])
        self.assertEqual(result['fixed_lines'], 1)
        self.assertTrue(all(r['delta_lower'] == r['delta_upper'] == 0 for r in result['ranking']))

    def test_replay_mismatch_is_visible(self):
        row = self.prepared()
        row[0]['est']['fast']['1']['m'] = 123
        result = G.evaluate([row], [1], 'us')
        self.assertEqual(result['exclusions'][0]['reason'], 'baseline_replay_mismatch')

    def test_window_does_not_backfill_missing_recent_games(self):
        result = G.evaluate([self.prepared(i) for i in range(1, 5)], range(1, 23), 'us')
        self.assertEqual(result['window_games'], list(range(3, 23)))
        self.assertEqual(result['missing_games'], list(range(5, 23)))
        self.assertEqual([g['game'] for g in result['per_game']], [3, 4])

    def test_deadline_propagates(self):
        class Deadline(Exception):
            pass
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 4:
                raise Deadline()
        with self.assertRaises(Deadline):
            G.evaluate([self.prepared()], [1], 'us', guard)

    def test_recorded_recall_and_policy_caps_preserved(self):
        rec = self.prepared()[0]
        rec['recall'] = {'mode': '2', 'lines': {'1': {'cap_hi': 60}}}
        self.assertEqual(G.replay(rec, 1, 1, .35), (60, 60))
        est = rec['est']['fast']['1']
        est['cap'] = 40
        self.assertEqual(G.replay(rec, 1, 1, .35), (40, 40))
        est.update(stated=30)
        self.assertEqual(G.replay(rec, 1, .5, 1.4), (30, 30))

    def test_coverage_and_uncertainty_are_not_assumed_away(self):
        rec = self.prepared()[0]
        est = rec['est']['fast']['1']
        est.update(p=.5, log_spread=1, uncertainty_applied=True)
        a, b = G.replay(rec, 1, .85, .35)
        self.assertLess(a, 85)
        self.assertEqual(b, 0)

    def test_walk_forward_does_not_select_using_test_game_or_future_pairs(self):
        past = [self.prepared(i) for i in range(1, 6)]
        first = self.prepared(6)
        changed = self.prepared(6, m=200)
        changed[0]['est']['unused'] = {'1': {'a_mult': .855, 'sigma': .345}}
        with patch.object(G, 'ALPHAS', (.75, 1.)), patch.object(G, 'SIGMAS', (.35, .8)):
            a = G.evaluate([*past, first], range(1, 7), 'us')['walk_forward']
            b = G.evaluate([*past, changed], range(1, 7), 'us')['walk_forward']
        self.assertEqual(len(a['games']), 1)
        self.assertEqual(a['games'][0]['train_games'], [1, 2, 3, 4, 5])
        self.assertEqual(a['games'][0]['game'], 6)
        for key in ('a_mult', 'sigma'):
            self.assertEqual(a['games'][0][key], b['games'][0][key])
        self.assertNotEqual(a['games'][0]['lower'], b['games'][0]['lower'])

    def test_missing_recall_evidence_is_not_reconstructed(self):
        row = self.prepared()
        row[0]['est']['fast']['1']['recall_applied'] = True
        result = G.evaluate([row], [1], 'us')
        self.assertEqual(result['exclusions'][0]['reason'], 'missing_historical_recall')


class GridIntegrationTests(unittest.TestCase):
    setUp = fixtures.FeedbackTests.setUp
    add = fixtures.FeedbackTests.add
    state = fixtures.FeedbackTests.state
    def test_grid_in_state_report_and_cutoff(self):
        for gid in range(1, 23):
            rec, data = fixture(gid)
            self.add(rec, data)
        state = self.state(before_game=22)
        self.assertEqual(state['parameter_grid']['window_games'], list(range(2, 22)))
        self.assertIsNotNone(state['parameter_grid']['best'])
        self.assertIn('last 20 completed cached games', F.render_report(state))
        self.assertEqual(state['parameter_grid']['window_size'], 20)
        F.write_state(state, self.root / 'grid.json')
        self.assertLess((self.root / 'grid.json').stat().st_size, 5_000_000)
        self.assertIsNotNone(F.load_state(self.root / 'grid.json', 22, now=NOW))
