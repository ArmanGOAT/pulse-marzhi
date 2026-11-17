"""Симулятор склада: запас не ниже месяца, подготовка уменьшает склад в день пересмотра, решение зависит от вероятности."""
import pytest

from pulse.domain import inventory as inv

ANNUAL, UNIT_COST = 150_000, 108_600


@pytest.mark.parametrize("prepare", [False, True])
@pytest.mark.parametrize("n", [1, 2, 4, 6, 12])
def test_stock_never_below_one_month(n, prepare):
    run = inv.simulate(ANNUAL, n, revision_day=180, prepare=prepare)
    assert run.min_stock_months >= 0.99


def test_preparing_lowers_stock_on_revision_day_and_costs_extra_shipments():
    regular = inv.simulate(ANNUAL, 2, revision_day=180)
    prepared = inv.simulate(ANNUAL, 2, revision_day=180, prepare=True)
    assert prepared.stock_on_revision < regular.stock_on_revision
    assert prepared.shipments > regular.shipments


def test_expected_exposure_two_shipments_is_four_months():
    assert inv.expected_exposure(ANNUAL, 2, prepare=False) == pytest.approx(ANNUAL * 4 / 12)
    assert inv.expected_exposure(ANNUAL, 2, prepare=True) == pytest.approx(ANNUAL * 1.5 / 12)


def test_devaluation_loss_formula():
    assert inv.devaluation_loss(50_000, UNIT_COST, 0.20) == pytest.approx(50_000 * UNIT_COST * 0.20 * 1.02)


def test_decision_flips_with_probability():
    low = inv.prepare_decision(0.05, 0.20, ANNUAL, 2, UNIT_COST)
    high = inv.prepare_decision(0.60, 0.20, ANNUAL, 2, UNIT_COST)
    assert not low.prepare and high.prepare
    assert low.threshold_prob == pytest.approx(high.threshold_prob)
    assert 0.05 < low.threshold_prob < 0.60
