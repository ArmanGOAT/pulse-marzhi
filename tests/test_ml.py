"""ML-радар: признаки без подглядывания в будущее, метки только вперёд, честная деловая метрика."""
import numpy as np
import pandas as pd
import pytest

from pulse.data.registry import norm_text, parse_strength, parse_units
from pulse.ml.features import WAVES, build_features, build_labels
from pulse.ml.radar import Economics, business_value
from pulse.ml.woe import WoEEncoder


def _panel() -> pd.DataFrame:
    rows = []
    for date, price in [("2025-11-01", 10.0), ("2025-12-01", 10.0), ("2026-01-01", 6.0)]:
        for key, local, p in [("a", False, price), ("b", False, 5.0), ("c", True, 4.0)]:
            rows.append({"date": pd.Timestamp(date), "key": key, "trademark": key.upper(), "inn": "x", "form": "tablet",
                         "strength": "20мг", "strength_mg": 20.0, "local": local, "rx": True, "currency": "USD", "base_price": p,
                         "base_usd": p, "usd_per_unit": p / 28, "units": 28.0, "reg_number": "DV/X 1/11/17",
                         "country": "Индия", "product": "Таблетки 20 мг №28", "manufacturer": "m"})
    return pd.DataFrame(rows)


def _events(panel: pd.DataFrame) -> pd.DataFrame:
    a = panel[panel.key == "a"].set_index("date")["base_price"]
    return pd.DataFrame({"key": ["a", "a"], "date_from": [pd.Timestamp("2025-11-01"), pd.Timestamp("2025-12-01")],
                         "date_to": [pd.Timestamp("2025-12-01"), pd.Timestamp("2026-01-01")],
                         "ratio": [a.iloc[1] / a.iloc[0], a.iloc[2] / a.iloc[1]]}).assign(cut=lambda d: 1 - d.ratio)


def test_features_ignore_future_snapshots_and_events():
    p = _panel()
    ev = _events(p)
    X = build_features("2025-12-01", p, ev).set_index("key")
    assert X.loc["a", "base_usd"] == 10.0          # январская цена 6.0 не видна
    assert not X.loc["a", "prior_cut"]             # январское снижение ещё не случилось


def test_labels_look_only_forward():
    p = _panel()
    ev = _events(p)
    y = build_labels(pd.Series(["a", "b"]), "2025-12-01", "2026-01-01", ev).set_index("key")
    assert y.loc["a", "y"] == 1 and y.loc["a", "cut_size"] == pytest.approx(0.4)
    assert y.loc["b", "y"] == 0


def test_wave_windows_do_not_overlap():
    for earlier, later in zip(WAVES[:-1], WAVES[1:], strict=True):
        assert pd.Timestamp(earlier.label_end) <= pd.Timestamp(later.asof)


def test_woe_unseen_category_is_neutral():
    X = pd.DataFrame({"n": [1, 2, 3, 4, 5, 6, 7, 8], "c": list("aabbaabb")})
    y = pd.Series([0, 0, 0, 1, 0, 1, 1, 1])
    enc = WoEEncoder(["n"], ["c"]).fit(X, y)
    out = enc.transform(pd.DataFrame({"n": [np.nan], "c": ["zzz"]}))
    assert out.loc[0, "c"] == 0.0 and out.loc[0, "n"] == 0.0


def test_business_value_baselines():
    eco = Economics(gain_per_cut=1000.0, prep_cost=100.0)
    y, cut = np.array([1, 0, 1, 0]), np.array([0.4, 0.0, 0.3, 0.0])
    never = business_value(np.zeros(4, bool), y, cut, eco)
    always = business_value(np.ones(4, bool), y, cut, eco)
    oracle = business_value(y.astype(bool), y, cut, eco)
    assert never == 0.0 and oracle > always


@pytest.mark.parametrize("text,strength,units", [
    ("Сербан Таблетки, покрытые пленочной оболочкой 20 мг блистеры №30(3x10)", "20мг", 30.0),
    ("ЦИКЛАЗЕН Капли глазные 3 мг/мл 5 мл флакон", "3мг", np.nan),
])
def test_text_parsers(text, strength, units):
    assert parse_strength(text) == strength
    got = parse_units(text)
    assert (np.isnan(got) and np.isnan(units)) or got == units


def test_key_normalization_ignores_case_and_punctuation():
    assert norm_text("СООО Лекфарм, Республика Беларусь") == norm_text("СООО Лекфарм Республика Беларусь")
    assert norm_text("Ltd Synokem") == norm_text("LTD  Synokem")
