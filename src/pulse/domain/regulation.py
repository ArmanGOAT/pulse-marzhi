"""M3. Движок правил референтного ценообразования (приказ Минздрава РУз № 3735 и предыдущие версии).

Правила хранятся в config/regulation/rules.yaml с датами вступления в силу. Движок отвечает на вопросы:
какие правила действовали на дату X, какой будет референтная цена по формуле, какое из чисел
формулы её определяет и нужна ли перерегистрация цены.

Цепочка цен (как в разборе команды): референтная цена → потолок импортёра (+ пошлина и прочие расходы)
→ опт (+ до 15%) → аптека (+ до 20%). Все суммы в UZS по курсу ЦБ на дату заявления.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict

from pulse.config import CONFIG_DIR

COMPONENT_LABELS = {
    "ref_mean": "Средняя по референтным странам",
    "origin": "Цена в стране производства + до 15%",
    "cip": "Средняя таможенная цена ввоза за 12 месяцев",
    "generic_cap": "80% цены оригинала (для дженерика)",
}


class RuleVersion(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    effective_from: date
    title: str
    basket: list[str]
    mean_method: Literal["mean", "median"]
    outlier_ratio: float | None
    origin_transport_cap: float | None
    cip_window_months: int
    cip_component: bool
    generic_cap_vs_original: float | None
    rereg_threshold: float
    review_every_months: int
    status: Literal["fact", "estimate", "demo"]
    source: str


class Markup(BaseModel):
    value: float
    status: Literal["fact", "estimate", "demo"]
    source: str


class Regulation(BaseModel):
    version: int
    markups: dict[str, Markup]
    versions: list[RuleVersion]

    def at(self, when: date | str | pd.Timestamp) -> RuleVersion:
        """Версия правил, действовавшая на дату `when`."""
        d = pd.Timestamp(when).date()
        valid = [v for v in sorted(self.versions, key=lambda v: v.effective_from) if v.effective_from <= d]
        if not valid:
            raise ValueError(f"Нет правил на дату {d}")
        return valid[-1]

    def m(self, name: str) -> float:
        return self.markups[name].value

    @property
    def importer_factor(self) -> float:
        """Во сколько раз потолок импортёра выше референтной цены."""
        return 1 + self.m("duty") + self.m("other_expenses")


@lru_cache(maxsize=1)
def load_regulation() -> Regulation:
    with (CONFIG_DIR / "regulation" / "rules.yaml").open(encoding="utf-8") as f:
        return Regulation.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class CeilingResult:
    rules_id: str
    components: dict[str, float]
    binding: str
    reference_price: float
    importer_ceiling: float
    wholesale_ceiling: float
    pharmacy_ceiling: float
    used_countries: list[str] = field(default_factory=list)
    excluded_countries: list[str] = field(default_factory=list)

    @property
    def binding_label(self) -> str:
        return COMPONENT_LABELS[self.binding]

    def gap_vs(self, current_importer_ceiling: float) -> float:
        """Насколько текущий потолок выше того, что даёт формула (0,10 = на 10% выше)."""
        return current_importer_ceiling / self.importer_ceiling - 1


def exclude_outliers(prices: dict[str, float], ratio: float | None) -> tuple[dict[str, float], list[str]]:
    """Цена страны не учитывается, если она больше чем на `ratio` выше цен в двух и более других странах."""
    if ratio is None or len(prices) < 3:
        return dict(prices), []
    kept, dropped = {}, []
    for country, p in prices.items():
        cheaper = sum(1 for c, q in prices.items() if c != country and p > (1 + ratio) * q)
        (dropped.append(country) if cheaper >= 2 else kept.__setitem__(country, p))
    return kept, dropped


def reference_price(when: date | str, ref_prices: dict[str, float], origin_price: float | None = None,
                    cip_avg: float | None = None, original_reference: float | None = None,
                    is_generic: bool = True, reg: Regulation | None = None) -> CeilingResult:
    """Референтная цена по формуле на дату `when`. Все цены в UZS за упаковку на уровне CIP."""
    reg = reg or load_regulation()
    rules = reg.at(when)
    in_basket = {c: p for c, p in ref_prices.items() if c in rules.basket and p > 0}
    kept, dropped = exclude_outliers(in_basket, rules.outlier_ratio)

    components: dict[str, float] = {}
    if kept:
        s = pd.Series(kept)
        components["ref_mean"] = float(s.mean() if rules.mean_method == "mean" else s.median())
    if origin_price and rules.origin_transport_cap is not None:
        components["origin"] = origin_price * (1 + rules.origin_transport_cap)
    if cip_avg and rules.cip_component:
        components["cip"] = cip_avg
    if is_generic and original_reference and rules.generic_cap_vs_original is not None:
        components["generic_cap"] = original_reference * rules.generic_cap_vs_original
    if not components:
        raise ValueError("Нет ни одного числа для формулы: нужны цены стран, цена производителя или таможенная цена")

    binding = min(components, key=components.get)
    ref = components[binding]
    importer = ref * reg.importer_factor
    wholesale = importer * (1 + reg.m("wholesale"))
    return CeilingResult(
        rules_id=rules.id, components=components, binding=binding, reference_price=ref,
        importer_ceiling=importer, wholesale_ceiling=wholesale,
        pharmacy_ceiling=wholesale * (1 + reg.m("retail")),
        used_countries=sorted(kept), excluded_countries=sorted(dropped),
    )


def reference_from_importer_ceiling(importer_ceiling: float, reg: Regulation | None = None) -> float:
    reg = reg or load_regulation()
    return importer_ceiling / reg.importer_factor


def needs_reregistration(fixed_price: float, actual_price: float, when: date | str,
                         reg: Regulation | None = None) -> bool:
    """Нужна ли перерегистрация: фактические цены упали ниже зарегистрированной на порог версии правил."""
    rules = (reg or load_regulation()).at(when)
    return actual_price <= fixed_price * (1 - rules.rereg_threshold)
