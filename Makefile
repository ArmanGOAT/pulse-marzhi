PY := PYTHONPATH=src .venv/bin/python

.PHONY: setup test lint fmt data train runs

setup:
	python3.12 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.lock
	$(PY) -m pip install -e . --no-deps

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

fmt:
	$(PY) -m ruff check --fix src tests

data:
	$(PY) -m pulse.data.loaders.mpa
	$(PY) -m pulse.data.loaders.mpa_api
	$(PY) -m pulse.data.build

train:
	$(PY) -m pulse.ml.radar

runs:
	$(PY) -m pulse.ml.experiments
