"""Parser, pricing and prompt regression tests; no API or LLM calls."""
import math
import unittest
from unittest.mock import patch

import runner as R


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.multiple(R, A_MULT=0.85, SIGMA=0.35, DISAGREE=1.5))

    def test_parse_case0_text(self):
        lines = """POS. DESCRIPTION AMOUNT UNIT TOTAL
    1 Leak detection and opening of the bathroom wall next 2 hrs
    to the washbasin
    2 Replacement of split copper supply pipe section, 15 1 pcs
    mm, incl. fittings
    3 Closing the wall opening, re-plastering and re-tiling 0.2 m2
    approx. 0.2 m2
    4 Annual service and descaling of the gas boiler in the 1 pcs
    hallway
    Created on 21 Aug 2026 Page 1 / 1""".splitlines()
        it = R.parse_lines(lines)
        assert sorted(it) == [1, 2, 3, 4]
        assert [(it[i]["quantity"], R.norm_unit(it[i]["unit"])) for i in (1, 2, 3, 4)] == \
            [(2.0, "hrs"), (1.0, "pcs"), (0.2, "m2"), (1.0, "pcs")]
        assert it[4]["description"].endswith("in the hallway")
        assert it[2]["description"].endswith("15 mm, incl. fittings")
        assert "Created" not in it[4]["description"]

    def test_last_number_unit_wins(self):
        it = R.parse_lines(["1 Cable 3 m 2 pcs", "2 Paint 10 l bucket 3 pcs"])
        assert (it[1]["quantity"], it[1]["unit"]) == (2.0, "pcs")
        assert (it[2]["quantity"], it[2]["unit"]) == (3.0, "pcs")

    def test_real_case0_pdf(self):
        d = R.OUT / "cases" / "case_000"
        if not (d / "invoices.pdf").exists():
            self.skipTest("out/cases/case_000 not decrypted")
        it = R.parse_items(d)
        assert sorted(it) == [1, 2, 3, 4], it
        assert it[4]["description"].endswith("hallway")

    def test_price_rule(self):
        # p = 1: a = A_MULT m, b = m * exp(SIGMA * z(1/3)) ~ 0.86 m for SIGMA 0.35
        a, b = R.price(1.0, 100.0, None, None, 50.0)
        assert a == 85.0 and 85.0 < b < 87.0, (a, b)
        # less sure it is covered -> lower b; p <= 2/3 -> reject everything
        _, b9 = R.price(0.9, 100.0, None, None, 50.0)
        _, b7 = R.price(0.7, 100.0, None, None, 50.0)
        _, b6 = R.price(0.6, 100.0, None, None, 50.0)
        assert b > b9 > b7 > 0 and b6 == 0.0, (b9, b7, b6)
        # stated value and cap unchanged
        assert R.price(1.0, 100.0, 120.0, None, 50.0) == (120.0, 120.0)
        assert R.price(1.0, 100.0, None, 40.0, 50.0) == (40.0, 40.0)
        # no estimate -> fallback base, never 0/0 charge
        a, _ = R.price(0.2, 0.0, None, None, 50.0)
        assert a == 42.5
        for x in R.price(1.0, 1e-9, None, None, 50.0):
            assert math.isfinite(x) and x >= 0

    def test_shared_cap_pro_rata(self):
        # three lines share one limit of 500 (sum 680 -> scale 500/680); an uncovered line is untouched
        aggs = {1: (0.9, 180.0, None, None, 500.0), 2: (1.0, 320.0, None, None, 500.0),
                3: (1.0, 180.0, None, None, 500.0), 4: (0.2, 100.0, None, None, 500.0),
                5: (1.0, 50.0, None, None, None)}
        out = R.apply_shared_caps(aggs)
        assert abs(sum(out[i][1] for i in (1, 2, 3)) - 500.0) < 1e-6, out
        assert out[4][1] == 100.0 and out[5][1] == 50.0
        # not binding -> unchanged
        assert R.apply_shared_caps({1: (1.0, 100.0, None, None, 5000.0)})[1][1] == 100.0

    def test_policy_amounts(self):
        pol = "2.3.3 Overvoltage\nsub-limit of EUR 5,000 per event.\n\n5.2.4 Lost water\ncapped at [amount].\n\n" \
              "2.6.8 End of life\nbelow 80 per cent of design capacity."
        got = R.policy_amounts(pol)
        assert len(got) == 2 and got[0].startswith("[2.3.3]") and got[1].startswith("[2.6.8]"), got
