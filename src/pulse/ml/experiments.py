"""Журнал экспериментов на файлах (вместо MLflow): каждый запуск — своя папка, общий индекс — index.jsonl.

artifacts/experiments/<ГГГГММДД-ЧЧММСС>_<имя>/
    config.json        конфиг радара, с которым шёл запуск
    environment.json   версии Python и пакетов, коммит Git, seed
    data.json          версии данных: снимки реестра с SHA-256, размеры волн, доля снижений
    search.json        все попытки подбора гиперпараметров с метриками на validation
    metrics.json       метрики всех моделей на validation и test, порог решения, экономика
    features.json      IV, интервалы WoE, коэффициенты регрессии, важности CatBoost
    rivaroxaban.json   прогноз по брендам ривароксабана 20 мг с причинами
    scores/*.parquet   прогнозы по каждой упаковке: validation, test, сегодня
    model.pkl          финальная модель-чемпион (только для своих файлов)
    summary.md         сводка для человека
artifacts/experiments/index.jsonl   одна строка на запуск: id, время, чемпион, ключевые метрики
artifacts/experiments/LATEST        id последнего успешного запуска
"""
from __future__ import annotations

import hashlib
import json
import pickle
import platform
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import pandas as pd

from pulse import __version__
from pulse.config import ROOT

EXPERIMENTS = ROOT / "artifacts" / "experiments"
PACKAGES = ["numpy", "pandas", "scikit-learn", "catboost", "pyarrow", "pydantic"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def environment(seed: int) -> dict:
    def pkg(name: str) -> str | None:
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                timeout=5).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
                                    timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return {
        "pulse_version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
        "packages": {p: pkg(p) for p in PACKAGES}, "git_commit": commit or "нет коммитов",
        "git_dirty": dirty, "seed": seed,
    }


def data_versions() -> dict:
    """Какие сырые данные лежали под запуском: снимки реестра с хешами и справочники."""
    manifest = ROOT / "data" / "raw" / "mpa_api" / "manifest.json"
    snapshots = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    refs = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "data" / "reference").glob("*"))}
    cbu_files = sorted((ROOT / "data" / "raw" / "cbu").glob("*.json"))
    return {
        "registry_snapshots": {d: {"rows": m["rows"], "sha256_uncompressed": m["sha256_uncompressed"]}
                               for d, m in sorted(snapshots.items()) if m.get("rows")},
        "reference_files": refs,
        "cbu_rate_files": len(cbu_files),
    }


class ExperimentLog:
    def __init__(self, name: str, root: Path = EXPERIMENTS):
        self.root = root
        self.started = time.time()
        self.started_at = datetime.now(UTC)
        self.run_id = f"{self.started_at:%Y%m%d-%H%M%S}_{_slug(name)}"
        self.dir = root / self.run_id
        (self.dir / "scores").mkdir(parents=True, exist_ok=True)

    def json(self, name: str, obj: Any) -> Path:
        path = self.dir / f"{name}.json"
        path.write_text(_dump(obj), encoding="utf-8")
        return path

    def frame(self, name: str, df: pd.DataFrame) -> Path:
        path = self.dir / "scores" / f"{name}.parquet"
        df.to_parquet(path, index=False)
        return path

    def pickle(self, name: str, obj: Any) -> Path:
        path = self.dir / f"{name}.pkl"
        with path.open("wb") as f:
            pickle.dump(obj, f)
        return path

    def text(self, name: str, content: str) -> Path:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def finish(self, summary: dict) -> dict:
        row = {"run_id": self.run_id, "started_at": self.started_at.isoformat(timespec="seconds"),
               "duration_s": round(time.time() - self.started, 1)} | summary
        with (self.root / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        (self.root / "LATEST").write_text(self.run_id, encoding="utf-8")
        return row


def runs(root: Path = EXPERIMENTS) -> pd.DataFrame:
    """Все запуски из index.jsonl: для сравнения экспериментов."""
    path = root / "index.jsonl"
    if not path.exists():
        return pd.DataFrame()
    return pd.DataFrame([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def latest_dir(root: Path = EXPERIMENTS) -> Path:
    return root / (root / "LATEST").read_text(encoding="utf-8").strip()


if __name__ == "__main__":
    df = runs()
    if df.empty:
        print("Запусков пока нет")
    else:
        cols = [c for c in ["run_id", "champion", "val_auc", "test_auc", "test_auc_rule", "test_value_mln",
                            "duration_s"] if c in df.columns]
        print(df[cols].to_string(index=False))
