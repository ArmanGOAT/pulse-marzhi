"""Инварианты двойника упаковки: ключевые цифры из условия задачи не должны «уплыть» при правках."""
import pandas as pd
import pytest

from pulse.config import load_levers
from pulse.domain import pack


def _one(**changes) -> pd.DataFrame:
    row = load_levers().current.model_dump() | changes
    df = pd.DataFrame([row])
    lv = load_levers()
    df["invest"] = pack.invest_of(df, lv)
    df["months"] = pack.months_of(df, lv)
    return df


def test_base_margin_matches_task_condition():
    c = pack.components(pack.current_strategy())
    assert c.margin_sum[0] == pytest.approx(3351, abs=1)
    assert c.margin[0] == pytest.approx(0.0223, abs=0.0001)


def test_usd_share_and_breakeven():
    c = pack.components(pack.current_strategy())
    assert c.usd_share[0] == pytest.approx(0.795, abs=0.001)
    assert c.breakeven_fx()[0] == pytest.approx(12322, abs=2)


def test_plus_three_percent_dollar_turns_margin_negative():
    c = pack.components(pack.current_strategy(), fx=12000 * 1.03)
    assert c.margin[0] < 0


def test_strategy_space_is_2160():
    assert len(pack.strategy_table()) == 6 * 3 * 3 * 2 * 5 * 4 == 2160


def test_breakdown_sums_to_price_minus_margin():
    b = pack.breakdown()
    c = pack.components(pack.current_strategy())
    assert b["uzs_per_pack"].sum() == pytest.approx(c.price - c.margin_sum[0], rel=1e-9)


def test_quick_measures_regression():
    quick = _one(shipments=6, pooled=True, procurement="joint", digital=0.8)
    c = pack.components(quick)
    assert c.margin[0] == pytest.approx(0.142, abs=0.002)
    assert quick["invest"][0] == pytest.approx(0.25e9)
    assert quick["months"][0] == 4


def test_air_freight_is_worse_than_auto():
    base = pack.components(pack.current_strategy()).margin[0]
    air = pack.components(_one(mode="air")).margin[0]
    assert air < base
