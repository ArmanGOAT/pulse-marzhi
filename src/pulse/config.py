"""Загрузка допущений и рычагов из config/*.yaml с проверкой типов.

Каждое число хранит происхождение (status, source), чтобы интерфейс и выгрузка
могли пометить его как факт, оценку или демо-данные.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

Status = Literal["fact", "estimate", "demo"]


class Param(BaseModel):
    """Одно входное число с единицей и происхождением."""

    model_config = ConfigDict(frozen=True)
    value: float
    unit: str
    status: Status
    source: str


class Assumptions(BaseModel):
    model_config = ConfigDict(frozen=True)
    version: int
    as_of: date
    market: dict[str, Param]
    costs: dict[str, Param]
    risk: dict[str, Param]

    def v(self, key: str) -> float:
        """Значение по ключу вида 'costs.money_rate'."""
        return self.p(key).value

    def p(self, key: str) -> Param:
        section, name = key.split(".", 1)
        return getattr(self, section)[name]


class ProcurementOption(BaseModel):
    label: str
    discount: float = Field(ge=0, lt=1)
    prepay: bool
    invest: float = Field(ge=0)
    months: int = Field(ge=0)
    enters_customs_value: bool


class ModeOption(BaseModel):
    label: str
    months: int = Field(ge=0)


class PooledOption(BaseModel):
    label: str
    delivery_mult: float = Field(gt=0, le=1)
    clearance_mult: float = Field(gt=0, le=1)
    invest: float = Field(ge=0)
    months: int = Field(ge=0)


class LocalizationOption(BaseModel):
    label: str
    save: float = Field(ge=0, lt=1)
    usd_share: float = Field(gt=0, le=1)
    delivery_mult: float = Field(gt=0, le=1)
    invest: float = Field(ge=0)
    months: int = Field(ge=0)


class Strategy(BaseModel):
    """Одна комбинация рычагов."""

    model_config = ConfigDict(frozen=True)
    shipments: int
    procurement: str
    mode: str
    pooled: bool
    digital: float
    loc: str


class Levers(BaseModel):
    version: int
    shipments_per_year: list[int]
    change_shipments_months: int
    procurement: dict[str, ProcurementOption]
    modes: dict[str, ModeOption]
    pooled: dict[str, PooledOption]
    digital_shares: list[float]
    digital_invest_full: float
    digital_months_full: int
    localization: dict[str, LocalizationOption]
    current: Strategy

    def pooled_option(self, pooled: bool) -> PooledOption:
        return self.pooled["true" if pooled else "false"]


def _read(name: str) -> dict:
    with (CONFIG_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_assumptions() -> Assumptions:
    return Assumptions.model_validate(_read("assumptions.yaml"))


@lru_cache(maxsize=1)
def load_levers() -> Levers:
    return Levers.model_validate(_read("levers.yaml"))
