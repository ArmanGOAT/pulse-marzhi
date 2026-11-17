"""Оптимизатор: жёсткие правила соблюдаются, решение быстрое, ловушка формулы учитывается."""
import pytest

from pulse.config import load_assumptions
from pulse.decision.optimizer import Inputs, optimize


@pytest.fixture(scope="module")
def base_result():
    return optimize(Inputs(target_margin=0.15, p_cut=0.04))


def test_space_and_speed(base_result):
    assert len(base_result.strategies) == 2160 * 4
    assert base_result.seconds < 2.0


def test_plan_respects_hard_rules(base_result):
    plan = base_result.plan
    assert base_result.feasible
    assert plan["p_loss"] <= load_assumptions().v("risk.max_loss_prob")
    assert plan["margin_now"] >= 0.15 and plan["invest"] <= 10e9


def test_higher_revision_risk_means_higher_current_loss_probability():
    low = optimize(Inputs(p_cut=0.05, n_scenarios=800)).current["p_loss"]
    high = optimize(Inputs(p_cut=0.6, n_scenarios=800)).current["p_loss"]
    assert high > low


def test_no_revision_risk_means_no_paid_protection():
    plan = optimize(Inputs(p_cut=0.0, n_scenarios=500)).plan
    assert not plan["levers"]["prepare"] and not plan["levers"]["protect"]


def test_formula_trap_uses_durable_margin():
    r = optimize(Inputs(target_margin=0.15, p_cut=0.04, cip_binds=True, n_scenarios=800))
    assert r.plan["margin_durable"] >= 0.15
    assert r.plan["margin_durable"] <= r.plan["margin_now"] + 1e-9
