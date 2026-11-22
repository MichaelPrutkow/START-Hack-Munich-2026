"""Offline between-round estimator replay: cashflow bounds, provenance and chronology."""
import copy
import json
import unittest
from unittest.mock import patch
from claim_to_fame import feedback as F, m_research as M
from tests.test_feedback import fixture, NOW
from tests import test_feedback as fixtures


def prepared(gid=1, **kwargs):
    rec, data = fixture(gid, **kwargs)
    lines, final = F._reconcile(rec, data, 'us')
    return rec, data, lines, final


class ResearchTests(unittest.TestCase):
    def test_baseline_exact_and_inputs_unchanged(self):
        row = prepared()
        before = copy.deepcopy(row)
        result = M.run([row], [1], 'us')
        baseline = next(r for r in result['ranking'] if r['source'] == 'recorded' and r['m_factor'] == 1)
        self.assertEqual((baseline['delta_lower'], baseline['delta_upper']), (0, 0))
        self.assertEqual(result['candidate_count'], 808)
        self.assertEqual(result['activation'], 'none')
        self.assertEqual(row, before)
        self.assertEqual(result['source_counts']['deep'], 0)
        self.assertNotIn('deep', {r['source'] for r in result['ranking']})
        json.dumps(result, allow_nan=False)

    def test_compatible_sources_and_model_deduplication(self):
        rec = prepared()[0]
        chosen = rec['est']['fast']['1']
        rec['est']['deep'] = {'1': {**chosen, 'm': 200}}
        rec['est']['focus'] = {'1': {**chosen, 'm': 1000}}
        values = M.alternatives(rec, 1, chosen)
        self.assertEqual(values['tier_median'], 150)
        self.assertAlmostEqual(values['tier_geomean'], (100*200)**.5)
        self.assertEqual(values['focus'], 1000)
        rec['models']['deep'] = rec['models']['fast']
        self.assertNotIn('tier_median', M.alternatives(rec, 1, chosen))
        self.assertNotIn('fast_deep_blend', M.alternatives(rec, 1, chosen))
        rec['est']['deep']['1']['p'] = .2
        self.assertNotIn('deep', M.alternatives(rec, 1, chosen))

    def test_missing_sources_keep_actual_not_scaled_fallback(self):
        one, two = prepared(1), prepared(2)
        one[0]['est']['deep'] = {'1': {**one[0]['est']['fast']['1'], 'm': 120}}
        result = M.run([one, two], [1, 2], 'us')
        self.assertEqual(result['per_game'][1]['unchanged_missing_source']['deep'], 1)
        self.assertEqual(result['source_counts']['deep'], 1)

    def test_shared_caps_and_unknown_baseline_fixed_for_all_candidates(self):
        row = prepared()
        row[0]['est']['fast']['1']['shared'] = 100
        result = M.run([row], [1], 'us')
        self.assertEqual(result['status'], 'no_replayable_estimates')
        self.assertEqual(result['exclusions'][0]['reason'], 'document_anchor_or_shared_allocation')
        self.assertEqual(result['ranking'], [])

    def test_same_inputs_reused_changed_input_invalidates(self):
        row = prepared()
        result = M.run([row], [1], 'us')
        reused = M.run([row], [1], 'us', previous=result)
        self.assertTrue(reused['reused'])
        row[0]['commit'] = 'new-estimator-version'
        changed = M.run([row], [1], 'us', previous=result)
        self.assertFalse(changed['reused'])
        self.assertNotEqual(changed['fingerprint'], result['fingerprint'])

    def test_deadline_propagates_and_no_partial_result(self):
        class Deadline(Exception): pass
        count = 0
        def check():
            nonlocal count
            count += 1
            if count == 5: raise Deadline()
        with self.assertRaises(Deadline):
            M.run([prepared()], [1], 'us', checkpoint=check)

    def test_later_game_does_not_change_selected_estimator_or_factor(self):
        past = [prepared(i) for i in range(1, 6)]
        with patch.object(M, 'FACTORS', (.8, 1., 1.2)):
            a = M.run([*past, prepared(6)], range(1, 7), 'us')['walk_forward']['games'][0]
            b = M.run([*past, prepared(6, m=200)], range(1, 7), 'us')['walk_forward']['games'][0]
        self.assertEqual(a['train_games'], list(range(1, 6)))
        self.assertEqual(a['game'], 6)
        self.assertEqual((a['source'], a['m_factor']), (b['source'], b['m_factor']))
        self.assertNotEqual(a['lower'], b['lower'])

    def test_every_recorded_factor_bounds_known_synthetic_payoff(self):
        row = prepared()
        result = M.run([row], [1], 'us')
        for entry in result['ranking']:
            if entry['source'] != 'recorded': continue
            m = 100 * entry['m_factor']
            a, b = M.price(1, m, None, None, 0, a_mult=.85, sigma=.35)
            opponents = [(100, 150), (120, 150), (0, 0)]
            incoming = sum(min(a, 540) if a <= ob else 1.5*a if a <= 110 else 0 for _, ob in opponents)
            outgoing = sum(min(oa, 540) if oa <= b else 1.5*oa if oa <= 110 else 0 for oa, _ in opponents)
            self.assertLessEqual(entry['lower'], incoming-outgoing+.011)
            if entry['upper'] is not None:
                self.assertGreaterEqual(entry['upper'], incoming-outgoing-.011)

    def test_window_and_missing_games(self):
        result = M.run([prepared(1), prepared(25)], range(1, 26), 'us')
        self.assertEqual(result['window_games'], list(range(6, 26)))
        self.assertEqual(result['missing_games'], list(range(6, 25)))


class ResearchIntegrationTests(unittest.TestCase):
    setUp = fixtures.FeedbackTests.setUp
    add = fixtures.FeedbackTests.add
    state = fixtures.FeedbackTests.state

    def test_learning_state_report_load_and_reuse(self):
        self.add(*fixture())
        state = self.state()
        self.assertIn('Conditional-price estimator research', F.render_report(state))
        self.assertEqual(state['m_research']['activation'], 'none')
        target = self.root / 'learning.json'
        F.write_state(state, target)
        self.assertIsNotNone(F.load_state(target, 2, now=NOW))
        rebuilt = self.state(previous_state=state)
        self.assertTrue(rebuilt['m_research']['reused'])
