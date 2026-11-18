// Форматирование чисел по-русски (ISO 4217 для валют, ISO 8601 для дат на входе).
const nf = (d) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: d, minimumFractionDigits: d });
const N0 = nf(0), N1 = nf(1), N2 = nf(2);

export const int = (x) => N0.format(Math.round(x));
export const dec1 = (x) => N1.format(x);
export const dec2 = (x) => N2.format(x);
export const pct = (x, d = 1) => `${nf(d).format(x * 100)}%`;
export const pct0 = (x) => `${N0.format(x * 100)}%`;
export const pp = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${N1.format(Math.abs(x))} п.п.`;
export const sum = (x) => `${int(x)} сум`;
export const usd = (x) => `$${N2.format(x)}`;
export const bn = (x) => `${N2.format(x / 1e9)} млрд`;
export const mln = (x) => `${N0.format(x / 1e6)} млн`;
export const money = (x) => (Math.abs(x) >= 1e9 ? `${bn(x)} сум` : `${mln(x)} сум`);
export const signed = (x, f) => `${x < 0 ? "−" : ""}${f(Math.abs(x))}`;

const MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
export const monthLabel = (d) => `${MONTHS[d.getMonth()]} ${String(d.getFullYear()).slice(2)}`;
export const dateRu = (iso) => {
  const d = new Date(iso);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
};
