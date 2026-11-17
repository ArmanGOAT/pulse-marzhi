"""Качество радара по сегментам и разбор ривароксабана — для презентации и карточки модели.

У каждого сегмента: размер выборки, число снижений, AUC модели и правила, 95% бутстрап-интервал AUC модели,
выгода на препарат. Слабые сегменты показываем так же, как сильные (Model Cards: disaggregated evaluation).
Сегменты с малой выборкой помечаются «мало данных» и без AUC.

Запуск: python -m pulse.ml.segments [run_id]   (по умолчанию последний запуск)
Пишет в папку эксперимента: segments.json, segments.md, rivaroxaban_case.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from sklearn.metrics import roc_auc_score

from pulse.config import CONFIG_DIR
from pulse.ml import experiments
from pulse.ml.radar import Economics, business_value


def load_segments_cfg() -> dict:
    with (CONFIG_DIR / "segments.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def is_cardio(inn: pd.Series, keywords: list[str]) -> pd.Series:
    pattern = "|".join(keywords)
    return inn.str.lower().str.contains(pattern, regex=True, na=False)


def bootstrap_auc(y: np.ndarray, p: np.ndarray, resamples: int, seed: int) -> tuple[float, float]:
    """95% интервал AUC бутстрапом по упаковкам."""
    rng = np.random.default_rng(seed)
    n, stats = len(y), []
    for _ in range(resamples):
        idx = rng.integers(0, n, n)
        if y[idx].min() == y[idx].max():
            continue
        stats.append(roc_auc_score(y[idx], p[idx]))
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return round(float(lo), 3), round(float(hi), 3)


def segment_masks(df: pd.DataFrame, cfg: dict) -> dict[str, pd.Series]:
    cardio = is_cardio(df["inn"], cfg["cardio"]["inn_keywords"])
    return {
        "Все рецептурные": pd.Series(True, index=df.index),
        "Импортные": ~df["local"],
        "Местные": df["local"],
        "Таблетки и капсулы": df["form"].isin(["tablet", "capsule"]),
        "Инъекции": df["form"] == "injection",
        "Кардио (ATC B01 и C)": cardio,
        "Кардио, импортные": cardio & ~df["local"],
        "Аналогам уже снижали цену": df["has_cut_peers"].astype(bool),
        "Аналогам ещё не снижали": ~df["has_cut_peers"].astype(bool),
    }


def segment_table(scores: pd.DataFrame, eco: Economics, threshold: float, cfg: dict,
                  model_col: str = "p_woe_logit") -> list[dict]:
    b = cfg["bootstrap"]
    rows = []
    for name, mask in segment_masks(scores, cfg).items():
        g = scores[mask]
        y, p = g["y"].to_numpy(), g[model_col].to_numpy()
        row = {"segment": name, "n": int(len(g)), "positives": int(y.sum()),
               "cut_share": round(float(y.mean()), 3) if len(g) else None}
        enough = len(g) >= b["min_n"] and y.sum() >= b["min_positives"] and (len(g) - y.sum()) >= b["min_positives"]
        if enough:
            row |= {
                "auc_model": round(float(roc_auc_score(y, p)), 3),
                "auc_ci95": bootstrap_auc(y, p, b["resamples"], b["seed"]),
                "auc_rule": round(float(roc_auc_score(y, g["p_rule"].to_numpy())), 3),
                "value_model_mln": round(business_value(p >= threshold, y, g["cut_size"].to_numpy(), eco) / 1e6, 1),
                "value_always_mln": round(business_value(np.ones(len(y), bool), y, g["cut_size"].to_numpy(), eco) / 1e6, 1),
            }
        else:
            row["note"] = "мало данных"
        rows.append(row)
    return rows


def rivaroxaban_case(scores: pd.DataFrame, model_col: str = "p_woe_logit") -> list[dict]:
    """Прогноз модели на дату волны против того, что случилось: ривароксабан 20 мг."""
    r = scores[scores["inn"].str.contains("ривароксабан", na=False) & (scores["strength"] == "20мг")]
    r = r.sort_values(model_col, ascending=False)
    return [{"trademark": x.trademark, "local": bool(x.local), "currency": x.currency,
             "base_price": float(x.base_price), "p_cut": round(float(getattr(x, model_col)), 3),
             "rank_in_wave_pct": round(float((scores[model_col] > getattr(x, model_col)).mean()), 3),
             "actual_cut": bool(x.y), "cut_size": round(float(x.cut_size), 3)} for x in r.itertuples()]


def _md(rows: list[dict], wave: str) -> list[str]:
    lines = [f"### {wave}", "", "| Сегмент | Упаковок | Снижено | AUC модели [95% ДИ] | AUC правила | Выгода модели / «всегда», млн сум |",
             "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        if "auc_model" in r:
            lo, hi = r["auc_ci95"]
            lines.append(f"| {r['segment']} | {r['n']} | {r['cut_share']:.0%} | {r['auc_model']} [{lo}–{hi}] | "
                         f"{r['auc_rule']} | {r['value_model_mln']} / {r['value_always_mln']} |")
        else:
            lines.append(f"| {r['segment']} | {r['n']} | {r['positives']} | мало данных | — | — |")
    return lines + [""]


def run(run_id: str | None = None) -> Path:
    d = experiments.EXPERIMENTS / run_id if run_id else experiments.latest_dir()
    cfg = load_segments_cfg()
    metrics = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
    eco = Economics(**metrics["economics"])
    out, md, case = {}, [f"# Качество радара по сегментам: {d.name}", "",
                         f"Модель: {metrics['champion']}. Кардио — ручное сопоставление МНН с ATC B01 и C (оценка).", ""], {}
    for part in ("validation", "test"):
        scores = pd.read_parquet(d / "scores" / f"{part}.parquet")
        info = metrics[part]
        rows = segment_table(scores, eco, info["threshold"], cfg, f"p_{metrics['champion']}")
        role = "подбор, оптимистично" if part == "validation" else "честный тест"
        out[info["wave"]] = {"role": role, "segments": rows}
        md += _md(rows, f"{info['wave']} ({role})")
        case[info["wave"]] = {"trained_on": info["train_waves"], "brands": rivaroxaban_case(scores, f"p_{metrics['champion']}")}
    (d / "segments.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "segments.md").write_text("\n".join(md), encoding="utf-8")
    (d / "rivaroxaban_case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


if __name__ == "__main__":
    folder = run(sys.argv[1] if len(sys.argv) > 1 else None)
    print((folder / "segments.md").read_text(encoding="utf-8"))
    case = json.loads((folder / "rivaroxaban_case.json").read_text(encoding="utf-8"))
    for wave, c in case.items():
        print(f"\n{wave}, модель училась на {c['trained_on']}:")
        for b in c["brands"]:
            print(f"  {b['trademark']:14s} p={b['p_cut']:.0%} (выше чем у {1 - b['rank_in_wave_pct']:.0%} упаковок)  "
                  f"факт: {'СНИЗИЛИ на ' + format(b['cut_size'], '.0%') if b['actual_cut'] else 'не снизили'}")
