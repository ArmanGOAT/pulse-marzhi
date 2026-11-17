"""M5. Оптимизатор плана мер с Монте-Карло.

Пространство: 2 160 комбинаций рычагов из условия × «готовиться к пересмотру» × «пункт о компенсации»
= 8 640 стратегий. Каждая проверяется на одних и тех же N сценариях года (scenarios.generate).

Прибыль стратегии в сценарии за год:
  до пересмотра (доля года f = месяц_пересмотра / 12) продаём по старому потолку,
  после — по сниженному; производитель снижает долларовую закупку на долю cut_pass_through;
  в день пересмотра склад обесценивается (меньше при подготовке, частично компенсируется по пункту контракта).

Жёсткие правила (стратегия, нарушившая их, в рекомендации не попадает):
  цену не поднимаем (по построению); запас ≥ 1 месяца (по построению симулятора и модели упаковки);
  вероятность убытка ≤ max_loss_prob; вложения ≤ бюджета; маржа ≥ цели — устойчивая маржа, если потолок
  привязан к таможенной цене (скидки на закупку и дешёвая доставка уйдут в следующий потолок).
Выбор: самая дешёвая по вложениям, при равных — самая быстрая, затем — с большей ожидаемой прибылью.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from pulse.config import load_assumptions, load_levers
from pulse.decision import scenarios as sc
from pulse.domain import inventory as inv
from pulse.domain import pack


@dataclass
class Inputs:
    fx: float | None = None              # курс, сум/$ (по умолчанию из конфига)
    target_margin: float = 0.15          # цель по марже
    budget: float = 10e9                 # бюджет вложений, сум
    max_months: int | None = None        # к какому месяцу нужен полный эффект (None — без ограничения)
    p_cut: float | None = None           # вероятность пересмотра; None — из радара для brand
    brand: str = "Сербан"
    cut_scale: float = 1.0               # «что если» снижения будут глубже/мельче реальных
    revision_month: int | None = None
    cip_binds: bool = False              # потолок привязан к нашей таможенной цене (ловушка формулы)
    n_scenarios: int | None = None
    seed: int = 42


@dataclass
class Result:
    inputs: dict
    p_cut: float
    p_cut_source: str
    cut_source: str
    current: dict
    plan: dict | None
    feasible: bool
    message: str
    actions: list[dict] = field(default_factory=list)
    strategies: pd.DataFrame | None = None
    seconds: float = 0.0


def _space() -> pd.DataFrame:
    base = pack.strategy_table()
    base["base_id"] = np.arange(len(base))
    variants = pd.DataFrame({"prepare": [False, False, True, True], "protect": [False, True, False, True]})
    return base.merge(variants, how="cross")


def _prep_cost_by_shipments(annual: float, pooled_clear_mult: float) -> dict[tuple[int, bool], float]:
    """Цена подготовки к пересмотру: лишние поставки × оформление (дешевле при общем грузе)."""
    a = load_assumptions()
    out = {}
    for n in load_levers().shipments_per_year:
        reg = inv.simulate(annual, n, revision_day=180)
        prep = inv.simulate(annual, n, revision_day=180, prepare=True)
        extra = max(prep.shipments - reg.shipments, 0)
        for pooled in (False, True):
            mult = pooled_clear_mult if pooled else 1.0
            out[(n, pooled)] = extra * a.v("costs.clearance_per_shipment") * mult
    return out


def optimize(inp: Inputs | None = None, chunk: int = 720) -> Result:
    t0 = time.time()
    inp = inp or Inputs()
    a, lv = load_assumptions(), load_levers()
    fx = inp.fx or a.v("market.fx_usd_uzs")
    price = a.v("market.price_ceiling")
    annual = a.v("market.annual_volume")
    t_rev = inp.revision_month or int(a.v("risk.revision_month"))
    if inp.p_cut is None:
        p_cut, p_src = sc.radar_probability(inp.brand)
    else:
        p_cut, p_src = inp.p_cut, "задано вручную"
    scen = sc.generate(p_cut, inp.n_scenarios, inp.seed, a)
    cut = np.clip(scen.cut * inp.cut_scale, 0, 0.9)

    space = _space()
    base = pack.strategy_table()
    comp = pack.components(base, fx=fx)
    cur_idx = int(base.index[(base[pack.LEVER_COLS] == pd.Series(lv.current.model_dump())).all(axis=1)][0])

    pt = a.v("costs.cut_pass_through")
    duty = a.v("market.duty_rate")
    share = a.v("costs.protection_share")
    prem = a.v("costs.protection_premium")
    prot_capital = a.v("costs.protection_invest") * (1 / a.v("costs.amort_years") + a.v("costs.invest_rate"))
    prep_costs = _prep_cost_by_shipments(annual, lv.pooled_option(True).clearance_mult)

    # склад в упаковках в день пересмотра: страховой + половина партии (при подготовке — мелкой партии)
    vol = comp.volume
    safety_units = (comp.stock_months - (12 / base["shipments"].to_numpy()) / 2) * vol / 12
    batch = vol / base["shipments"].to_numpy()
    small = vol / 12 * a.v("costs.prep_batch_months")
    exposure_reg = safety_units + batch / 2
    exposure_prep = safety_units + np.minimum(batch, small) / 2

    # устойчивая маржа: если потолок следует за нашей таможенной ценой, её снижение уходит в потолок.
    # Считаем только скидки и доставку импортируемого товара: при локализации препарат переходит
    # в режим местного производителя, и как там считают потолок — открытый вопрос (docs/RISKS.md, R6).
    as_import = base.assign(loc="import")
    cip_import = pack.components(as_import, fx=fx).customs_value
    cip_ratio = np.minimum(1.0, cip_import / cip_import[cur_idx])
    price_durable = price * (cip_ratio if inp.cip_binds else np.ones_like(cip_ratio))
    cost_now = comp.total
    margin_now = comp.margin

    fx_k, dem_k, del_k = scen.fx_mult[None, :], scen.demand_mult[None, :], scen.delivery_mult[None, :]
    has_cut = cut > 0
    f = np.where(has_cut, t_rev / 12, 1.0)[None, :]
    c = cut[None, :]

    rows = []
    for start in range(0, len(base), chunk):
        sl = slice(start, start + chunk)
        usd, deliv = comp.usd[sl, None], comp.deliv[sl, None]
        sum_var, fixed, v = comp.sum_var[sl, None], comp.fixed[sl, None], comp.volume[sl, None]
        p_usd = comp.purchase_usd[sl, None]
        units = v * dem_k
        common = deliv * fx_k * del_k + sum_var + fixed / units
        cost_pre = usd * fx_k + common + comp.recv_rate * price
        cost_post = usd * (1 - pt * c) * fx_k + common + comp.recv_rate * price * (1 - c)
        rev = units * (f * price + (1 - f) * price * (1 - c))
        base_profit = units * (f * (price - cost_pre) + (1 - f) * (price * (1 - c) - cost_post))
        premium = units * prem * p_usd * fx_k
        deval_unit = p_usd * c * pt * (1 + duty)
        for prepare in (False, True):
            exp_units = (exposure_prep if prepare else exposure_reg)[sl, None]
            n = base["shipments"].to_numpy()[sl]
            pooled = base["pooled"].to_numpy()[sl]
            prep_cost = np.array([prep_costs[(int(k), bool(pl))] for k, pl in zip(n, pooled, strict=True)])[:, None] \
                if prepare else 0.0
            for protect in (False, True):
                deval = exp_units * deval_unit * (1 - share if protect else 1.0)
                profit = base_profit - deval - prep_cost - (premium + prot_capital if protect else 0.0)
                loss = (profit < 0).mean(axis=1)
                mean_profit = profit.mean(axis=1)
                mean_margin = (profit / rev).mean(axis=1)
                for i, bi in enumerate(range(sl.start, min(sl.stop, len(base)))):
                    rows.append((bi, prepare, protect, loss[i], mean_profit[i], mean_margin[i]))
    mc = pd.DataFrame(rows, columns=["base_id", "prepare", "protect", "p_loss", "exp_profit", "exp_margin"])
    df = space.merge(mc, on=["base_id", "prepare", "protect"])

    prem_unit = prem * comp.purchase_usd
    df["margin_now"] = margin_now[df["base_id"]] - np.where(df["protect"], prem_unit[df["base_id"]] / price, 0.0)
    pd_ = price_durable[df["base_id"]]
    df["margin_durable"] = (pd_ - (cost_now[df["base_id"]] - comp.recv_rate * price + comp.recv_rate * pd_)
                            - np.where(df["protect"], prem_unit[df["base_id"]], 0.0)) / pd_
    df["invest"] = df["invest"] + np.where(df["protect"], a.v("costs.protection_invest"), 0.0)
    df["months"] = np.maximum(df["months"], np.where(df["protect"], a.v("costs.protection_months"), 0))
    check = df["margin_durable"] if inp.cip_binds else df["margin_now"]
    df["feasible"] = (check >= inp.target_margin) & (df["p_loss"] <= a.v("risk.max_loss_prob")) & \
                     (df["invest"] <= inp.budget)
    if inp.max_months is not None:
        df["feasible"] &= df["months"] <= inp.max_months

    current_row = df[(df["base_id"] == cur_idx) & ~df["prepare"] & ~df["protect"]].iloc[0]
    feasible = df[df["feasible"]]
    if len(feasible):
        best = feasible.sort_values(["invest", "months", "exp_profit"], ascending=[True, True, False]).iloc[0]
        msg = "План найден: самый дешёвый по вложениям, который даёт цель и держит риск убытка не выше допустимого."
        ok = True
    else:
        safe = df[(df["p_loss"] <= a.v("risk.max_loss_prob")) & (df["invest"] <= inp.budget)]
        if len(safe):
            best = safe.sort_values(check.name if False else "margin_now", ascending=False).iloc[0]
            msg = "Цель по марже недостижима в бюджете при допустимом риске. Показан план с наибольшей маржой при риске не выше допустимого."
        else:
            best = df[df["invest"] <= inp.budget].sort_values("p_loss").iloc[0]
            msg = "Ни один план в бюджете не держит риск убытка в допустимых пределах. Показан план с наименьшим риском."
        ok = False

    detail = _detail(best, scen, cut, f, comp, price, t_rev, pt, duty, share, prem, prot_capital, prep_costs,
                     exposure_reg, exposure_prep)
    cur_detail = _detail(current_row, scen, cut, f, comp, price, t_rev, pt, duty, share, prem, prot_capital,
                         prep_costs, exposure_reg, exposure_prep)
    res = Result(
        inputs=asdict(inp) | {"fx": fx, "revision_month": t_rev}, p_cut=p_cut, p_cut_source=p_src,
        cut_source=scen.cut_source, current=cur_detail, plan=detail, feasible=ok, message=msg,
        actions=_actions(best, current_row, comp, base, exposure_reg, exposure_prep, t_rev, prep_costs),
        strategies=df[["shipments", "procurement", "mode", "pooled", "digital", "loc", "prepare", "protect",
                       "invest", "months", "margin_now", "margin_durable", "p_loss", "exp_profit", "feasible"]],
    )
    res.seconds = round(time.time() - t0, 2)
    return res


def _detail(row: pd.Series, scen, cut, f, comp, price, t_rev, pt, duty, share, prem, prot_capital, prep_costs,
            exposure_reg, exposure_prep) -> dict:
    """Распределение прибыли и маржи одной стратегии по сценариям (для графика «риск»)."""
    i = int(row["base_id"])
    fx_k, dem_k, del_k = scen.fx_mult, scen.demand_mult, scen.delivery_mult
    units = comp.volume[i] * dem_k
    common = comp.deliv[i] * fx_k * del_k + comp.sum_var[i] + comp.fixed[i] / units
    cost_pre = comp.usd[i] * fx_k + common + comp.recv_rate * price
    cost_post = comp.usd[i] * (1 - pt * cut) * fx_k + common + comp.recv_rate * price * (1 - cut)
    ff = f[0]
    rev = units * (ff * price + (1 - ff) * price * (1 - cut))
    profit = units * (ff * (price - cost_pre) + (1 - ff) * (price * (1 - cut) - cost_post))
    exp_units = exposure_prep[i] if row["prepare"] else exposure_reg[i]
    deval = exp_units * comp.purchase_usd[i] * cut * pt * (1 + duty) * ((1 - share) if row["protect"] else 1.0)
    profit = profit - deval
    if row["prepare"]:
        profit = profit - prep_costs[(int(row["shipments"]), bool(row["pooled"]))]
    if row["protect"]:
        profit = profit - units * prem * comp.purchase_usd[i] * fx_k - prot_capital
    margin = profit / rev
    q = np.percentile
    return {
        "levers": {k: (bool(row[k]) if isinstance(row[k], bool | np.bool_) else row[k])
                   for k in pack.LEVER_COLS + ["prepare", "protect"]},
        "invest": float(row["invest"]), "months": int(row["months"]),
        "margin_now": float(row["margin_now"]), "margin_durable": float(row["margin_durable"]),
        "p_loss": float(row["p_loss"]), "exp_profit": float(row["exp_profit"]),
        "profit_p10_p50_p90": [float(q(profit, 10)), float(q(profit, 50)), float(q(profit, 90))],
        "margin_p10_p50_p90": [float(q(margin, 10)), float(q(margin, 50)), float(q(margin, 90))],
        "margin_samples": np.round(margin, 4).tolist(),
        "stock_on_revision_units": float(exp_units),
    }


def _actions(best: pd.Series, cur: pd.Series, comp, base, exposure_reg, exposure_prep, t_rev, prep_costs) -> list[dict]:
    """Что изменить относительно «как сейчас»: мера, вложения, срок, эффект на маржу в одиночку."""
    lv, a = load_levers(), load_assumptions()
    out = []
    cur_margin = comp.margin[int(cur["base_id"])]

    def solo(**change) -> float:
        row = cur[pack.LEVER_COLS].to_dict() | change
        m = (base[pack.LEVER_COLS] == pd.Series(row)).all(axis=1)
        return float(comp.margin[int(base.index[m][0])] - cur_margin)

    def add(kind, text, invest, months, effect_pp, durable, note=""):
        out.append({"kind": kind, "text": text, "invest": float(invest), "months": int(months),
                    "effect_pp": round(effect_pp * 100, 2) if effect_pp is not None else None,
                    "durable": durable, "note": note})

    if best["shipments"] != cur["shipments"]:
        add("shipments", f"Перейти с {int(cur['shipments'])} на {int(best['shipments'])} поставок в год", 0,
            lv.change_shipments_months, solo(shipments=best["shipments"]), True,
            "меньше денег в запасах и списаний, больше оформлений")
    if best["procurement"] != cur["procurement"]:
        o = lv.procurement[best["procurement"]]
        add("procurement", o.label, o.invest, o.months, solo(procurement=best["procurement"]),
            not o.enters_customs_value, "снижает таможенную цену: при пересмотре потолок может опуститься вслед")
    if best["mode"] != cur["mode"]:
        add("mode", f"Доставка: {lv.modes[best['mode']].label}", 0, lv.modes[best["mode"]].months,
            solo(mode=best["mode"]), False, "доставка входит в таможенную цену")
    if bool(best["pooled"]) != bool(cur["pooled"]):
        o = lv.pooled_option(True)
        add("pooled", o.label, o.invest, o.months, solo(pooled=True), False,
            "дешевле доставка (входит в таможенную цену) и оформление (не входит)")
    if best["digital"] != cur["digital"]:
        add("digital", f"Цифровой маркетинг: {best['digital']:.0%} контактов с врачами", best["digital"] * lv.digital_invest_full,
            round(best["digital"] * lv.digital_months_full), solo(digital=best["digital"]), True,
            "сначала пилот в части регионов")
    if best["loc"] != cur["loc"]:
        o = lv.localization[best["loc"]]
        add("loc", o.label, o.invest, o.months, solo(loc=best["loc"]), True, "включает перерегистрацию")
    if best["prepare"]:
        i = int(best["base_id"])
        add("prepare", f"За 2 месяца до ожидаемой волны (≈ месяц {max(t_rev - 2, 0)}) возить мелкими партиями",
            prep_costs[(int(best["shipments"]), bool(best["pooled"]))], 0, None, True,
            f"склад в день пересмотра {exposure_reg[i]:,.0f} → {exposure_prep[i]:,.0f} упаковок".replace(",", " "))
    if best["protect"]:
        add("protect", f"Пункт в контракте: производитель компенсирует {a.v('costs.protection_share'):.0%} "
            "обесценения склада при снижении потолка", a.v("costs.protection_invest"), a.v("costs.protection_months"),
            -a.v("costs.protection_premium") * comp.purchase_usd[int(best["base_id"])] / a.v("market.price_ceiling"),
            True, "надбавка к цене закупки за пункт — допущение")
    return out


if __name__ == "__main__":
    for target, p in [(0.15, None), (0.15, 0.6), (0.25, None)]:
        r = optimize(Inputs(target_margin=target, p_cut=p))
        print(f"\nЦель {target:.0%}, p пересмотра {r.p_cut:.0%} ({r.p_cut_source}); снижения: {r.cut_source}; {r.seconds} с")
        print(" ", r.message)
        cu, pl = r.current, r.plan
        print(f"  сейчас: маржа {cu['margin_now']:.1%}, P(убытка) {cu['p_loss']:.0%}, прибыль P50 {cu['profit_p10_p50_p90'][1]/1e9:.2f} млрд")
        print(f"  план:   маржа {pl['margin_now']:.1%} (устойчивая {pl['margin_durable']:.1%}), P(убытка) {pl['p_loss']:.0%}, "
              f"вложения {pl['invest']/1e9:.2f} млрд, срок {pl['months']} мес., прибыль P50 {pl['profit_p10_p50_p90'][1]/1e9:.2f} млрд")
        for act in r.actions:
            eff = f"{act['effect_pp']:+.1f} п.п." if act["effect_pp"] is not None else "защита от пересмотра"
            print(f"   - {act['text']}: {eff}, вложения {act['invest']/1e6:.0f} млн, {act['months']} мес. "
                  f"{'(останется)' if act['durable'] else '(может уйти в потолок)'}")
