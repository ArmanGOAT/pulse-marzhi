"""Проверка любого рецептурного препарата: поиск, понятная карточка и отчёт.

Берёт готовый прогноз радара на последнюю дату (scores/now.parquet основного запуска: все рецептурные
упаковки реестра) и историю цен из панели реестра. Пересчёта модели нет, поэтому всё отвечает мгновенно.
Тексты — простым языком: страницу должен понять человек без подготовки.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache

import pandas as pd

from pulse.data import registry
from pulse.ml import experiments

HIGH = 0.30  # граница «высокого» риска; нижняя граница «среднего» — порог окупаемости подготовки из радара

# Проверка на честной волне C (ноябрь 2026, 12 213 упаковок): у скольких из 100 цену реально снизили
BAND_CHECK = {"high": 26, "mid": 19, "low": 9, "all": 14}

FORM_RU = {"tablet": "таблетки", "capsule": "капсулы", "injection": "раствор для инъекций", "solution": "раствор",
           "powder": "порошок", "cream": "крем", "drops": "капли", "syrup": "сироп", "spray": "спрей",
           "suspension": "суспензия", "suppository": "свечи", "other": ""}

LEVELS = {
    "high": {"label": "Высокий риск", "tone": "loss",
             "meaning": "Цену на этот препарат, скорее всего, пересмотрят в ближайшую волну.",
             "todo": "Не завозите большую партию перед волной: везите малыми партиями, пока не станет ясна новая цена. "
                     "Обсудите с производителем компенсацию при снижении цены."},
    "mid": {"label": "Средний риск", "tone": "warn",
            "meaning": "Пересмотр возможен. Подготовка к нему в среднем окупается.",
            "todo": "Перед ближайшей волной держите на складе запас поменьше, но не меньше месяца продаж."},
    "low": {"label": "Низкий риск", "tone": "good",
            "meaning": "Пересмотр в ближайшую волну маловероятен.",
            "todo": "Специально готовиться не нужно. Проверьте препарат снова после следующего обновления реестра."},
}


def _id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _norm(s: object) -> str:
    return str(s).lower().replace("ё", "е").replace("®", "").replace("™", "").strip()


@lru_cache(maxsize=1)
def threshold() -> float:
    riv = json.loads((experiments.latest_dir() / "rivaroxaban.json").read_text(encoding="utf-8"))
    return float(riv["threshold"])


@lru_cache(maxsize=1)
def catalog() -> pd.DataFrame:
    """Все рецептурные упаковки с прогнозом на последнюю дату и описанием из реестра."""
    now = pd.read_parquet(experiments.latest_dir() / "scores" / "now.parquet")
    panel = registry.panel()
    last = panel[panel["date"] == panel["date"].max()].drop_duplicates("key")
    cols = ["key", "product", "country", "base_usd", "units", "reg_number", "date"]
    df = now.merge(last[cols], on="key", how="left")
    df["id"] = df["key"].map(_id)
    df["search"] = (df["trademark"].map(_norm) + " " + df["inn"].map(_norm) + " " + df["manufacturer"].map(_norm))
    df["peer_group"] = df["inn"] + "|" + df["form"] + "|" + df["strength"]
    med = df.groupby("peer_group")["base_usd"].transform("median")
    df["peers_n"] = df.groupby("peer_group")["key"].transform("count") - 1
    df["vs_peers"] = df["base_usd"] / med
    thr = threshold()
    df["level"] = pd.cut(df["p_cut"], [-1, thr, HIGH, 2], labels=["low", "mid", "high"], right=False).astype(str)
    return df.set_index("id", drop=False)


def _title(r) -> str:
    return str(r["trademark"]).replace("®", "").strip()


def _subtitle(r) -> str:
    form = FORM_RU.get(r["form"], "")
    parts = [r["inn"], " ".join(x for x in (form, r["strength"] if r["strength"] != "?" else "") if x),
             r["country"] if isinstance(r["country"], str) else ""]
    return " · ".join(p for p in parts if p)


def _price(r) -> str:
    v = r["base_price"]
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",") if v < 1000 else f"{v:,.0f}".replace(",", " ")
    return f"{s} {'сум' if r['currency'] == 'UZS' else r['currency']}"


def search(q: str, limit: int = 20) -> list[dict]:
    q = _norm(q)
    if len(q) < 2:
        return []
    df = catalog()
    hit = df[df["search"].str.contains(q, regex=False)]
    starts = hit["trademark"].map(_norm).str.startswith(q) | hit["inn"].map(_norm).str.startswith(q)
    hit = hit.assign(_s=~starts).sort_values(["_s", "trademark", "base_price"]).head(limit)
    return [{"id": r["id"], "title": _title(r), "subtitle": _subtitle(r), "price": _price(r),
             "p_cut": round(float(r["p_cut"]), 3), "level": r["level"], "level_label": LEVELS[r["level"]]["label"]}
            for _, r in hit.iterrows()]


def _reasons(r) -> list[str]:
    """Только факторы, направление которых проверено на волнах B и C (scores/*.parquet):
    недавнее снижение уменьшает риск (на волне C: 3 из 100 против 18), дорогие относительно аналогов
    препараты пересматривают чаще (правило «цена выше медианы аналогов» даёт AUC 0,63 > 0,5).
    «Аналогам снижали» и «импорт/местный» меняют знак от волны к волне, поэтому их не называем."""
    out = []
    if r["prior_cut"]:
        out.append("Цену на эту упаковку уже снижали. После снижения повторный пересмотр в ближайшую волну "
                   "бывает редко: на проверке — у 3 из 100 таких препаратов против 18 из 100 у остальных.")
    v = r["vs_peers"]
    if r["peers_n"] >= 1 and pd.notna(v):
        if v >= 1.15:
            out.append(f"Цена выше средней у аналогов той же дозы в {v:.1f} раза. "
                       "Более дорогие препараты пересматривают чаще.".replace(".", ",", 1))
        elif v <= 0.87:
            out.append("Цена ниже, чем у большинства аналогов той же дозы. Это снижает риск.")
        else:
            out.append("Цена примерно как у аналогов той же дозы.")
    else:
        out.append("Аналогов с тем же веществом и дозой в реестре нет — сравнить цену не с чем.")
    return out


def history(key: str) -> list[dict]:
    p = registry.panel()
    g = p[p["key"] == key].sort_values("date")
    return [{"date": d.date().isoformat(), "price": float(v), "currency": c}
            for d, v, c in zip(g["date"], g["base_price"], g["currency"], strict=True)]


def card(pid: str) -> dict | None:
    df = catalog()
    if pid not in df.index:
        return None
    r = df.loc[pid]
    hist = history(r["key"])
    peers = df[(df["peer_group"] == r["peer_group"]) & (df["id"] != pid)].sort_values("p_cut", ascending=False)
    lv = LEVELS[r["level"]]
    return {
        "id": pid, "title": _title(r), "subtitle": _subtitle(r), "product": r["product"],
        "manufacturer": r["manufacturer"], "country": r["country"], "inn": r["inn"],
        "form": FORM_RU.get(r["form"], r["form"]), "strength": r["strength"],
        "reg_number": r["reg_number"], "local": bool(r["local"]), "price": _price(r),
        "asof": str(pd.Timestamp(r["date"]).date()) if pd.notna(r["date"]) else None,
        "p_cut": round(float(r["p_cut"]), 3), "level": r["level"], "level_label": lv["label"], "tone": lv["tone"],
        "bands": {"mid_from": round(threshold(), 4), "high_from": HIGH},
        "meaning": lv["meaning"], "todo": lv["todo"],
        "band_check": {"this": BAND_CHECK[r["level"]], "all": BAND_CHECK["all"]},
        "reasons": _reasons(r),
        "peers_n": int(r["peers_n"]),
        "history": hist,
        "peers": [{"id": p["id"], "title": _title(p), "manufacturer": p["manufacturer"], "price": _price(p),
                   "p_cut": round(float(p["p_cut"]), 3), "level_label": LEVELS[p["level"]]["label"]}
                  for _, p in peers.head(8).iterrows()],
        "peers_total": int(len(peers)),
        "method": ("Оценка радара — модели, обученной на 25 ежемесячных снимках госреестра цен mpa.uz. "
                   "Проверка на ноябрьской волне 2026 года, которую модель не видела: среди препаратов с высоким "
                   f"риском цену снизили у {BAND_CHECK['high']} из 100, со средним — у {BAND_CHECK['mid']}, "
                   f"с низким — у {BAND_CHECK['low']}. В среднем за волну снижают {BAND_CHECK['all']} из 100. "
                   "Это прогноз, а не решение регулятора."),
    }
