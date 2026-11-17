"""Рынок ривароксабана 20 мг в Ташкенте: справочник брендов и аптечные цены (факт, liki.uz)."""
from __future__ import annotations

import pandas as pd

from pulse.config import ROOT, load_assumptions

REFERENCE = ROOT / "data" / "reference" / "rivaroxaban_tashkent.csv"
OBSERVED_ON = "2026-11-17"
OUR_DRUG = "RIV-SERBAN"  # допущение: ближе всех к условию (аптечная цена ≈ потолок 150 000 × 1,15 × 1,2)


def load_reference() -> pd.DataFrame:
    df = pd.read_csv(REFERENCE, dtype={"is_local": bool, "is_original": bool})
    df["pharmacy_mid_uzs"] = (df["pharmacy_min_uzs"] + df["pharmacy_max_uzs"]) / 2
    return df


def drugs() -> pd.DataFrame:
    ref = load_reference()
    cols = ["drug_id", "trade_name", "inn", "atc", "strength", "pack_size", "manufacturer",
            "country", "is_local", "is_original", "status", "source"]
    return ref[cols].copy()


def prices() -> pd.DataFrame:
    ref = load_reference()
    a = load_assumptions()
    pharmacy = pd.DataFrame({
        "drug_id": ref["drug_id"],
        "price_type": "pharmacy",
        "country": "UZ",
        "currency": "UZS",
        "amount": ref["pharmacy_mid_uzs"],
        "valid_from": pd.Timestamp(OBSERVED_ON),
        "valid_to": pd.NaT,
        "status": "fact",
        "source": ref["source"],
    })
    ceiling = pd.DataFrame([{
        "drug_id": OUR_DRUG,
        "price_type": "ceiling",
        "country": "UZ",
        "currency": "UZS",
        "amount": a.v("market.price_ceiling"),
        "valid_from": pd.Timestamp("2026-07-01"),
        "valid_to": pd.NaT,
        "status": "estimate",
        "source": "Условие задачи №16 (150 000); привязка к Сербан-20 и дата — допущение",
    }])
    return pd.concat([pharmacy, ceiling], ignore_index=True)
