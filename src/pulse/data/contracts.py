"""Контракты данных: как выглядит каждая таблица и какие проверки качества она проходит.

Проверки соответствуют измерениям качества данных из ISO/IEC 5259 и ISO/IEC 25012:
полнота (нет пропусков в обязательных полях), точность (значения в допустимых диапазонах),
согласованность (типы, коды по стандартам), уникальность (нет дублей по ключу).

Коды: страны по ISO 3166-1 alpha-2, валюты по ISO 4217, даты по ISO 8601,
препараты по ATC (ВОЗ) и INN (ВОЗ).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

STATUSES = {"fact", "estimate", "demo"}
CURRENCIES = {"UZS", "USD", "EUR", "RUB"}
PRICE_TYPES = {"ceiling", "pharmacy", "ref_country", "origin", "customs_cip"}


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str  # "str" | "float" | "int" | "bool" | "date"
    required: bool = True
    min: float | None = None
    max: float | None = None
    allowed: frozenset[str] | None = None


@dataclass(frozen=True)
class Contract:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    description: str = ""
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


DRUGS = Contract(
    name="drugs",
    description="Справочник препаратов: одна строка на торговое наименование, дозировку и упаковку",
    key=("drug_id",),
    columns=(
        Column("drug_id", "str"),
        Column("trade_name", "str"),
        Column("inn", "str"),
        Column("atc", "str"),
        Column("strength", "str"),
        Column("pack_size", "int", min=1),
        Column("manufacturer", "str"),
        Column("country", "str"),
        Column("is_local", "bool"),
        Column("is_original", "bool"),
        Column("status", "str", allowed=frozenset(STATUSES)),
        Column("source", "str"),
    ),
)

PRICES = Contract(
    name="prices",
    description="Цены с датами действия: потолок, аптека, референтные страны, страна производства, таможенная цена",
    key=("drug_id", "price_type", "country", "valid_from"),
    columns=(
        Column("drug_id", "str"),
        Column("price_type", "str", allowed=frozenset(PRICE_TYPES)),
        Column("country", "str"),
        Column("currency", "str", allowed=frozenset(CURRENCIES)),
        Column("amount", "float", min=0.0),
        Column("valid_from", "date"),
        Column("valid_to", "date", required=False),
        Column("status", "str", allowed=frozenset(STATUSES)),
        Column("source", "str"),
    ),
)

REVISIONS = Contract(
    name="revisions",
    description="События пересмотра потолка: старый и новый потолок на дату пересмотра",
    key=("revision_date", "drug_id"),
    columns=(
        Column("revision_date", "date"),
        Column("drug_id", "str"),
        Column("old_ceiling", "float", min=0.0),
        Column("new_ceiling", "float", min=0.0),
        Column("status", "str", allowed=frozenset(STATUSES)),
        Column("source", "str"),
    ),
)

FX = Contract(
    name="fx",
    description="Официальный курс ЦБ Республики Узбекистан",
    key=("date", "currency"),
    columns=(
        Column("date", "date"),
        Column("currency", "str", allowed=frozenset(CURRENCIES)),
        Column("rate_uzs", "float", min=1.0),
        Column("status", "str", allowed=frozenset(STATUSES)),
        Column("source", "str"),
    ),
)

ALL = {c.name: c for c in (DRUGS, PRICES, REVISIONS, FX)}


class DataQualityError(ValueError):
    """Таблица не прошла проверки качества."""


def validate(df: pd.DataFrame, contract: Contract) -> pd.DataFrame:
    """Приводит типы и проверяет таблицу. Возвращает проверенную копию или бросает DataQualityError."""
    problems: list[str] = []
    missing = [c for c in contract.column_names if c not in df.columns]
    missing_required = [c.name for c in contract.columns if c.name in missing and c.required]
    if missing_required:
        raise DataQualityError(f"{contract.name}: нет обязательных колонок {missing_required}")
    out = df.copy()
    for c in contract.columns:
        if c.name not in out.columns:
            out[c.name] = pd.NA
            continue
        s = out[c.name]
        if c.dtype == "date":
            out[c.name] = pd.to_datetime(s, errors="coerce").dt.normalize()
        elif c.dtype == "float":
            out[c.name] = pd.to_numeric(s, errors="coerce").astype(float)
        elif c.dtype == "int":
            out[c.name] = pd.to_numeric(s, errors="coerce").astype("Int64")
        elif c.dtype == "bool":
            out[c.name] = s.astype(bool)
        else:
            out[c.name] = s.astype("string")
        s = out[c.name]
        nulls = int(s.isna().sum())
        if c.required and nulls:
            problems.append(f"{c.name}: {nulls} пропусков")
        if c.min is not None and (s.dropna() < c.min).any():
            problems.append(f"{c.name}: значения меньше {c.min}")
        if c.max is not None and (s.dropna() > c.max).any():
            problems.append(f"{c.name}: значения больше {c.max}")
        if c.allowed is not None:
            bad = set(s.dropna().astype(str)) - set(c.allowed)
            if bad:
                problems.append(f"{c.name}: недопустимые значения {sorted(bad)[:5]}")
    dups = int(out.duplicated(list(contract.key)).sum())
    if dups:
        problems.append(f"дубли по ключу {contract.key}: {dups}")
    if problems:
        raise DataQualityError(f"{contract.name}: " + "; ".join(problems))
    return out[contract.column_names]
