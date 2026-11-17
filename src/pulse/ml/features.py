"""Признаки и метки для радара пересмотра.

Правило против подглядывания (ADR-0003): признаки на дату `asof` строятся только из снимков
с датой ≤ asof и событий, закончившихся не позже asof. Метка смотрит строго вперёд: сократили ли
базовую цену на 10% и больше в окне (asof, label_end].
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pulse.data import registry as reg_data
from pulse.ml.settings import WaveCfg, load_radar_cfg

CUT_THRESHOLD = 0.10

NUMERIC = [
    "log_usd_per_unit", "log_base_usd", "inn_ratio_median", "inn_ratio_min", "inn_rank_pct",
    "n_brands_inn", "units", "reg_age_years", "months_since_change",
    "ratio_to_cut_peers", "share_cut_in_group",
]
CATEGORICAL = ["local", "currency", "country_group", "form", "has_local_analog", "prior_cut", "single_brand_inn",
               "has_cut_peers"]
FEATURES = NUMERIC + CATEGORICAL

# Набор history: история пересмотров по МНН и производителю, цена за 1 мг между дозами.
# Закрывает слепую зону «аналогам этой дозы ещё не снижали».
HISTORY_NUMERIC = ["inn_mg_ratio_median", "ratio_to_cut_peers_inn_mg", "inn_cut_share", "mfr_cut_share",
                   "mfr_n_packages"]
HISTORY_CATEGORICAL = ["inn_untouched"]

FEATURE_SETS = {
    "base": (NUMERIC, CATEGORICAL),
    "history": (NUMERIC + HISTORY_NUMERIC, CATEGORICAL + HISTORY_CATEGORICAL),
}

TOP_COUNTRIES = ["Узбекистан", "Индия", "Пакистан", "Республика Корея", "Турция", "Германия", "Китай",
                 "Российская Федерация", "Республика Беларусь", "Египет", "Иран", "Венгрия", "Словения", "Польша"]


WAVES = load_radar_cfg().waves


def _reg_year(reg: str) -> float:
    tail = str(reg).strip().split("/")[-1]
    return 2000 + float(tail) if tail.isdigit() and len(tail) == 2 else np.nan


def build_features(asof: str | pd.Timestamp, panel: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Признаки всех упаковок реестра на дату `asof` (только прошлое)."""
    t = pd.Timestamp(asof)
    hist = panel[panel["date"] <= t]
    snap = hist[hist["date"] == hist["date"].max()].copy()
    past = events[events["date_to"] <= t]

    snap["price_unit"] = snap["usd_per_unit"].where(snap["usd_per_unit"] > 0, snap["base_usd"])
    # аналоги: то же МНН, та же форма и та же дозировка
    snap["group"] = snap["inn"] + "|" + snap["form"] + "|" + snap["strength"]
    group = snap.groupby("group")["price_unit"]
    snap["inn_median"] = group.transform("median")
    snap["inn_min"] = group.transform("min")
    snap["inn_ratio_median"] = snap["price_unit"] / snap["inn_median"]
    snap["inn_ratio_min"] = snap["price_unit"] / snap["inn_min"]
    snap["inn_rank_pct"] = group.rank(pct=True)
    snap["n_brands_inn"] = snap.groupby("inn")["trademark"].transform("nunique")
    snap["single_brand_inn"] = snap["n_brands_inn"] <= 1
    local_inn = snap.loc[snap["local"], "inn"].unique()
    snap["has_local_analog"] = snap["inn"].isin(local_inn) & ~snap["local"]
    snap["log_usd_per_unit"] = np.log(snap["price_unit"].clip(lower=1e-4))
    snap["log_base_usd"] = np.log(snap["base_usd"].clip(lower=1e-4))
    snap["reg_age_years"] = t.year - snap["reg_number"].map(_reg_year)
    snap["country_group"] = snap["country"].where(snap["country"].isin(TOP_COUNTRIES), "Другая")

    cuts = past[past["cut"] >= CUT_THRESHOLD]
    snap["prior_cut"] = snap["key"].isin(cuts["key"])
    # цены аналогов, которым уже снижали цену: так выглядит то, что считает формула агентства
    peers = snap[snap["prior_cut"]].groupby("group")["price_unit"].median()
    snap["cut_peer_median"] = snap["group"].map(peers)
    snap["ratio_to_cut_peers"] = snap["price_unit"] / snap["cut_peer_median"]
    snap["has_cut_peers"] = snap["cut_peer_median"].notna()
    snap["share_cut_in_group"] = snap.groupby("group")["prior_cut"].transform("mean")

    # набор history: цена за 1 мг позволяет сравнить с аналогами другой дозы того же МНН
    mg = snap["strength_mg"].where(snap["strength_mg"] > 0)
    snap["usd_per_mg"] = snap["price_unit"] / mg
    inn_form = snap["inn"] + "|" + snap["form"]
    snap["inn_mg_ratio_median"] = snap["usd_per_mg"] / snap.groupby(inn_form)["usd_per_mg"].transform("median")
    cut_mg = snap[snap["prior_cut"]].groupby(inn_form[snap["prior_cut"]])["usd_per_mg"].median()
    snap["ratio_to_cut_peers_inn_mg"] = snap["usd_per_mg"] / inn_form.map(cut_mg)
    snap["inn_cut_share"] = snap.groupby("inn")["prior_cut"].transform("mean")
    snap["inn_untouched"] = snap["inn_cut_share"] == 0
    mfr = snap["manufacturer"].map(reg_data.norm_text)
    snap["mfr_cut_share"] = snap.groupby(mfr)["prior_cut"].transform("mean")
    snap["mfr_n_packages"] = snap.groupby(mfr)["key"].transform("count")

    changed = past[past["cut"].abs() >= 0.01].groupby("key")["date_to"].max()
    # словари вместо Series: пустая Series дат при map ломает приведение типов в pandas 3
    last_change = pd.to_datetime(snap["key"].map(changed.to_dict()))
    first_seen = hist.groupby("key")["date"].min().to_dict()
    ref = last_change.fillna(pd.to_datetime(snap["key"].map(first_seen)))
    snap["months_since_change"] = (t - ref).dt.days / 30.4
    snap["asof"] = t
    return snap.reset_index(drop=True)


def build_labels(keys: pd.Series, asof: str, label_end: str, events: pd.DataFrame,
                 threshold: float = CUT_THRESHOLD) -> pd.DataFrame:
    """Метка: сократили ли цену на ≥10% в окне (asof, label_end]; плюс размер снижения."""
    t0, t1 = pd.Timestamp(asof), pd.Timestamp(label_end)
    window = events[(events["date_from"] >= t0) & (events["date_to"] <= t1)]
    # накопленное изменение за окно: произведение отношений цен
    cum = window.groupby("key")["ratio"].prod()
    observed = window.groupby("key")["date_to"].max() >= t1
    out = pd.DataFrame({"key": keys})
    out["ratio"] = out["key"].map(cum)
    out["observed"] = out["key"].map(observed).fillna(False).astype(bool)
    out["cut_size"] = (1 - out["ratio"]).clip(lower=0)
    out["y"] = (out["cut_size"] >= threshold).astype(int)
    return out


def wave_dataset(wave: WaveCfg, panel: pd.DataFrame | None = None, events: pd.DataFrame | None = None,
                 threshold: float = CUT_THRESHOLD) -> pd.DataFrame:
    """Признаки на дату волны + метки; только упаковки, которые дожили до конца окна (иначе метка неизвестна)."""
    panel = reg_data.panel() if panel is None else panel
    events = reg_data.events(panel) if events is None else events
    X = build_features(wave.asof, panel, events)
    y = build_labels(X["key"], wave.asof, wave.label_end, events, threshold)
    df = X.merge(y, on="key")
    df = df[df["observed"] & df["rx"]].copy()
    df["wave"] = wave.name
    return df.reset_index(drop=True)
