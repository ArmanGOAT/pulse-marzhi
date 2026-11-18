"""Снимки реестра референтных цен на дату через публичный API портала mpa.uz.

Тот же API использует страница https://mpa.uz/ru/reference-prices. Параметр date (ДД.ММ.ГГГГ)
отдаёт реестр, действовавший на эту дату, поэтому по снимкам видна история цен каждой упаковки.

Сырые ответы сохраняются без изменений в data/raw/mpa_api/registry_ГГГГ-ММ-ДД.jsonl.gz
(gzip без потерь), рядом manifest.json с адресом, временем и SHA-256.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from datetime import UTC, date, datetime

import httpx
import pandas as pd

from pulse.config import ROOT

RAW = ROOT / "data" / "raw" / "mpa_api"
API = "https://api.mpa.uz/drug-catalog-api/v1/referent-price/all"
PAGE_SIZE = 5000
PAUSE_S = 0.5


def month_starts(start: date, end: date) -> list[date]:
    days, d = [], date(start.year, start.month, 1)
    while d <= end:
        days.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return days


def snapshot_path(day: date):
    return RAW / f"registry_{day.isoformat()}.jsonl.gz"


def fetch_snapshot(day: date, client: httpx.Client) -> dict:
    """Скачивает весь реестр на дату `day`, если его ещё нет. Возвращает запись манифеста."""
    path = snapshot_path(day)
    rows: list[dict] = []
    page, total_pages = 0, 1
    while page < total_pages:
        params = {"date": day.strftime("%d.%m.%Y"), "page": page, "size": PAGE_SIZE}
        for attempt in range(3):
            try:
                resp = client.get(API, params=params)
                resp.raise_for_status()
                result = resp.json()["result"]
                break
            except (httpx.HTTPError, KeyError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2 + 2 * attempt)
        rows.extend(result["content"])
        total_pages = result["totalPages"]
        page += 1
        time.sleep(PAUSE_S)
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")
    path.write_bytes(gzip.compress(payload, mtime=0))
    return {
        "date": day.isoformat(), "rows": len(rows), "url": API, "params": {"date": day.strftime("%d.%m.%Y")},
        "sha256_uncompressed": hashlib.sha256(payload).hexdigest(),
        "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def download(start: date = date(2024, 7, 1), end: date | None = None, extra: list[date] | None = None) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    days = sorted(set(month_starts(start, end or date.today()) + (extra or [])))
    with httpx.Client(timeout=120, headers={"User-Agent": "pulse-marzhi/0.1 (research)",
                                             "Accept": "application/json"}) as client:
        for day in days:
            key = day.isoformat()
            if key in manifest and snapshot_path(day).exists():
                continue
            manifest[key] = fetch_snapshot(day, client)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{key}: {manifest[key]['rows']} строк", flush=True)
    return manifest


def load_snapshot(day: date | str) -> pd.DataFrame:
    d = pd.Timestamp(day).date()
    with gzip.open(snapshot_path(d), "rt", encoding="utf-8") as f:
        df = pd.DataFrame([json.loads(line) for line in f if line.strip()])
    df["snapshot_date"] = pd.Timestamp(d)
    return df


# Последний снимок, на котором посчитаны все цифры в README и документах. По умолчанию make data
# скачивает ровно этот набор, чтобы результат воспроизводился в любой день; свежие снимки — флаг --until today.
SNAPSHOT_END = date(2026, 11, 17)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Снимки реестра mpa.uz на 1-е число каждого месяца")
    parser.add_argument("--until", default=SNAPSHOT_END.isoformat(),
                        help="последняя дата снимка ГГГГ-ММ-ДД или today (по умолчанию %(default)s)")
    args = parser.parse_args()
    end = date.today() if args.until == "today" else date.fromisoformat(args.until)
    download(end=end, extra=[end] if end.day != 1 else None)


if __name__ == "__main__":
    main()
