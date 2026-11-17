"""M2. Двойник упаковки: 10 статей затрат, маржа и доля затрат в долларах.

Считает сразу для любого числа стратегий (векторно), чтобы оптимизатор
перебирал все комбинации рычагов за миллисекунды. Все числа берутся из
config/assumptions.yaml и config/levers.yaml.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pulse.config import Assumptions, Levers, load_assumptions, load_levers

# (ключ, подпись, валюта платежа по ISO 4217)
COST_ITEMS: list[tuple[str, str, str]] = [
    ("purchase", "Закупка у производителя", "USD"),
    ("delivery", "Международная доставка", "USD"),
    ("duty", "Пошлина 2%", "USD"),
    ("clearance", "Оформление и контроль серии", "UZS"),
    ("inventory", "Склад и деньги в запасах", "UZS"),
    ("writeoff", "Списания по сроку годности", "USD"),
    ("receivables", "Отсрочка платежа аптекам", "UZS"),
    ("regional", "Доставка по регионам", "UZS"),
    ("marketing", "Маркетинг", "UZS"),
    ("regulatory", "Регистрация и фармаконадзор", "UZS"),
    ("capital", "Вложения в меры: амортизация и проценты", "UZS"),
]
LEVER_COLS = ["shipments", "procurement", "mode", "pooled", "digital", "loc"]


def strategy_table(levers: Levers | None = None) -> pd.DataFrame:
    """Все комбинации рычагов: 6 × 3 × 3 × 2 × 5 × 4 = 2 160."""
    lv = levers or load_levers()
    rows = itertools.product(
        lv.shipments_per_year, list(lv.procurement), list(lv.modes), [False, True],
        lv.digital_shares, list(lv.localization),
    )
    df = pd.DataFrame(list(rows), columns=LEVER_COLS)
    df["invest"] = invest_of(df, lv)
    df["months"] = months_of(df, lv)
    return df


def current_strategy(levers: Levers | None = None) -> pd.DataFrame:
    lv = levers or load_levers()
    df = pd.DataFrame([lv.current.model_dump()])
    df["invest"] = invest_of(df, lv)
    df["months"] = months_of(df, lv)
    return df


def invest_of(df: pd.DataFrame, lv: Levers) -> np.ndarray:
    return (
        df["procurement"].map(lambda k: lv.procurement[k].invest).to_numpy(float)
        + df["pooled"].map(lambda k: lv.pooled_option(bool(k)).invest).to_numpy(float)
        + df["digital"].to_numpy(float) * lv.digital_invest_full
        + df["loc"].map(lambda k: lv.localization[k].invest).to_numpy(float)
    )


def months_of(df: pd.DataFrame, lv: Levers) -> np.ndarray:
    parts = np.vstack([
        df["procurement"].map(lambda k: lv.procurement[k].months).to_numpy(float),
        df["pooled"].map(lambda k: lv.pooled_option(bool(k)).months).to_numpy(float),
        df["mode"].map(lambda k: lv.modes[k].months).to_numpy(float),
        np.where(df["shipments"].to_numpy() != lv.current.shipments, lv.change_shipments_months, 0.0),
        np.round(df["digital"].to_numpy(float) * lv.digital_months_full),
        df["loc"].map(lambda k: lv.localization[k].months).to_numpy(float),
    ])
    return parts.max(axis=0)


@dataclass(frozen=True)
class Components:
    """Затраты на упаковку по стратегиям, разложенные для Монте-Карло.

    cost = usd * fx_mult + deliv * fx_mult * delivery_mult + sum_var + fixed / volume + recv_rate * price
    """

    items: dict[str, np.ndarray]
    usd: np.ndarray
    deliv: np.ndarray
    sum_var: np.ndarray
    fixed: np.ndarray
    volume: np.ndarray
    recv_rate: float
    price: float
    fx: float
    customs_value: np.ndarray  # таможенная стоимость (CIP) на упаковку, UZS: закупка в $ + доставка
    purchase_usd: np.ndarray   # платёж производителю в долларах на упаковку, UZS по текущему курсу
    stock_months: np.ndarray   # средний запас (с учётом страхового) в месяцах продаж

    @property
    def total(self) -> np.ndarray:
        return sum(self.items.values())

    @property
    def margin_sum(self) -> np.ndarray:
        return self.price - self.total

    @property
    def margin(self) -> np.ndarray:
        return self.margin_sum / self.price

    @property
    def usd_share(self) -> np.ndarray:
        """Доля затрат, которые платятся в долларах."""
        usd_items = sum(self.items[k] for k, _, cur in COST_ITEMS if cur == "USD")
        return usd_items / self.total

    @property
    def fx_sensitivity(self) -> np.ndarray:
        """Сколько сумов затрат на упаковку добавляет +1% к доллару."""
        return (self.usd + self.deliv) * 0.01

    def breakeven_fx(self) -> np.ndarray:
        """Курс, при котором маржа становится нулевой."""
        rest = self.total - self.usd - self.deliv
        return self.fx * (self.price - rest) / (self.usd + self.deliv)


def components(df: pd.DataFrame, fx: float | None = None, price: float | None = None,
               volume: float | None = None, safety_months: float | None = None,
               a: Assumptions | None = None, lv: Levers | None = None) -> Components:
    a = a or load_assumptions()
    lv = lv or load_levers()
    fx = a.v("market.fx_usd_uzs") if fx is None else fx
    price = a.v("market.price_ceiling") if price is None else price
    volume = a.v("market.annual_volume") if volume is None else volume
    floor = a.v("costs.safety_stock_min_months")
    safety_months = floor if safety_months is None else max(safety_months, floor)

    n = df["shipments"].to_numpy(float)
    mode = df["mode"].to_numpy()
    pooled = df["pooled"].to_numpy(bool)
    d = df["digital"].to_numpy(float)
    proc = [lv.procurement[k] for k in df["procurement"]]
    loc = [lv.localization[k] for k in df["loc"]]
    disc = np.array([p.discount for p in proc])
    prepay = np.array([p.prepay for p in proc])
    save = np.array([o.save for o in loc])
    usd_share = np.array([o.usd_share for o in loc])
    loc_deliv = np.array([o.delivery_mult for o in loc])
    pool_deliv = np.where(pooled, lv.pooled_option(True).delivery_mult, 1.0)
    pool_clear = np.where(pooled, lv.pooled_option(True).clearance_mult, 1.0)

    deliv_usd = {m: a.v(f"costs.delivery_usd_{m}") for m in lv.modes}
    transit = {m: a.v(f"costs.transit_months_{m}") for m in lv.modes}
    buffer = {m: (a.v("costs.delay_buffer_months_rail") if m == "rail" else 0.0) for m in lv.modes}

    vol = volume * (1 - a.v("costs.digital_sales_loss_full") * d)
    p0 = a.v("costs.purchase_usd") * fx
    purchase_usd = p0 * usd_share * (1 - disc)
    purchase_sum = p0 * (1 - save - usd_share)
    deliv = np.array([deliv_usd[m] for m in mode]) * fx * loc_deliv * pool_deliv
    duty_rate = a.v("market.duty_rate")
    duty_p, duty_d = duty_rate * purchase_usd, duty_rate * deliv
    clearance_annual = a.v("costs.clearance_per_shipment") * n * pool_clear

    batch_months = 12 / n
    safety = safety_months + np.array([buffer[m] for m in mode])
    credit = np.where(prepay, 0.0, a.v("costs.supplier_credit_months"))
    stock_months = batch_months / 2 + safety + np.array([transit[m] for m in mode]) - credit
    hold = (a.v("costs.money_rate") + a.v("costs.storage_rate")) * stock_months / 12
    wo_rate = a.v("costs.writeoff_per_batch_month") * batch_months
    recv_rate = a.v("costs.money_rate") * a.v("costs.pharmacy_deferral_days") / 365

    marketing_annual = a.v("costs.marketing_annual") * (1 - a.v("costs.digital_cost_saving") * d)
    capital_annual = df["invest"].to_numpy(float) * (1 / a.v("costs.amort_years") + a.v("costs.invest_rate"))
    regulatory_annual = a.v("costs.regulatory_annual")

    items = {
        "purchase": purchase_usd + purchase_sum,
        "delivery": deliv,
        "duty": duty_p + duty_d,
        "clearance": clearance_annual / vol,
        "inventory": (purchase_usd + purchase_sum) * hold,
        "writeoff": (purchase_usd + purchase_sum) * wo_rate,
        "receivables": np.full(len(df), price * recv_rate),
        "regional": np.full(len(df), a.v("costs.regional_delivery")),
        "marketing": marketing_annual / vol,
        "regulatory": regulatory_annual / vol,
        "capital": capital_annual / vol,
    }
    return Components(
        items=items,
        usd=purchase_usd * (1 + duty_rate + hold + wo_rate),
        deliv=deliv + duty_d,
        sum_var=purchase_sum * (1 + hold + wo_rate) + a.v("costs.regional_delivery"),
        fixed=clearance_annual + marketing_annual + regulatory_annual + capital_annual,
        volume=vol, recv_rate=recv_rate, price=price, fx=fx,
        customs_value=purchase_usd + deliv,
        purchase_usd=purchase_usd,
        stock_months=batch_months / 2 + safety,
    )


def breakdown(fx: float | None = None, price: float | None = None,
              strategy: pd.DataFrame | None = None) -> pd.DataFrame:
    """Статьи затрат одной стратегии (по умолчанию «как сейчас») с долей от цены."""
    s = current_strategy() if strategy is None else strategy
    c = components(s, fx=fx, price=price)
    rows = [(key, label, cur, float(c.items[key][0])) for key, label, cur in COST_ITEMS]
    out = pd.DataFrame(rows, columns=["key", "item", "currency", "uzs_per_pack"])
    out["share_of_price"] = out["uzs_per_pack"] / c.price
    return out
