"""Synthetic chronological price learning with conservative settlement scoring."""
import copy
import json
import unittest
from claim_to_fame import price_research as P, price_memory as M, feedback as F
from tests.test_feedback import fixture, POLICY, NOW
from tests import test_feedback as fixtures


def prepared(gid, **kw):
    rec, data = fixture(gid, **kw)
    lines, final = F._reconcile(rec, data, 'us')
    return rec, data, lines, final


class PriceResearchTests(unittest.TestCase):
    def test_repeated_underpricing_can_validate_and_raise_only_after_validation(self):
        rows = [prepared(i, lines=4, m=50) for i in range(1, 8)]
        before = copy.deepcopy(rows)
        research = P.evaluate(rows, 'us')
        c = research['cohorts'][0]
        self.assertEqual(c['status'], 'validated', c)
        self.assertGreater(c['without_best_game'], 0)
        self.assertTrue(P.valid(research, list(range(1, 8))))
        self.assertEqual(rows, before)
        for record in c['rows']:
            self.assertLess(max(record['source_games']), record['game'])
        memory = [M.make_entry(r, 1, r['items']['1'], r['est']['fast']['1'], l[1]['t'], 'cache')
                  for r, d, l, f in rows]
        state = {'price_research': research, 'memory': memory, 'max_game': 7}
        rec = rows[-1][0]
        est = rec['est']['fast']['1']
        args = (state, rec['items']['1'], est, P.identity(rec, 'fast'), rec['policy_hash'], 8)
        changed, diagnostic = P.adjust(*args)
        self.assertGreater(changed, 50)
        self.assertTrue(diagnostic['applied'])
        self.assertEqual(P.adjust(*args, mode='shadow')[0], 50)
        self.assertEqual(P.adjust(*args, mode='off')[0], 50)
        self.assertEqual(P.adjust(*args[:-1], 7)[0], 50)
        invalid = copy.deepcopy(research)
        invalid['cohorts'][0]['rows'][0]['source_games'].append(7)
        self.assertFalse(P.valid(invalid, list(range(1, 8))))
        invalid = copy.deepcopy(research)
        invalid['cohorts'][0]['changed_lines'] += 1
        self.assertFalse(P.valid(invalid, list(range(1, 8))))
        json.dumps(research, allow_nan=False)

    def test_current_and_future_outcomes_do_not_change_prediction(self):
        past = [prepared(i, m=50) for i in range(1, 5)]
        a = P.evaluate([*past, prepared(5, m=50)], 'us')['cohorts'][0]['rows']
        b = P.evaluate([*past, prepared(5, m=50, low=20, high=40, true_t=30), prepared(6)], 'us')['cohorts'][0]['rows']
        self.assertEqual([r['m_after'] for r in a], [r['m_after'] for r in b if r['game'] <= 5])
        self.assertNotEqual(a[-1]['delta_lower'], [r for r in b if r['game'] == 5][0]['delta_lower'])

    def test_category_fit_is_shrunk_and_isolated_by_identity(self):
        who = P.identity(prepared(1)[0], 'fast')
        obs = [{'game': g, 'line': i, 'identity': who, 'category': 'equipment', 'ratio': [1.5, 2.]}
               for g in range(1, 4) for i in range(3)]
        fit = P.category_fit(obs, 'equipment', who)
        self.assertGreater(fit['factor'], 1)
        self.assertLess(fit['factor'], 1.25)
        self.assertIsNone(P.category_fit(obs, 'labour', who))
        self.assertIsNone(P.category_fit(obs, 'equipment', {**who, 'model': 'other'}))
        self.assertIsNone(P.category_fit(obs[:7], 'equipment', who))

    def test_category_learning_validates_without_matching_items_and_does_not_enable_other_categories(self):
        rows = [prepared(i, lines=4, m=50) for i in range(1, 8)]
        for rec, data, lines, final in rows:
            rec['policy_hash'] = f"different-policy-{rec['game']}"
            for est in rec['est']['fast'].values():
                est['valuation_category'] = 'equipment'
        research = P.evaluate(rows, 'us')
        candidate = research['cohorts'][0]
        self.assertEqual(candidate['channel'], 'category_interval_fit')
        self.assertEqual(candidate['status'], 'validated', candidate)
        rec = rows[-1][0]
        state = {'price_research': research, 'memory': [], 'max_game': 7}
        est = rec['est']['fast']['1']
        args = (state, rec['items']['1'], est, P.identity(rec, 'fast'), rec['policy_hash'], 8)
        self.assertGreater(P.adjust(*args)[0], 50)
        self.assertEqual(P.adjust(*args[:2], {**est, 'valuation_category': 'labour'}, *args[3:])[0], 50)

    def test_no_changes_for_document_caps_or_existing_calibration(self):
        for patch in ({'stated': 100}, {'cap': 100}, {'shared': 200}, {'p': .5},
                      {'calibration_applied': True}):
            row = prepared(1, m=50)
            row[0]['est']['fast']['1'].update(patch)
            self.assertFalse(P.scope(row[0]['est']['fast']['1']))

    def test_small_sample_or_harmful_later_data_cannot_promote(self):
        small = P.evaluate([prepared(i, m=50) for i in range(1, 5)], 'us')
        self.assertEqual(small['cohorts'][0]['status'], 'shadow')
        rows = [prepared(i, lines=4, m=50) for i in range(1, 3)]
        rows += [prepared(i, lines=4, m=50, low=40, high=50, true_t=45) for i in range(3, 7)]
        result = P.evaluate(rows, 'us')
        self.assertTrue(all(c['status'] == 'shadow' for c in result['cohorts']))

    def test_deadline_is_not_swallowed(self):
        def expired(): raise TimeoutError('deadline')
        with self.assertRaises(TimeoutError):
            P.evaluate([prepared(1)], 'us', checkpoint=expired)


class PriceLearningIntegration(unittest.TestCase):
    setUp = fixtures.FeedbackTests.setUp
    add = fixtures.FeedbackTests.add
    state = fixtures.FeedbackTests.state

    def test_state_roundtrip_and_report(self):
        for gid in range(1, 7):
            self.add(*fixture(gid, lines=4, m=50))
        state = self.state()
        self.assertEqual(state['price_research']['cohorts'][0]['status'], 'validated')
        self.assertIn('Comparable-item and category', F.render_report(state))
        path = self.root / 'learning.json'
        F.write_state(state, path)
        self.assertIsNotNone(F.load_state(path, 7, now=NOW))
