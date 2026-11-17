"""M4. Радар пересмотра: подбор гиперпараметров, проверка во времени, финальная модель, журнал эксперимента.

Схема (config/radar.yaml):
1. Подбор: учим на tune_train (волна A), меряем на validation (волна B) — сетка из конфига.
2. Выбор: лучшие гиперпараметры и чемпион по метрике на validation. Волну C не трогаем.
3. Тест: учим лучшими гиперпараметрами на test_train (A+B), один раз меряем на test (C).
4. Финал: чемпион учится на всех волнах и даёт прогноз на последнюю дату реестра.
Всё пишется в artifacts/experiments/<id>/ (pulse.ml.experiments).

Деловая метрика: средняя выгода на препарат в экономике нашего импортёра — готовимся к волне,
если p × ожидаемое снижение × K > C (K — выигрыш от меньшего склада на 1,0 снижения, C — цена подготовки).
"""
from __future__ import annotations

import argparse
import time
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold

from pulse.config import load_assumptions
from pulse.data import registry as reg_data
from pulse.domain import inventory as inv
from pulse.ml import experiments
from pulse.ml.features import FEATURE_SETS, build_features, wave_dataset
from pulse.ml.settings import RadarCfg, load_radar_cfg
from pulse.ml.woe import WoEEncoder

# ---------- экономика решения «готовиться» ----------

@dataclass(frozen=True)
class Economics:
    gain_per_cut: float   # K: сколько сберегает подготовка на 1,0 снижения потолка, сум
    prep_cost: float      # C: цена подготовки, сум


def economics() -> Economics:
    a = load_assumptions()
    annual, unit_cost = a.v("market.annual_volume"), a.v("costs.purchase_usd") * a.v("market.fx_usd_uzs")
    d = inv.prepare_decision(0.5, 1.0, annual, int(a.v("costs.base_shipments")), unit_cost)
    k = inv.devaluation_loss(d.exposure_regular - d.exposure_prepared, unit_cost, 1.0)
    return Economics(gain_per_cut=k, prep_cost=d.prep_cost)


def business_value(prepare: np.ndarray, y: np.ndarray, cut_size: np.ndarray, eco: Economics) -> float:
    """Средняя выгода на препарат, сум: подготовка окупается, только если цену действительно снизили."""
    gain = np.where(y == 1, cut_size * eco.gain_per_cut, 0.0)
    return float(np.mean(np.where(prepare, gain - eco.prep_cost, 0.0)))


def decision_threshold(train: pd.DataFrame, eco: Economics) -> tuple[float, float]:
    """Порог вероятности из экономики: p* = C / (ожидаемое снижение × K). Ожидаемое снижение — по обучающим волнам."""
    expected_cut = float(train.loc[train["y"] == 1, "cut_size"].mean())
    return eco.prep_cost / (expected_cut * eco.gain_per_cut), expected_cut


# ---------- модели ----------

class RuleModel:
    """Бейзлайн: цена выше медианы аналогов той же дозы. Вероятности — частоты на обучающих волнах."""

    def __init__(self, params: dict | None = None, seed: int = 0, cols: tuple = ((), ()), **_):
        self.params = params or {}

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> RuleModel:
        flag = self._flag(X)
        self.p1 = float(y[flag].mean()) if flag.any() else 0.5
        self.p0 = float(y[~flag].mean()) if (~flag).any() else 0.5
        return self

    @staticmethod
    def _flag(X: pd.DataFrame) -> np.ndarray:
        return (X["inn_ratio_median"].fillna(1.0) > 1.0).to_numpy()

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return np.where(self._flag(X), self.p1, self.p0)


class WoELogit:
    def __init__(self, params: dict, seed: int = 0, cols: tuple = FEATURE_SETS["base"], **_):
        self.params = params
        self.numeric, self.categorical = list(cols[0]), list(cols[1])

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> WoELogit:
        p = self.params
        self.enc = WoEEncoder(self.numeric, self.categorical, n_bins=p["n_bins"], min_share=p["min_share"],
                              smoothing=p["smoothing"]).fit(X, y)
        self.lr = LogisticRegression(C=p["C"], max_iter=3000).fit(self.enc.transform(X), y)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return self.lr.predict_proba(self.enc.transform(X))[:, 1]

    def contributions(self, X: pd.DataFrame) -> pd.DataFrame:
        return self.enc.transform(X) * self.lr.coef_[0]

    def describe(self) -> dict:
        coefs = dict(zip(self.enc.bins, self.lr.coef_[0].round(4).tolist(), strict=True))
        return {
            "intercept": float(self.lr.intercept_[0]),
            "coefficients": coefs,
            "information_values": self.enc.information_values(),
            "woe_bins": {n: {"kind": b.kind, "edges": b.edges, "woe": b.woe} for n, b in self.enc.bins.items()},
        }


class CalibratedCatBoost:
    def __init__(self, params: dict, seed: int = 0, cols: tuple = FEATURE_SETS["base"], calibrate: bool = True):
        self.params, self.seed, self.calibrate = params, seed, calibrate
        self.numeric, self.categorical = list(cols[0]), list(cols[1])
        self.features = self.numeric + self.categorical

    def _prep(self, X: pd.DataFrame) -> pd.DataFrame:
        Z = X[self.features].copy()
        for c in self.categorical:
            Z[c] = Z[c].astype(str)
        return Z

    def _model(self) -> CatBoostClassifier:
        p = self.params
        return CatBoostClassifier(iterations=p["iterations"], depth=p["depth"], learning_rate=p["learning_rate"],
                                  l2_leaf_reg=p["l2_leaf_reg"], random_seed=self.seed, verbose=0,
                                  cat_features=self.categorical, allow_writing_files=False)

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> CalibratedCatBoost:
        Z = self._prep(X)
        self.iso = None
        if self.calibrate:
            oof = np.zeros(len(y))
            folds = StratifiedKFold(self.params["calibration_folds"], shuffle=True, random_state=self.seed)
            for tr, va in folds.split(Z, y):
                oof[va] = self._model().fit(Z.iloc[tr], y[tr]).predict_proba(Z.iloc[va])[:, 1]
            self.iso = IsotonicRegression(out_of_bounds="clip").fit(oof, y)
        self.cb = self._model().fit(Z, y)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        raw = self.cb.predict_proba(self._prep(X))[:, 1]
        return self.iso.predict(raw) if self.iso is not None else raw

    def describe(self) -> dict:
        imp = self.cb.get_feature_importance()
        return {"feature_importance": dict(sorted(zip(self.features, np.round(imp, 3).tolist(), strict=True),
                                                  key=lambda kv: -kv[1]))}


MODEL_CLASSES = {"rule": RuleModel, "woe_logit": WoELogit, "catboost": CalibratedCatBoost}


def make_model(name: str, params: dict, seed: int, cols: tuple = FEATURE_SETS["base"], **kw):
    return MODEL_CLASSES[name](params, seed, cols=cols, **kw)


# ---------- метрики ----------

def metrics(y: np.ndarray, p: np.ndarray, cut_size: np.ndarray, eco: Economics, threshold: float) -> dict:
    top = np.argsort(-p)[: max(1, len(p) // 10)]
    prepare = p >= threshold
    return {
        "n": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4),
        "auc": round(float(roc_auc_score(y, p)), 4), "brier": round(float(brier_score_loss(y, p)), 4),
        "precision_top10": round(float(y[top].mean()), 4),
        "recall_at_threshold": round(float(y[prepare].sum() / max(y.sum(), 1)), 4),
        "flagged_share": round(float(prepare.mean()), 4),
        "value_per_drug_uzs": round(business_value(prepare, y, cut_size, eco), 0),
    }


def strategy_values(y: np.ndarray, cut_size: np.ndarray, eco: Economics) -> dict:
    return {"never_prepare": 0.0,
            "always_prepare": round(business_value(np.ones(len(y), bool), y, cut_size, eco), 0)}


# ---------- подбор гиперпараметров ----------

def tune(name: str, cfg: RadarCfg, train: pd.DataFrame, val: pd.DataFrame) -> list[dict]:
    """Все комбинации сетки: учим на train, меряем на validation.
    CatBoost в подборе без калибровки: на порядок объектов (AUC) калибровка почти не влияет."""
    trials = []
    y_tr, y_va = train["y"].to_numpy(), val["y"].to_numpy()
    for params in cfg.models[name].grid():
        t0 = time.time()
        kw = {"calibrate": False} if name == "catboost" else {}
        p = make_model(name, params, cfg.seed, FEATURE_SETS[cfg.feature_set], **kw).fit(train, y_tr).predict_proba(val)
        trials.append({"model": name, "params": params, "val_auc": round(float(roc_auc_score(y_va, p)), 4),
                       "val_brier": round(float(brier_score_loss(y_va, p)), 4), "seconds": round(time.time() - t0, 1)})
    return sorted(trials, key=lambda t: -t[f"val_{cfg.selection['metric']}"])


# ---------- причины прогноза ----------

def _reason(name: str, row: pd.Series) -> str:
    v = row[name]
    templates = {
        "inn_ratio_median": lambda: f"цена в {v:.1f} раза выше медианы аналогов той же дозы" if v >= 1
        else f"цена ниже медианы аналогов (×{v:.2f})",
        "inn_ratio_min": lambda: f"в {v:.1f} раза дороже самого дешёвого аналога той же дозы",
        "inn_rank_pct": lambda: f"дороже {v:.0%} аналогов той же дозы",
        "n_brands_inn": lambda: f"{int(v)} брендов с тем же МНН",
        "has_local_analog": lambda: "есть местный аналог" if str(v) == "True" else "местного аналога нет",
        "prior_cut": lambda: "цену уже снижали раньше" if str(v) == "True" else "цену ещё не снижали",
        "local": lambda: "местный препарат" if str(v) == "True" else "импортный препарат",
        "currency": lambda: f"базовая цена в {v}",
        "country_group": lambda: f"производитель: {v}",
        "form": lambda: f"форма выпуска: {v}",
        "months_since_change": lambda: f"{v:.0f} мес. без изменения цены",
        "log_usd_per_unit": lambda: f"${np.exp(v):.2f} за единицу",
        "log_base_usd": lambda: f"${np.exp(v):.2f} за упаковку",
        "reg_age_years": lambda: f"регистрации {v:.0f} лет",
        "single_brand_inn": lambda: "единственный бренд по МНН" if str(v) == "True" else "есть другие бренды",
        "units": lambda: f"{v:.0f} единиц в упаковке",
        "ratio_to_cut_peers": lambda: f"цена в {v:.1f} раза выше аналогов, которым уже снизили цену" if v >= 1
        else f"цена ниже аналогов, которым уже снизили цену (×{v:.2f})",
        "share_cut_in_group": lambda: f"{v:.0%} аналогов той же дозы уже снижали",
        "has_cut_peers": lambda: "у аналогов той же дозы цену уже снижали" if str(v) == "True"
        else "аналогам той же дозы цену ещё не снижали",
    }
    try:
        return templates[name]()
    except (ValueError, TypeError, KeyError):
        return name


def top_reasons(model: WoELogit, X: pd.DataFrame, k: int = 3) -> list[list[str]]:
    contrib = model.contributions(X)
    out = []
    for i in range(len(X)):
        row = contrib.iloc[i].sort_values(ascending=False)
        out.append([_reason(n, X.iloc[i]) for n, c in row.items() if c > 0][:k])
    return out


# ---------- полный прогон ----------

SCORE_COLS = ["key", "trademark", "inn", "strength", "form", "manufacturer", "currency", "base_price", "local",
              "prior_cut", "has_cut_peers"]


def _fit_predict(names: list[str], best: dict[str, dict], cfg: RadarCfg, train: pd.DataFrame,
                 target: pd.DataFrame) -> dict[str, tuple[Any, np.ndarray]]:
    y = train["y"].to_numpy()
    out = {}
    for n in names:
        m = make_model(n, best.get(n, {}), cfg.seed, FEATURE_SETS[cfg.feature_set]).fit(train, y)
        out[n] = (m, m.predict_proba(target))
    return out


def _scores(df: pd.DataFrame, fitted: dict[str, tuple[Any, np.ndarray]]) -> pd.DataFrame:
    return df[SCORE_COLS + ["y", "cut_size"]].assign(**{f"p_{n}": p for n, (_, p) in fitted.items()})


def _scope(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    """imports — только импортные упаковки (наш пользователь — импортёр); all — все рецептурные."""
    return df[~df["local"]].reset_index(drop=True) if scope == "imports" else df


def run(name: str | None = None, overrides: dict | None = None) -> dict:
    cfg = load_radar_cfg()
    if overrides:
        data_cfg = cfg.model_dump()
        for k, v in overrides.items():
            if v is None:
                continue
            if k in ("train", "eval"):
                data_cfg["scope"][k] = v
            else:
                data_cfg[k] = v
        cfg = type(cfg).model_validate(data_cfg)
    name = name or f"radar-{cfg.feature_set}-train-{cfg.scope.train}-eval-{cfg.scope.eval}"
    numeric, categorical = FEATURE_SETS[cfg.feature_set]
    exp = experiments.ExperimentLog(name)
    exp.json("config", cfg.model_dump())
    exp.json("environment", experiments.environment(cfg.seed))

    panel = reg_data.panel()
    events = reg_data.events(panel)
    thr = cfg.label["cut_threshold"]
    data = {w.name: wave_dataset(w, panel, events, thr) for w in cfg.waves}
    eco = economics()
    exp.json("data", experiments.data_versions() | {
        "source": "mpa.uz, реестр референтных цен (снимки API)", "panel_rows": int(len(panel)),
        "snapshots": int(panel["date"].nunique()), "asof_now": str(panel["date"].max().date()),
        "features": {"set": cfg.feature_set, "numeric": numeric, "categorical": categorical},
        "scope": cfg.scope.model_dump(),
        "waves": {w.name: {"asof": w.asof, "label_end": w.label_end, "rows": int(len(data[w.name])),
                           "cut_share": round(float(data[w.name]["y"].mean()), 4)} for w in cfg.waves},
    })

    s = cfg.split
    tune_train = _scope(pd.concat([data[n] for n in s.tune_train], ignore_index=True), cfg.scope.train)
    val, test = _scope(data[s.validation], cfg.scope.eval), _scope(data[s.test], cfg.scope.eval)
    test_train = _scope(pd.concat([data[n] for n in s.test_train], ignore_index=True), cfg.scope.train)
    metric = cfg.selection["metric"]
    names = ["rule", *cfg.selection["candidates"]]

    # 1. подбор гиперпараметров на validation
    search = {n: tune(n, cfg, tune_train, val) for n in cfg.selection["candidates"]}
    best = {n: trials[0]["params"] for n, trials in search.items()}
    exp.json("search", {"metric": metric, "best_params": best, "trials": search})

    # 2. validation с лучшими параметрами (оптимистично: на ней подбирали) → чемпион
    thr_val, cut_val = decision_threshold(tune_train, eco)
    fitted_val = _fit_predict(names, best, cfg, tune_train, val)
    y_val, c_val = val["y"].to_numpy(), val["cut_size"].to_numpy()
    m_val = {n: metrics(y_val, p, c_val, eco, thr_val) for n, (_, p) in fitted_val.items()}
    champion = max(cfg.selection["candidates"], key=lambda n: m_val[n][metric])

    # 3. тест один раз: учим на A+B, меряем на C
    thr_test, cut_test = decision_threshold(test_train, eco)
    fitted_test = _fit_predict(names, best, cfg, test_train, test)
    y_test, c_test = test["y"].to_numpy(), test["cut_size"].to_numpy()
    m_test = {n: metrics(y_test, p, c_test, eco, thr_test) for n, (_, p) in fitted_test.items()}
    strat_val, strat_test = strategy_values(y_val, c_val, eco), strategy_values(y_test, c_test, eco)

    exp.json("metrics", {
        "economics": asdict(eco), "champion": champion, "selection_metric": metric,
        "validation": {"wave": s.validation, "train_waves": s.tune_train, "threshold": round(thr_val, 4),
                       "expected_cut": round(cut_val, 4),
                       "note": "на этой волне подбирали гиперпараметры и выбирали чемпиона: оценка оптимистична",
                       "models": m_val, "strategies": strat_val},
        "test": {"wave": s.test, "train_waves": s.test_train, "threshold": round(thr_test, 4),
                 "expected_cut": round(cut_test, 4), "note": "честная оценка: волну не трогали до этого шага",
                 "models": m_test, "strategies": strat_test},
    })
    exp.frame("validation", _scores(val, fitted_val))
    exp.frame("test", _scores(test, fitted_test))

    # 4. финальная модель на всех волнах → прогноз на сегодня
    final_train = _scope(pd.concat(data.values(), ignore_index=True), cfg.scope.train)
    thr_now, cut_now = decision_threshold(final_train, eco)
    y_all = final_train["y"].to_numpy()
    cols = FEATURE_SETS[cfg.feature_set]
    model = make_model(champion, best[champion], cfg.seed, cols).fit(final_train, y_all)
    explainer = model if isinstance(model, WoELogit) else make_model("woe_logit", best["woe_logit"], cfg.seed, cols).fit(final_train, y_all)
    now = build_features(panel["date"].max(), panel, events)
    now = now[now["rx"]].copy()
    now["p_cut"] = model.predict_proba(now)
    now["prepare"] = now["p_cut"] >= thr_now
    exp.frame("now", now[SCORE_COLS + ["p_cut", "prepare"]])

    exp.json("features", {
        "woe_logit": explainer.describe(),
        "catboost": fitted_test["catboost"][0].describe() if "catboost" in fitted_test else None,
    })

    riv = now[now["inn"].str.contains("ривароксабан") & (now["strength"] == "20мг")].copy()
    riv["reasons"] = top_reasons(explainer, riv)
    riv = riv.sort_values("p_cut", ascending=False)
    exp.json("rivaroxaban", {
        "asof": str(panel["date"].max().date()), "threshold": round(thr_now, 4), "expected_cut": round(cut_now, 4),
        "brands": [{"trademark": r.trademark, "manufacturer": r.manufacturer, "local": bool(r.local),
                    "currency": r.currency, "base_price": float(r.base_price), "base_usd": round(float(r.base_usd), 2),
                    "p_cut": round(float(r.p_cut), 4), "prepare": bool(r.prepare), "prior_cut": bool(r.prior_cut),
                    "reasons": r.reasons} for r in riv.itertuples()],
    })
    exp.pickle("model", {"champion": champion, "params": best[champion], "model": model, "explainer": explainer,
                         "feature_set": cfg.feature_set, "features": numeric + categorical, "threshold": thr_now})

    v, t = m_val[champion], m_test[champion]
    n_trials = sum(len(x) for x in search.values())
    exp.text("summary.md", "\n".join([
        f"# Эксперимент {exp.run_id}", "",
        f"Чемпион: **{champion}**, гиперпараметры: `{best[champion]}`. Попыток подбора: {n_trials}.", "",
        "| Волна | Роль | AUC модели | AUC правила | Выгода модели, млн сум | Всегда готовиться, млн сум |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| {s.validation} | подбор и выбор (оптимистично) | {v['auc']} | {m_val['rule']['auc']} | "
        f"{v['value_per_drug_uzs'] / 1e6:.1f} | {strat_val['always_prepare'] / 1e6:.1f} |",
        f"| {s.test} | честный тест | {t['auc']} | {m_test['rule']['auc']} | "
        f"{t['value_per_drug_uzs'] / 1e6:.1f} | {strat_test['always_prepare'] / 1e6:.1f} |",
    ]) + "\n")
    return exp.finish({
        "feature_set": cfg.feature_set, "train_scope": cfg.scope.train, "eval_scope": cfg.scope.eval,
        "champion": champion, "params": best[champion], "trials": n_trials,
        "val_auc_rule": m_val["rule"]["auc"],
        "val_auc": v["auc"], "test_auc": t["auc"], "test_auc_rule": m_test["rule"]["auc"],
        "test_value_mln": round(t["value_per_drug_uzs"] / 1e6, 1),
        "test_always_mln": round(strat_test["always_prepare"] / 1e6, 1),
    })


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Обучение и проверка радара пересмотра")
    ap.add_argument("--feature-set", choices=["base", "history"])
    ap.add_argument("--train-scope", choices=["all", "imports"])
    ap.add_argument("--eval-scope", choices=["all", "imports"])
    ap.add_argument("--name")
    a = ap.parse_args()
    row = run(a.name, {"feature_set": a.feature_set, "train": a.train_scope, "eval": a.eval_scope})
    print(row)
    print((experiments.EXPERIMENTS / row["run_id"] / "summary.md").read_text(encoding="utf-8"))
