"""PDF-записка и Excel по результату оптимизатора. Работают без интернета.

Шрифт DejaVu Sans (свободная лицензия, файл LICENSE_DEJAVU рядом) нужен для кириллицы в PDF.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from pulse.config import load_assumptions
from pulse.decision.optimizer import Result
from pulse.domain import pack

FONTS = Path(__file__).parent / "fonts"
STATUS_RU = {"fact": "факт", "estimate": "оценка", "demo": "демо-данные"}


def _bn(x: float) -> str:
    # округление половины вверх, как на странице (Intl): 0,975 млрд → 0,98, а не 0,97 из-за двоичной записи
    v = Decimal(repr(round(x / 1e9, 9))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return f"{v:,.2f} млрд".replace(",", " ").replace(".", ",")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".", ",")


def _summary_rows(res: Result) -> list[tuple[str, str, str]]:
    cu, pl = res.current, res.plan
    return [
        ("Маржа при текущих условиях", _pct(cu["margin_now"]), _pct(pl["margin_now"])),
        ("Маржа после пересмотра (устойчивая)", _pct(cu["margin_durable"]), _pct(pl["margin_durable"])),
        ("Вероятность убытка за год", f"{cu['p_loss']:.0%}", f"{pl['p_loss']:.0%}"),
        ("Прибыль за год, обычный сценарий (P50)", _bn(cu["profit_p10_p50_p90"][1]), _bn(pl["profit_p10_p50_p90"][1])),
        ("Прибыль за год, плохой сценарий (P10)", _bn(cu["profit_p10_p50_p90"][0]), _bn(pl["profit_p10_p50_p90"][0])),
        ("Вложения", "—", _bn(pl["invest"])),
        ("Срок до полного эффекта", "—", f"{pl['months']} мес."),
    ]


def pdf_bytes(res: Result, brand: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font("DejaVu", "B", 16)
    pdf.multi_cell(w, 8, f"План защиты маржи: {brand}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    inp = res.inputs
    fx = f"{inp['fx']:,.0f}".replace(",", " ")
    pdf.multi_cell(w, 5, f"Пульс маржи · {date.today().isoformat()} · курс {fx} сум/$ · цель {inp['target_margin']:.0%} · "
                         f"бюджет {_bn(inp['budget'])} · вероятность пересмотра {res.p_cut:.0%} ({res.p_cut_source})",
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("DejaVu", "", 10)
    pdf.multi_cell(w, 5, res.message, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(w, 7, "Итог", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    cols = [w * 0.5, w * 0.25, w * 0.25]
    for i, (a, b, c) in enumerate([("Показатель", "Сейчас", "С планом"), *_summary_rows(res)]):
        pdf.set_font("DejaVu", "B" if i == 0 else "", 9)
        for text, cw in zip((a, b, c), cols, strict=True):
            pdf.cell(cw, 6, text, border=1)
        pdf.ln()
    pdf.ln(3)

    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(w, 7, "Что сделать", new_x="LMARGIN", new_y="NEXT")
    acols = [w * 0.46, w * 0.14, w * 0.10, w * 0.12, w * 0.18]
    pdf.set_font("DejaVu", "B", 8)
    for text, cw in zip(("Мера", "Вложения", "Срок", "Эффект", "После пересмотра"), acols, strict=True):
        pdf.cell(cw, 6, text, border=1)
    pdf.ln()
    pdf.set_font("DejaVu", "", 8)
    for act in res.actions:
        eff = f"{act['effect_pp']:+.1f} п.п.".replace(".", ",", 1) if act["effect_pp"] is not None else "защита"
        row = (act["text"], f"{act['invest'] / 1e6:,.0f} млн".replace(",", " "), f"{act['months']} мес.", eff,
               "останется" if act["durable"] else "может уйти")
        y0 = pdf.get_y()
        pdf.multi_cell(acols[0], 5, row[0], border=1, new_x="RIGHT", new_y="TOP")
        h = pdf.get_y() - y0 if pdf.get_y() > y0 else 5
        h = max(h, 5)
        for text, cw in zip(row[1:], acols[1:], strict=True):
            pdf.cell(cw, h, text, border=1)
        pdf.ln(h)
    pdf.ln(3)

    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(w, 7, "Допущения и статус цифр", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 8)
    a = load_assumptions()
    for key in ("market.price_ceiling", "market.annual_volume", "market.fx_usd_uzs", "costs.purchase_usd",
                "risk.fx_drift", "risk.fx_sd", "costs.cut_pass_through", "costs.protection_share"):
        p = a.p(key)
        pdf.multi_cell(w, 4.5, f"• {key}: {p.value:g} {p.unit} — {STATUS_RU[p.status]}; {p.source}", new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(w, 4.5, f"• Размер снижений в сценариях: {res.cut_source}.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("DejaVu", "", 7)
    pdf.multi_cell(w, 4, "Расчёт на модели «Пульс маржи». Цены не повышаются, запас не опускается ниже месяца продаж, "
                         "риск убытка рекомендованного плана не выше 10%. Оценки основаны на допущениях о затратах "
                         "импортёра и требуют проверки на его данных.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def xlsx_bytes(res: Result, brand: str) -> bytes:
    wb = Workbook()
    head = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDE6FA")

    ws = wb.active
    ws.title = "План"
    ws.append([f"План защиты маржи: {brand}", "", ""])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([res.message])
    ws.append([])
    ws.append(["Показатель", "Сейчас", "С планом"])
    for r in _summary_rows(res):
        ws.append(list(r))
    ws.append([])
    ws.append(["Мера", "Вложения, сум", "Срок, мес.", "Эффект, п.п.", "После пересмотра", "Комментарий"])
    for act in res.actions:
        ws.append([act["text"], act["invest"], act["months"], act["effect_pp"],
                   "останется" if act["durable"] else "может уйти в потолок", act["note"]])

    ws2 = wb.create_sheet("Затраты")
    ws2.append(["Статья", "Валюта", "Сейчас, сум/уп.", "С планом, сум/уп."])
    cur = pack.breakdown(fx=res.inputs["fx"])
    lev = {k: v for k, v in res.plan["levers"].items() if k in pack.LEVER_COLS}
    plan_df = pd.DataFrame([lev])
    from pulse.config import load_levers  # локальный импорт: только для выгрузки
    plan_df["invest"] = pack.invest_of(plan_df, load_levers())
    plan_df["months"] = pack.months_of(plan_df, load_levers())
    pl = pack.breakdown(fx=res.inputs["fx"], strategy=plan_df)
    for (_, c), (_, p) in zip(cur.iterrows(), pl.iterrows(), strict=True):
        ws2.append([c["item"], c["currency"], round(c["uzs_per_pack"]), round(p["uzs_per_pack"])])

    ws3 = wb.create_sheet("Стратегии")
    strat = res.strategies.copy()
    ws3.append(list(strat.columns))
    for row in strat.itertuples(index=False):
        ws3.append([bool(v) if isinstance(v, bool) else v for v in row])

    ws4 = wb.create_sheet("Допущения")
    ws4.append(["Параметр", "Значение", "Единица", "Статус", "Источник"])
    a = load_assumptions()
    for section in ("market", "costs", "risk"):
        for name, p in getattr(a, section).items():
            ws4.append([f"{section}.{name}", p.value, p.unit, STATUS_RU[p.status], p.source])

    for sheet in wb.worksheets:
        for cell in sheet[1 if sheet.title != "План" else 4]:
            cell.font = head
            cell.fill = fill
        for i, col in enumerate(sheet.columns, start=1):
            width = min(60, max(10, max(len(str(c.value or "")) for c in col) + 2))
            sheet.column_dimensions[get_column_letter(i)].width = width
        for row in sheet.iter_rows():
            for c in row:
                c.alignment = Alignment(wrap_text=False, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- отчёт по одному препарату (страница «Проверить препарат») ----------

def _pdf() -> tuple[FPDF, float]:
    pdf = FPDF(format="A4")
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    return pdf, pdf.w - pdf.l_margin - pdf.r_margin


def product_pdf_bytes(c: dict) -> bytes:
    pdf, w = _pdf()
    nl = {"new_x": "LMARGIN", "new_y": "NEXT", "align": "L"}
    pdf.set_font("DejaVu", "B", 18)
    pdf.multi_cell(w, 9, c["title"], **nl)
    pdf.set_font("DejaVu", "", 10)
    pdf.multi_cell(w, 5, f"{c['subtitle']} · {c['manufacturer']}", **nl)
    pdf.multi_cell(w, 5, f"Прогноз на ближайшую волну пересмотра цен · данные госреестра mpa.uz на {c['asof']}", **nl)
    pdf.ln(4)

    pdf.set_font("DejaVu", "B", 16)
    pdf.multi_cell(w, 8, f"{c['level_label']}: {c['p_cut']:.0%}", **nl)
    pdf.set_font("DejaVu", "", 11)
    pdf.multi_cell(w, 6, c["meaning"], **nl)
    pdf.ln(2)
    pdf.set_font("DejaVu", "B", 11)
    pdf.multi_cell(w, 6, "Что делать", **nl)
    pdf.set_font("DejaVu", "", 11)
    pdf.multi_cell(w, 6, c["todo"], **nl)
    pdf.ln(2)
    pdf.set_font("DejaVu", "B", 11)
    pdf.multi_cell(w, 6, "Почему так", **nl)
    pdf.set_font("DejaVu", "", 10)
    for r in c["reasons"]:
        pdf.multi_cell(w, 5.5, f"• {r}", **nl)
    pdf.ln(3)

    pdf.set_font("DejaVu", "B", 11)
    pdf.multi_cell(w, 6, "О препарате", **nl)
    pdf.set_font("DejaVu", "", 10)
    facts = [("Действующее вещество", c["inn"]), ("Форма и дозировка", f"{c['form']} {c['strength']}".strip()),
             ("Производитель", c["manufacturer"]), ("Страна", c["country"] or "—"),
             ("Регистрационный номер", c["reg_number"] or "—"), ("Референтная цена сейчас", c["price"]),
             ("Аналогов той же дозы в реестре", str(c["peers_n"]))]
    for k, v in facts:
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(w * 0.38, 6, k, border=1)
        pdf.set_font("DejaVu", "", 9)
        pdf.cell(w * 0.62, 6, str(v)[:70], border=1, **nl)
    pdf.ln(3)

    if c["history"]:
        pdf.set_font("DejaVu", "B", 11)
        pdf.multi_cell(w, 6, "История референтной цены", **nl)
        pdf.set_font("DejaVu", "", 9)
        changes, prev = [], None
        for h in c["history"]:
            if prev is None or h["price"] != prev:
                changes.append(h)
            prev = h["price"]
        for h in changes:
            pdf.cell(w * 0.3, 5.5, h["date"], border=1)
            pdf.cell(w * 0.3, 5.5, f"{h['price']:,.2f} {h['currency']}".replace(",", " ").replace(".", ","), border=1, **nl)
        pdf.set_font("DejaVu", "", 8)
        pdf.multi_cell(w, 4.5, f"Показаны только даты, когда цена менялась; всего снимков: {len(c['history'])}.", **nl)
        pdf.ln(3)

    if c["peers"]:
        pdf.set_font("DejaVu", "B", 11)
        pdf.multi_cell(w, 6, "Аналоги той же дозы: риск пересмотра", **nl)
        cols = [w * 0.28, w * 0.4, w * 0.16, w * 0.16]
        pdf.set_font("DejaVu", "B", 8)
        for t, cw in zip(("Бренд", "Производитель", "Цена", "Риск"), cols, strict=True):
            pdf.cell(cw, 6, t, border=1)
        pdf.ln()
        pdf.set_font("DejaVu", "", 8)
        for p in c["peers"]:
            for t, cw in zip((p["title"][:28], p["manufacturer"][:42], p["price"], f"{p['p_cut']:.0%}"), cols, strict=True):
                pdf.cell(cw, 5.5, t, border=1)
            pdf.ln()
        pdf.ln(3)

    pdf.set_font("DejaVu", "", 8)
    pdf.multi_cell(w, 4.5, c["method"], **nl)
    return bytes(pdf.output())


def product_xlsx_bytes(c: dict) -> bytes:
    wb = Workbook()
    head, fill = Font(bold=True), PatternFill("solid", fgColor="DDE6FA")
    ws = wb.active
    ws.title = "Итог"
    rows = [("Препарат", c["title"]), ("Описание", c["subtitle"]), ("Производитель", c["manufacturer"]),
            ("Дата данных", c["asof"]), ("Уровень риска", c["level_label"]), ("Вероятность пересмотра", c["p_cut"]),
            ("Что это значит", c["meaning"]), ("Что делать", c["todo"]),
            *[("Почему так", r) for r in c["reasons"]],
            ("Референтная цена", c["price"]), ("Регистрационный номер", c["reg_number"]),
            ("Аналогов той же дозы", c["peers_n"]), ("Как считается", c["method"])]
    for k, v in rows:
        ws.append([k, v])
        ws.cell(ws.max_row, 1).font = head
    ws["B6"].number_format = "0%"
    ws2 = wb.create_sheet("История цены")
    ws2.append(["Дата", "Референтная цена", "Валюта"])
    for h in c["history"]:
        ws2.append([h["date"], h["price"], h["currency"]])
    ws3 = wb.create_sheet("Аналоги")
    ws3.append(["Бренд", "Производитель", "Цена", "Вероятность пересмотра", "Уровень риска"])
    for p in c["peers"]:
        ws3.append([p["title"], p["manufacturer"], p["price"], p["p_cut"], p["level_label"]])
        ws3.cell(ws3.max_row, 4).number_format = "0%"
    for sheet in wb.worksheets:
        if sheet.title != "Итог":
            for cell in sheet[1]:
                cell.font, cell.fill = head, fill
        for i, col in enumerate(sheet.columns, start=1):
            sheet.column_dimensions[get_column_letter(i)].width = min(80, max(12, max(len(str(x.value or "")) for x in col) + 2))
        for row in sheet.iter_rows():
            for x in row:
                x.alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
