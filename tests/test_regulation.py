"""Движок правил: версия по дате, минимум из чисел формулы, отбрасывание выбросов, перерегистрация."""
import pytest

from pulse.domain import regulation as r


def test_version_is_picked_by_effective_date():
    reg = r.load_regulation()
    assert reg.at("2026-03-27").id == "pp-4554-2020"
    assert reg.at("2026-03-28").id == "mz-3735-2026"


def test_basket_change_belarus_to_moldova():
    reg = r.load_regulation()
    assert "BY" in reg.at("2025-12-05").basket and "MD" not in reg.at("2025-12-05").basket
    assert "MD" in reg.at("2026-07-01").basket and "BY" not in reg.at("2026-07-01").basket


def test_reference_is_minimum_and_binding_is_reported():
    res = r.reference_price("2026-07-01", {"RU": 130_000, "KZ": 140_000, "PL": 150_000},
                            origin_price=120_000, cip_avg=113_400)
    assert res.binding == "cip"
    assert res.reference_price == pytest.approx(113_400)
    assert res.importer_ceiling == pytest.approx(113_400 * 1.04)
    assert res.pharmacy_ceiling == pytest.approx(113_400 * 1.04 * 1.15 * 1.20)


def test_cip_is_not_a_component_before_order_3735():
    res = r.reference_price("2025-12-05", {"RU": 130_000, "KZ": 140_000}, cip_avg=50_000)
    assert "cip" not in res.components and res.binding == "ref_mean"


def test_outlier_country_is_excluded():
    kept, dropped = r.exclude_outliers({"RU": 100, "KZ": 105, "PL": 110, "HU": 300}, ratio=0.5)
    assert dropped == ["HU"] and set(kept) == {"RU", "KZ", "PL"}


def test_countries_outside_basket_are_ignored():
    res = r.reference_price("2026-07-01", {"RU": 100_000, "US": 10_000})
    assert res.used_countries == ["RU"]


def test_generic_cap_vs_original():
    res = r.reference_price("2026-07-01", {"RU": 200_000}, original_reference=150_000, is_generic=True)
    assert res.binding == "generic_cap" and res.reference_price == pytest.approx(120_000)


def test_reregistration_threshold_changed_from_20_to_15_percent():
    assert not r.needs_reregistration(100, 83, "2025-12-05")
    assert r.needs_reregistration(100, 84, "2026-07-01")


def test_gap_vs_current_ceiling():
    res = r.reference_price("2026-07-01", {"RU": 125_000}, cip_avg=113_400)
    assert res.gap_vs(150_000) == pytest.approx(150_000 / (113_400 * 1.04) - 1)
