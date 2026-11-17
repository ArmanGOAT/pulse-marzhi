"""Разбивка по сегментам: кардио-сопоставление, бутстрап-интервал, сегменты с малой выборкой."""
import numpy as np
import pandas as pd

from pulse.ml.radar import Economics
from pulse.ml.segments import bootstrap_auc, is_cardio, load_segments_cfg, segment_table


def test_cardio_matcher():
    kw = load_segments_cfg()["cardio"]["inn_keywords"]
    s = pd.Series(["Ривароксабан", "амлодипин + валсартан", "парацетамол"])
    assert is_cardio(s, kw).tolist() == [True, True, False]


def test_bootstrap_interval_contains_point_estimate():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 400)
    p = y * 0.3 + rng.random(400) * 0.7
    lo, hi = bootstrap_auc(y, p, resamples=200, seed=1)
    assert lo < hi and lo > 0.5


def test_small_segments_are_marked_not_scored():
    df = pd.DataFrame({"inn": ["ривароксабан"] * 5 + ["x"] * 200, "local": [False] * 205,
                       "form": ["tablet"] * 205, "has_cut_peers": [True] * 205,
                       "y": [1, 0, 1, 0, 1] + [0, 1] * 100, "cut_size": [0.3] * 205,
                       "p_woe_logit": np.linspace(0, 1, 205), "p_rule": [0.5] * 205})
    rows = {r["segment"]: r for r in segment_table(df, Economics(1000.0, 100.0), 0.1, load_segments_cfg())}
    assert rows["Кардио (ATC B01 и C)"]["note"] == "мало данных"
    assert "auc_model" in rows["Все рецептурные"]
