"""API «Пульс маржи». Запуск: uvicorn pulse.api.main:app --port 8000 (или make serve).

Схема OpenAPI: /docs. Интерфейс демо отдаётся с того же адреса (папка web/).
Работает без интернета: все данные и модели читаются с диска.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import date
from functools import lru_cache
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pulse import __version__, products
from pulse.config import ROOT, load_assumptions, load_levers
from pulse.data import registry
from pulse.decision.optimizer import Inputs, optimize
from pulse.domain import inventory as inv
from pulse.domain import pack, regulation
from pulse.export import report
from pulse.ml import experiments

log = logging.getLogger("uvicorn.error")


def data_status() -> dict:
    """Что из данных на месте. В git лежат курсы ЦБ, списки пересмотров и отчёты радара;
    снимки реестра и прогнозы радара по упаковкам появляются после make data и make train."""
    snaps = len(registry.snapshot_dates())
    try:
        run = experiments.latest_dir()
        run_id, scores = run.name, (run / "scores" / "test.parquet").exists()
    except OSError:
        run_id, scores = None, False
    missing = []
    if not snaps:
        missing.append("снимки реестра mpa.uz — выполните make data (нужен интернет)")
    if not scores:
        missing.append("прогнозы радара по упаковкам — выполните make train")
    return {"ready": not missing, "registry_snapshots": snaps, "radar_run": run_id, "radar_scores": scores,
            "missing": missing}


def _warm() -> None:
    status = data_status()
    if not status["ready"]:
        log.warning("Пульс маржи: не хватает данных: %s. Быстрый путь: make demo", "; ".join(status["missing"]))
        return
    with contextlib.suppress(Exception):  # прогрев не должен ронять сервер
        rivaroxaban_history()
        products.catalog()


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm, daemon=True).start()
    yield


app = FastAPI(title="Пульс маржи", version=__version__, lifespan=lifespan,
              description="Прогноз пересмотров потолка цены и план мер для импортёра лекарств (задача №16)")


def _clean(x):
    """numpy → обычные типы для JSON."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


# ---------- справочное ----------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "data": data_status()}


@app.get("/api/meta")
def meta() -> dict:
    a, lv = load_assumptions(), load_levers()
    params = {f"{s}.{n}": p.model_dump() for s in ("market", "costs", "risk") for n, p in getattr(a, s).items()}
    try:
        riv = json.loads((experiments.latest_dir() / "rivaroxaban.json").read_text(encoding="utf-8"))
        brands = [b["trademark"] for b in riv["brands"] if not b["local"]]
    except (FileNotFoundError, OSError):
        brands = ["Сербан"]
    return {"assumptions": params, "brands": brands, "default_brand": "Сербан",
            "levers": {"procurement": {k: v.label for k, v in lv.procurement.items()},
                       "modes": {k: v.label for k, v in lv.modes.items()},
                       "localization": {k: v.label for k, v in lv.localization.items()}}}


# ---------- упаковка ----------

@app.get("/api/pack")
def pack_view(fx: float | None = None, price: float | None = None) -> dict:
    a = load_assumptions()
    fx = fx or a.v("market.fx_usd_uzs")
    price = price or a.v("market.price_ceiling")
    cur = pack.current_strategy()
    c = pack.components(cur, fx=fx, price=price)
    b = pack.breakdown(fx=fx, price=price)
    b = b[b["uzs_per_pack"] > 0]
    return _clean({
        "fx": fx, "price": price,
        "items": b.to_dict(orient="records"),
        "margin_uzs": float(c.margin_sum[0]), "margin": float(c.margin[0]),
        "usd_share": float(c.usd_share[0]), "breakeven_fx": float(c.breakeven_fx()[0]),
        "pp_per_1pct_fx": float(-c.fx_sensitivity[0] / price * 100),
        "annual_profit": float(c.margin_sum[0] * a.v("market.annual_volume")),
        "status": {"price": "fact", "volume": "fact", "costs": "estimate", "fx": a.p("market.fx_usd_uzs").status},
    })


# ---------- потолок ----------

class CeilingIn(BaseModel):
    when: date = Field(default_factory=date.today)
    ref_prices: dict[str, float] = {}
    origin_price: float | None = None
    cip_avg: float | None = None
    original_reference: float | None = None
    is_generic: bool = True
    current_importer_ceiling: float | None = None


@app.post("/api/ceiling")
def ceiling(body: CeilingIn) -> dict:
    try:
        r = regulation.reference_price(body.when, body.ref_prices, body.origin_price, body.cip_avg,
                                       body.original_reference, body.is_generic)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    rules = regulation.load_regulation().at(body.when)
    cur = body.current_importer_ceiling or load_assumptions().v("market.price_ceiling")
    return _clean({
        "rules": {"id": rules.id, "title": rules.title, "basket": rules.basket, "rereg_threshold": rules.rereg_threshold,
                  "status": rules.status, "source": rules.source},
        "components": {k: {"value": v, "label": regulation.COMPONENT_LABELS[k]} for k, v in r.components.items()},
        "binding": r.binding, "binding_label": r.binding_label, "reference_price": r.reference_price,
        "importer_ceiling": r.importer_ceiling, "pharmacy_ceiling": r.pharmacy_ceiling,
        "current_importer_ceiling": cur, "change_vs_current": r.importer_ceiling / cur - 1,
        "excluded_countries": r.excluded_countries,
    })


# ---------- рынок: реальная история цен ----------

@lru_cache(maxsize=1)
def rivaroxaban_history() -> dict:
    p = registry.panel()
    r = p[p["inn"].str.contains("ривароксабан") & (p["strength"] == "20мг")].copy()
    r["brand"] = r["trademark"].str.replace("®", "", regex=False).str.strip().str.title()
    out = []
    for (brand, local), g in r.groupby(["brand", "local"]):
        s = g.groupby("date")["base_usd"].median().sort_index()
        out.append({"brand": brand, "local": bool(local),
                    "points": [{"date": d.date().isoformat(), "usd": round(float(v), 2)} for d, v in s.items()]})
    out.sort(key=lambda b: -b["points"][0]["usd"])
    return {"source": "mpa.uz, реестр референтных цен, снимки на 1-е число месяца", "status": "fact", "brands": out}


@app.get("/api/market/rivaroxaban")
def market_rivaroxaban() -> dict:
    if not registry.snapshot_dates():
        raise HTTPException(503, "Нет снимков реестра mpa.uz. Выполните make data (нужен интернет) и перезапустите сервер.")
    return rivaroxaban_history()


# ---------- радар ----------

@app.get("/api/radar")
def radar() -> dict:
    try:
        d = experiments.latest_dir()
    except (FileNotFoundError, OSError) as e:
        raise HTTPException(404, "Радар ещё не обучен: make train") from e

    def read(name: str):
        path = d / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    metrics = read("metrics.json")
    return _clean({"run_id": d.name, "status": "fact", "metrics": metrics, "segments": read("segments.json"),
                   "rivaroxaban": read("rivaroxaban.json"), "case": read("rivaroxaban_case.json"),
                   "data": {k: v for k, v in (read("data.json") or {}).items() if k in ("waves", "snapshots", "asof_now")}})


# ---------- склад и пересмотр ----------

@app.get("/api/stock")
def stock(shipments: int = 2, revision_day: int = 120, p_cut: float = 0.5, cut: float = 0.2) -> dict:
    """Склад по дням при обычных поставках и при подготовке. По умолчанию пересмотр через месяц после прихода партии."""
    a = load_assumptions()
    annual = a.v("market.annual_volume")
    unit_cost = a.v("costs.purchase_usd") * a.v("market.fx_usd_uzs")
    reg = inv.simulate(annual, shipments, revision_day=revision_day)
    prep = inv.simulate(annual, shipments, revision_day=revision_day, prepare=True)
    dec = inv.prepare_decision(p_cut, cut, annual, shipments, unit_cost)
    loss_reg = inv.devaluation_loss(reg.stock_on_revision, unit_cost, cut)
    loss_prep = inv.devaluation_loss(prep.stock_on_revision, unit_cost, cut)
    return _clean({
        "days": list(range(365)), "regular": reg.daily["stock_start"].round(0).tolist(),
        "prepared": prep.daily["stock_start"].round(0).tolist(),
        "safety_units": a.v("costs.safety_stock_min_months") * annual / 12, "revision_day": revision_day,
        "stock_on_revision": {"regular": reg.stock_on_revision, "prepared": prep.stock_on_revision},
        "loss_if_cut": {"regular": loss_reg, "prepared": loss_prep},
        "shipments": {"regular": reg.shipments, "prepared": prep.shipments},
        "decision": {"prepare": dec.prepare, "threshold_prob": dec.threshold_prob, "prep_cost": dec.prep_cost,
                     "expected_loss_regular": dec.loss_regular, "expected_loss_prepared": dec.loss_prepared},
        "annual_profit_now": 3351 * annual,
    })


# ---------- план мер ----------

class PlanIn(BaseModel):
    fx: float | None = None
    target_margin: float = Field(0.15, ge=0, le=0.6)
    budget: float = Field(10e9, ge=0)
    max_months: int | None = Field(None, ge=0, le=36)
    p_cut: float | None = Field(None, ge=0, le=1)
    brand: str = "Сербан"
    cut_scale: float = Field(1.0, ge=0, le=3)
    revision_month: int | None = Field(None, ge=1, le=12)
    cip_binds: bool = False


@lru_cache(maxsize=64)
def _plan_cached(key: str):
    return optimize(Inputs(**json.loads(key)))


def _plan(body: PlanIn):
    return _plan_cached(json.dumps(body.model_dump(), sort_keys=True))


@app.post("/api/plan")
def plan(body: PlanIn) -> dict:
    res = _plan(body)
    s = res.strategies
    sample = s.sample(n=min(2500, len(s)), random_state=0) if len(s) > 2500 else s
    out = {
        "inputs": res.inputs, "p_cut": res.p_cut, "p_cut_source": res.p_cut_source, "cut_source": res.cut_source,
        "feasible": res.feasible, "message": res.message, "seconds": res.seconds,
        "warnings": [] if data_status()["radar_scores"] else
        ["Размер снижений взят из допущения конфига, а не из реальных снижений: нет прогнозов радара (make train)."],
        "current": res.current, "plan": res.plan, "actions": res.actions,
        "strategies": {c: sample[c].tolist() for c in ("invest", "margin_now", "margin_durable", "p_loss", "feasible")},
        "n_strategies": int(len(s)),
    }
    return _clean(out)


@app.post("/api/export/{fmt}")
def export(fmt: str, body: PlanIn) -> Response:
    res = _plan(body)
    stamp = date.today().isoformat()
    if fmt == "pdf":
        return Response(report.pdf_bytes(res, body.brand), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="plan-{stamp}.pdf"'})
    if fmt == "xlsx":
        return Response(report.xlsx_bytes(res, body.brand),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="plan-{stamp}.xlsx"'})
    raise HTTPException(404, "Формат: pdf или xlsx")


# ---------- проверка любого препарата: поиск, карточка, отчёт ----------

def _need_catalog() -> None:
    if not registry.snapshot_dates():
        raise HTTPException(503, "Нет снимков реестра mpa.uz. Выполните make data (нужен интернет) и перезапустите сервер.")


@app.get("/api/products")
def products_search(q: str = "", limit: int = Query(20, ge=1, le=50)) -> dict:
    _need_catalog()
    return {"query": q, "results": products.search(q, limit)}


@app.get("/api/products/{pid}")
def product_card(pid: str) -> dict:
    _need_catalog()
    card = products.card(pid)
    if card is None:
        raise HTTPException(404, "Препарат не найден")
    return _clean(card)


@app.get("/api/products/{pid}/report/{fmt}")
def product_report(pid: str, fmt: str) -> Response:
    card = product_card(pid)
    safe = quote(f"Пульс маржи — {card['title']} {card['strength']}.{fmt}")
    disp = f"attachment; filename=\"pulse-report.{fmt}\"; filename*=UTF-8''{safe}"
    if fmt == "pdf":
        return Response(report.product_pdf_bytes(card), media_type="application/pdf", headers={"Content-Disposition": disp})
    if fmt == "xlsx":
        return Response(report.product_xlsx_bytes(card), headers={"Content-Disposition": disp},
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    raise HTTPException(404, "Формат: pdf или xlsx")


WEB = ROOT / "web"
if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

