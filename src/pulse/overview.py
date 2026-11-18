"""Сводка проекта в JSON: python -m pulse.overview (или make overview) → docs/project-overview.json.

Все числа считаются теми же функциями, что отдают страница демо и API, поэтому сводка не может
разойтись с тем, что видит жюри. Текстовые поля — короткие формулировки для объяснения на чекпоинте.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime

from pulse import __version__
from pulse.api import main as api
from pulse.config import ROOT, load_assumptions, load_levers
from pulse.data import registry
from pulse.domain import regulation

OUT = ROOT / "docs" / "project-overview.json"


def _r(x, nd: int = 4):
    """Округление для читаемого JSON; numpy-типы → обычные."""
    if isinstance(x, dict):
        return {k: _r(v, nd) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_r(v, nd) for v in x]
    if hasattr(x, "item"):
        x = x.item()
    if isinstance(x, float):
        return round(x, nd)
    return x


def _md_table(path: str) -> list[dict]:
    """Первая markdown-таблица файла после заголовка → список словарей."""
    rows, header = [], None
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            if header and rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
        else:
            rows.append(dict(zip(header, cells, strict=False)))
    return rows


def _md_tables(path: str) -> list[dict]:
    """Все строки всех таблиц со столбцом «Статус»."""
    out, header = [], None
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
        elif "Статус" in header:
            out.append(dict(zip(header, cells, strict=False)))
    return out


def _tests_count() -> int | None:
    try:
        res = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:warnings"],
                             cwd=ROOT, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+) tests? collected", res.stdout)
    if m:
        return int(m.group(1))
    per_file = [int(n) for n in re.findall(r"^tests/\S+\.py: (\d+)$", res.stdout, re.M)]  # формат при -qq
    return sum(per_file) or None


def _market() -> dict:
    hist = api.rivaroxaban_history()
    imported = [b for b in hist["brands"] if not b["local"]]

    def max_step_drop(b):
        pts = [p["usd"] for p in b["points"]]
        return max([1 - pts[i] / pts[i - 1] for i in range(1, len(pts))] + [0.0])

    def total_drop(b):
        pts = [p["usd"] for p in b["points"]]
        return 1 - pts[-1] / max(pts)

    cut = [b for b in imported if max_step_drop(b) >= 0.05]
    drops = sorted(total_drop(b) for b in cut)
    deepest = max(cut, key=total_drop)
    ours = next(b for b in hist["brands"] if b["brand"] == "Сербан")
    return {
        "source": hist["source"],
        "brands_total": len(hist["brands"]),
        "brands_imported": len(imported),
        "brands_imported_cut_at_least_once": len(cut),
        "cut_rule": "хотя бы одно снижение цены ≥5% между соседними снимками",
        "median_drop_from_peak": drops[len(drops) // 2],
        "deepest": {"brand": deepest["brand"], "drop_from_peak": total_drop(deepest)},
        "our_example": {"brand": "Сербан", "first_usd": ours["points"][0]["usd"], "last_usd": ours["points"][-1]["usd"],
                        "first_date": ours["points"][0]["date"], "last_date": ours["points"][-1]["date"]},
    }


def _radar() -> dict:
    r = api.radar()
    m, seg, riv, case = r["metrics"], r["segments"], r["rivaroxaban"], r["case"]
    run_dir = ROOT / "artifacts" / "experiments" / r["run_id"]
    best = json.loads((run_dir / "search.json").read_text(encoding="utf-8"))["best_params"]
    by_test = {s["segment"]: s for s in seg["C: ноябрь 2026"]["segments"]}
    case_b = case["B: июль 2026"]["brands"]
    cuts = [b for b in case_b if b["actual_cut"]]
    return {
        "question": "Снизит ли Агентство референтную цену препарата на 10% и больше в ближайшей волне пересмотра (окно ~2 месяца)",
        "unit": "упаковка рецептурного препарата в реестре (номер регистрации + упаковка + производитель)",
        "run_id": r["run_id"],
        "champion": m["champion"],
        "champion_params": best.get(m["champion"], best) if isinstance(best, dict) else best,
        "models_compared": ["правило: цена выше медианы аналогов той же дозы", "WoE-логистическая регрессия",
                            "CatBoost с изотонической калибровкой"],
        "validation_scheme": {
            "waves": r["data"]["waves"],
            "tuning": "учим на A, подбираем гиперпараметры и чемпиона по AUC на B",
            "honest_test": "учим на A+B лучшими гиперпараметрами, один раз меряем на C",
            "leakage_guard": "признаки на дату прогноза строятся только из снимков до неё (ADR-0003, tests/test_ml.py)",
        },
        "metrics": {
            wave: {"role": v["note"], "models": {k: {"auc": x["auc"], "brier": x["brier"],
                                                      "value_per_drug_mln_uzs": x["value_per_drug_uzs"] / 1e6}
                                                  for k, x in v["models"].items()},
                   "always_prepare_mln_uzs": v["strategies"]["always_prepare"] / 1e6}
            for wave, v in (("B_validation", m["validation"]), ("C_test", m["test"]))
        },
        "economics": {"prep_cost_uzs": m["economics"]["prep_cost"], "gain_per_full_cut_uzs": m["economics"]["gain_per_cut"],
                      "rule": "готовимся, если вероятность × ожидаемое снижение × выгода > стоимость подготовки"},
        "segments_test_C": {k: {"auc": v["auc_model"], "ci95": v["auc_ci95"], "auc_rule": v["auc_rule"], "n": v["n"]}
                            for k, v in by_test.items()},
        "rivaroxaban_backtest_wave_B": {
            "trained_on": case["B: июль 2026"]["trained_on"],
            "brands_cut": [{"brand": b["trademark"], "p_cut": b["p_cut"], "top_pct": b["rank_in_wave_pct"],
                            "cut_size": b["cut_size"]} for b in cuts],
            "hits_in_top_8pct": sum(b["rank_in_wave_pct"] <= 0.08 for b in cuts),
            "wave_size": r["data"]["waves"]["B: июль 2026"]["rows"],
        },
        "forecast_now": {
            "asof": riv["asof"], "prepare_threshold": riv["threshold"],
            "imported_brands": [{"brand": b["trademark"], "p_cut": b["p_cut"], "prepare": b["prepare"], "reasons": b["reasons"]}
                                for b in riv["brands"] if not b["local"]],
        },
        "threshold_set_in_advance": {"auc_target": 0.75, "met_overall": m["test"]["models"][m["champion"]]["auc"] >= 0.75,
                                     "met_cardio_imports_test": by_test["Кардио, импортные"]["auc_model"] >= 0.75},
    }


def _plan(**kw) -> dict:
    res = api.plan(api.PlanIn(**kw))
    pl, cu = res["plan"], res["current"]
    out = {"inputs": {k: v for k, v in res["inputs"].items() if k in ("target_margin", "budget", "cip_binds", "p_cut")},
           "p_cut_used": res["p_cut"], "p_cut_source": res["p_cut_source"], "feasible": res["feasible"],
           "message": res["message"], "seconds": res["seconds"], "n_strategies": res["n_strategies"],
           "now": {"margin_durable": cu["margin_durable"], "p_loss": cu["p_loss"],
                   "profit_p10_p50_p90_uzs": cu["profit_p10_p50_p90"]}}
    if res["plan"]:
        out["plan" if res["feasible"] else "best_available"] = {"invest_uzs": pl["invest"], "months": pl["months"], "margin_now": pl["margin_now"],
                       "margin_durable": pl["margin_durable"], "p_loss": pl["p_loss"],
                       "profit_p10_p50_p90_uzs": pl["profit_p10_p50_p90"], "levers": pl["levers"],
                       "actions": [{"text": a["text"], "invest_uzs": a["invest"], "months": a["months"],
                                    "effect_pp": a["effect_pp"], "survives_revision": a["durable"]} for a in res["actions"]]}
    return out


def build() -> dict:
    a, lv = load_assumptions(), load_levers()
    pk = api.pack_view()
    st = api.stock(revision_day=120, p_cut=0.5, cut=0.2)
    rules = regulation.load_regulation().at(date.today())
    p = registry.panel()
    meta = json.loads(registry.PANEL_CACHE.with_suffix(".meta.json").read_text(encoding="utf-8"))
    base = _plan(target_margin=0.15)
    return _r({
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generated_by": "python -m pulse.overview",
        "project": {
            "name": "Пульс маржи", "version": __version__, "task": "Хакатон, задача №16: маржа импортного кардиопрепарата",
            "example_drug": "ривароксабан 20 мг (антикоагулянт, ATC B01AF01), бренд-пример Сербан",
            "country": "Узбекистан", "repository": "github.com/ArmanGOAT/pulse-marzhi (приватный)",
            "one_liner": "Заранее предсказываем пересмотр потолка цены и подбираем самый дешёвый план мер, "
                         "чтобы импортёр пережил его без убытка и без перебоев с лекарством",
            "user": "финансовый директор импортёра; решение принимает человек",
            "not": ["не медицинское изделие", "не GxP-система", "не обрабатывает персональные данные"],
        },
        "problem": {
            "price_ceiling_uzs": pk["price"], "annual_volume_packs": a.v("market.annual_volume"), "fx_uzs_per_usd": pk["fx"],
            "margin": pk["margin"], "margin_per_pack_uzs": pk["margin_uzs"], "annual_profit_uzs": pk["annual_profit"],
            "usd_share_of_costs": pk["usd_share"], "breakeven_fx": pk["breakeven_fx"],
            "margin_pp_lost_per_1pct_fx": -pk["pp_per_1pct_fx"],
            "cost_items_uzs_per_pack": {i["item"]: {"uzs": i["uzs_per_pack"], "currency": i["currency"]} for i in pk["items"]},
            "status": "цена и объём — факт из условия; курс — факт ЦБ; структура затрат — оценка команды",
        },
        "insight": {
            "double_hit": "Снижение потолка бьёт дважды: новая цена ниже для будущих продаж, и товар на складе, "
                          "купленный по старой цене, дешевеет",
            "stock_loss_at_20pct_cut_uzs": st["loss_if_cut"], "stock_on_revision_day_packs": st["stock_on_revision"],
            "years_of_profit_lost": st["loss_if_cut"]["regular"] / pk["annual_profit"],
            "prepare_if_probability_above": st["decision"]["threshold_prob"],
            "formula_trap": "Таможенная цена (CIP) входит в формулу потолка: скидка на закупку снижает CIP, "
                            "и при пересмотре потолок может опуститься вслед за ней",
        },
        "regulation": {
            "rules_in_force": rules.id, "title": rules.title, "basket": rules.basket,
            "formula": "референтная цена = минимум из (средняя по референтным странам без выбросов, "
                       "цена в стране производства +15%, средняя таможенная цена CIP за 12 месяцев); "
                       "потолок импортёра = референтная × 1,04",
            "rereg_threshold": rules.rereg_threshold, "cip_window_months": rules.cip_window_months,
            "status": rules.status, "source": rules.source,
        },
        "market_rivaroxaban_20mg": _market(),
        "data": {
            "registry_snapshots_nonempty": int(p["date"].nunique()),
            "period": [str(p["date"].min().date()), str(p["date"].max().date())],
            "registry_rows": len(p), "registry_panel_cache": {"rows": meta["rows"], "sha256": meta["sha256"]},
            "revision_lists": 8, "cbu_fx_months": 47, "currencies": ["UZS", "USD", "EUR", "RUB"],
            "sources": ["mpa.uz: реестр референтных цен (публичный API) и официальные списки пересмотров",
                        "cbu.uz: официальные курсы ЦБ РУз", "liki.uz: аптечные цены ривароксабана в Ташкенте"],
            "integrity": "сырые файлы не меняются; обработанные таблицы с паспортом SHA-256 (ALCOA+)",
        },
        "radar_ml": _radar(),
        "optimizer": {
            "what": "перебирает все сочетания мер, на каждом прогоняет сценарии Монте-Карло и выбирает самый дешёвый план, "
                    "который даёт цель по марже и проходит жёсткие правила",
            "search_space": {"shipments_per_year": lv.shipments_per_year, "procurement": list(lv.procurement),
                             "transport": list(lv.modes), "pooled_cargo": [False, True], "digital_share": lv.digital_shares,
                             "localization": list(lv.localization), "prepare_for_revision": [False, True],
                             "price_protection_clause": [False, True], "total_strategies": base["n_strategies"]},
            "scenarios": {"count": a.v("risk.n_scenarios"), "random": ["курс (дрейф и разброс по истории ЦБ)", "спрос",
                          "задержки поставок", "пересмотр: вероятность из радара, размер из 2450 реальных снижений"]},
            "hard_rules": ["запас на складе не меньше 1 месяца продаж", "риск убытка за год не выше 10%",
                           "вложения в пределах бюджета", "цены не повышаются"],
            "choice_order": ["меньше вложений", "меньше месяцев до эффекта", "больше прибыли"],
            "results": {
                "target_15_radar": base,
                "target_15_cip_trap": _plan(target_margin=0.15, cip_binds=True),
                "target_15_p_cut_60": _plan(target_margin=0.15, p_cut=0.6),
                "target_25_radar": _plan(target_margin=0.25),
            },
        },
        "demo": {
            "url": "http://localhost:8000 (make serve или docker compose up)",
            "screens": ["Упаковка: куда уходят 150 000 сум, ползунок курса", "Пересмотры: реальная история цен по брендам",
                        "Радар: проверка на июле 2026 и прогноз на сейчас, AUC по сегментам",
                        "Склад: запас по дням и потеря в день пересмотра", "План: меры, риск в 2000 сценариях, облако стратегий"],
            "presenter_mode": "кнопка «▶ Показ» или клавиша P, пробел — следующий из 8 шагов",
            "exports": ["PDF-записка для руководства", "Excel: план, затраты, все стратегии, допущения"],
        },
        "engineering": {
            "stack": {"python": "3.12", "api": "FastAPI", "ml": "scikit-learn, CatBoost", "data": "pandas, parquet",
                      "frontend": "HTML/CSS/JS без сборки, графики на SVG", "export": "fpdf2, openpyxl", "container": "Docker"},
            "layers": ["data", "domain", "ml", "decision", "api + export", "web", "ops"],
            "api": ["GET /api/pack", "POST /api/ceiling", "GET /api/market/rivaroxaban", "GET /api/radar", "GET /api/stock",
                    "POST /api/plan", "POST /api/export/{pdf,xlsx}", "GET /docs (OpenAPI)"],
            "tests": _tests_count(), "lint": "ruff", "offline": "работает без сети, проверено в контейнере с --network none",
            "experiment_log": "artifacts/experiments/<id>/ (конфиг, версии данных, подбор, метрики, признаки, прогнозы)",
        },
        "standards": [{"standard": r.get("Стандарт"), "status": r.get("Статус")} for r in _md_tables("docs/STANDARDS.md")
                      if r.get("Стандарт")],
        "risks": [{"id": r["#"], "risk": r["Риск"], "status": r["Статус"]} for r in _md_table("docs/RISKS.md")],
        "limitations": [
            "AUC по всему рынку 0,68 ниже цели 0,75; в нашем сегменте (импортные кардио) 0,80, но только на волне C",
            "затраты импортёра — оценка команды, нужны его данные",
            "цен в референтных странах нет: эту часть формулы потолка по реальным данным не считаем",
            "снимки помесячные: дата пересмотра известна с точностью до месяца",
            "автоматического переключения на правило, если модель хуже, нет",
        ],
        "docs": ["README.md", "docs/model_card.md", "docs/datasheet.md", "docs/RISKS.md", "docs/STANDARDS.md",
                 "docs/design-system.md", "docs/adr/"],
    })


def main() -> None:
    data = build()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(json.dumps(data, ensure_ascii=False))} символов")


if __name__ == "__main__":
    main()
