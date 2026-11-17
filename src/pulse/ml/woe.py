"""WoE-кодирование (weight of evidence) для объяснимой логистической регрессии.

Числовые признаки режем на квантильные интервалы, категориальные берём как есть (редкие → «прочие»).
Для каждого интервала: WoE = ln(доля «снизили» в интервале / доля «не снизили» в интервале) со сглаживанием.
Интервалы и WoE считаются только на обучающих данных.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MISSING = "нет данных"


@dataclass
class FeatureBins:
    name: str
    kind: str                         # "num" | "cat"
    edges: list[float] = field(default_factory=list)
    woe: dict[str, float] = field(default_factory=dict)
    iv: float = 0.0


def _num_labels(x: pd.Series, edges: list[float]) -> pd.Series:
    cut = pd.cut(x, bins=[-np.inf, *edges, np.inf], include_lowest=True)
    return cut.astype(str).where(x.notna(), MISSING)


class WoEEncoder:
    def __init__(self, numeric: list[str], categorical: list[str], n_bins: int = 6, min_share: float = 0.01,
                 smoothing: float = 0.5):
        self.numeric, self.categorical = numeric, categorical
        self.n_bins, self.min_share, self.smoothing = n_bins, min_share, smoothing
        self.bins: dict[str, FeatureBins] = {}

    def _labels(self, name: str, x: pd.Series) -> pd.Series:
        b = self.bins[name]
        if b.kind == "num":
            return _num_labels(pd.to_numeric(x, errors="coerce"), b.edges)
        lab = x.astype(str)
        return lab.where(lab.isin(b.woe), "прочие")

    def fit(self, X: pd.DataFrame, y: pd.Series) -> WoEEncoder:
        y = pd.Series(np.asarray(y), index=X.index)
        pos_total, neg_total = y.sum(), (1 - y).sum()
        for name in self.numeric + self.categorical:
            if name in self.numeric:
                x = pd.to_numeric(X[name], errors="coerce")
                qs = np.unique(np.nanquantile(x.dropna(), np.linspace(0, 1, self.n_bins + 1)[1:-1])) if x.notna().any() else []
                b = FeatureBins(name, "num", edges=[float(q) for q in qs])
                self.bins[name] = b
                lab = _num_labels(x, b.edges)
            else:
                lab = X[name].astype(str)
                share = lab.value_counts(normalize=True)
                keep = set(share[share >= self.min_share].index)
                lab = lab.where(lab.isin(keep), "прочие")
                b = FeatureBins(name, "cat")
                self.bins[name] = b
            stats = pd.DataFrame({"lab": lab, "y": y}).groupby("lab")["y"].agg(["sum", "count"])
            k = len(stats)
            pos = (stats["sum"] + self.smoothing) / (pos_total + self.smoothing * k)
            neg = (stats["count"] - stats["sum"] + self.smoothing) / (neg_total + self.smoothing * k)
            woe = np.log(pos / neg)
            b.woe = {str(i): float(v) for i, v in woe.items()}
            b.woe.setdefault("прочие", 0.0)
            b.woe.setdefault(MISSING, 0.0)
            b.iv = float(((pos - neg) * woe).sum())
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        out = {}
        for name, b in self.bins.items():
            out[name] = self._labels(name, X[name]).map(b.woe).fillna(0.0).astype(float)
        return pd.DataFrame(out, index=X.index)

    def bin_label(self, name: str, value: object) -> str:
        return str(self._labels(name, pd.Series([value])).iloc[0])

    def information_values(self) -> dict[str, float]:
        return dict(sorted(((n, b.iv) for n, b in self.bins.items()), key=lambda kv: -kv[1]))
