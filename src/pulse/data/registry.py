"""Панель реестра референтных цен: одна строка на упаковку и дату снимка.

Источник — снимки API mpa.uz (pulse.data.loaders.mpa_api). Здесь мы:
- строим устойчивый ключ упаковки (номер регистрации + упаковка + производитель, без регистра и пунктуации);
- приводим валюты к ISO 4217 и считаем базовую цену в долларах по курсу ЦБ на дату снимка;
- достаём из текста страну производителя, форму выпуска и число единиц в упаковке;
- считаем события пересмотра между соседними снимками.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from pulse.config import ROOT
from pulse.data.loaders import cbu, mpa_api
from pulse.data.loaders.mpa_api import RAW, load_snapshot

CURRENCY = {"dollar": "USD", "evro": "EUR", "rubl": "RUB", "so'm": "UZS"}

COUNTRIES = [
    "Республика Корея", "Корея Южная", "Республика Беларусь", "Российская Федерация", "Соединенные Штаты Америки",
    "Объединенные Арабские Эмираты", "Великобритания", "Узбекистан", "Индия", "Пакистан", "Китай", "Турция",
    "Германия", "Австрия", "Швейцария", "Франция", "Италия", "Испания", "Польша", "Венгрия", "Словения",
    "Сербия", "Хорватия", "Болгария", "Румыния", "Чехия", "Словакия", "Латвия", "Литва", "Эстония", "Украина",
    "Беларусь", "Россия", "Казахстан", "Кыргызстан", "Таджикистан", "Молдова", "Армения", "Грузия",
    "Азербайджан", "Иран", "Египет", "Иордания", "Кипр", "Греция", "Мальта", "Ирландия", "Нидерланды",
    "Бельгия", "Дания", "Швеция", "Финляндия", "Норвегия", "Португалия", "Израиль", "Япония", "США",
    "Бангладеш", "Вьетнам", "Индонезия", "Аргентина", "Бразилия", "Канада", "Австралия", "Сингапур", "Тайвань",
]

FORMS = [
    ("tablet", r"таблет|драже"), ("capsule", r"капсул"), ("injection", r"инъекц|инфуз|ампул|шприц|флакон.*раствор"),
    ("solution", r"раствор|сироп|суспенз|капли|эмульс"), ("topical", r"мазь|крем|гель|пластыр|спрей"),
    ("powder", r"порошок|гранул|лиофилиз"), ("suppository", r"суппозит|свеч"),
]


def norm_text(x: object) -> str:
    s = re.sub(r"[^\w№]+", " ", str(x).lower())
    return re.sub(r"\s+", " ", s).strip()


def parse_country(manufacturer: str) -> str:
    m = str(manufacturer)
    for c in COUNTRIES:
        if m.endswith(c) or f" {c}" in m:
            return {"Корея Южная": "Республика Корея", "Беларусь": "Республика Беларусь",
                    "Россия": "Российская Федерация", "США": "Соединенные Штаты Америки"}.get(c, c)
    return "Другая"


def parse_form(text: str) -> str:
    t = str(text).lower()
    for name, pattern in FORMS:
        if re.search(pattern, t):
            return name
    return "other"


def parse_units(text: str) -> float:
    m = re.search(r"№\s*(\d+)", str(text))
    return float(m.group(1)) if m else np.nan


def parse_strength_mg(text: str) -> float:
    """Дозировка в миллиграммах (г → ×1000, мкг → ÷1000). Нужна для цены за 1 мг между разными дозами."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(мкг|мг|г)(?![а-я])", str(text).lower())
    if not m:
        return np.nan
    value = float(m.group(1).replace(",", "."))
    return value * {"мкг": 0.001, "мг": 1.0, "г": 1000.0}[m.group(2)]


def parse_strength(text: str) -> str:
    """Дозировка из описания упаковки: «20 мг» → «20мг». Нужна, чтобы сравнивать аналоги одной дозы."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(мкг|мг|г|ме|ед|%)(?![а-я])", str(text).lower())
    return f"{m.group(1).replace(',', '.')}{m.group(2)}" if m else "?"


def snapshot_dates() -> list[pd.Timestamp]:
    return sorted(pd.Timestamp(p.name[9:19]) for p in RAW.glob("registry_*.jsonl.gz")
                  if p.stat().st_size > 100)


def fx_table() -> pd.DataFrame:
    """Курсы ЦБ (UZS за единицу валюты) на первое число месяца, из сохранённых ответов."""
    frames = [cbu.fetch(ccy=c, offline=True) for c in ("USD", "EUR", "RUB")]
    fx = pd.concat(frames).pivot_table(index="date", columns="currency", values="rate_uzs").sort_index()
    fx["UZS"] = 1.0
    return fx


def _rate_on(fx: pd.DataFrame, when: pd.Timestamp) -> pd.Series:
    return fx.loc[:when].iloc[-1] if (fx.index <= when).any() else fx.iloc[0]


PROCESSED = ROOT / "data" / "processed"
PANEL_CACHE = PROCESSED / "registry_panel.parquet"


def _fingerprint() -> str:
    """SHA-256 содержимого снимков реестра, курсов ЦБ и кода разбора.

    Меняется, если добавили снимок, обновили курс или поправили разбор, — тогда панель пересобирается.
    Время изменения файлов не используем: Docker и git его не сохраняют.
    """
    h = hashlib.sha256()
    files = sorted(RAW.glob("registry_*.jsonl.gz")) + sorted(cbu.RAW.glob("*.json"))
    for f in [*files, Path(__file__), Path(mpa_api.__file__), Path(cbu.__file__)]:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@lru_cache(maxsize=1)
def panel() -> pd.DataFrame:
    """Все снимки в одной таблице. Сборка из сырых снимков занимает ~20 с, поэтому результат
    хранится в data/processed/registry_panel.parquet с паспортом и берётся оттуда, пока
    отпечаток исходных файлов и кода совпадает."""
    fp = _fingerprint()
    meta_path = PANEL_CACHE.with_suffix(".meta.json")
    with contextlib.suppress(OSError, ValueError, KeyError):
        if json.loads(meta_path.read_text(encoding="utf-8"))["fingerprint"] == fp:
            return pd.read_parquet(PANEL_CACHE)
    p = build_panel()
    with contextlib.suppress(OSError):  # нет прав на запись — работаем без кэша
        _save_cache(p, fp, meta_path)
    return p


def _save_cache(p: pd.DataFrame, fp: str, meta_path: Path) -> None:
    PANEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PANEL_CACHE.with_suffix(".parquet.tmp")
    p.to_parquet(tmp, index=False)
    tmp.replace(PANEL_CACHE)  # атомарно: читатель не увидит недописанный файл
    meta = {
        "table": "registry_panel", "rows": int(len(p)), "snapshots": int(p["date"].nunique()),
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "fingerprint": fp,
        "sha256": hashlib.sha256(PANEL_CACHE.read_bytes()).hexdigest(),
        "sources": ["mpa.uz, реестр референтных цен (data/raw/mpa_api)", "cbu.uz, курсы ЦБ РУз (data/raw/cbu)"],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def build_panel() -> pd.DataFrame:
    """Собирает панель из сырых снимков: нормализует поля и пересчитывает цены в доллары."""
    fx = fx_table()
    frames = []
    for day in snapshot_dates():
        s = load_snapshot(day)
        if s.empty:
            continue
        rate = _rate_on(fx, day)
        out = pd.DataFrame({
            "date": day,
            "key": (s.registrationNumber.map(norm_text) + "|" + s.medicinalProductIncludingPackaging.map(norm_text)
                    + "|" + s.manufacturer.map(norm_text)),
            "trademark": s.trademark.astype(str).str.strip(),
            "inn": s.innName.astype(str).str.strip().str.lower(),
            "manufacturer": s.manufacturer.astype(str),
            "product": s.medicinalProductIncludingPackaging.astype(str),
            "reg_number": s.registrationNumber.astype(str),
            "currency": s.currency.map(CURRENCY).fillna("OTHER"),
            "base_price": pd.to_numeric(s.basePrice, errors="coerce"),
            "limit_price_uzs": pd.to_numeric(s.price, errors="coerce"),
            "local": s.local.astype(bool),
            # в старых снимках поле пустое: реестр с 05.2025 ведётся только для рецептурных ЛС
            "rx": ~s.prescription.fillna("").astype(str).str.contains("Без", case=False),
            "uploaded_at": pd.to_datetime(s.uploadedAt, errors="coerce"),
        })
        out = out[out["currency"] != "OTHER"].dropna(subset=["base_price"])
        out["base_usd"] = out["base_price"] * out["currency"].map(rate) / rate["USD"]
        frames.append(out.drop_duplicates(["key"]))
    p = pd.concat(frames, ignore_index=True)
    p["country"] = p["manufacturer"].map(parse_country)
    p["form"] = p["product"].map(parse_form)
    p["units"] = p["product"].map(parse_units)
    p["strength"] = p["product"].map(parse_strength)
    p["strength_mg"] = p["product"].map(parse_strength_mg)
    p["usd_per_unit"] = p["base_usd"] / p["units"]
    return p


def price_in_currency(base_price: float, from_ccy: str, to_ccy: str, rate: pd.Series) -> float:
    return base_price * rate[from_ccy] / rate[to_ccy]


def events(p: pd.DataFrame | None = None) -> pd.DataFrame:
    """Изменения цены между соседними снимками: ratio = новая / старая в валюте старой цены."""
    p = panel() if p is None else p
    fx = fx_table()
    dates = sorted(p["date"].unique())
    rows = []
    for prev, nxt in zip(dates[:-1], dates[1:], strict=True):
        a = p[p.date == prev].set_index("key")
        b = p[p.date == nxt].set_index("key")
        common = a.index.intersection(b.index)
        rate = _rate_on(fx, pd.Timestamp(nxt))
        old, new = a.loc[common], b.loc[common]
        new_in_old_ccy = new["base_price"] * new["currency"].map(rate).values / old["currency"].map(rate).values
        ratio = new_in_old_ccy / old["base_price"]
        rows.append(pd.DataFrame({"key": common, "date_from": prev, "date_to": nxt, "ratio": ratio.values,
                                  "currency_changed": (old["currency"].values != new["currency"].values)}))
    ev = pd.concat(rows, ignore_index=True)
    ev["cut"] = 1 - ev["ratio"]
    return ev

