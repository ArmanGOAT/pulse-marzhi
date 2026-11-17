"""Сценарии следующего года для Монте-Карло.

Одни и те же сценарии (общие случайные числа) используются для всех стратегий: разница между
стратегиями — эффект мер, а не шум генератора.

Откуда берутся распределения:
- курс: дрейф и разброс годового изменения по истории ЦБ (config/assumptions.yaml, источник cbu.uz);
- снижение потолка: эмпирическое распределение реальных снижений импортных препаратов в волнах A–C
  (последний эксперимент радара); если отчёта нет — равномерно в [cut_min, cut_max] из конфига;
- вероятность пересмотра: прогноз радара для выбранного бренда (или значение по умолчанию).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pulse.config import Assumptions, load_assumptions
from pulse.ml import experiments


@dataclass(frozen=True)
class Scenarios:
    fx_mult: np.ndarray       # множитель курса к текущему
    demand_mult: np.ndarray   # множитель спроса
    delivery_mult: np.ndarray # множитель тарифа доставки
    cut: np.ndarray           # снижение потолка в этом сценарии (0, если пересмотра нет)
    cut_source: str

    @property
    def n(self) -> int:
        return len(self.fx_mult)


def empirical_cuts(imports_only: bool = True) -> np.ndarray | None:
    """Реальные размеры снижений (при снижении ≥10%) из прогнозов последнего эксперимента радара."""
    try:
        d = experiments.latest_dir()
        frames = [pd.read_parquet(d / "scores" / f"{p}.parquet") for p in ("validation", "test")]
    except (FileNotFoundError, OSError):
        return None
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["y"] == 1) & (df["cut_size"] > 0)]
    if imports_only:
        df = df[~df["local"].astype(bool)]
    return df["cut_size"].clip(upper=0.9).to_numpy() if len(df) >= 50 else None


def radar_probability(trademark: str) -> tuple[float, str]:
    """Вероятность снижения для бренда из прогноза радара на последнюю дату реестра."""
    try:
        riv = json.loads((experiments.latest_dir() / "rivaroxaban.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return load_assumptions().v("risk.cut_prob_default"), "по умолчанию: отчёта радара нет"
    for b in riv["brands"]:
        if b["trademark"].lower() == trademark.lower():
            return float(b["p_cut"]), f"радар, прогноз на {riv['asof']}"
    return load_assumptions().v("risk.cut_prob_default"), f"по умолчанию: бренда {trademark} нет в прогнозе"


def generate(p_cut: float, n: int | None = None, seed: int = 42, a: Assumptions | None = None,
             cuts: np.ndarray | None = None) -> Scenarios:
    a = a or load_assumptions()
    n = int(n or a.v("risk.n_scenarios"))
    rng = np.random.default_rng(seed)
    fx = 1 + a.v("risk.fx_drift") + a.v("risk.fx_sd") * rng.standard_normal(n)
    demand = 1 + a.v("risk.demand_sd") * rng.standard_normal(n)
    delivery = 1 + a.v("risk.delivery_sd") * rng.standard_normal(n)
    happens = rng.random(n) < p_cut
    pool = empirical_cuts() if cuts is None else cuts
    if pool is not None and len(pool):
        size, source = rng.choice(pool, n), f"реальные снижения импортных препаратов, волны A–C ({len(pool)} шт.)"
    else:
        size = rng.uniform(a.v("risk.cut_min"), a.v("risk.cut_max"), n)
        source = "равномерно по допущению из конфига"
    return Scenarios(
        fx_mult=np.clip(fx, 0.5, 2.0), demand_mult=np.clip(demand, 0.3, 2.0),
        delivery_mult=np.clip(delivery, 0.2, 3.0), cut=np.where(happens, size, 0.0), cut_source=source,
    )
