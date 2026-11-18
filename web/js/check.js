// Страница «Проверить препарат»: поиск → карточка простым языком → отчёт PDF или Excel.
// Названия препаратов приходят из реестра, поэтому в DOM они попадают только через textContent.
import * as F from "./fmt.js";
import { h } from "./charts.js";

const $ = (id) => document.getElementById(id);
const input = $("ck-q"), list = $("ck-results"), status = $("ck-status"), card = $("ck-card");

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) {
    let msg = "Сервер не ответил. Попробуйте ещё раз.";
    try { msg = (await r.json()).detail || msg; } catch { /* не JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

const riskPill = (level, label) => h("span", { class: `risk risk-${level}`, text: label });

// ---------- поиск ----------
let seq = 0;
async function search(q) {
  const my = ++seq;
  q = q.trim();
  if (q.length < 2) { list.replaceChildren(); status.textContent = ""; return; }
  status.textContent = "Ищем…";
  try {
    const { results } = await getJSON(`/api/products?q=${encodeURIComponent(q)}`);
    if (my !== seq) return;  // пришёл ответ на старый запрос
    status.textContent = results.length
      ? `Найдено: ${results.length}${results.length === 20 ? " (показаны первые 20, уточните название)" : ""}. Выберите упаковку.`
      : "Ничего не нашли. Проверьте написание или введите действующее вещество, например «ривароксабан».";
    list.replaceChildren(...results.map((r) => h("li", {}, h("button", { class: "ck-result", type: "button", "data-id": r.id }, [
      h("span", {}, [h("b", { text: r.title }), h("span", { class: "sub", text: `${r.subtitle} · ${r.price}` })]),
      riskPill(r.level, `${r.level_label} · ${F.pct0(r.p_cut)}`),
    ]))));
  } catch (e) {
    status.textContent = e.message;
  }
}

let timer;
input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => search(input.value), 200); });
for (const b of document.querySelectorAll(".ck-example")) {
  b.addEventListener("click", () => { input.value = b.textContent; search(input.value); input.focus(); });
}
list.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-id]");
  if (b) open(b.dataset.id);
});

// ---------- карточка ----------
function scale(c) {
  const { mid_from: a, high_from: b } = c.bands;
  const p = c.p_cut;
  const pos = p < a ? (p / a) / 3 : p < b ? 1 / 3 + ((p - a) / (b - a)) / 3 : 2 / 3 + Math.min(1, (p - b) / 0.4) / 3;
  const marker = h("div", { class: "marker", "aria-hidden": "true" });
  marker.style.left = `${Math.max(0.5, Math.min(99.5, pos * 100))}%`;
  return h("div", {}, [
    h("div", { class: "scale", role: "img", "aria-label": `${c.level_label}, ${F.pct0(p)}` },
      [h("div", { class: "z-low" }), h("div", { class: "z-mid" }), h("div", { class: "z-high" }), marker]),
    h("div", { class: "scale-labels" }, [
      h("span", { text: `Низкий: до ${F.pct0(a)}` }), h("span", { text: "Средний" }), h("span", { text: `Высокий: от ${F.pct0(b)}` }),
    ]),
  ]);
}

function compare(c) {
  const row = (label, n, ours) => {
    const bar = h("div", { class: `bar${ours ? " ours" : ""}` });
    bar.style.width = `${n * 2.5}%`;
    return h("div", { class: "row" }, [h("span", { text: label }), h("div", {}, bar), h("b", { text: `${n} из 100` })]);
  };
  return h("div", { class: "ck-block" }, [
    h("h3", { text: "Насколько можно верить" }),
    h("p", { class: "note", text: "Проверка на ноябрьской волне 2026 года, которую модель не видела: у скольких препаратов цену действительно снизили." }),
    h("div", { class: "ck-compare" }, [
      row(`С таким же уровнем риска («${c.level_label.toLowerCase()}»)`, c.band_check.this, true),
      row("Все рецептурные препараты", c.band_check.all, false),
    ]),
  ]);
}

function priceStory(c) {
  const hist = c.history;
  if (!hist.length) return null;
  const changes = [];
  for (let i = 1; i < hist.length; i++) if (hist[i].price !== hist[i - 1].price) changes.push([hist[i - 1], hist[i]]);
  const money = (x) => (x.currency === "UZS" ? `${F.int(x.price)} сум` : `${F.dec2(x.price)} ${x.currency}`);
  let text;
  if (!changes.length) text = `Цена не менялась с ${F.dateRu(hist[0].date)}: ${money(hist.at(-1))}.`;
  else {
    const [a, b] = changes.at(-1);
    const d = b.price / a.price - 1;
    text = `С ${F.dateRu(hist[0].date)} цена менялась ${changes.length} ${changes.length === 1 ? "раз" : changes.length < 5 ? "раза" : "раз"}. `
      + `Последнее изменение — ${F.dateRu(b.date)}: ${money(a)} → ${money(b)} (${d > 0 ? "+" : "−"}${F.pct0(Math.abs(d))}).`;
  }
  return h("div", { class: "ck-block" }, [h("h3", { text: "История цены" }), h("p", { text })]);
}

async function open(id) {
  card.hidden = false;
  card.replaceChildren(h("p", { class: "note", text: "Загружаем…" }));
  let c;
  try { c = await getJSON(`/api/products/${encodeURIComponent(id)}`); } catch (e) {
    card.replaceChildren(h("p", { class: "alert", text: e.message }));
    return;
  }
  history.replaceState(null, "", `#${id}`);
  const facts = [["Действующее вещество", c.inn], ["Форма и доза", `${c.form} ${c.strength}`.trim()],
    ["Производитель", c.manufacturer], ["Страна", c.country || "—"], ["Цена в реестре сейчас", c.price],
    ["Аналогов той же дозы", String(c.peers_n)]];
  card.replaceChildren(...[
    h("div", {}, [h("h2", { text: c.title }), h("p", { class: "sub", text: c.subtitle })]),
    h("div", { class: "ck-verdict" }, [
      h("div", { class: `big ${c.level}`, text: `${c.level_label}: ${F.pct0(c.p_cut)}` }),
      h("p", { text: `${c.meaning} В среднем за волну цену снижают ${c.band_check.all} препаратам из 100.` }),
      scale(c),
    ]),
    h("div", { class: "ck-todo" }, [h("h3", { text: "Что делать" }), h("p", { text: c.todo })]),
    h("div", { class: "ck-block" }, [h("h3", { text: "Почему так" }), h("ul", {}, c.reasons.map((r) => h("li", { text: r })))]),
    h("div", { class: "ck-actions" }, [
      h("a", { class: "btn btn-primary", href: `/api/products/${c.id}/report/pdf`, text: "Скачать отчёт PDF" }),
      h("a", { class: "btn", href: `/api/products/${c.id}/report/xlsx`, text: "Скачать Excel" }),
    ]),
    compare(c),
    priceStory(c),
    h("div", { class: "ck-block" }, [h("h3", { text: "О препарате" }),
      h("dl", { class: "ck-facts" }, facts.map(([k, v]) => h("div", {}, [h("dt", { text: k }), h("dd", { text: v })])))]),
    c.peers.length ? h("div", { class: "ck-block" }, [
      h("h3", { text: "Аналоги той же дозы" }),
      h("ul", { class: "ck-peers" }, c.peers.slice(0, 5).map((p) => h("li", {}, h("button", { type: "button", "data-id": p.id }, [
        h("span", { text: `${p.title} · ${p.price}` }), h("span", { class: "note", text: `${p.level_label}, ${F.pct0(p.p_cut)}` }),
      ])))),
    ]) : null,
    h("details", {}, [h("summary", { class: "note", text: "Как считается" }), h("p", { class: "note", text: c.method })]),
  ].filter(Boolean));
  for (const b of card.querySelectorAll(".ck-peers button")) b.addEventListener("click", () => open(b.dataset.id));
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

if (location.hash.length > 1) open(location.hash.slice(1));
