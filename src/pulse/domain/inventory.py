"""Симулятор склада по дням и расчёт удара пересмотра по складу.

Политика пополнения: поставка приходит, когда запас опускается до страхового минимума
(жёсткое правило: не меньше месяца продаж). Обычная политика возит партии на 12/n месяцев;
политика «подготовка» в окне перед пересмотром возит только мелкие партии, а после
пересмотра возвращается к обычным.

Удар по складу: после снижения потолка производитель снижает цену закупки для новых партий
(доля cut_pass_through), а товар на складе уже куплен по старой цене. Убыток = единицы на складе
× закупочная цена × снижение × доля × (1 + пошлина).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from pulse.config import Assumptions, load_assumptions

DAYS_PER_MONTH = 365 / 12


@dataclass(frozen=True)
class StockRun:
    daily: pd.DataFrame          # day, stock_start, sold, received, stock_end
    shipments: int
    stock_on_revision: float     # единицы на складе утром дня пересмотра
    min_stock_months: float      # наименьший запас за период, в месяцах продаж


def simulate(annual_demand: float, shipments_per_year: int, horizon_days: int = 365,
             revision_day: int | None = None, prepare: bool = False, start_stock: float | None = None,
             a: Assumptions | None = None) -> StockRun:
    a = a or load_assumptions()
    d = annual_demand / 365
    ss = a.v("costs.safety_stock_min_months") * DAYS_PER_MONTH * d
    big = annual_demand / shipments_per_year
    small = a.v("costs.prep_batch_months") * DAYS_PER_MONTH * d
    window = int(a.v("costs.prep_window_days"))
    stock = ss + big / 2 if start_stock is None else start_stock

    rows, shipments, on_revision = [], 0, float("nan")
    for day in range(horizon_days):
        received = 0.0
        if stock - d < ss:  # завтра опустимся ниже минимума: поставка приходит сегодня
            # подготовка: крупная партия не успела бы распродаться до пересмотра → везём мелкую
            straddles = revision_day is not None and day < revision_day < day + big / d
            in_window = prepare and straddles and revision_day - day <= window
            received = small if in_window else big
            shipments += 1
        start = stock + received
        if revision_day is not None and day == revision_day:
            on_revision = start
        stock = start - d
        rows.append((day, start - received, d, received, stock))
    daily = pd.DataFrame(rows, columns=["day", "stock_start", "sold", "received", "stock_end"])
    return StockRun(daily=daily, shipments=shipments, stock_on_revision=on_revision,
                    min_stock_months=float(daily["stock_end"].min() / (DAYS_PER_MONTH * d)))


def expected_exposure(annual_demand: float, shipments_per_year: int, prepare: bool,
                      a: Assumptions | None = None) -> float:
    """Ожидаемые единицы на складе в случайный день пересмотра: страховой запас + половина партии."""
    a = a or load_assumptions()
    ss = a.v("costs.safety_stock_min_months") * annual_demand / 12
    batch = annual_demand * a.v("costs.prep_batch_months") / 12 if prepare else annual_demand / shipments_per_year
    return ss + batch / 2


def devaluation_loss(units: float, unit_purchase_uzs: float, cut: float, a: Assumptions | None = None) -> float:
    """Убыток по складу при снижении потолка на `cut` (доля)."""
    a = a or load_assumptions()
    return units * unit_purchase_uzs * cut * a.v("costs.cut_pass_through") * (1 + a.v("market.duty_rate"))


@dataclass(frozen=True)
class PrepareDecision:
    exposure_regular: float
    exposure_prepared: float
    loss_regular: float          # ожидаемый убыток по складу без подготовки
    loss_prepared: float         # с подготовкой, включая её стоимость
    prep_cost: float
    threshold_prob: float        # при какой вероятности снижения подготовка окупается
    prepare: bool


def prepare_decision(prob_cut: float, expected_cut: float, annual_demand: float, shipments_per_year: int,
                     unit_purchase_uzs: float, a: Assumptions | None = None) -> PrepareDecision:
    """Готовиться ли к пересмотру: сравнение ожидаемых потерь с ценой подготовки."""
    a = a or load_assumptions()
    reg = simulate(annual_demand, shipments_per_year, revision_day=180, a=a)
    prep = simulate(annual_demand, shipments_per_year, revision_day=180, prepare=True, a=a)
    extra = max(prep.shipments - reg.shipments, 0)
    prep_cost = extra * a.v("costs.clearance_per_shipment")
    e_reg = expected_exposure(annual_demand, shipments_per_year, prepare=False, a=a)
    e_prep = expected_exposure(annual_demand, shipments_per_year, prepare=True, a=a)
    unit_loss = devaluation_loss(1.0, unit_purchase_uzs, expected_cut, a=a)
    loss_reg = prob_cut * unit_loss * e_reg
    loss_prep = prob_cut * unit_loss * e_prep + prep_cost
    saved_if_cut = unit_loss * max(e_reg - e_prep, 0.0)
    threshold = prep_cost / saved_if_cut if saved_if_cut > 0 else float(np.inf)
    return PrepareDecision(e_reg, e_prep, loss_reg, loss_prep, prep_cost, threshold, loss_prep < loss_reg)
