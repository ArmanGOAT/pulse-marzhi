# Пульс маржи: API и страница демо в одном контейнере; работает без интернета.
# Снимки реестра и прогнозы радара лежат в git и копируются в образ; при сборке из них собирается
# панель реестра. Если чего-то нет (например, удалили), образ сам скачает снимки mpa.uz по 17.11.2026
# и обучит радар. Запуск: docker compose up --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app
RUN useradd --system --uid 10001 pulse
COPY requirements.lock pyproject.toml ./
RUN pip install -r requirements.lock

# Слои от редко меняющихся к частым: при правке кода слои данных берутся из кэша сборки
# и не перекачиваются на сервер. data/ и artifacts/ принадлежат pulse: сборка ниже идёт от его имени
# (без отдельного слоя chown -R, который копировал бы все данные), а сервер пишет в data/ кэш панели.
COPY --chown=pulse data ./data
COPY --chown=pulse artifacts ./artifacts
COPY config ./config
COPY src ./src

USER pulse
RUN set -e; \
    if ! ls data/raw/mpa_api/registry_*.jsonl.gz >/dev/null 2>&1; then \
      echo "Нет снимков реестра: скачиваю с mpa.uz"; python -m pulse.data.loaders.mpa_api; \
    fi; \
    if [ ! -f data/processed/registry_panel.parquet ]; then \
      echo "Собираю панель реестра"; python -m pulse.data.build --offline; \
    fi; \
    if [ ! -f "artifacts/experiments/$(cat artifacts/experiments/LATEST)/scores/test.parquet" ]; then \
      echo "Нет прогнозов радара: обучаю"; python -m pulse.ml.radar; \
    fi; \
    python -c "from pulse.api.main import data_status as s; d = s(); print(d); assert d['ready'], d['missing']"

# Страница последней: её правки не пересобирают панель реестра.
COPY web ./web

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"
CMD ["uvicorn", "pulse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
