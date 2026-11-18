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
