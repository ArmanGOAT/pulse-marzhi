"""Слой данных: проверки качества по контрактам и выборка «на дату» без подглядывания в будущее."""
import json

import pandas as pd
import pytest

from pulse.data import store
from pulse.data.contracts import PRICES, DataQualityError, validate


def _price(**over) -> dict:
    row = {"drug_id": "D1", "price_type": "ceiling", "country": "UZ", "currency": "UZS",
           "amount": 100.0, "valid_from": "2025-12-05", "valid_to": None, "status": "fact", "source": "test"}
    return row | over


def test_valid_table_passes_and_types_are_coerced():
    out = validate(pd.DataFrame([_price()]), PRICES)
    assert str(out["valid_from"].dtype).startswith("datetime64")
    assert out["amount"].dtype == float


@pytest.mark.parametrize("bad", [
    {"amount": -5.0},          # точность: цена не может быть отрицательной
    {"currency": "XXX"},       # согласованность: валюта не по ISO 4217
    {"status": "maybe"},       # статус только факт / оценка / демо
    {"drug_id": None},         # полнота: обязательное поле
])
def test_bad_rows_are_rejected(bad):
    with pytest.raises(DataQualityError):
        validate(pd.DataFrame([_price(**bad)]), PRICES)


def test_duplicate_keys_are_rejected():
    with pytest.raises(DataQualityError):
        validate(pd.DataFrame([_price(), _price(amount=90.0)]), PRICES)


def test_as_of_never_returns_future_records():
    df = validate(pd.DataFrame([
        _price(amount=100.0, valid_from="2025-06-01", valid_to="2025-12-05"),
        _price(amount=80.0, valid_from="2025-12-05", valid_to=None),
    ]), PRICES)
    before = store.as_of(df, "2025-12-04")
    assert before["amount"].tolist() == [100.0]
    on_day = store.as_of(df, "2025-12-05")
    assert on_day["amount"].tolist() == [80.0]
    # признаки для пересмотра 5 декабря: решение этого дня ещё неизвестно
    features_view = store.as_of(df, "2025-12-05", strictly_before=True)
    assert features_view["amount"].tolist() == [100.0]


def test_registry_panel_cache_reuses_and_invalidates(tmp_path, monkeypatch):
    """Панель берётся с диска, пока отпечаток исходников тот же; новый снимок или код — пересборка."""
    from pulse.data import registry

    built = []
    df = pd.DataFrame({"date": pd.to_datetime(["2026-01-01", "2026-02-01"]), "key": ["a|b|c", "d|e|f"],
                       "local": [True, False], "base_usd": [12.5, 14.5]})
    fingerprint = {"value": "v1"}

    def fake_build():
        built.append(1)
        return df.copy()

    monkeypatch.setattr(registry, "PANEL_CACHE", tmp_path / "registry_panel.parquet")
    monkeypatch.setattr(registry, "build_panel", fake_build)
    monkeypatch.setattr(registry, "_fingerprint", lambda: fingerprint["value"])
    registry.panel.cache_clear()
    try:
        first = registry.panel()
        registry.panel.cache_clear()
        second = registry.panel()
        assert len(built) == 1, "второй вызов должен читать кэш"
        pd.testing.assert_frame_equal(first, second)
        meta = json.loads((tmp_path / "registry_panel.meta.json").read_text(encoding="utf-8"))
        assert meta["rows"] == 2 and meta["fingerprint"] == "v1" and len(meta["sha256"]) == 64

        fingerprint["value"] = "v2"
        registry.panel.cache_clear()
        registry.panel()
        assert len(built) == 2, "сменился отпечаток — панель пересобирается"
    finally:
        registry.panel.cache_clear()
