"""Shadow variants (logged, never sent) and their scoring. Synthetic data only; no network."""
import unittest

import runner as R
from claim_to_fame import tuning as LR
from tests.test_tuning import example_game


class ShadowTests(unittest.TestCase):
    def test_variants_change_only_llm_lines_without_document_amount(self):
        items = {1: {"quantity": 1}, 2: {"quantity": 12}, 3: {"quantity": 1}, 4: {"quantity": 1}}
        final = {1: (100.0, 90.0), 2: (200.0, 210.0), 3: (50.0, 50.0), 4: (30.0, 40.0)}
        details = {1: {"stated": None}, 2: {"stated": None}, 3: {"stated": 50.0}}   # 4: rules
        v = R.shadow_variants(items, final, details)
        self.assertEqual(v["a80_all"], {1: (80.0, 90.0), 2: (160.0, 210.0), 3: (50.0, 50.0), 4: (30.0, 40.0)})
        self.assertEqual(v["a80_uncertain"][1], (80.0, 90.0))      # one lump sum: uncertain
        self.assertEqual(v["a80_uncertain"][2], (200.0, 210.0))    # quantity basis given
        self.assertEqual(v["b_le_80pct_a"][1], (100.0, 80.0))
        self.assertEqual(v["b_le_80pct_a"][3], (50.0, 50.0))       # document amount untouched
        self.assertEqual(v["b_le_80pct_a"][4], (30.0, 40.0))       # rule line untouched

    def test_report_scores_variant_against_what_was_sent(self):
        # Beta sent 150/90 (official example, net -300). A fair 100 would be paid by Delta (b 110)
        # and earn 1.5 x 100 from Alpha's rejection or 100 from acceptance (Alpha b in [100, 150)).
        rounds = {1: {"game": 1, "final_ab": {"1": [150.0, 90.0]},
                      "shadow": {"fair": {"1": [100.0, 90.0]}, "same": {"1": [150.0, 90.0]}}}}
        rep = LR.shadow_report({1: example_game()}, rounds, "Beta")
        self.assertEqual(rep["same"]["lines"], 0)
        self.assertEqual(rep["fair"]["lines"], 1)
        self.assertGreaterEqual(rep["fair"]["lower"], 200.0)
        self.assertLessEqual(rep["fair"]["upper"], 250.0)


if __name__ == "__main__":
    unittest.main()
