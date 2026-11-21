import json
import unittest
from unittest.mock import patch
import research_prices as E
from tests.test_price_research import prepared
from tests.test_price_memory import entry
from tests.test_feedback import POLICY


class ExperimentTests(unittest.TestCase):
    def test_same_budget_paired_variants_no_future_evidence_and_frozen_coverage(self):
        rec, data, lines, final = prepared(3)
        calls = []
        def provider(messages):
            calls.append(messages)
            return [{1: {'covered_prob': 1., 't_if_covered': 110}}], {'calls': 1}
        memory = [entry(1, policy_hash=rec['policy_hash']), entry(3, policy_hash=rec['policy_hash'])]
        rows = E.experiment(rec, data, lines, final, POLICY, 'synthetic loss', memory, provider,
                            lambda: None, team='us', holdout_from=3)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('HISTORICAL RANGE', calls[0][1]['content'])
        self.assertIn('"source_game":1', calls[1][1]['content'])
        self.assertNotIn('"source_game":3', calls[1][1]['content'])
        self.assertTrue(all(r['status'] == 'completed' and r['usable_lines'] == 1 for r in rows))
        self.assertEqual(E.summarize(rows)[2]['paired_games'], [3])
        json.dumps(rows, allow_nan=False)

    def test_failed_variant_is_excluded_from_paired_comparison(self):
        rec, data, lines, final = prepared(3)
        outcomes = iter([([{1: {'covered_prob': 1., 't_if_covered': 110}}], {}), RuntimeError('private')])
        def provider(messages):
            value = next(outcomes)
            if isinstance(value, Exception): raise value
            return value
        rows = E.experiment(rec, data, lines, final, POLICY, '', [], provider, lambda: None,
                            team='us', holdout_from=3)
        self.assertEqual(rows[1]['reason'], 'RuntimeError')
        self.assertNotIn('private', str(rows))
        self.assertTrue(all(r['paired_games'] == [] for r in E.summarize(rows)))

    def test_deadline_retains_completed_variant_and_stops_new_calls(self):
        rec, data, lines, final = prepared(3)
        calls = []
        def guard():
            if calls: raise TimeoutError()
        def provider(messages):
            calls.append(messages)
            return [{1: {'covered_prob': 1., 't_if_covered': 110}}], {}
        rows = E.experiment(rec, data, lines, final, POLICY, '', [], provider, guard,
                            team='us', holdout_from=3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(calls), 1)

    def test_default_command_only_plans_without_credentials_or_network(self):
        with patch('builtins.print') as output, patch('requests.sessions.Session.request', side_effect=AssertionError):
            E.main(['--games', '1', '2', '--holdout-from', '2'])
        self.assertEqual(json.loads(output.call_args.args[0])['mode'], 'plan_only')
