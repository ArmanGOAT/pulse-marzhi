"""Журнал экспериментов и сетка подбора гиперпараметров."""
import json

import pandas as pd

from pulse.ml import experiments
from pulse.ml.settings import ModelCfg, load_radar_cfg


def test_grid_is_full_product_over_defaults():
    cfg = ModelCfg(defaults={"a": 1, "b": 2, "c": 3}, search={"a": [1, 5], "b": [2, 7, 9]})
    grid = cfg.grid()
    assert len(grid) == 6
    assert all(g["c"] == 3 for g in grid)
    assert {"a": 5, "b": 9, "c": 3} in grid


def test_radar_config_split_keeps_test_wave_out_of_tuning():
    cfg = load_radar_cfg()
    assert cfg.split.test not in cfg.split.tune_train
    assert cfg.split.test != cfg.split.validation
    names = [w.name for w in cfg.waves]
    assert names.index(cfg.split.validation) < names.index(cfg.split.test)  # тест позже валидации во времени
    assert len(cfg.models["woe_logit"].grid()) == 4 * 2 * 5


def test_experiment_log_writes_folder_and_index(tmp_path):
    log = experiments.ExperimentLog("Тест: прогон", root=tmp_path)
    log.json("metrics", {"auc": 0.7})
    log.frame("now", pd.DataFrame({"key": ["a"], "p": [0.5]}))
    log.text("summary.md", "ok")
    row = log.finish({"champion": "woe_logit", "test_auc": 0.68})
    assert (log.dir / "metrics.json").exists() and (log.dir / "scores" / "now.parquet").exists()
    assert (tmp_path / "LATEST").read_text() == log.run_id
    lines = (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["champion"] == "woe_logit" and row["run_id"] == log.run_id
    assert experiments.runs(tmp_path)["test_auc"].tolist() == [0.68]
    assert experiments.latest_dir(tmp_path) == log.dir


def test_environment_records_versions_and_seed():
    env = experiments.environment(seed=42)
    assert env["seed"] == 42 and env["packages"]["catboost"] and env["python"].startswith("3.")
