"""Хранилище таблиц в Parquet с паспортом (lineage) и выборкой «на дату».

Паспорт рядом с каждой таблицей хранит: число строк, источники, статусы (факт/оценка/демо),
время сохранения и SHA-256 содержимого — так любую цифру можно проследить (ALCOA+).
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pulse.config import ROOT
from pulse.data.contracts import ALL, validate

PROCESSED = ROOT / "data" / "processed"


def _paths(name: str, base: Path) -> tuple[Path, Path]:
    return base / f"{name}.parquet", base / f"{name}.meta.json"


def save(df: pd.DataFrame, name: str, base: Path = PROCESSED) -> dict:
    """Проверяет таблицу по контракту, сохраняет её и паспорт. Возвращает паспорт."""
    checked = validate(df, ALL[name])
    base.mkdir(parents=True, exist_ok=True)
    data_path, meta_path = _paths(name, base)
    checked.to_parquet(data_path, index=False)
    digest = hashlib.sha256(data_path.read_bytes()).hexdigest()
    meta = {
        "table": name,
        "rows": int(len(checked)),
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": digest,
        "status_counts": checked["status"].astype(str).value_counts().to_dict(),
        "sources": sorted(checked["source"].astype(str).unique().tolist())[:50],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def load(name: str, base: Path = PROCESSED) -> pd.DataFrame:
    data_path, _ = _paths(name, base)
    if not data_path.exists():
        raise FileNotFoundError(f"Таблица {name} не собрана: запустите сборку данных")
    return pd.read_parquet(data_path)


def meta(name: str, base: Path = PROCESSED) -> dict:
    _, meta_path = _paths(name, base)
    return json.loads(meta_path.read_text(encoding="utf-8"))


def as_of(prices: pd.DataFrame, at: str | pd.Timestamp, strictly_before: bool = False) -> pd.DataFrame:
    """Цены, известные на дату `at`: действующие на эту дату, по одной на (препарат, тип, страна).

    strictly_before=True берёт только записи, начавшие действовать раньше `at`. Так строятся
    признаки для пересмотра в дату `at`: решение агентства в этот день ещё неизвестно.
    """
    t = pd.Timestamp(at).normalize()
    if strictly_before:
        # состояние в последний момент перед `at`: запись, закончившаяся ровно в `at`, ещё действовала
        started = prices["valid_from"] < t
        alive = prices["valid_to"].isna() | (prices["valid_to"] >= t)
    else:
        started = prices["valid_from"] <= t
        alive = prices["valid_to"].isna() | (prices["valid_to"] > t)
    current = prices[started & alive]
    return (current.sort_values("valid_from")
            .groupby(["drug_id", "price_type", "country"], as_index=False, observed=True)
            .tail(1)
            .reset_index(drop=True))
