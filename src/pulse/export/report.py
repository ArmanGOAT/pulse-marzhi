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


FREQ = {1: "раз в год", 2: "раз в полгода", 3: "раз в 4 месяца", 4: "раз в 3 месяца", 6: "раз в 2 месяца", 12: "каждый месяц"}


def _bn_words(x: float) -> str:
    return f"{_bn(x)} сум" if abs(x) >= 1e9 else f"{x / 1e6:,.0f} млн сум".replace(",", " ")


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:]


def _steps(res: Result) -> list[tuple[int, str, float]]:
    """Меры оптимизатора → конкретные шаги «что сделать»: (через сколько месяцев готово, текст, вложения)."""

    from pulse.config import load_levers
    a, lv = load_assumptions(), load_levers()
    levers = res.plan["levers"]
    out = []
    for act in res.actions:
        k = act["kind"]
        if k == "shipments":
            n = int(levers["shipments"])
            batch = a.v("market.annual_volume") / n
            weeks = max(1, round(a.v(f"costs.transit_months_{levers['mode']}") * 4.3))
            text = (f"Заказывать у завода по {batch:,.0f} упаковок {FREQ.get(n, f'{n} раз в год')}. "
                    f"Отправлять заказ за {weeks} нед. до нужной даты прихода.").replace(",", " ")
        elif k == "procurement":
            o = lv.procurement[levers["procurement"]]
            text = (f"Договориться с 2–3 другими импортёрами о совместной закупке и получить у завода скидку {o.discount:.0%}."
                    if levers["procurement"] == "joint" else
                    f"Подписать с заводом годовой контракт со скидкой {o.discount:.0%} и оплатой вперёд.")
        elif k == "pooled":
            text = "Отправлять груз вместе с другими импортёрами: доставка дешевле на 30%, оформление — на 50%."
        elif k == "mode":
            text = f"Перевозить груз так: {_lower_first(lv.modes[levers['mode']].label)}."
        elif k == "digital":
            text = f"Перевести {levers['digital']:.0%} встреч с врачами в онлайн. Начать с пилота в части регионов."
        elif k == "loc":
            text = f"Запустить: {_lower_first(lv.localization[levers['loc']].label)}. Сюда входит перерегистрация."
        elif k == "prepare":
            from datetime import timedelta
            months_ru = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре",
                         "октябре", "ноябре", "декабре"]
            wave = date.today() + timedelta(days=30 * int(res.inputs.get("revision_month") or 3))
            text = (f"Ожидаемая волна пересмотра цен — примерно в {months_ru[wave.month - 1]} {wave.year}. "
                    "За 2 месяца до неё заказывать партии не больше чем на месяц продаж; большую партию — после волны.")
        elif k == "protect":
            text = "Добавить в контракт с заводом пункт о защите цены: " + act["text"].split(":", 1)[-1].strip()
        else:
            text = act["text"]
        out.append((int(act["months"]), text, float(act["invest"])))
    return sorted(out, key=lambda x: x[0])


def _action_page(pdf: FPDF, w: float, res: Result, brand: str) -> None:
    """Первая страница плана: только действия с датами и количествами, без процентов и терминов."""
    from datetime import timedelta
    a = load_assumptions()
    today = date.today()
    nl = {"new_x": "LMARGIN", "new_y": "NEXT", "align": "L"}
    pdf.set_font("DejaVu", "B", 20)
    pdf.multi_cell(w, 10, f"Что сделать — {brand}", **nl)
    pdf.set_font("DejaVu", "", 9)
    pdf.multi_cell(w, 5, f"Обновлено: {today.strftime('%d.%m.%Y')}. Отмечайте выполненное галочкой.", **nl)
    pdf.ln(3)
    if not res.feasible:
        pdf.set_text_color(208, 59, 59)
        pdf.set_font("DejaVu", "B", 11)
        pdf.multi_cell(w, 6, "Внимание: в этом бюджете нельзя достичь цели без риска. Ниже — лучший доступный план.", **nl)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    for months, text, invest in _steps(res):
        when = "Сразу" if months == 0 else f"До {(today + timedelta(days=30 * months)).strftime('%d.%m.%Y')}"
        y = pdf.get_y()
        if y > pdf.h - 40:
            pdf.add_page()
            y = pdf.get_y()
        pdf.rect(pdf.l_margin, y + 1.2, 4.5, 4.5)  # квадрат для галочки
        pdf.set_xy(pdf.l_margin + 8, y)
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(36, 7, when)
        pdf.set_xy(pdf.l_margin + 46, y)
        pdf.set_font("DejaVu", "", 11)
        pdf.multi_cell(w - 46, 6.5, text, **nl)
        if invest > 0:
            pdf.set_x(pdf.l_margin + 46)
            pdf.set_font("DejaVu", "", 9)
            pdf.multi_cell(w - 46, 5, f"Вложения: {_bn_words(invest)}", **nl)
        pdf.ln(3)

    safety = a.v("costs.safety_stock_min_months") * a.v("market.annual_volume") / 12
    pdf.ln(2)
    pdf.set_font("DejaVu", "B", 12)
    pdf.multi_cell(w, 7, "НЕЛЬЗЯ", **nl)
    pdf.set_font("DejaVu", "", 11)
    pdf.multi_cell(w, 6, f"• Держать на складе меньше {safety:,.0f} упаковок — это месяц продаж. "
                         "Иначе пациенты могут остаться без лекарства.".replace(",", " "), **nl)
    if any(not act["durable"] for act in res.actions):
        pdf.multi_cell(w, 6, "• Считать скидку завода прибылью навсегда: при пересмотре цены она может уйти в новый потолок.", **nl)

    pdf.ln(2)
    pdf.set_font("DejaVu", "B", 12)
    pdf.multi_cell(w, 7, "СТОП-СИГНАЛ", **nl)
    pdf.set_font("DejaVu", "", 11)
    pdf.multi_cell(w, 6, "Если на странице «Проверить препарат» у этого препарата появится «высокий риск» "
                         "(обновляется раз в месяц) — за 2 месяца до волны пересмотра заказывайте партии "
                         "не больше чем на месяц продаж и свяжитесь с заводом о защите цены.", **nl)

    cu, pl = res.current, res.plan
    pdf.ln(4)
    pdf.set_font("DejaVu", "B", 12)
    pdf.multi_cell(w, 7, "ИТОГ", **nl)
    pdf.set_font("DejaVu", "", 11)
    pdf.multi_cell(w, 6, f"Вложить {_bn_words(pl['invest'])}. Прибыль за обычный год: было {_bn_words(cu['profit_p10_p50_p90'][1])}, "
                         f"станет {_bn_words(pl['profit_p10_p50_p90'][1])}. Убыточных лет: было {cu['p_loss'] * 100:.0f} из 100, "
                         f"станет {pl['p_loss'] * 100:.0f} из 100.", **nl)
    pdf.ln(3)
    pdf.set_font("DejaVu", "", 8)
    pdf.multi_cell(w, 4.5, "Подробный расчёт, сравнение и допущения — на следующей странице.", **nl)


def pdf_bytes(res: Result, brand: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin
    _action_page(pdf, w, res, brand)
    pdf.add_page()

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


# ---------- отчёты по препаратам (страница «Проверить препарат») ----------

NL = {"new_x": "LMARGIN", "new_y": "NEXT", "align": "L"}
LEVEL_RGB = {"high": (208, 59, 59), "mid": (170, 110, 0), "low": (0, 120, 0)}


def _pdf() -> tuple[FPDF, float]:
    pdf = FPDF(format="A4")
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_title("Пульс маржи — отчёт")
    pdf.add_page()
    return pdf, pdf.w - pdf.l_margin - pdf.r_margin


def _h(pdf: FPDF, w: float, text: str, size: int = 12) -> None:
    pdf.ln(2)
    pdf.set_font("DejaVu", "B", size)
    pdf.multi_cell(w, 7, text, **NL)
    pdf.set_font("DejaVu", "", 10)


def _p(pdf: FPDF, w: float, text: str, size: int = 10, h: float = 5.5) -> None:
    pdf.set_font("DejaVu", "", size)
    pdf.multi_cell(w, h, text, **NL)


def _table(pdf: FPDF, cols: list[float], head: tuple, rows: list[tuple], size: int = 8) -> None:
    pdf.set_font("DejaVu", "B", size)
    for t, cw in zip(head, cols, strict=True):
        pdf.cell(cw, 6, t, border=1)
    pdf.ln()
    pdf.set_font("DejaVu", "", size)
    for row in rows:
        if pdf.get_y() > pdf.h - 25:
            pdf.add_page()
        for t, cw in zip(row, cols, strict=True):
            limit = int(cw / (size * 0.19))  # примерно столько символов влезает в ячейку
            t = str(t)
            pdf.cell(cw, 5.5, t if len(t) <= limit else t[: limit - 1] + "…", border=1)
        pdf.ln()


def _money(v: float, ccy: str) -> str:
    if ccy == "UZS":
        return f"{v:,.0f} сум".replace(",", " ")
    return f"{v:,.2f} {ccy}".replace(",", " ").replace(".", ",")


def _verdict(pdf: FPDF, w: float, c: dict) -> None:
    pdf.set_font("DejaVu", "B", 18)
    pdf.set_text_color(*LEVEL_RGB[c["level"]])
    pdf.multi_cell(w, 9, f"{c['level_label']}: {c['p_cut']:.0%}", **NL)
    pdf.set_text_color(0, 0, 0)
    _p(pdf, w, f"{c['meaning']} В среднем за волну цену снижают {c['band_check']['all']} препаратам из 100.", 11, 6)


def _product_body(pdf: FPDF, w: float, c: dict) -> None:
    _verdict(pdf, w, c)
    tc = c["typical_cut"]
    _h(pdf, w, "Что делать")
    for i, step in enumerate(c["steps"], 1):
        _p(pdf, w, f"{i}. {step}")
    _h(pdf, w, "Почему такая оценка")
    for r in c["reasons"]:
        _p(pdf, w, f"• {r}")
    _h(pdf, w, "Если цену снизят")
    _p(pdf, w, f"Типичное снижение — на {tc['value']:.0%} ({tc['basis']}). Сильнее всего это бьёт по товару, "
               "который уже лежит на складе: он куплен по старой цене, а продавать его придётся по новой.")
    ps = c["price_story"]
    if ps["snapshots"]:
        _h(pdf, w, "Цена в реестре")
        drop = f", это на {abs(ps['from_max']):.0%} ниже максимума {_money(ps['max'], ps['currency'])}" if ps["from_max"] < -0.005 else ""
        _p(pdf, w, f"Сейчас {_money(ps['now'], ps['currency'])}{drop}. За {ps['snapshots']} ежемесячных снимков "
                   f"цена менялась {ps['changes']} раз.")
        changes, prev = [], None
        for hh in c["history"]:
            if prev is None or hh["price"] != prev:
                changes.append((hh["date"], _money(hh["price"], hh["currency"])))
            prev = hh["price"]
        _table(pdf, [w * 0.3, w * 0.3], ("Дата", "Цена с этой даты"), changes, 9)
    _h(pdf, w, "Рынок этого вещества")
    im = c["inn_market"]
    pos = (f" Дешевле этой упаковки {c['cheaper_peers']} из {c['peers_n']} аналогов той же дозы."
           if c["peers_n"] and c["cheaper_peers"] is not None else "")
    _p(pdf, w, f"В реестре {im['packages']} упаковок вещества «{c['inn']}» от {im['brands']} брендов; "
               f"раньше цену снижали у {im['prior_cut_share']:.0%} из них.{pos}")
    if c["peers"]:
        _table(pdf, [w * 0.26, w * 0.42, w * 0.16, w * 0.16], ("Аналог той же дозы", "Производитель", "Цена", "Риск"),
               [(p["title"], p["manufacturer"], p["price"], f"{p['p_cut']:.0%}") for p in c["peers"]])
    _h(pdf, w, "О препарате")
    _table(pdf, [w * 0.36, w * 0.64], ("Поле", "Значение"), [
        ("Действующее вещество", c["inn"]), ("Форма и дозировка", f"{c['form']} {c['strength']}".strip()),
        ("Упаковка в реестре", c["product"] or "—"), ("Производитель", c["manufacturer"]),
        ("Страна", c["country"] or "—"), ("Регистрационный номер", c["reg_number"] or "—"),
        ("Референтная цена сейчас", c["price"])], 8)


def _method(pdf: FPDF, w: float, text: str) -> None:
    _h(pdf, w, "Как считается и ограничения", 10)
    _p(pdf, w, text + " Уровни: низкий — ниже порога, при котором подготовка окупается (около 9%); средний — "
                      "от этого порога до 30%; высокий — от 30%. Данные: госреестр референтных цен mpa.uz, "
                      "курсы ЦБ РУз. Отчёт сформирован системой «Пульс маржи».", 8, 4.5)


def product_pdf_bytes(c: dict) -> bytes:
    pdf, w = _pdf()
    pdf.set_font("DejaVu", "B", 20)
    pdf.multi_cell(w, 10, c["title"], **NL)
    _p(pdf, w, f"{c['subtitle']} · {c['manufacturer']}")
    _p(pdf, w, f"Прогноз на ближайшую волну пересмотра цен · данные госреестра mpa.uz на {c['asof']} · "
               f"отчёт от {date.today().isoformat()}", 9)
    pdf.ln(3)
    _product_body(pdf, w, c)
    _method(pdf, w, c["method"])
    return bytes(pdf.output())


def batch_pdf_bytes(res: dict) -> bytes:
    pdf, w = _pdf()
    s = res["summary"]
    pdf.set_font("DejaVu", "B", 20)
    pdf.multi_cell(w, 10, "Проверка списка препаратов", **NL)
    _p(pdf, w, f"Данные госреестра mpa.uz на {res['asof']} · отчёт от {date.today().isoformat()}", 9)
    pdf.ln(2)
    _p(pdf, w, f"Строк в списке: {s['lines']}. Найдено упаковок: {s['packages']}. Высокий риск: {s['high']}, "
               f"средний: {s['mid']}, низкий: {s['low']}. Не найдено в реестре: {s['not_found']}.", 11, 6)
    _h(pdf, w, "Сводка: сначала самые рискованные")
    found = sorted((m | {"query": r["query"]} for r in res["rows"] for m in r["matches"]), key=lambda m: -m["p_cut"])
    _table(pdf, [w * 0.2, w * 0.24, w * 0.14, w * 0.1, w * 0.32],
           ("Препарат", "Вещество и доза", "Цена", "Риск", "Что делать"),
           [(m["title"], m["subtitle"].split(" · ")[0] + " " + m["strength"], m["price"], f"{m['p_cut']:.0%}", m["short"])
            for m in found], 7)
    missing = [r for r in res["rows"] if r["status"] in ("absent", "otc", "gone")]
    if missing:
        _h(pdf, w, "Не найдено в реестре")
        for r in missing:
            _p(pdf, w, f"• «{r['query']}» — {r['message']}", 9, 5)
    risky = [c for c in sorted(res["cards"], key=lambda c: -c["p_cut"]) if c and c["level"] != "low"][:30]
    if risky:
        _h(pdf, w, "Подробно: препараты со средним и высоким риском")
        for c in risky:
            if pdf.get_y() > pdf.h - 70:
                pdf.add_page()
            pdf.set_font("DejaVu", "B", 12)
            pdf.multi_cell(w, 7, f"{c['title']} — {c['subtitle']}", **NL)
            _p(pdf, w, f"{c['level_label']}: {c['p_cut']:.0%}. {c['todo']}")
            for r in c["reasons"]:
                _p(pdf, w, f"• {r}", 9, 5)
            pdf.ln(2)
    if res["cards"]:
        _method(pdf, w, res["cards"][0]["method"])
    return bytes(pdf.output())


def _style(wb: Workbook) -> None:
    head, fill = Font(bold=True), PatternFill("solid", fgColor="DDE6FA")
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font, cell.fill = head, fill
        for i, col in enumerate(sheet.columns, start=1):
            sheet.column_dimensions[get_column_letter(i)].width = min(70, max(10, max(len(str(x.value or "")) for x in col) + 2))
        for row in sheet.iter_rows():
            for x in row:
                x.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"


def _xlsx(wb: Workbook) -> bytes:
    _style(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def product_xlsx_bytes(c: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Итог"
    ws.append(["Поле", "Значение"])
    tc, ps = c["typical_cut"], c["price_story"]
    for k, v in [("Препарат", c["title"]), ("Описание", c["subtitle"]), ("Производитель", c["manufacturer"]),
                 ("Дата данных", c["asof"]), ("Уровень риска", c["level_label"]), ("Вероятность пересмотра", c["p_cut"]),
                 ("Что это значит", c["meaning"]), *[(f"Что делать, шаг {i}", st) for i, st in enumerate(c["steps"], 1)],
                 *[("Почему так", r) for r in c["reasons"]],
                 ("Типичное снижение", tc["value"]), ("Основа оценки снижения", tc["basis"]),
                 ("Цена сейчас", c["price"]), ("Падение от максимума", ps["from_max"]),
                 ("Изменений цены", ps["changes"]), ("Аналогов той же дозы", c["peers_n"]),
                 ("Регистрационный номер", c["reg_number"]), ("Как считается", c["method"])]:
        ws.append([k, v])
        if k in ("Вероятность пересмотра", "Типичное снижение", "Падение от максимума"):
            ws.cell(ws.max_row, 2).number_format = "0%"
    ws2 = wb.create_sheet("История цены")
    ws2.append(["Дата", "Референтная цена", "Валюта"])
    for hh in c["history"]:
        ws2.append([hh["date"], hh["price"], hh["currency"]])
    ws3 = wb.create_sheet("Аналоги")
    ws3.append(["Бренд", "Производитель", "Цена", "Вероятность пересмотра", "Уровень риска"])
    for p in c["peers"]:
        ws3.append([p["title"], p["manufacturer"], p["price"], p["p_cut"], p["level_label"]])
        ws3.cell(ws3.max_row, 4).number_format = "0%"
    return _xlsx(wb)


def batch_xlsx_bytes(res: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Строка из списка", "Препарат", "Вещество, форма, страна", "Дозировка", "Производитель", "Цена",
               "Вероятность пересмотра", "Уровень риска", "Что делать"])
    for r in res["rows"]:
        for m in r["matches"]:
            ws.append([r["query"], m["title"], m["subtitle"], m["strength"], m["manufacturer"], m["price"],
                       m["p_cut"], m["level_label"], m["short"]])
            ws.cell(ws.max_row, 7).number_format = "0%"
    ws2 = wb.create_sheet("Не найдено")
    ws2.append(["Строка из списка", "Почему"])
    for r in res["rows"]:
        if r["status"] in ("absent", "otc", "gone"):
            ws2.append([r["query"], r["message"]])
    ws3 = wb.create_sheet("Подробно")
    ws3.append(["Препарат", "Дозировка", "Уровень риска", "Вероятность", "Что делать", "Почему так", "Типичное снижение"])
    for c in res["cards"]:
        if c:
            ws3.append([c["title"], c["strength"], c["level_label"], c["p_cut"], " ".join(c["steps"]),
                        " ".join(c["reasons"]), c["typical_cut"]["value"]])
            ws3.cell(ws3.max_row, 4).number_format = "0%"
            ws3.cell(ws3.max_row, 7).number_format = "0%"
    ws4 = wb.create_sheet("Как читать")
    ws4.append(["Термин", "Объяснение"])
    for k, v in [("Вероятность пересмотра", "Оценка модели, что цену снизят на 10% и больше в ближайшую волну."),
                 ("Низкий риск", "Ниже порога, при котором подготовка окупается (около 9%)."),
                 ("Средний риск", "От этого порога до 30%."), ("Высокий риск", "От 30%."),
                 ("Проверка", "На ноябрьской волне 2026 цену снизили у 26 из 100 с высоким риском, 19 со средним, 9 с низким."),
                 ("Источник", f"Госреестр референтных цен mpa.uz на {res['asof']}.")]:
        ws4.append([k, v])
    return _xlsx(wb)
