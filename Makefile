# Пульс маржи. Всё с нуля одной командой: make demo (нужны Python 3.12 и make; данные уже лежат в git).
PYTHON ?= python3.12
PY := PYTHONPATH=src .venv/bin/python
LATEST_SCORES = artifacts/experiments/$$(cat artifacts/experiments/LATEST 2>/dev/null)/scores/test.parquet

.PHONY: demo check setup test lint fmt data data-latest train runs serve overview

demo:  ## окружение, данные, радар и сервер; уже сделанные шаги пропускаются
	@test -x .venv/bin/python || $(MAKE) setup
	@ls data/raw/mpa_api/registry_*.jsonl.gz >/dev/null 2>&1 || $(MAKE) data
	@test -f data/processed/registry_panel.parquet || $(PY) -m pulse.data.build --offline
	@test -f "$(LATEST_SCORES)" || $(MAKE) train
	@$(MAKE) check
	$(MAKE) serve

check:  ## что из данных на месте (то же показывает /api/health)
	@$(PY) -c "import json; from pulse.api.main import data_status; print(json.dumps(data_status(), ensure_ascii=False, indent=2))"

setup:  ## окружение .venv с точными версиями зависимостей; другой Python: make setup PYTHON=python3
	$(PYTHON) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.lock
	$(PY) -m pip install -e . --no-deps

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

fmt:
	$(PY) -m ruff check --fix src tests

data:  ## докачать недостающие снимки реестра по 17.11.2026 (в git уже все) и собрать таблицы; с нуля ~5 мин
	$(PY) -m pulse.data.loaders.mpa
	$(PY) -m pulse.data.loaders.mpa_api
	$(PY) -m pulse.data.build

data-latest:  ## то же, но снимки реестра по сегодняшний день: цифры станут новее, чем в README
	$(PY) -m pulse.data.loaders.mpa_api --until today
	$(PY) -m pulse.data.build

train:  ## подбор на волне B, честный тест на волне C, отчёт в artifacts/experiments/
	$(PY) -m pulse.ml.radar

runs:
	$(PY) -m pulse.ml.experiments

serve:  ## API и интерфейс демо: http://localhost:8000 (схема API: /docs)
	$(PY) -m uvicorn pulse.api.main:app --port 8000

overview:  ## сводка проекта с цифрами: docs/project-overview.json
	$(PY) -m pulse.overview
