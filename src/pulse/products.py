"""Проверка препаратов: один препарат или список. Поиск, понятная карточка, отчёт.

Берёт готовый прогноз радара на последнюю дату (scores/now.parquet основного запуска: все рецептурные
упаковки реестра), историю цен из панели реестра и реальные снижения из проверочных волн.
Пересчёта модели нет, поэтому всё отвечает мгновенно. Тексты — простым языком.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from functools import lru_cache

import pandas as pd

from pulse.data import registry
from pulse.ml import experiments

HIGH = 0.30  # граница «высокого» риска; нижняя граница «среднего» — порог окупаемости подготовки из радара
MAX_ITEMS = 200  # строк в одном списке
MAX_MATCHES = 10  # упаковок на одну строку списка

# Проверка на честной волне C (ноябрь 2026, 12 213 упаковок): у скольких из 100 цену реально снизили
BAND_CHECK = {"high": 26, "mid": 19, "low": 9, "all": 14}

FORM_RU = {"tablet": "таблетки", "capsule": "капсулы", "injection": "раствор для инъекций", "solution": "раствор",
           "powder": "порошок", "topical": "для наружного применения", "suppository": "свечи", "other": ""}

LEVELS = {
    "high": {"label": "Высокий риск", "tone": "loss", "short": "Готовиться: малые партии, защита цены",
             "meaning": "Цену на этот препарат, скорее всего, пересмотрят в ближайшую волну.",
             "todo": "Не завозите большую партию перед волной: везите малыми партиями, пока не станет ясна новая цена. "
                     "Обсудите с производителем компенсацию при снижении цены.",
             "steps": ["Ближайшие 2–3 месяца везите партии не больше чем на месяц продаж.",
                       "Держите страховой запас на месяц продаж, чтобы пациенты не остались без лекарства.",
                       "Попросите у производителя пункт о защите цены: компенсацию части потерь склада при снижении.",
                       "Не берите скидки, которые снижают таможенную цену, без расчёта: при пересмотре они могут уйти в новый потолок."]},
    "mid": {"label": "Средний риск", "tone": "warn", "short": "Держать запас поменьше",
            "meaning": "Пересмотр возможен. Подготовка к нему в среднем окупается.",
            "todo": "Перед ближайшей волной держите на складе запас поменьше, но не меньше месяца продаж.",
            "steps": ["Перед ожидаемой волной не завозите партию больше чем на 2 месяца продаж.",
                      "Держите страховой запас на месяц продаж.",
                      "Проверьте препарат снова после следующего обновления реестра."]},
    "low": {"label": "Низкий риск", "tone": "good", "short": "Готовиться не нужно",
            "meaning": "Пересмотр в ближайшую волну маловероятен.",
            "todo": "Специально готовиться не нужно. Проверьте препарат снова после следующего обновления реестра.",
            "steps": ["Работайте в обычном режиме поставок.",
                      "Проверяйте препарат после каждого ежемесячного обновления реестра."]},
}

NOT_FOUND = {
    "absent": "Такого препарата нет в реестре референтных цен. Скорее всего, он безрецептурный — на безрецептурные "
              "лекарства нет государственного потолка цены, поэтому пересмотр им не грозит. Или название написано "
              "иначе: попробуйте торговое название без дозировки или действующее вещество.",
    "otc": "В реестре этот препарат значится как безрецептурный. На безрецептурные лекарства нет государственного "
           "потолка цены, поэтому пересмотр им не грозит.",
    "gone": "Препарат был в реестре до {last}, но в текущем реестре его нет: регистрация могла закончиться "
            "или препарат сняли с учёта. Прогноз для него не строится.",
}


def _id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _norm(s: object) -> str:
    s = str(s).lower().replace("ё", "е").replace("®", "").replace("™", "")
    return re.sub(r"\s+", " ", s).strip()


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
    df["n_trademark"] = df["trademark"].map(_norm)
    df["n_inn"] = df["inn"].map(_norm)
    df["search"] = df["n_trademark"] + " " + df["n_inn"] + " " + df["manufacturer"].map(_norm)
    df["peer_group"] = df["inn"] + "|" + df["form"] + "|" + df["strength"]
    med = df.groupby("peer_group")["base_usd"].transform("median")
    df["peers_n"] = df.groupby("peer_group")["key"].transform("count") - 1
    df["vs_peers"] = df["base_usd"] / med
    df["cheaper_peers"] = df.groupby("peer_group")["base_usd"].rank(method="min") - 1  # сколько аналогов дешевле
    thr = threshold()
    df["level"] = pd.cut(df["p_cut"], [-1, thr, HIGH, 2], labels=["low", "mid", "high"], right=False).astype(str)
    return df.set_index("id", drop=False)


@lru_cache(maxsize=1)
def names_index() -> pd.DataFrame:
    """Все упаковки, когда-либо бывшие в реестре: для объяснения, почему препарат не найден."""
    p = registry.panel()
    g = p.groupby("key").agg(trademark=("trademark", "last"), inn=("inn", "last"), rx=("rx", "last"),
                             last=("date", "max"))
    g["search"] = g["trademark"].map(_norm) + " " + g["inn"].map(_norm)
    return g


@lru_cache(maxsize=1)
def cut_stats() -> dict:
    """Размеры реальных снижений цены (≥10%) на проверочных волнах B и C."""
    d = experiments.latest_dir() / "scores"
    s = pd.concat([pd.read_parquet(d / f"{w}.parquet") for w in ("validation", "test")])
    c = s[(s["y"] == 1) & (s["cut_size"] > 0)]
    by_inn = c.groupby("inn")["cut_size"].agg(["median", "size"])
    return {"local": float(c[c["local"]]["cut_size"].median()), "import": float(c[~c["local"]]["cut_size"].median()),
            "n": int(len(c)), "by_inn": {k: (float(v["median"]), int(v["size"])) for k, v in by_inn.iterrows() if v["size"] >= 10}}


def typical_cut(inn: str, local: bool) -> dict:
    st = cut_stats()
    if inn in st["by_inn"]:
        med, n = st["by_inn"][inn]
        return {"value": med, "basis": f"медиана {n} реальных снижений по этому веществу"}
    return {"value": st["local" if local else "import"],
            "basis": f"медиана реальных снижений {'местных' if local else 'импортных'} препаратов ({st['n']} снижений всего)"}


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


def _row(r) -> dict:
    return {"id": r["id"], "title": _title(r), "subtitle": _subtitle(r), "manufacturer": r["manufacturer"],
            "strength": r["strength"], "price": _price(r), "p_cut": round(float(r["p_cut"]), 3), "level": r["level"],
            "level_label": LEVELS[r["level"]]["label"], "short": LEVELS[r["level"]]["short"]}


def _find(q: str, dose: str | None = None) -> pd.DataFrame:
    """Совпадения в каталоге: точное торговое название → начало названия или вещества → вхождение."""
    df = catalog()
    hit = df[df["search"].str.contains(q, regex=False)]
    if dose:
        num = dose.replace(",", ".")
        hit = hit[hit["strength"].str.match(rf"^{re.escape(num)}(мг|мкг|г|%|ме)?$", na=False)] if len(hit) else hit
    exact = hit[(hit["n_trademark"] == q) | (hit["n_inn"] == q)]
    if len(exact):
        hit = exact
    starts = hit["n_trademark"].str.startswith(q) | hit["n_inn"].str.startswith(q)
    return hit.assign(_s=~starts).sort_values(["_s", "trademark", "base_price"])


def explain_missing(q: str) -> dict:
    idx = names_index()
    m = idx[idx["search"].str.contains(q, regex=False)]
    if len(m) and (~m["rx"]).any():
        return {"kind": "otc", "message": NOT_FOUND["otc"]}
    if len(m):
        last = pd.Timestamp(m["last"].max()).strftime("%d.%m.%Y")
        return {"kind": "gone", "message": NOT_FOUND["gone"].format(last=last)}
    return {"kind": "absent", "message": NOT_FOUND["absent"]}


def search(q: str, limit: int = 20) -> dict:
    q = _norm(q)
    if len(q) < 2:
        return {"results": [], "total": 0, "not_found": None}
    hit = _find(q)
    rows = [_row(r) for _, r in hit.head(limit).iterrows()]
    return {"results": rows, "total": int(len(hit)), "not_found": None if rows else explain_missing(q)}


_DOSE = re.compile(r"\s(\d+(?:[.,]\d+)?)\s*(мг|mg|мкг|г|%)?\s*$")


def parse_line(line: str) -> tuple[str, str | None]:
    """«Сербан 20 мг» → ("сербан", "20"). Дозировка в конце строки необязательна."""
    s = _norm(line).strip(" .,;\t")
    m = _DOSE.search(" " + s)
    if m and len(s[: m.start()].strip()) >= 2:
        return s[: m.start()].strip(" ,"), m.group(1)
    return s, None


def match_line(line: str) -> dict:
    name, dose = parse_line(line)
    out = {"query": line.strip(), "name": name, "dose": dose}
    if len(name) < 2:
        return out | {"status": "skip", "matches": [], "more": 0, "message": "Слишком короткая строка."}
    hit = _find(name, dose)
    if not len(hit) and dose:  # с такой дозировкой нет — покажем все дозировки
        hit = _find(name)
        if len(hit):
            out["message"] = f"Дозировки {dose} в реестре нет, показаны все дозировки."
    if not len(hit):
        miss = explain_missing(name)
        return out | {"status": miss["kind"], "matches": [], "more": 0, "message": miss["message"]}
    rows = [_row(r) for _, r in hit.head(MAX_MATCHES).iterrows()]
    return out | {"status": "found", "matches": rows, "more": int(max(len(hit) - MAX_MATCHES, 0))}


def split_items(text: str) -> list[str]:
    items = [x.strip() for x in re.split(r"[\n;]+", text) if x.strip()]
    return items[:MAX_ITEMS]


def _join_row(row) -> str:
    """Название в первом столбце; если во втором дозировка («20», «20 мг») — добавляем её."""
    name = str(row[0]).strip()
    if len(row) > 1 and row[1] is not None and re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*(мг|mg|мкг|г|%)?\s*", str(row[1])):
        return f"{name} {str(row[1]).strip()}"
    return name


def parse_file(name: str, data: bytes) -> list[str]:
    """Список названий из .txt, .csv или .xlsx: берём первый столбец, пропускаем заголовок."""
    low = name.lower()
    if low.endswith(".xlsx"):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        cells = [_join_row(r) for r in ws.iter_rows(values_only=True) if r and r[0] is not None]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        if low.endswith(".csv"):
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t") if text.strip() else csv.excel
            cells = [_join_row(r) for r in csv.reader(io.StringIO(text), dialect) if r and r[0].strip()]
        else:
            return split_items(text)
    if cells and _norm(cells[0]) in {"препарат", "название", "наименование", "лекарство", "name", "drug", "product"}:
        cells = cells[1:]
    return [c for c in cells if c][:MAX_ITEMS]


MISSING = ("absent", "otc", "gone")


def batch(items: list[str]) -> dict:
    rows, seen = [], set()
    for x in items[:MAX_ITEMS]:
        r = match_line(x)
        uniq = [m for m in r["matches"] if m["id"] not in seen]
        if r["matches"] and not uniq:
            r |= {"status": "duplicate", "message": "Эти упаковки уже есть в списке выше."}
        seen.update(m["id"] for m in uniq)
        rows.append(r | {"matches": uniq})
    found = [m for r in rows for m in r["matches"]]
    count = {lv: sum(m["level"] == lv for m in found) for lv in ("high", "mid", "low")}
    return {"rows": rows, "summary": {"lines": len(rows), "packages": len(found), **count,
                                      "not_found": sum(r["status"] in MISSING for r in rows)},
            "asof": catalog()["date"].max().date().isoformat(), "band_check": BAND_CHECK}


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
    same_inn = df[df["inn"] == r["inn"]]
    lv = LEVELS[r["level"]]
    prices = [h["price"] for h in hist]
    changes = sum(1 for a, b in zip(prices, prices[1:], strict=False) if a != b)
    tc = typical_cut(r["inn"], bool(r["local"]))
    return {
        "id": pid, "title": _title(r), "subtitle": _subtitle(r), "product": r["product"],
        "manufacturer": r["manufacturer"], "country": r["country"], "inn": r["inn"],
        "form": FORM_RU.get(r["form"], r["form"]), "strength": r["strength"],
        "reg_number": r["reg_number"], "local": bool(r["local"]), "price": _price(r),
        "asof": str(pd.Timestamp(r["date"]).date()) if pd.notna(r["date"]) else None,
        "p_cut": round(float(r["p_cut"]), 3), "level": r["level"], "level_label": lv["label"], "tone": lv["tone"],
        "bands": {"mid_from": round(threshold(), 4), "high_from": HIGH},
        "meaning": lv["meaning"], "todo": lv["todo"], "steps": lv["steps"],
        "band_check": {"this": BAND_CHECK[r["level"]], "all": BAND_CHECK["all"]},
        "reasons": _reasons(r),
        "peers_n": int(r["peers_n"]),
        "cheaper_peers": int(r["cheaper_peers"]) if pd.notna(r["cheaper_peers"]) else None,
        "typical_cut": {"value": round(tc["value"], 3), "basis": tc["basis"]},
        "price_story": {"first": prices[0] if prices else None, "max": max(prices) if prices else None,
                        "now": prices[-1] if prices else None, "changes": changes, "snapshots": len(prices),
                        "from_max": round(prices[-1] / max(prices) - 1, 3) if prices else None,
                        "currency": hist[-1]["currency"] if hist else None},
        "inn_market": {"packages": int(len(same_inn)), "brands": int(same_inn["trademark"].nunique()),
                       "prior_cut_share": round(float(same_inn["prior_cut"].mean()), 3)},
        "history": hist,
        "peers": [{"id": p["id"], "title": _title(p), "manufacturer": p["manufacturer"], "price": _price(p),
                   "p_cut": round(float(p["p_cut"]), 3), "level_label": LEVELS[p["level"]]["label"]}
                  for _, p in peers.head(10).iterrows()],
        "peers_total": int(len(peers)),
        "method": ("Оценка радара — модели, обученной на 25 ежемесячных снимках госреестра цен mpa.uz. "
                   "Проверка на ноябрьской волне 2026 года, которую модель не видела: среди препаратов с высоким "
                   f"риском цену снизили у {BAND_CHECK['high']} из 100, со средним — у {BAND_CHECK['mid']}, "
                   f"с низким — у {BAND_CHECK['low']}. В среднем за волну снижают {BAND_CHECK['all']} из 100. "
                   "Это прогноз, а не решение регулятора."),
    }
