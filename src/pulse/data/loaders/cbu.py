"""Официальный курс ЦБ Республики Узбекистан (публичный JSON API cbu.uz).

Берём курс на первое число каждого месяца. Каждый ответ сохраняем в data/raw/cbu как есть;
повторный запуск читает сохранённые файлы и не ходит в сеть.
"""
from __future__ import annotations

import json
import time
from datetime import date

import httpx
import pandas as pd

from pulse.config import ROOT

RAW = ROOT / "data" / "raw" / "cbu"
URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/{ccy}/{day}/"
SOURCE = "cbu.uz, официальный курс ЦБ РУз"


def _month_starts(start: date, end: date) -> list[date]:
    days, d = [], date(start.year, start.month, 1)
    while d <= end:
        days.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return days


def fetch(start: date = date(2023, 1, 1), end: date | None = None, ccy: str = "USD",
          offline: bool = False) -> pd.DataFrame:
    end = end or date.today()
    RAW.mkdir(parents=True, exist_ok=True)
    rows = []
    with httpx.Client(timeout=15, headers={"User-Agent": "pulse-marzhi/0.1 (research)"}) as client:
        for day in _month_starts(start, end):
            raw_path = RAW / f"{ccy}_{day.isoformat()}.json"
            if raw_path.exists():
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
            elif offline:
                continue
            else:
                for attempt in range(3):
                    try:
                        resp = client.get(URL.format(ccy=ccy, day=day.isoformat()))
                        resp.raise_for_status()
                        payload = resp.json()
                        break
                    except (httpx.HTTPError, ValueError):
                        if attempt == 2:
                            raise
                        time.sleep(1 + attempt)
                raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                time.sleep(0.2)
            for item in payload:
                rows.append({
                    "date": pd.to_datetime(item["Date"], format="%d.%m.%Y"),
                    "currency": item["Ccy"],
                    "rate_uzs": float(item["Rate"]) / float(item.get("Nominal", 1) or 1),
                    "status": "fact",
                    "source": SOURCE,
                })
    return pd.DataFrame(rows).drop_duplicates(["date", "currency"]).sort_values("date").reset_index(drop=True)


def annual_stats(fx: pd.DataFrame) -> dict[str, float]:
    """Годовой дрейф и разброс курса по месячным данным: для калибровки Монте-Карло."""
    s = fx.set_index("date")["rate_uzs"].sort_index()
    yoy = s.pct_change(12).dropna()
    return {
        "months": int(len(s)),
        "first": float(s.iloc[0]),
        "last": float(s.iloc[-1]),
        "mean_yoy": float(yoy.mean()),
        "sd_yoy": float(yoy.std()),
    }
