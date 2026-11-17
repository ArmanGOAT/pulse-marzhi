"""Официальные списки референтных цен Агентства по развитию медицины и фармацевтики (mpa.uz).

Файлы опубликованы на https://mpa.uz/ru/reference-prices. Каждый скачанный файл сохраняется
в data/raw/mpa без изменений; рядом пишется manifest.json с адресом, заголовком, временем
скачивания и SHA-256 (ALCOA+: Attributable, Original, Contemporaneous).
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx

from pulse.config import ROOT

RAW = ROOT / "data" / "raw" / "mpa"
PAGE = "https://mpa.uz/ru/reference-prices"
FILE_URL = "https://api.mpa.uz/file-api/v2/files/{id}"

# (дата, вид, id файла, заголовок на сайте)
FILES: list[tuple[str, str, str, str]] = [
    ("2026-03-01", "full_list", "f6c2123f-b181-4fa2-803a-d00e2d8e0221",
     "Начиная с 1 марта 2026 года список референтных цен на рецептурные лекарственные средства"),
    ("2026-04-01", "revised", "f2cb76fb-317f-4677-995e-c0c4d6caf041",
     "Список рецептурных ЛС, референтная цена которых была пересмотрена по состоянию на 1 апреля 2026 года"),
    ("2026-04-11", "revised", "f54aec1d-15c8-45fb-bfbd-5a5538852e4a",
     "Список рецептурных ЛС, референтная цена которых была пересмотрена по состоянию на 11 апреля 2026 года"),
    ("2026-05-01", "revised", "52b36cd8-d70a-4f3c-9827-a45543322498",
     "С 1 мая 2026 года для производителей и поставщиков были пересмотрены референтные цены на рецептурные ЛС"),
    ("2026-09-01", "revised", "85d71378-567b-45f7-93e0-926d431070b1",
     "Список рецептурных ЛС, референтная цена которых была пересмотрена по состоянию на 1 сентября 2026 года"),
    ("2026-09-16", "revised", "22d3c51e-221e-4f44-9160-f44d475c7774",
     "Список рецептурных ЛС, референтная цена которых была пересмотрена по состоянию на 16 сентября 2026 года"),
    ("2026-09-24", "revised", "5d111fd6-e016-4070-a265-15b3e128c4a8",
     "Список рецептурных ЛС, референтная цена которых была пересмотрена по состоянию на 24 сентября 2026 года"),
    ("2026-10-01", "revised", "37d0fe6d-610f-4935-9b88-842ffb5bee29",
     "Список рецептурных ЛС, референтная цена которых была пересмотрена по состоянию на 1 октября 2026 года"),
]


def local_name(day: str, kind: str) -> str:
    return f"{day}_{kind}.xlsx"


def download(force: bool = False) -> list[dict]:
    """Скачивает файлы, которых ещё нет, и обновляет manifest.json. Возвращает записи манифеста."""
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": "pulse-marzhi/0.1 (research)"}) as client:
        for day, kind, file_id, title in FILES:
            name = local_name(day, kind)
            path = RAW / name
            if path.exists() and not force and name in manifest:
                continue
            resp = client.get(FILE_URL.format(id=file_id))
            resp.raise_for_status()
            path.write_bytes(resp.content)
            manifest[name] = {
                "date": day, "kind": kind, "title": title, "page": PAGE,
                "url": FILE_URL.format(id=file_id),
                "content_type": resp.headers.get("content-type"),
                "bytes": len(resp.content),
                "sha256": hashlib.sha256(resp.content).hexdigest(),
                "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return [manifest[local_name(d, k)] | {"file": local_name(d, k)} for d, k, _, _ in FILES]


if __name__ == "__main__":
    for row in download():
        print(f"{row['file']:28s} {row['bytes']:>9,} байт  {row['content_type']}")
