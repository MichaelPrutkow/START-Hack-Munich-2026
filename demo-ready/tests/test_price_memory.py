"""Synthetic lookalikes, unit/scope boundaries and per-line retrieval coverage."""
import copy
import json
import unittest
from claim_to_fame import price_memory as M


def entry(game=1, **kwargs):
    return {'game': game, 'line': 1, 'description': 'Replace damaged copper supply pipe',
            'quantity': 2, 'unit': 'm', 'policy_hash': 'policy', 'fair_total': [100., 120.],
            'policy_limited': False, 'full_share': True, 'coverage': 'historically covered',
            'source': {'cache': f'game_{game}.json'}, **kwargs}


def item(**kwargs):
    return {'description': 'Replace damaged copper supply pipe', 'quantity': 2, 'unit': 'm', **kwargs}


class MemoryTests(unittest.TestCase):
    def test_similar_wording_and_reordering_retrieve(self):
        for text in ('Replacement of damaged copper supply pipe', 'Copper supply pipe replacement',
                     'Replace copper pipe'):
            with self.subTest(text=text):
                hits = M.retrieve([entry()], {'7': item(description=text)}, 'policy')
                self.assertIn('7', hits)
                self.assertGreaterEqual(hits['7'][0]['similarity'], .6)

    def test_object_action_size_negation_and_quality_are_not_equivalent(self):
        for a, b in [('Replace copper pipe', 'Inspect copper pipe'),
                     ('Replace 55 inch TV', 'Replace 65 inch TV'),
                     ('Replace premium TV', 'Replace standard TV'),
                     ('Replace copper supply pipe', 'Replace plastic drainage pipe'),
                     ('Repair device with no water contact', 'Repair device with water contact')]:
            self.assertEqual(M.similarity(a, b), 0., (a, b))

    def test_future_duplicates_and_same_game_are_not_independent_evidence(self):
        memory = [entry(1), entry(1), entry(2), entry(3)]
        hits = M.retrieve(memory, {1: item()}, 'policy', before_game=3)['1']
        self.assertEqual({h['source_game'] for h in hits}, {1, 2})
        self.assertIsNotNone(M.suggest(50, hits))
        self.assertIsNone(M.suggest(50, hits[:1] * 3))
        many = [entry(1), *[entry(2, line=i) for i in range(10)]]
        self.assertEqual({h['source_game'] for h in M.retrieve(many, {1: item()}, 'policy')['1']}, {1, 2})

    def test_scaled_comparisons_are_not_current_bounds_or_unit_market_prices(self):
        hits = M.retrieve([entry(1), entry(2)], {1: item(quantity=4)}, 'policy')['1']
        self.assertEqual(hits[0]['comparison_total_eur'], [200., 240.])
        self.assertEqual(hits[0]['basis'], 'quantity_scaled_comparison')
        self.assertFalse(hits[0]['numeric_eligible'])
        self.assertIsNone(M.suggest(50, hits))
        for prior in (entry(full_share=False), entry(policy_limited=True)):
            self.assertEqual(M.retrieve([prior], {1: item(quantity=4)}, 'policy'), {})
        self.assertEqual(M.retrieve([entry()], {1: item(unit='hrs')}, 'policy'), {})

    def test_cross_policy_is_advisory_and_capped_cross_policy_rejected(self):
        hits = M.retrieve([entry(1), entry(2)], {1: item()}, 'other')['1']
        self.assertFalse(hits[0]['same_policy'])
        self.assertIsNone(M.suggest(50, hits))
        self.assertEqual(M.retrieve([entry(policy_limited=True)], {1: item()}, 'other'), {})

    def test_suggestions_can_raise_and_lower_without_midpoint_or_open_bound_guess(self):
        hits = M.retrieve([entry(1), entry(2)], {1: item()}, 'policy')['1']
        self.assertGreater(M.suggest(50, hits)['m'], 50)
        self.assertLessEqual(M.suggest(50, hits)['m'], 62.5)
        self.assertLess(M.suggest(200, hits)['m'], 200)
        self.assertGreaterEqual(M.suggest(200, hits)['m'], 150)
        self.assertIsNone(M.suggest(110, hits))
        for bounds in ([130, 140], [100, None], [100, 300]):
            different = M.retrieve([entry(1), entry(2, fair_total=bounds)], {1: item()}, 'policy')['1']
            self.assertIsNone(M.suggest(50, different))

    def test_round_robin_includes_more_than_four_positions_and_obeys_budget(self):
        matches = M.retrieve([entry(1), entry(2)], {i: item() for i in range(1, 8)}, 'policy')
        text = M.context(matches, max_chars=5000)
        records = [json.loads(line) for line in text.splitlines()[1:]]
        self.assertEqual([r['current_line'] for r in records[:7]], [str(i) for i in range(1, 8)])
        self.assertLessEqual(len(text), 5000)
        self.assertEqual(M.context(matches, max_chars=100), '')

    def test_inputs_unchanged_and_malformed_records_ignored(self):
        memory = [entry(), {}, entry(quantity=float('nan')), entry(fair_total=[0, 100])]
        before = copy.deepcopy(memory[:2])
        M.retrieve(memory, {1: item()}, 'policy')
        self.assertEqual(memory[:2], before)
