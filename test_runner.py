"""Offline tests (no API, no LLM): uv run python test_runner.py"""
import math
from pathlib import Path

import runner as R


def test_parse_case0_text():
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


def test_last_number_unit_wins():
    it = R.parse_lines(["1 Cable 3 m 2 pcs", "2 Paint 10 l bucket 3 pcs"])
    assert (it[1]["quantity"], it[1]["unit"]) == (2.0, "pcs")
    assert (it[2]["quantity"], it[2]["unit"]) == (3.0, "pcs")


def test_real_case0_pdf():
    d = R.OUT / "cases" / "case_000"
    if not (d / "invoices.pdf").exists():
        print("  skip: out/cases/case_000 not decrypted")
        return
    it = R.parse_items(d)
    assert sorted(it) == [1, 2, 3, 4], it
    assert it[4]["description"].endswith("hallway")


def test_price_rule():
    # p = 1: a = 0.9 m, b = m * exp(0.35 * z(1/3)) ~ 0.86 m
    a, b = R.price(1.0, 100.0, None, None, 50.0)
    assert a == 90.0 and 85.0 < b < 87.0, (a, b)
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
    assert a == 45.0
    for x in R.price(1.0, 1e-9, None, None, 50.0):
        assert math.isfinite(x) and x >= 0


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
