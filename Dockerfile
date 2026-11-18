# Пульс маржи: API и страница демо в одном контейнере. Работает без интернета:
# данные (data/) и отчёты экспериментов (artifacts/) кладутся в образ при сборке.
# В свежем клоне сначала: make setup && make data && make train
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install -r requirements.lock

COPY src ./src
COPY config ./config
COPY web ./web
COPY data ./data
COPY artifacts ./artifacts

RUN useradd --system --uid 10001 pulse && chown -R pulse /app/data
USER pulse

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"
CMD ["uvicorn", "pulse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
