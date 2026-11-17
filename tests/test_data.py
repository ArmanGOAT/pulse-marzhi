"""Слой данных: проверки качества по контрактам и выборка «на дату» без подглядывания в будущее."""
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
