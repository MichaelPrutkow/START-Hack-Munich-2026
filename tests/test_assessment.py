"""Offline price semantics, arithmetic, independent review and publication guards."""
import math
import threading
import time
import unittest
from unittest.mock import patch
from claim_to_fame import assessment as A, pricing as P, prompts
from claim_to_fame.decision_review import group_diagnostics
import runner as R
from tests import test_runtime as runtime


def structured(amount=60, share=.5, base=120):
    return {'covered_prob': 1., 't_if_covered': amount, 'valuation': {
        'category': 'ancillary', 'components': [{'label': 'estimated freight', 'quantity': 1, 'unit_gross': base}],
        'covered_share': share, 'share_basis': 'equal allocation under hypothetical policy',
        'coverage_basis': 'covered principal item'}}


class AssessmentTests(unittest.TestCase):
    def test_share_changes_amount_not_coverage_or_twice(self):
        e = P.estimate_votes([structured()])
        self.assertTrue(e['valuation_consistent'])
        self.assertEqual((e['p'], e['m']), (1, 60))
        a, b = P.price(e['p'], e['m'], None, None, 0, a_mult=.85, sigma=.35)
        self.assertEqual(a, 51)
        self.assertGreater(b, 50)
        uncertain = structured()
        uncertain['covered_prob'] = .5
        self.assertEqual(P.estimate_votes([uncertain])['p'], .5)

    def test_invalid_breakdown_rejected_legacy_and_null_still_readable(self):
        for change in ('amount', 'share', 'nan', 'overflow'):
            v = structured()
            if change == 'amount': v['t_if_covered'] = 120
            if change == 'share': v['valuation']['covered_share'] = 2
            if change == 'nan': v['valuation']['components'][0]['unit_gross'] = math.nan
            if change == 'overflow': v['valuation']['components'][0].update(quantity=1e308, unit_gross=1e308)
            self.assertEqual(P.estimate_votes([v])['valid_count'], 0)
        for v in ({'covered_prob': 1, 't_if_covered': 60},
                  {'covered_prob': 1, 't_if_covered': 60, 'valuation': None}):
            self.assertEqual(P.estimate_votes([v])['valid_count'], 1)

    def test_line_cap_applied_but_shared_cap_not_applied_twice(self):
        v = structured(amount=40)
        v.update(stated_cap=40, shared_cap=30)
        self.assertEqual(A.audit(v)['status'], 'consistent')
        self.assertEqual(A.audit(v)['conditional_total'], 40)

    def test_same_model_votes_do_not_gain_confidence_by_repetition(self):
        one = P.estimate_votes([structured()])
        many = P.estimate_votes([structured() for _ in range(20)])
        self.assertEqual(R.estimate_quality(one), R.estimate_quality(many))

    def test_large_change_needs_independent_consistent_breakdown(self):
        old = P.estimate_votes([structured(amount=1400, base=1400, share=1)])
        new = P.estimate_votes([structured(amount=1200, base=1200, share=1)]*3)
        self.assertFalse(R.prefer_estimate(new, old, level=2, previous_level=1))
        new['independent_recheck'] = True
        self.assertTrue(R.prefer_estimate(new, old, level=2.5, previous_level=1))
        new['valuation_consistent'] = False
        self.assertFalse(R.prefer_estimate(new, old, level=2.5, previous_level=1))

    def test_focus_has_original_full_invoice_but_no_previous_prices_or_history(self):
        items={1: {'description': 'covered unit', 'quantity': 1, 'unit': 'pcs'},
               2: {'description': 'excluded unit', 'quantity': 1, 'unit': 'pcs'}}
        msg=prompts.build_focus_prompt('Policy', 'Damage', {1: items[1]},
                                      {1: {'m': 987654, 'clause': 'SECRET PRIOR'}},
                                      historical_context='SECRET HISTORY', all_items=items)[1]['content']
        self.assertIn('excluded unit', msg)
        self.assertIn('and no others: 1.', msg)
        for text in ('987654', 'SECRET PRIOR', 'SECRET HISTORY'):
            self.assertNotIn(text, msg)

    def test_strict_response_schema_requires_every_declared_property(self):
        def check(schema):
            if 'object' in ([schema.get('type')] if isinstance(schema.get('type'), str) else schema.get('type', [])):
                self.assertEqual(set(schema['properties']), set(schema['required']))
                self.assertFalse(schema['additionalProperties'])
                for value in schema['properties'].values():
                    check(value)
            if schema.get('type') == 'array':
                check(schema['items'])
        check(prompts.SCHEMA)

    def test_group_diagnostics_are_descriptive_intervals(self):
        rows=[{'game': 1, 'lines': [{'t': [40, 50], 'decision_review': {
            'valuation_category': 'ancillary', 'm_raw': 60, 'estimate_position': 'above_known_unfair'}}]}]
        result=group_diagnostics(rows)['ancillary']
        self.assertEqual(result['activation'], 'none')
        self.assertAlmostEqual(result['median_fair_to_estimate_lower'], 2/3, places=4)


class SelectionRuntimeTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    prepare_round = runtime.RuntimeTests.prepare_round

    def test_unreviewed_deep_retained_but_independent_focus_can_correct(self):
        self.enterContext(patch.dict(R.os.environ, {'FOCUS_ENABLED': '1', 'FOCUS_MAX_LINES': '1'}))
        self.prepare_round([R.Tier('fast', 1, 'fake', None, 1, 20), R.Tier('deep', 2, 'fake', None, 3, 50)])
        ready=threading.Event()
        original=R.submit.side_effect
        def submit(*args):
            result=original(*args)
            if args[-1]=='fast': ready.set()
            return result
        R.submit.side_effect=submit
        def assess(msg, budget, tier):
            if tier.name=='deep': self.assertTrue(ready.wait(2))
            amount={'fast': 1400, 'deep': 1200, 'focus': 1350}[tier.name]
            return [{1: structured(amount=amount, base=amount, share=1)} for _ in range(tier.votes)], {}
        self.enterContext(patch.object(R, 'assess', side_effect=assess))
        rec=R.play_game(1, time.time(), dry=True)
        self.assertEqual([tag for tag, _ in self.sent], ['provisional', 'fast', 'focus'])
        self.assertEqual(rec['line_sources'][1], 'focus')
        self.assertEqual(rec['final_ab'][1][0], 1147.5)
        self.assertEqual(rec['final_ab'][4], (60, 60))
        self.assertEqual(rec['est']['deep'][1]['selection']['reason'], 'large_price_change_needs_independent_breakdown')

    def test_focus_failure_keeps_confirmed_fast_when_deep_change_is_blocked(self):
        self.enterContext(patch.dict(R.os.environ, {'FOCUS_ENABLED': '1', 'FOCUS_MAX_LINES': '1'}))
        self.prepare_round([R.Tier('fast', 1, 'fake', None, 1, 20), R.Tier('deep', 2, 'fake', None, 3, 50)])
        ready = threading.Event()
        original = R.submit.side_effect
        def submit(*args):
            result = original(*args)
            if args[-1] == 'fast': ready.set()
            return result
        R.submit.side_effect = submit
        def assess(msg, budget, tier):
            if tier.name == 'deep': self.assertTrue(ready.wait(2))
            if tier.name == 'focus': raise TimeoutError('fake focus timeout')
            amount = 1400 if tier.name == 'fast' else 1200
            return [{1: structured(amount=amount, base=amount, share=1)}], {}
        self.enterContext(patch.object(R, 'assess', side_effect=assess))
        rec = R.play_game(1, time.time(), dry=True)
        self.assertEqual([tag for tag, _ in self.sent], ['provisional', 'fast'])
        self.assertEqual(rec['final_ab'][1][0], 1190)
        self.assertEqual(rec['line_sources'][1], 'fast')
