// Графики на чистом SVG: без библиотек и CDN, работают офлайн.
// Правила: тонкие линии 2px, маркеры ≥8px с кольцом цвета фона, бары ≤24px со скруглением 4px
// только на конце, сетка — волосяная. Подписи — текстом, не цветом серии. У каждого графика
// есть подсказка при наведении и таблица со всеми значениями (WCAG 2.2: 1.1.1, 1.4.1).

const SVG = "http://www.w3.org/2000/svg";

export function s(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

export function h(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") e.textContent = v;
    else if (k === "class") e.className = v;
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return e;
}

export function text(parent, x, y, str, cls = "lbl", anchor = "start", extra = {}) {
  const t = s("text", { x, y, class: cls, "text-anchor": anchor, ...extra }, parent);
  t.textContent = str;
  return t;
}

export const linear = (d0, d1, r0, r1) => {
  const k = (r1 - r0) / ((d1 - d0) || 1);
  const f = (v) => r0 + (v - d0) * k;
  f.invert = (p) => d0 + (p - r0) / k;
  return f;
};

export function niceTicks(min, max, count = 5) {
  const span = max - min || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => span / st <= count) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

// ---------- подсказка ----------
const tipEl = h("div", { class: "tip", role: "tooltip" });
document.body.appendChild(tipEl);

export const tip = {
  show(x, y, rows) {
    tipEl.replaceChildren(...rows.map((r) => {
      const row = h("div", { class: "row" });
      if (r.key) row.appendChild(h("span", { class: `key-line ${r.key}` }));
      if (r.value !== undefined) row.appendChild(h("span", { class: "tv", text: r.value }));
      if (r.label) row.appendChild(h("span", { class: "tl", text: r.label }));
      return row;
    }));
    tipEl.classList.add("on");
    const w = tipEl.offsetWidth, hh = tipEl.offsetHeight;
    const left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 14));
    const top = y - hh - 14 < 8 ? y + 18 : y - hh - 14;
    tipEl.style.left = `${left}px`;
    tipEl.style.top = `${top}px`;
  },
  hide() { tipEl.classList.remove("on"); },
};

// ---------- таблица ----------
export function tableView(parent, headers, rows, caption = "Таблица значений") {
  parent.querySelector(":scope > details.tbl")?.remove();
  const d = h("details", { class: "tbl" }, [h("summary", { text: caption })]);
  const table = h("table");
  table.appendChild(h("thead", {}, h("tr", {}, headers.map((t) => h("th", { scope: "col", text: t })))));
  const tb = h("tbody");
  for (const r of rows) tb.appendChild(h("tr", {}, r.map((c) => h("td", { text: String(c) }))));
  table.appendChild(tb);
  d.appendChild(h("div", { class: "tbl-wrap" }, table));
  parent.appendChild(d);
}

// ---------- адаптивность: перерисовка при смене ширины ----------
export function mount(el, draw) {
  el._draw = draw;
  if (!el._ro) {
    let last = 0;
    el._ro = new ResizeObserver(() => {
      const w = Math.round(el.clientWidth);
      if (w && Math.abs(w - last) > 4) { last = w; el._draw(w); }
    });
    el._ro.observe(el);
  }
  const w = el.clientWidth || 600;
  draw(w);
}

function svgRoot(el, w, hgt, label) {
  el.querySelector(":scope > svg")?.remove();
  const root = s("svg", { viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label });
  el.prepend(root);
  return root;
}

function pointer(evt, root) {
  const r = root.getBoundingClientRect();
  const k = root.viewBox.baseVal.width / r.width;
  return { x: (evt.clientX - r.left) * k, y: (evt.clientY - r.top) * k, cx: evt.clientX, cy: evt.clientY };
}

// ---------- 1. сегментная полоса цены упаковки ----------
// parts: [{label, value, cls, light}] — сумма затрат; ceiling — потолок цены.
export function priceBar(el, { parts, ceiling, fmt, label }) {
  mount(el, (w) => {
    const total = parts.reduce((a, p) => a + p.value, 0);
    const max = Math.max(ceiling, total) * 1.06;
    const H = 132, top = 44, bh = 28, pad = 0;
    const root = svgRoot(el, w, H, label);
    const x = linear(0, max, pad, w - pad);
    let acc = 0;
    for (const p of parts) {
      const x0 = x(acc), x1 = x(acc + p.value);
      const g = s("g", { tabindex: 0, role: "listitem", "aria-label": `${p.label}: ${fmt(p.value)}` }, root);
      const width = Math.max(0, x1 - x0 - 2);
      s("rect", { x: x0, y: top, width, height: bh, rx: 4, class: p.cls }, g);
      const share = `${Math.round((p.value / ceiling) * 100)}%`;
      if (width > 46) text(g, x0 + 8, top + 19, share, p.light ? "t-on-light" : "t-on");
      const show = (e) => tip.show(e.clientX, e.clientY, [{ value: fmt(p.value), label: `${p.label} · ${share} цены` }]);
      g.addEventListener("pointermove", show);
      g.addEventListener("pointerleave", () => tip.hide());
      g.addEventListener("focus", () => { const b = g.getBoundingClientRect(); show({ clientX: b.left + b.width / 2, clientY: b.top }); });
      g.addEventListener("blur", () => tip.hide());
      acc += p.value;
    }
    // маржа или убыток относительно потолка
    if (total < ceiling) {
      const x0 = x(total), x1 = x(ceiling);
      s("rect", { x: x0, y: top, width: Math.max(2, x1 - x0 - 2), height: bh, rx: 4, class: "f-good" }, root);
    } else {
      const x0 = x(ceiling), x1 = x(total);
      s("rect", { x: x0, y: top - 6, width: Math.max(2, x1 - x0), height: bh + 12, rx: 4, class: "area-loss" }, root);
      const right = x1 + 60 < w;
      text(root, right ? x1 + 6 : x0 - 6, top + bh + 34, "убыток", "lbl-strong", right ? "start" : "end");
    }
    // потолок
    const xc = x(ceiling);
    s("line", { x1: xc, x2: xc, y1: top - 16, y2: top + bh + 16, class: "rule-ink" }, root);
    text(root, xc, top - 22, `потолок ${fmt(ceiling)}`, "lbl-strong", "end");
    s("line", { x1: 0, x2: w, y1: top + bh + 16, y2: top + bh + 16, class: "baseline" }, root);
    const ax = s("g", { class: "ax" }, root);
    for (const t of niceTicks(0, max, Math.max(3, Math.floor(w / 110)))) {
      if (Math.abs(x(t) - xc) < 40) continue;
      text(ax, x(t), top + bh + 34, fmt(t, true), "lbl", t === 0 ? "start" : "middle");
    }
  });
}

// ---------- 2. строки-полосы (HTML) ----------
// rows: [{name, strong, value, cls, valText, tipRows, marker}], max — шкала.
export function barList(el, rows, { max, marker } = {}) {
  const m = max ?? Math.max(...rows.map((r) => r.value), 1e-9);
  const wrap = h("div", { class: "bars", role: "list" });
  for (const r of rows) {
    const nameEl = h("span", { class: "name" });
    if (r.strong) nameEl.appendChild(h("b", { text: r.name })); else nameEl.textContent = r.name;
    if (r.badge) { nameEl.appendChild(document.createTextNode(" ")); nameEl.appendChild(r.badge); }
    const track = h("span", { class: "track" });
    const fill = h("span", { class: `fill ${r.cls || "muted"}` });
    fill.style.width = `${Math.max(0, Math.min(1, r.value / m)) * 100}%`;
    track.appendChild(fill);
    if (marker !== undefined) {
      const t = h("span", { class: "tick-mark", "aria-hidden": "true" });
      t.style.left = `${Math.min(1, marker / m) * 100}%`;
      track.appendChild(t);
    }
    const row = h("div", { class: "bar-row", role: "listitem", tabindex: 0, "aria-label": `${r.name}: ${r.valText}` },
      [nameEl, track, h("span", { class: "val", text: r.valText })]);
    if (r.tipRows) {
      const show = (e) => tip.show(e.clientX, e.clientY, r.tipRows);
      row.addEventListener("pointermove", show);
      row.addEventListener("pointerleave", () => tip.hide());
      row.addEventListener("focus", () => { const b = row.getBoundingClientRect(); show({ clientX: b.right - 80, clientY: b.top }); });
      row.addEventListener("blur", () => tip.hide());
    }
    wrap.appendChild(row);
  }
  el.querySelector(":scope > .bars")?.remove();
  el.prepend(wrap);
}

// ---------- 3. линии во времени / по дням ----------
// series: [{name, cls, points:[{x, y}], step, emphasis}], mode: "nearest" (много серий) | "all" (до 3 серий)
export function lines(el, opt) {
  const { series, height = 340, yFmt, xFmt, xTicks, mode = "all", markers = [], bands = [], yMin, label, endLabels = true } = opt;
  mount(el, (w) => {
    const H = height, m = { t: 16, r: endLabels ? 96 : 16, b: 30, l: 56 };
    const root = svgRoot(el, w, H, label);
    const xs = series.flatMap((se) => se.points.map((p) => +p.x));
    const ys = series.flatMap((se) => se.points.map((p) => p.y));
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const yLo = yMin ?? Math.min(0, ...ys), yHi = Math.max(...ys) * 1.06;
    const x = linear(x0, x1, m.l, w - m.r), y = linear(yLo, yHi, H - m.b, m.t);
    const grid = s("g", { class: "grid" }, root), ax = s("g", { class: "ax" }, root);
    for (const t of niceTicks(yLo, yHi, 5)) {
      s("line", { x1: m.l, x2: w - m.r, y1: y(t), y2: y(t) }, grid);
      text(ax, m.l - 8, y(t) + 4, yFmt(t), "lbl", "end");
    }
    s("line", { x1: m.l, x2: w - m.r, y1: y(yLo), y2: y(yLo), class: "baseline" }, root);
    for (const t of xTicks(x0, x1, w)) text(ax, x(+t), H - 8, xFmt(t), "lbl", "middle");
    for (const b of bands) {
      s("rect", { x: x(+b.from), y: m.t, width: Math.max(1, x(+b.to) - x(+b.from)), height: H - m.t - m.b, class: b.cls }, root);
    }
    for (const mk of markers) {
      const xm = x(+mk.x);
      s("line", { x1: xm, x2: xm, y1: m.t, y2: H - m.b, class: mk.cls || "rule-ink" }, root);
      text(root, xm + (mk.anchor === "end" ? -6 : 6), m.t + 12 + (mk.dy || 0), mk.label, "lbl-strong", mk.anchor || "start");
    }
    const path = (pts, step) => pts.map((p, i) => {
      const X = x(+p.x), Y = y(p.y);
      if (i === 0) return `M${X},${Y}`;
      return step ? `H${X}V${Y}` : `L${X},${Y}`;
    }).join("");
    const order = [...series].sort((a, b) => (a.emphasis ? 1 : 0) - (b.emphasis ? 1 : 0));
    const paths = new Map();
    for (const se of order) {
      if (se.area) s("path", { d: `${path(se.points, se.step)}V${y(yLo)}H${x(+se.points[0].x)}Z`, class: se.area }, root);
      paths.set(se, s("path", { d: path(se.points, se.step), class: `ln ${se.cls}` }, root));
    }
    if (endLabels) {
      const placed = [];
      for (const se of series.filter((q) => q.endLabel)) {
        const p = se.points.at(-1);
        let Y = y(p.y) + 4;
        while (placed.some((q) => Math.abs(q - Y) < 14)) Y += 14;
        placed.push(Y);
        s("circle", { cx: x(+p.x), cy: y(p.y), r: 4, class: se.dot || "mark-ink" }, root);
        text(root, x(+p.x) + 8, Y, se.endLabel, "lbl-strong");
      }
    }
    // слой наведения
    const cross = s("line", { y1: m.t, y2: H - m.b, class: "crosshair", visibility: "hidden" }, root);
    const dots = s("g", {}, root);
    const hit = s("rect", { x: m.l, y: m.t, width: w - m.l - m.r, height: H - m.t - m.b, class: "hit" }, root);
    const at = (se, xv) => {
      let best = se.points[0];
      for (const p of se.points) if (+p.x <= xv) best = p;
      return best;
    };
    hit.addEventListener("pointermove", (e) => {
      const p = pointer(e, root);
      const xv = x.invert(p.x);
      cross.setAttribute("x1", p.x); cross.setAttribute("x2", p.x); cross.setAttribute("visibility", "visible");
      dots.replaceChildren();
      if (mode === "nearest") {
        const yv = y.invert(p.y);
        let best = null, bd = Infinity;
        for (const se of series) {
          if (+se.points[0].x > xv) continue;
          const d = Math.abs(at(se, xv).y - yv);
          if (d < bd) { bd = d; best = se; }
        }
        for (const [se, pe] of paths) pe.classList.toggle("hot", se === best && !se.emphasis);
        if (!best) return tip.hide();
        const pt = at(best, xv);
        s("circle", { cx: p.x, cy: y(pt.y), r: 4, class: best.emphasis ? "mark-ours" : "mark-ink" }, dots);
        const rows = [{ value: yFmt(pt.y, true), label: best.name, key: best.key }];
        const ours = series.find((q) => q.emphasis);
        if (ours && ours !== best && +ours.points[0].x <= xv) rows.push({ value: yFmt(at(ours, xv).y, true), label: ours.name, key: ours.key });
        rows.push({ label: xFmt(pt.x, true) });
        tip.show(p.cx, p.cy, rows);
      } else {
        const rows = [];
        for (const se of series) {
          const pt = at(se, xv);
          s("circle", { cx: p.x, cy: y(pt.y), r: 4, class: se.dot || "mark-ink" }, dots);
          rows.push({ value: yFmt(pt.y, true), label: se.name, key: se.key });
        }
        rows.push({ label: xFmt(xv, true) });
        tip.show(p.cx, p.cy, rows);
      }
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden"); dots.replaceChildren(); tip.hide();
      for (const pe of paths.values()) pe.classList.remove("hot");
    });
  });
}

// ---------- 4. точка с интервалом (AUC по сегментам) ----------
export function dotWhisker(el, rows, { domain = [0.4, 0.9], fmt, label, refLine }) {
  mount(el, (w) => {
    const rowH = 30, m = { t: 8, r: 20, b: 30, l: Math.min(230, w * 0.42) };
    const H = m.t + rows.length * rowH + m.b;
    const root = svgRoot(el, w, H, label);
    const x = linear(domain[0], domain[1], m.l, w - m.r);
    const grid = s("g", { class: "grid" }, root), ax = s("g", { class: "ax" }, root);
    for (const t of niceTicks(domain[0], domain[1], 5)) {
      s("line", { x1: x(t), x2: x(t), y1: m.t, y2: H - m.b }, grid);
      text(ax, x(t), H - 10, fmt(t), "lbl", "middle");
    }
    if (refLine !== undefined) {
      s("line", { x1: x(refLine.v), x2: x(refLine.v), y1: m.t, y2: H - m.b, class: "rule-ink" }, root);
    }
    rows.forEach((r, i) => {
      const cy = m.t + i * rowH + rowH / 2;
      const g = s("g", { tabindex: 0, "aria-label": `${r.name}: ${fmt(r.est)}` }, root);
      s("rect", { x: 0, y: cy - rowH / 2, width: w, height: rowH, class: "hit" }, g);
      text(g, m.l - 12, cy + 4, r.name, r.strong ? "lbl-strong" : "lbl-2", "end");
      s("line", { x1: x(r.lo), x2: x(r.hi), y1: cy, y2: cy, class: `ln ${r.strong ? "ln-ours" : "ln-ink"}`, "stroke-width": 2 }, g);
      if (r.ref !== undefined) s("circle", { cx: x(r.ref), cy, r: 4.5, class: "mark-hollow" }, g);
      s("circle", { cx: x(r.est), cy, r: 6, class: r.strong ? "mark-ours" : "mark-ink" }, g);
      const show = (e) => tip.show(e.clientX, e.clientY, [
        { value: fmt(r.est), label: `${r.name}: модель` },
        { label: `95% интервал ${fmt(r.lo)}–${fmt(r.hi)}` },
        ...(r.ref !== undefined ? [{ value: fmt(r.ref), label: "простое правило" }] : []),
        ...(r.extra || []),
      ]);
      g.addEventListener("pointermove", show);
      g.addEventListener("pointerleave", () => tip.hide());
      g.addEventListener("focus", () => { const b = g.getBoundingClientRect(); show({ clientX: b.left + b.width * 0.6, clientY: b.top }); });
      g.addEventListener("blur", () => tip.hide());
    });
  });
}

// ---------- 5. распределение (две гистограммы контуром) ----------
export function histogram(el, { series, bins = 40, domain, fmt, label, zero = 0, height = 220 }) {
  mount(el, (w) => {
    const H = height, m = { t: 14, r: 12, b: 30, l: 12 };
    const root = svgRoot(el, w, H, label);
    const [d0, d1] = domain;
    const bw = (d1 - d0) / bins;
    const counts = series.map((se) => {
      const c = new Array(bins).fill(0);
      for (const v of se.values) {
        const i = Math.min(bins - 1, Math.max(0, Math.floor((v - d0) / bw)));
        c[i] += 1;
      }
      return c.map((n) => n / se.values.length);
    });
    const yMax = Math.max(...counts.flat()) * 1.1;
    const x = linear(d0, d1, m.l, w - m.r), y = linear(0, yMax, H - m.b, m.t);
    const ax = s("g", { class: "ax" }, root);
    // зона убытка
    s("rect", { x: x(d0), y: m.t, width: x(zero) - x(d0), height: H - m.t - m.b, class: "area-loss" }, root);
    text(root, x(zero) - 6, m.t + 12, "убыток", "lbl-strong", "end");
    series.forEach((se, k) => {
      let d = `M${x(d0)},${y(0)}`;
      counts[k].forEach((c, i) => { d += `V${y(c)}H${x(d0 + (i + 1) * bw)}`; });
      d += `V${y(0)}Z`;
      s("path", { d, class: se.area }, root);
      s("path", { d, class: `ln ${se.cls}` }, root);
    });
    s("line", { x1: x(zero), x2: x(zero), y1: m.t, y2: H - m.b, class: "rule-loss" }, root);
    s("line", { x1: m.l, x2: w - m.r, y1: y(0), y2: y(0), class: "baseline" }, root);
    for (const t of niceTicks(d0, d1, Math.max(4, Math.floor(w / 90)))) text(ax, x(t), H - 8, fmt(t), "lbl", "middle");
    const cross = s("line", { y1: m.t, y2: H - m.b, class: "crosshair", visibility: "hidden" }, root);
    const hit = s("rect", { x: m.l, y: m.t, width: w - m.l - m.r, height: H - m.t - m.b, class: "hit" }, root);
    hit.addEventListener("pointermove", (e) => {
      const p = pointer(e, root);
      const v = x.invert(p.x);
      const i = Math.min(bins - 1, Math.max(0, Math.floor((v - d0) / bw)));
      cross.setAttribute("x1", p.x); cross.setAttribute("x2", p.x); cross.setAttribute("visibility", "visible");
      tip.show(p.cx, p.cy, [
        ...series.map((se, k) => ({ value: `${Math.round(counts[k][i] * 1000) / 10}%`, label: se.name, key: se.key })),
        { label: `маржа ${fmt(d0 + i * bw)} … ${fmt(d0 + (i + 1) * bw)}` },
      ]);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hide(); });
  });
}

// ---------- 6. облако стратегий ----------
export function scatter(el, { points, chosen, current, xFmt, yFmt, label, hline, height = 260 }) {
  mount(el, (w) => {
    const H = height, m = { t: 14, r: 16, b: 32, l: 52 };
    const root = svgRoot(el, w, H, label);
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const xMax = Math.max(...xs) * 1.03, yLo = Math.min(...ys, 0), yHi = Math.max(...ys) * 1.08;
    const x = linear(0, xMax, m.l, w - m.r), y = linear(yLo, yHi, H - m.b, m.t);
    const grid = s("g", { class: "grid" }, root), ax = s("g", { class: "ax" }, root);
    for (const t of niceTicks(yLo, yHi, 5)) {
      s("line", { x1: m.l, x2: w - m.r, y1: y(t), y2: y(t) }, grid);
      text(ax, m.l - 8, y(t) + 4, yFmt(t), "lbl", "end");
    }
    for (const t of niceTicks(0, xMax, Math.max(3, Math.floor(w / 100)))) text(ax, x(t), H - 10, xFmt(t), "lbl", "middle");
    if (hline !== undefined) {
      s("line", { x1: m.l, x2: w - m.r, y1: y(hline.v), y2: y(hline.v), class: "rule-ink" }, root);
      text(root, m.l + 6, y(hline.v) - 6, hline.label, "lbl-strong", "start");
    }
    const g = s("g", {}, root);
    for (const p of points) s("circle", { cx: x(p.x), cy: y(p.y), r: 2.5, class: p.ok ? "mark-good" : "mark-muted", opacity: p.ok ? 0.85 : 0.55 }, g);
    if (current) {
      s("circle", { cx: x(current.x), cy: y(current.y), r: 6, class: "mark-ink" }, root);
      text(root, x(current.x) + 10, y(current.y) + 4, "сейчас", "lbl-strong");
    }
    if (chosen) {
      s("circle", { cx: x(chosen.x), cy: y(chosen.y), r: 8, class: "mark-ours" }, root);
      text(root, x(chosen.x) + 12, y(chosen.y) - 8, "план", "lbl-strong");
    }
    const hl = s("circle", { r: 6, class: "mark-ink", visibility: "hidden" }, root);
    const hit = s("rect", { x: m.l, y: m.t, width: w - m.l - m.r, height: H - m.t - m.b, class: "hit" }, root);
    hit.addEventListener("pointermove", (e) => {
      const p = pointer(e, root);
      let best = null, bd = Infinity;
      for (const q of points) {
        const d = (x(q.x) - p.x) ** 2 + (y(q.y) - p.y) ** 2;
        if (d < bd) { bd = d; best = q; }
      }
      if (!best || bd > 900) { hl.setAttribute("visibility", "hidden"); return tip.hide(); }
      hl.setAttribute("cx", x(best.x)); hl.setAttribute("cy", y(best.y)); hl.setAttribute("visibility", "visible");
      tip.show(p.cx, p.cy, best.tip);
    });
    hit.addEventListener("pointerleave", () => { hl.setAttribute("visibility", "hidden"); tip.hide(); });
  });
}
