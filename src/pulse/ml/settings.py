"""Загрузка config/radar.yaml с проверкой типов."""
from __future__ import annotations

import itertools
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from pulse.config import CONFIG_DIR


class WaveCfg(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    asof: str
    label_end: str


class SplitCfg(BaseModel):
    tune_train: list[str]
    validation: str
    test_train: list[str]
    test: str


class ModelCfg(BaseModel):
    defaults: dict[str, Any]
    search: dict[str, list[Any]] = {}

    def grid(self) -> list[dict[str, Any]]:
        """Все комбинации пространства поиска поверх значений по умолчанию."""
        if not self.search:
            return [dict(self.defaults)]
        keys = list(self.search)
        return [self.defaults | dict(zip(keys, combo, strict=True))
                for combo in itertools.product(*(self.search[k] for k in keys))]


class ScopeCfg(BaseModel):
    train: Literal["all", "imports"] = "all"
    eval: Literal["all", "imports"] = "all"


class RadarCfg(BaseModel):
    version: int
    seed: int
    label: dict[str, float]
    waves: list[WaveCfg]
    split: SplitCfg
    selection: dict[str, Any]
    feature_set: Literal["base", "history"] = "base"
    scope: ScopeCfg = ScopeCfg()
    models: dict[Literal["woe_logit", "catboost"], ModelCfg]

    def wave(self, name: str) -> WaveCfg:
        return next(w for w in self.waves if w.name == name)


def load_radar_cfg() -> RadarCfg:
    with (CONFIG_DIR / "radar.yaml").open(encoding="utf-8") as f:
        return RadarCfg.model_validate(yaml.safe_load(f))
