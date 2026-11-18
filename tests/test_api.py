"""API: ответы по контракту, жёсткие правила в плане, выгрузка открывается."""
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from pulse.api.main import app

client = TestClient(app)
BODY = {"target_margin": 0.15, "p_cut": 0.04, "brand": "Сербан"}


def test_health_and_pack():
    assert client.get("/api/health").json()["status"] == "ok"
    p = client.get("/api/pack").json()
    assert p["margin"] == pytest.approx(0.0223, abs=1e-3)
    assert client.get("/api/pack", params={"fx": 12360}).json()["margin"] < 0


def test_ceiling_reports_binding_component():
    r = client.post("/api/ceiling", json={"when": "2026-07-01", "ref_prices": {"RU": 130000}, "cip_avg": 113400}).json()
    assert r["binding"] == "cip" and r["change_vs_current"] < 0


def test_stock_and_plan():
    s = client.get("/api/stock", params={"p_cut": 0.6}).json()
    assert s["stock_on_revision"]["prepared"] < s["stock_on_revision"]["regular"]
    plan = client.post("/api/plan", json=BODY).json()
    assert plan["feasible"] and plan["plan"]["p_loss"] <= 0.10 and plan["n_strategies"] == 8640


@pytest.mark.parametrize("fmt,magic", [("pdf", b"%PDF"), ("xlsx", b"PK")])
def test_export(fmt, magic):
    r = client.post(f"/api/export/{fmt}", json=BODY)
    assert r.status_code == 200 and r.content[:4].startswith(magic)
    if fmt == "xlsx":
        wb = load_workbook(io.BytesIO(r.content))
        assert {"План", "Затраты", "Стратегии", "Допущения"} <= set(wb.sheetnames)


def test_health_reports_data_status():
    data = client.get("/api/health").json()["data"]
    assert {"ready", "registry_snapshots", "radar_scores", "missing"} <= set(data)


def test_missing_data_gives_clear_messages(monkeypatch):
    """Свежий клон без make data / make train: понятная ошибка и предупреждение, а не 500 и тихая подмена."""
    from pulse.api import main
    from pulse.data import registry

    monkeypatch.setattr(registry, "snapshot_dates", lambda: [])
    r = client.get("/api/market/rivaroxaban")
    assert r.status_code == 503 and "make data" in r.json()["detail"]

    monkeypatch.setattr(main, "data_status", lambda: {"ready": False, "radar_scores": False, "missing": ["x"]})
    plan = client.post("/api/plan", json=BODY).json()
    assert plan["warnings"] and "make train" in plan["warnings"][0]


def test_product_search_card_and_reports():
    """Страница «Проверить препарат»: поиск находит бренд, карточка простым языком, отчёты открываются."""
    found = client.get("/api/products", params={"q": "сербан"}).json()["results"]
    assert found and all("Сербан" in r["title"] for r in found)
    pid = next(r["id"] for r in found if "20мг" in r["subtitle"])
    card = client.get(f"/api/products/{pid}").json()
    assert card["level"] in {"low", "mid", "high"} and card["reasons"] and card["todo"] and card["history"]
    assert 0 <= card["p_cut"] <= 1
    pdf = client.get(f"/api/products/{pid}/report/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    xlsx = client.get(f"/api/products/{pid}/report/xlsx")
    assert {"Итог", "История цены", "Аналоги"} <= set(load_workbook(io.BytesIO(xlsx.content)).sheetnames)
    assert client.get("/api/products/unknown").status_code == 404
    assert client.get("/api/products", params={"q": "x"}).json()["results"] == []


def test_product_list_check_and_reports():
    """Список препаратов: найденные, ненайденные с объяснением, без дублей; файл xlsx читается; отчёты открываются."""
    text = "Сербан 20 мг\nКсарелто\nабракадабра\nКсарелто 99 мг"
    res = client.post("/api/products/batch", json={"text": text}).json()
    status = {r["query"]: r["status"] for r in res["rows"]}
    assert status["Сербан 20 мг"] == "found" and status["абракадабра"] == "absent"
    assert status["Ксарелто 99 мг"] == "duplicate"  # те же упаковки, что в строке «Ксарелто»
    ids = [m["id"] for r in res["rows"] for m in r["matches"]]
    assert len(ids) == len(set(ids)) == res["summary"]["packages"] and res["summary"]["not_found"] == 1
    assert client.get("/api/products", params={"q": "абракадабра"}).json()["not_found"]["kind"] == "absent"
    from openpyxl import Workbook
    wb = Workbook()
    for row in (["Препарат", "Доза"], ["Сербан", 20], ["Ксилтесс", None]):
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    parsed = client.post("/api/products/batch/parse?filename=list.xlsx", content=buf.getvalue()).json()
    assert parsed["items"] == ["Сербан 20", "Ксилтесс"]
    assert client.post("/api/products/batch/parse?filename=list.doc", content=b"x").status_code == 415
    pdf = client.post("/api/products/batch/report/pdf", json={"text": text})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    xlsx = client.post("/api/products/batch/report/xlsx", json={"text": text})
    assert {"Сводка", "Не найдено", "Подробно", "Как читать"} <= set(load_workbook(io.BytesIO(xlsx.content)).sheetnames)
    assert client.post("/api/products/batch", json={"text": " "}).status_code == 422
