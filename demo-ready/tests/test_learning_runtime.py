"""Automatic learning orchestration uses only mocked services and game APIs here."""
import os
import unittest
from unittest.mock import Mock, patch

import runner as R


class LearningRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.multiple(R, A_MULT=.85, SIGMA=.35, DISAGREE=1.5,
            TIERS=[R.Tier("fast", 1, "fake-fast", None, 3, 20),
                   R.Tier("deep", 2, "fake-deep", "high", 3, 50)]))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")))

    def test_dry_and_disabled_runs_do_not_create_learning_service(self):
        with patch("claim_to_fame.learning_service.LearningService") as service:
            self.assertIsNone(R.make_learning_service(True))
            with patch.dict(os.environ, {"LEARNING_AUTO": "0"}):
                self.assertIsNone(R.make_learning_service(False))
            service.assert_not_called()

    def test_live_scheduler_configures_single_service_and_closes_on_stop(self):
        service = Mock()
        with patch.object(R, "make_learning_service", return_value=service), \
             patch.object(R, "run_schedule", side_effect=KeyboardInterrupt) as schedule:
            with self.assertRaises(KeyboardInterrupt):
                R.run_forever(False, True)
        schedule.assert_called_once_with(False, True, service)
        service.close.assert_called_once_with()

    def test_schedule_hands_off_background_work_without_waiting(self):
        service = Mock()
        schedule = [(1, 1), (2, 1000)]
        with patch.object(R, "POOL", Mock(names=[])), \
             patch.object(R, "list_games", return_value=schedule), \
             patch.object(R.time, "time", return_value=100), \
             patch.object(R.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(R, "play_game") as play:
            with self.assertRaises(KeyboardInterrupt):
                R.run_schedule(False, True, service)
        service.tick.assert_called_once_with(schedule, now=100)
        play.assert_not_called()

    def test_identity_does_not_reset_when_prices_or_other_tiers_change(self):
        before = R.strategy_identity()
        R.A_MULT, R.SIGMA = .72, .5
        R.TIERS.append(R.Tier("max", 3, "fake-max", "high", 3, 52))
        after = R.strategy_identity()
        for tier in ("fast", "deep", "focus"):
            self.assertEqual(before["config_hash"][tier], after["config_hash"][tier])
            self.assertEqual(before["prompt_hash"][tier], after["prompt_hash"][tier])
        self.assertNotEqual(before["pricing_defaults"], after["pricing_defaults"])
        self.assertIn("max", after["config_hash"])

    def test_identity_changes_for_estimator_model_votes_or_prompt(self):
        before = R.strategy_identity()
        R.TIERS[0].votes = 5
        changed = R.strategy_identity()
        self.assertNotEqual(before["config_hash"]["fast"], changed["config_hash"]["fast"])
        self.assertEqual(before["config_hash"]["deep"], changed["config_hash"]["deep"])
        with patch.object(R, "SYSTEM", R.SYSTEM + " Different estimator instructions."):
            self.assertNotEqual(before["prompt_hash"]["deep"], R.strategy_identity()["prompt_hash"]["deep"])
        self.assertNotEqual(before["prompt_hash"]["focus"], before["prompt_hash"]["deep"])
