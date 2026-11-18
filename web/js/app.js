// Пульс маржи — страница демо. Все цифры приходят из API (то же ядро, что в тестах и выгрузке).
import * as F from "./fmt.js";
import { barList, dotWhisker, h, histogram, lines, priceBar, scatter, tableView } from "./charts.js";

const $ = (id) => document.getElementById(id);
const OUR = "Сербан";

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r;
}
const json = async (path, body) => (await api(path, body)).json();

function fail(el, err) {
  el.replaceChildren(h("p", { class: "alert", text: `Нет ответа от сервера (${err.message}). Запустите make serve.` }));
}

function stat(label, value, sub, opts = {}) {
  const v = h("div", { class: "stat-value" });
  if (opts.from) { v.appendChild(h("span", { class: "stat-from", text: `${opts.from} → ` })); }
  v.appendChild(h("span", { class: opts.cls || "", text: value }));
  return h("div", { class: "stat" }, [h("div", { class: "stat-label", text: label }), v, sub ? h("div", { class: "stat-delta", text: sub }) : null]);
}

const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const title = (s) => s.replace("®", "").trim().toLowerCase().replace(/(^|[\s-])\S/g, (c) => c.toUpperCase());

function segControl(id, onChange) {
  const g = $(id);
  g.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    setSeg(id, b.dataset.v);
    onChange(b.dataset.v);
  });
}
function setSeg(id, v) {
  for (const b of $(id).querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.v === String(v)));
}
const segValue = (id) => $(id).querySelector('[aria-pressed="true"]').dataset.v;

// ================= 1. Упаковка =================
const pack = {
  async render(fx) {
    $("fx").value = fx;
    $("fx-out").textContent = `${F.int(fx)} сум`;
    const box = $("pack-bar");
    box.classList.add("loading");
    let d;
    try { d = await json(`/api/pack?fx=${fx}`); } catch (e) { return fail(box, e); } finally { box.classList.remove("loading"); }
    const usd = d.items.filter((i) => i.currency === "USD").reduce((a, i) => a + i.uzs_per_pack, 0);
    const uzs = d.items.filter((i) => i.currency !== "USD").reduce((a, i) => a + i.uzs_per_pack, 0);
    const neg = d.margin_uzs < 0;
    $("pack-margin-sum").textContent = neg ? `убыток ${F.int(-d.margin_uzs)} сум` : `${F.int(d.margin_uzs)} сум`;
    $("pack-margin-sum").className = neg ? "loss-text" : "";
    $("pack-usd-share").textContent = F.pct0(d.usd_share);
    $("pack-sens").textContent = `${F.dec1(Math.abs(d.pp_per_1pct_fx))} п.п.`;
    const fmt = (v, axis) => (axis ? (v ? `${F.int(v / 1000)} тыс` : "0") : F.sum(v));
    priceBar(box, {
      parts: [
        { label: "Затраты в долларах", value: usd, cls: "f-usd" },
        { label: "Затраты в сумах", value: uzs, cls: "f-uzs", light: true },
      ],
      ceiling: d.price, fmt, label: `Цена ${F.sum(d.price)}: затраты в долларах ${F.sum(usd)}, в сумах ${F.sum(uzs)}, маржа ${F.sum(d.margin_uzs)}`,
    });
    tableView(box, ["Часть цены", "сум/уп.", "доля цены"], [
      ["Затраты в долларах", F.int(usd), F.pct(usd / d.price)],
      ["Затраты в сумах", F.int(uzs), F.pct(uzs / d.price)],
      [neg ? "Убыток" : "Маржа", F.int(d.margin_uzs), F.pct(d.margin)],
    ]);
    const items = [...d.items].sort((a, b) => b.uzs_per_pack - a.uzs_per_pack);
    const list = $("pack-items");
    barList(list, items.map((i) => ({
      name: i.item, value: i.uzs_per_pack, cls: i.currency === "USD" ? "usd" : "uzs",
      valText: F.int(i.uzs_per_pack),
      tipRows: [{ value: F.sum(i.uzs_per_pack), label: i.item }, { label: `${F.pct(i.share_of_price)} цены · платится в ${i.currency === "USD" ? "долларах" : "сумах"}` }],
    })));
    tableView(list, ["Статья", "Валюта", "сум/уп.", "доля цены"], items.map((i) => [i.item, i.currency, F.int(i.uzs_per_pack), F.pct(i.share_of_price)]));
    $("pack-stats").replaceChildren(
      stat("Маржа", F.pct(d.margin), `${F.sum(d.margin_uzs)} с упаковки`, { cls: neg ? "loss-text" : "" }),
      stat("Прибыль за год", F.signed(d.annual_profit, F.bn), "150 000 упаковок", { cls: neg ? "loss-text" : "" }),
      stat("Курс, при котором маржа = 0", F.int(d.breakeven_fx), `запас ${F.pct((d.breakeven_fx / fx) - 1)} от курса`),
    );
  },
  init() {
    const go = debounce((v) => pack.render(+v), 40);
    $("fx").addEventListener("input", (e) => { $("fx-out").textContent = `${F.int(+e.target.value)} сум`; go(e.target.value); });
    return pack.render(12000);
  },
};

// ================= 2. История цен =================
const market = {
  async init() {
    const box = $("mk-chart");
    let d;
    try { d = await json("/api/market/rivaroxaban"); } catch (e) { return fail(box, e); }
    const imported = d.brands.filter((b) => !b.local);
    const drops = (b) => {
      let maxStep = 0;
      for (let i = 1; i < b.points.length; i++) maxStep = Math.max(maxStep, 1 - b.points[i].usd / b.points[i - 1].usd);
      return maxStep;
    };
    const total = (b) => 1 - b.points.at(-1).usd / Math.max(...b.points.map((p) => p.usd));
    const cut = imported.filter((b) => drops(b) >= 0.05);
    $("mk-cut").textContent = cut.length;
    $("mk-n").textContent = imported.length;
    const deepest = [...cut].sort((a, b) => total(b) - total(a))[0];
    const med = cut.map(total).sort((a, b) => a - b)[Math.floor(cut.length / 2)];
    const ours = d.brands.find((b) => b.brand === OUR);
    $("mk-stats").replaceChildren(
      stat("Типичное снижение", `−${F.pct0(med)}`, "медиана от максимума цены", { cls: "loss-text" }),
      stat("Самое глубокое", `−${F.pct0(total(deepest))}`, deepest.brand, { cls: "loss-text" }),
      stat(`${OUR}`, `${F.usd(ours.points.at(-1).usd)}`, `было ${F.usd(ours.points[0].usd)}`),
    );
    const toPts = (b) => b.points.map((p) => ({ x: new Date(p.date), y: p.usd }));
    const series = d.brands.map((b) => ({
      name: `${b.brand}${b.local ? " (местный)" : ""}`, points: toPts(b), step: true,
      cls: b.brand === OUR ? "ln-ours" : "ln-muted", emphasis: b.brand === OUR, key: b.brand === OUR ? "k-ours" : "k-muted",
      endLabel: b.brand === OUR ? `${OUR} ${F.usd(b.points.at(-1).usd)}` : null, dot: "mark-ours",
    }));
    lines(box, {
      series, height: 380, mode: "nearest", label: "История референтных цен ривароксабана 20 мг по брендам",
      yFmt: (v, full) => (full ? F.usd(v) : `$${F.int(v)}`),
      xFmt: (t, full) => (full ? F.dateRu(new Date(+t)) : F.monthLabel(new Date(+t))),
      xTicks: (a, b) => { const out = []; const d0 = new Date(a); d0.setDate(1); for (let t = new Date(d0); +t <= b; t.setMonth(t.getMonth() + 6)) out.push(new Date(t)); return out; },
      markers: [{ x: new Date("2026-03-28"), label: "новая формула", cls: "rule-loss", anchor: "end" }],
    });
    $("mk-source").textContent = `${d.brands[0].points.length} снимков реестра mpa.uz`;
    tableView(box, ["Бренд", "Происхождение", "Первая цена, $", "Последняя, $", "Изменение"],
      d.brands.map((b) => [b.brand, b.local ? "местный" : "импорт", F.dec2(b.points[0].usd), F.dec2(b.points.at(-1).usd), F.pct(b.points.at(-1).usd / b.points[0].usd - 1)]));
  },
};

// ================= 3. Радар =================
const radar = {
  data: null,
  async init() {
    try { radar.data = await json("/api/radar"); } catch (e) { return fail($("radar-list"), e); }
    const r = radar.data, t = r.metrics.test.models, v = r.metrics.validation.models;
    const segC = r.segments["C: ноябрь 2026"].segments;
    const by = Object.fromEntries(segC.map((x) => [x.segment, x]));
    const ci = by["Кардио, импортные"];
    $("radar-stats").replaceChildren(
      stat("Импортные кардиопрепараты", F.dec2(ci.auc_model), `AUC, интервал ${F.dec2(ci.auc_ci95[0])}–${F.dec2(ci.auc_ci95[1])}; правило ${F.dec2(ci.auc_rule)}`, { cls: "ours-text" }),
      stat("Все рецептурные", F.dec2(t.woe_logit.auc), `AUC; правило ${F.dec2(t.rule.auc)}, июль ${F.dec2(v.woe_logit.auc)}`),
      stat("Выгода на препарат", F.mln(t.woe_logit.value_per_drug_uzs), `против ${F.mln(t.rule.value_per_drug_uzs)} при «готовиться всегда»`),
    );
    const pick = ["Все рецептурные", "Импортные", "Местные", "Таблетки и капсулы", "Кардио (ATC B01 и C)", "Кардио, импортные", "Аналогам уже снижали цену", "Аналогам ещё не снижали"];
    const rows = pick.filter((k) => by[k]).map((k) => ({
      name: k, est: by[k].auc_model, lo: by[k].auc_ci95[0], hi: by[k].auc_ci95[1], ref: by[k].auc_rule, strong: k === "Кардио, импортные",
      extra: [{ label: `${F.int(by[k].n)} препаратов, снижали ${F.pct0(by[k].cut_share)}` }],
    }));
    const seg = $("radar-seg");
    dotWhisker(seg, rows, { domain: [0.4, 0.9], fmt: F.dec2, label: "AUC модели и правила по сегментам", refLine: { v: 0.5 } });
    tableView(seg, ["Сегмент", "Препаратов", "AUC модели", "95% интервал", "AUC правила"],
      segC.map((x) => [x.segment, F.int(x.n), F.dec2(x.auc_model), `${F.dec2(x.auc_ci95[0])}–${F.dec2(x.auc_ci95[1])}`, F.dec2(x.auc_rule)]));
    segControl("radar-mode", (m) => radar.show(m));
    radar.show("case");
  },
  show(mode) {
    setSeg("radar-mode", mode);
    const r = radar.data;
    if (!r) return;
    const list = $("radar-list");
    const nameCount = {};
    const label = (b) => title(b.trademark);
    if (mode === "case") {
      const c = r.case["B: июль 2026"];
      const imp = c.brands.filter((b) => !b.local);
      const brands = [...imp.slice(0, 10), ...imp.slice(10).filter((b) => b.actual_cut)];
      const cuts = imp.filter((b) => b.actual_cut);
      const hits = cuts.filter((b) => b.rank_in_wave_pct <= 0.08);
      const miss = cuts.filter((b) => b.rank_in_wave_pct > 0.08);
      for (const b of brands) nameCount[label(b)] = (nameCount[label(b)] || 0) + 1;
      $("radar-list-title").textContent = "Июль 2026: прогноз на 1 июня и что случилось";
      $("radar-note").textContent = `Модель обучена только на волне A (до 1 февраля 2026). Из ${cuts.length} брендов ривароксабана, которым потом снизили цену, ${hits.length} оказались в верхних 8% из ${F.int(r.data.waves["B: июль 2026"].rows)} препаратов.`
        + (miss.length ? ` Промах: ${miss.map((b) => `${label(b)} (верхние ${F.pct0(b.rank_in_wave_pct)})`).join(", ")}.` : "");
      barList(list, brands.map((b) => {
        const nm = nameCount[label(b)] > 1 ? `${label(b)} ${b.currency === "USD" ? "$" : ""}${F.dec2(b.base_price)}` : label(b);
        return {
          name: nm, strong: label(b) === OUR, value: b.p_cut, cls: label(b) === OUR ? "ours" : "muted",
          valText: F.pct0(b.p_cut),
          badge: b.actual_cut ? h("span", { class: "chip chip-loss", text: `снизили на ${F.pct0(b.cut_size)}` }) : null,
          tipRows: [{ value: F.pct0(b.p_cut), label: `${nm}: вероятность` }, { label: `в верхних ${F.pct(b.rank_in_wave_pct, 1)} из всех препаратов` },
            { label: b.actual_cut ? `цену снизили на ${F.pct0(b.cut_size)}` : "цену не снижали" }],
        };
      }), { max: 0.8 });
      $("radar-legend").replaceChildren(
        h("span", {}, [h("i", { class: "key k-ours" }), "Сербан"]),
        h("span", {}, [h("i", { class: "key k-muted" }), "другие импортные бренды"]),
        h("span", { class: "chip chip-loss", text: "снизили в июле" }),
      );
      tableView(list, ["Бренд", "Вероятность", "Место в волне", "Снизили?"], c.brands.map((b) => [label(b), F.pct0(b.p_cut), `верхние ${F.pct(b.rank_in_wave_pct)}`, b.actual_cut ? `да, на ${F.pct0(b.cut_size)}` : "нет"]));
    } else {
      const rv = r.rivaroxaban;
      const brands = rv.brands.filter((b) => !b.local);
      for (const b of brands) nameCount[label(b)] = (nameCount[label(b)] || 0) + 1;
      const top = brands.slice(0, 9);
      if (!top.some((b) => label(b) === OUR)) top.push(brands.find((b) => label(b) === OUR));
      $("radar-list-title").textContent = `Прогноз на ${F.dateRu(rv.asof)}: импортные бренды`;
      $("radar-note").textContent = `Порог «готовиться» — ${F.pct0(rv.threshold)}: выше него подготовка окупается. Наведите на бренд, чтобы увидеть причины.`;
      barList(list, top.filter(Boolean).map((b) => {
        const nm = nameCount[label(b)] > 1 ? `${label(b)} $${F.dec2(b.base_usd)}` : label(b);
        return {
          name: nm, strong: label(b) === OUR, value: b.p_cut, cls: label(b) === OUR ? "ours" : b.prepare ? "alt" : "muted",
          valText: F.pct0(b.p_cut),
          tipRows: [{ value: F.pct0(b.p_cut), label: nm }, ...b.reasons.map((x) => ({ label: `• ${x}` })), { label: b.prepare ? "→ готовиться" : "→ готовиться не нужно" }],
        };
      }), { max: 0.8, marker: rv.threshold });
      $("radar-legend").replaceChildren(
        h("span", {}, [h("i", { class: "key k-ours" }), "Сербан"]),
        h("span", {}, [h("i", { class: "key k-alt" }), "выше порога: готовиться"]),
        h("span", {}, [h("i", { class: "key k-muted" }), "ниже порога"]),
        h("span", {}, [h("i", { class: "key-line k-ink" }), "порог"]),
      );
      tableView(list, ["Бренд", "Вероятность", "Готовиться", "Причины"], brands.map((b) => [label(b), F.pct0(b.p_cut), b.prepare ? "да" : "нет", b.reasons.join("; ")]));
    }
  },
};

// ================= 4. Склад =================
const stock = {
  async render(day) {
    $("rev").value = day;
    $("rev-out").textContent = `${day}-й (≈ ${F.int(day / 30.4 + 1)}-й месяц)`;
    const box = $("st-chart");
    box.classList.add("loading");
    let d;
    try { d = await json(`/api/stock?revision_day=${day}&p_cut=0.5&cut=0.2`); } catch (e) { return fail(box, e); } finally { box.classList.remove("loading"); }
    const years = d.loss_if_cut.regular / d.annual_profit_now;
    $("st-loss").textContent = `${F.bn(d.loss_if_cut.regular)} сум`;
    $("st-lead").textContent = `Это ${F.dec1(years)} ${years >= 2 && years < 5 ? "года" : "лет"} прибыли при нынешней марже. Товар уже куплен в долларах по старой цене, а продавать его придётся по новой.`;
    const k = (v) => v / 1000;
    lines(box, {
      height: 340, mode: "all", yMin: 0, endLabels: false, label: "Запас на складе по дням: как сейчас и с подготовкой",
      series: [
        { name: "как сейчас", cls: "ln-ink", dot: "mark-ink", key: "k-ink", points: d.days.map((x, i) => ({ x, y: k(d.regular[i]) })) },
        { name: "с подготовкой", cls: "ln-ours", dot: "mark-ours", key: "k-ours", emphasis: true, points: d.days.map((x, i) => ({ x, y: k(d.prepared[i]) })) },
      ],
      yFmt: (v, full) => (full ? `${F.int(v * 1000)} уп.` : F.int(v)),
      xFmt: (t, full) => (full ? `день ${F.int(t)}` : `${F.int(t / 30.4)} мес.`),
      xTicks: () => [0, 61, 122, 182, 243, 304, 365],
      markers: [{ x: d.revision_day, label: "пересмотр", cls: "rule-loss", anchor: d.revision_day > 250 ? "end" : "start" }],
    });
    tableView(box, ["", "Как сейчас", "С подготовкой"], [
      ["Поставок в год", d.shipments.regular, d.shipments.prepared],
      ["На складе в день пересмотра, уп.", F.int(d.stock_on_revision.regular), F.int(d.stock_on_revision.prepared)],
      ["Потеря при снижении на 20%", F.money(d.loss_if_cut.regular), F.money(d.loss_if_cut.prepared)],
    ]);
    $("st-stats").replaceChildren(
      stat("На складе в день пересмотра", F.int(d.stock_on_revision.prepared / 1000), "тыс. упаковок", { from: F.int(d.stock_on_revision.regular / 1000) }),
      stat("Потеря при снижении на 20%", F.bn(d.loss_if_cut.prepared), "сум", { from: F.bn(d.loss_if_cut.regular), cls: "good-text" }),
      stat("Когда готовиться", `> ${F.pct0(d.decision.threshold_prob)}`, `вероятность пересмотра; подготовка стоит ${F.mln(d.decision.prep_cost)} сум`),
    );
  },
  init() {
    const go = debounce((v) => stock.render(+v), 60);
    $("rev").addEventListener("input", (e) => { $("rev-out").textContent = `${e.target.value}-й`; go(e.target.value); });
    return stock.render(120);
  },
};

// ================= 5. План =================
const plan = {
  body() {
    const p = segValue("pl-pcut");
    return {
      target_margin: +segValue("pl-target"), budget: +segValue("pl-budget"),
      p_cut: p === "radar" ? null : +p, cip_binds: $("pl-cip").checked, brand: $("pl-brand").value || OUR,
    };
  },
  async render() {
    const hist = $("pl-hist"), sc = $("pl-scatter");
    for (const el of [hist, sc]) el.classList.add("loading");
    const body = plan.body();
    let d;
    try { d = await json("/api/plan", body); } catch (e) { return fail(hist, e); } finally { for (const el of [hist, sc]) el.classList.remove("loading"); }
    const cu = d.current, pl = d.plan;
    const summary = $("pl-summary");
    const heroes = h("div", { class: "hero-row" }, [
      h("div", {}, [h("div", { class: "stat-label", text: "Маржа после пересмотра" }),
        h("div", { class: "hero" }, [h("small", { text: `${F.pct(cu.margin_durable)} → ` }), h("span", { class: d.feasible ? "ours-text" : "", text: F.pct(pl.margin_durable) })])]),
      h("div", {}, [h("div", { class: "stat-label", text: "Риск убытка за год" }),
        h("div", { class: "hero" }, [h("small", { text: `${F.pct0(cu.p_loss)} → ` }), h("span", { class: pl.p_loss <= 0.1 ? "good-text" : "loss-text", text: F.pct0(pl.p_loss) })])]),
    ]);
    const stats = h("div", { class: "stats" }, [
      stat("Вложения", F.bn(pl.invest), "сум"),
      stat("Срок до эффекта", `${pl.months} мес.`, ""),
      stat("Прибыль за год", F.bn(pl.profit_p10_p50_p90[1]),
        `плохой год ${F.signed(pl.profit_p10_p50_p90[0], F.bn)}; сейчас ${F.signed(cu.profit_p10_p50_p90[1], F.bn)} и ${F.signed(cu.profit_p10_p50_p90[0], F.bn)}`),
    ]);
    const msg = h("p", { class: d.feasible ? "note" : "alert", text: `${d.message} Вероятность пересмотра ${F.pct0(d.p_cut)} (${d.p_cut_source}). Перебрано ${F.int(d.n_strategies)} стратегий × 2000 сценариев за ${F.dec2(d.seconds)} с.` });
    summary.replaceChildren(h("div", { class: "slide-text" }, [heroes, stats, msg]));
    $("pl-actions").replaceChildren(...d.actions.map((a) => h("li", { class: "action" }, [
      h("div", {}, [h("div", { class: "action-text", text: a.text }),
        h("div", { class: "action-meta", text: `${a.invest ? F.money(a.invest) : "без вложений"} · ${a.months} мес. · ${a.note}` })]),
      h("div", { class: "action-eff" }, [
        h("div", { text: a.effect_pp === null ? "защита" : F.pp(a.effect_pp) }),
        h("span", { class: `chip ${a.durable ? "chip-good" : "chip-warn"}`, text: a.durable ? "останется" : "может уйти в потолок" }),
      ]),
    ])));
    const lo = -0.3, hi = 0.35;
    const clip = (xs) => xs.map((v) => Math.max(lo, Math.min(hi - 1e-9, v)));
    histogram(hist, {
      label: "Распределение годовой маржи: как сейчас и с планом", domain: [lo, hi], bins: 52, fmt: (v) => F.pct0(v),
      series: [
        { name: "как сейчас", cls: "ln-ink", area: "area-muted", key: "k-ink", values: clip(cu.margin_samples) },
        { name: "с планом", cls: "ln-ours", area: "area-ours", key: "k-ours", values: clip(pl.margin_samples) },
      ],
    });
    tableView(hist, ["", "Как сейчас", "С планом"], [
      ["Маржа P10", F.pct(cu.margin_p10_p50_p90[0]), F.pct(pl.margin_p10_p50_p90[0])],
      ["Маржа P50", F.pct(cu.margin_p10_p50_p90[1]), F.pct(pl.margin_p10_p50_p90[1])],
      ["Маржа P90", F.pct(cu.margin_p10_p50_p90[2]), F.pct(pl.margin_p10_p50_p90[2])],
      ["Вероятность убытка", F.pct0(cu.p_loss), F.pct0(pl.p_loss)],
    ]);
    const st = d.strategies;
    const pts = st.invest.map((x, i) => ({
      x: x / 1e9, y: st.margin_durable[i], ok: st.feasible[i],
      tip: [{ value: F.pct(st.margin_durable[i]), label: "маржа после пересмотра" }, { value: F.bn(x), label: "вложения" }, { value: F.pct0(st.p_loss[i]), label: "риск убытка" }],
    }));
    $("pl-scatter-title").textContent = `${F.int(d.n_strategies)} стратегий: вложения и маржа после пересмотра`;
    scatter(sc, {
      points: pts, label: "Облако стратегий: вложения против маржи после пересмотра",
      chosen: d.feasible ? { x: pl.invest / 1e9, y: pl.margin_durable } : null, current: { x: 0, y: cu.margin_durable },
      xFmt: (v) => `${F.int(v)} млрд`, yFmt: (v) => F.pct0(v), hline: { v: body.target_margin, label: `цель ${F.pct0(body.target_margin)}` },
    });
    tableView(sc, ["", "Стратегий"], [["Всего", F.int(d.n_strategies)], ["Показано на графике", F.int(pts.length)], ["Проходят все правила (из показанных)", F.int(pts.filter((p) => p.ok).length)]]);
  },
  async download(fmt, btn) {
    btn.setAttribute("aria-busy", "true");
    try {
      const r = await api(`/api/export/${fmt}`, plan.body());
      const blob = await r.blob();
      const a = h("a", { href: URL.createObjectURL(blob), download: `plan-${new Date().toISOString().slice(0, 10)}.${fmt}` });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (e) { alert(`Не удалось выгрузить: ${e.message}`); } finally { btn.removeAttribute("aria-busy"); }
  },
  async init() {
    try {
      const m = await json("/api/meta");
      const sel = $("pl-brand");
      for (const b of m.brands) sel.appendChild(h("option", { value: b, text: title(b) }));
      sel.value = m.brands.includes(m.default_brand) ? m.default_brand : m.brands[0];
    } catch { $("pl-brand").appendChild(h("option", { value: OUR, text: OUR })); }
    for (const id of ["pl-target", "pl-budget", "pl-pcut"]) segControl(id, () => plan.render());
    $("pl-cip").addEventListener("change", () => plan.render());
    $("pl-brand").addEventListener("change", () => plan.render());
    $("exp-pdf").addEventListener("click", (e) => plan.download("pdf", e.currentTarget));
    $("exp-xlsx").addEventListener("click", (e) => plan.download("xlsx", e.currentTarget));
    return plan.render();
  },
  async set({ target, budget, pcut, cip }) {
    if (target !== undefined) setSeg("pl-target", target);
    if (budget !== undefined) setSeg("pl-budget", budget);
    if (pcut !== undefined) setSeg("pl-pcut", pcut);
    if (cip !== undefined) $("pl-cip").checked = cip;
    return plan.render();
  },
};

// ================= режим презентации =================
const go = (id) => document.getElementById(id).scrollIntoView({ behavior: "smooth", block: "start" });
const SCRIPT = [
  { say: "Упаковка: маржа 2,2%", run: () => { go("pack"); return pack.render(12000); } },
  { say: "Курс +3%: маржа уходит в минус", run: () => { go("pack"); return pack.render(12400); } },
  { say: "Пересмотры реальны", run: () => go("market") },
  { say: "Радар: проверка на июле", run: () => { go("radar"); radar.show("case"); } },
  { say: "Радар: прогноз сейчас", run: () => { go("radar"); radar.show("now"); } },
  { say: "Склад и пересмотр", run: () => { go("stock"); return stock.render(120); } },
  { say: "План на 15%", run: () => { go("plan"); return plan.set({ target: "0.15", budget: "10e9", pcut: "radar", cip: false }); } },
  { say: "Скидки уходят в потолок", run: () => { go("plan"); return plan.set({ cip: true }); } },
];
let step = -1;
function hud() {
  $("hud-text").textContent = `${step + 1} / ${SCRIPT.length} · ${SCRIPT[step]?.say ?? ""}`;
  $("hud-dots").replaceChildren(...SCRIPT.map((_, i) => h("i", { class: i === step ? "on" : "" })));
}
function goto(i) {
  step = Math.max(0, Math.min(SCRIPT.length - 1, i));
  hud();
  SCRIPT[step].run();
}
function present(on) {
  document.body.classList.toggle("presenting", on);
  if (on) {
    document.documentElement.requestFullscreen?.().catch(() => {});
    goto(0);
  } else if (document.fullscreenElement) document.exitFullscreen?.();
}
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea")) return;
  const on = document.body.classList.contains("presenting");
  if (!on) {
    if (e.key === "p" || e.key === "з") present(true);
    return;
  }
  if ([" ", "ArrowRight", "ArrowDown", "PageDown", "Enter"].includes(e.key)) { e.preventDefault(); goto(step + 1); }
  else if (["ArrowLeft", "ArrowUp", "PageUp", "Backspace"].includes(e.key)) { e.preventDefault(); goto(step - 1); }
  else if (e.key === "Escape") present(false);
});
document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) document.body.classList.remove("presenting"); });

// ================= тема и навигация =================
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("pulse-theme"); } catch { /* приватный режим */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $("theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("pulse-theme", next); } catch { /* ничего */ }
  });
}
function initNav() {
  const links = [...document.querySelectorAll(".steps a")];
  const io = new IntersectionObserver((es) => {
    for (const e of es) if (e.isIntersecting) {
      for (const a of links) a.toggleAttribute("aria-current", a.getAttribute("href") === `#${e.target.id}`);
      for (const a of links) if (a.hasAttribute("aria-current")) a.setAttribute("aria-current", "step");
    }
  }, { rootMargin: "-45% 0px -45% 0px" });
  for (const sec of document.querySelectorAll(".slide")) io.observe(sec);
}

$("present").addEventListener("click", () => present(true));
initTheme();
initNav();
await Promise.allSettled([pack.init(), market.init(), radar.init(), stock.init(), plan.init()]);
document.body.dataset.ready = "1";
