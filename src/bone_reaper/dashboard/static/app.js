"use strict";

// Read-only view over /api/state. This file only ever issues GET requests. Text from reports goes
// through textContent. innerHTML is used only for the fixed icon strings below.

const POLL_MS = 5000;
const RUNNING_WITHIN_MS = 20000;

const ICONS = {
  overview: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
  chart: '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  minus: '<path d="M5 12h14"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
};

const TONE_ICON = { good: "check", critical: "x", warning: "alert", neutral: "minus" };

const STRATEGY = {
  scanner_only: "Scanner only",
  taker_pair_research: "Pair taker",
  passive_maker: "Passive quoting, $10 stop",
  passive_constrained_hedge: "Quoting + hedges, $10 stop",
  passive_maker_no_loss_stop: "Passive quoting, no stop",
  passive_hedge_no_loss_stop: "Quoting + hedges, no stop",
};
const SCENARIO = {
  pessimistic: "Pessimistic queue",
  central: "Central queue",
  optimistic: "Optimistic queue",
  pessimistic_2x_latency: "Pessimistic, 2x latency",
  central_2x_latency: "Central, 2x latency",
  central_higher_fees: "Central, 1.5x fees",
  central_late_hedge: "Central, later hedge",
};
const PARTITION = {
  all: "All markets",
  train: "Train",
  validation: "Validation",
  untouched_test: "Untouched test",
};
const VARIANT = {
  midpoint_baseline: "Midpoint baseline",
  random_walk_model: "Random-walk model",
  logistic_model: "Logistic model",
  logistic_model_delayed_twap_5s: "Logistic, TWAP delayed 5s",
};

const RUN_COMMANDS = {
  compare: "uv run bone-reaper compare --input data/raw --config config/paper.yaml --output reports/compare --workers 4",
  scan: "uv run bone-reaper scan --input data/raw --config config/read_only.yaml --report reports/scan_real/scan.json",
  forecast: "uv run python scripts/run_forecast_research.py --markets 2500 --benchmark-markets 300",
  audit: "uv run python scripts/run_model_audit.py --input data/raw --workers 5",
  performance: "uv run python scripts/profile_performance.py",
  record: "uv run bone-reaper record --config config/read_only.yaml --output data/raw --binance --seconds 3600",
  paper: "uv run bone-reaper paper --config config/paper.yaml --output reports/paper_run2 --seconds 900",
  shadow: "uv run bone-reaper shadow --config config/shadow.yaml --output reports/shadow_run2 --seconds 900",
};

const ui = {
  state: null,
  paused: false,
  lastOk: null,
  failed: false,
  signature: "",
  partition: "all",
  strategy: "any",
  recorder: null,
  session: null,
  sorts: {},
};

// --- DOM helpers ---------------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  node.append(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

function s(tag, attrs, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  applyAttrs(node, attrs);
  node.append(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

function applyAttrs(node, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
}

function icon(name) {
  const node = s("svg", { class: "icon", viewBox: "0 0 24 24", "aria-hidden": "true" });
  node.innerHTML = ICONS[name];
  return node;
}

// --- Formatting ----------------------------------------------------------------------------------

const MINUS = "−";
const NA = "n/a";

function num(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value, digits = 2) {
  const n = num(value);
  if (n === null) return NA;
  const text = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${n < 0 ? MINUS : ""}$${text}`;
}

function pnl(value, digits = 2) {
  const n = num(value);
  if (n === null) return NA;
  return `${n > 0 ? "+" : ""}${money(n, digits)}`;
}

function cents(value) {
  const n = num(value);
  if (n === null) return NA;
  return `${n < 0 ? MINUS : n > 0 ? "+" : ""}${Math.abs(n * 100).toFixed(1)}¢`;
}

function count(value) {
  const n = num(value);
  return n === null ? NA : Math.round(n).toLocaleString();
}

function compact(value) {
  const n = num(value);
  if (n === null) return NA;
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

function pct(value, digits = 0) {
  const n = num(value);
  return n === null ? NA : `${(n * 100).toFixed(digits)}%`;
}

function score(value) {
  const n = num(value);
  return n === null ? NA : n.toFixed(4);
}

function words(text) {
  const clean = String(text ?? "").replaceAll("_", " ").replace(/^roi/i, "ROI");
  return clean.charAt(0).toUpperCase() + clean.slice(1);
}

function clock(ms) {
  const n = num(ms);
  if (n === null) return NA;
  return new Date(n).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function when(ms) {
  const n = num(ms);
  if (n === null) return NA;
  return new Date(n).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function span(ms) {
  const n = Math.max(0, num(ms) ?? 0);
  if (n < 1500) return `${Math.round(n)} ms`;
  const secs = Math.round(n / 1000);
  if (secs < 90) return `${secs} s`;
  const mins = Math.round(secs / 60);
  if (mins < 90) return `${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

function slugTime(slug) {
  const match = /(\d{10})$/.exec(slug || "");
  return match ? when(Number(match[1]) * 1000) : slug;
}

// --- Components ----------------------------------------------------------------------------------

function pill(tone, text) {
  return h("span", { class: `pill ${tone}` }, icon(TONE_ICON[tone] || "minus"), text);
}

function tile(label, value, sub) {
  return h("div", { class: "tile" },
    h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value),
    sub ? h("div", { class: "tile-sub" }, sub) : null);
}

function card(title, intro, ...children) {
  return h("section", { class: "card" }, h("h2", {}, title), intro ? h("p", { class: "card-note" }, intro) : null,
    h("div", { class: intro ? "" : "card-body" }, children));
}

function source(section) {
  if (!section || !section.available) return null;
  const age = section.modified_utc_ms ? `, updated ${when(section.modified_utc_ms)}` : "";
  return h("p", { class: "source" }, `Source: ${section.path}${age}`);
}

function note(text, name = "info") {
  return h("p", { class: "note" }, icon(name), h("span", {}, text));
}

function missing(section, command) {
  return card("No report yet",
    `${section.path} does not exist or could not be read, so nothing is shown here rather than a blank result.`,
    command ? h("pre", { class: "cmd" }, command) : null);
}

function command(text) {
  return h("pre", { class: "cmd" }, text);
}

function table(id, columns, rows, caption) {
  const sort = ui.sorts[id];
  let shown = rows;
  if (sort) {
    const column = columns.find((c) => c.key === sort.key);
    if (column) {
      const value = column.sort || ((row) => row[column.key]);
      shown = [...rows].sort((a, b) => {
        const [x, y] = [value(a), value(b)];
        const [nx, ny] = [num(x), num(y)];
        const order = nx !== null && ny !== null ? nx - ny : String(x ?? "").localeCompare(String(y ?? ""));
        return sort.dir * order;
      });
    }
  }
  const header = columns.map((column) => {
    const className = column.num ? "num" : null;
    if (column.noSort) return h("th", { scope: "col", class: className }, column.label);
    const active = sort && sort.key === column.key;
    const button = h("button", {
      class: "sort", type: "button", id: `${id}-sort-${column.key}`,
      onclick: () => {
        ui.sorts[id] = { key: column.key, dir: active && sort.dir === 1 ? -1 : 1 };
        renderView();
      },
    }, column.label, active ? icon(sort.dir === 1 ? "up" : "down") : null);
    return h("th", {
      scope: "col", class: className,
      "aria-sort": active ? (sort.dir === 1 ? "ascending" : "descending") : null,
    }, button);
  });
  const body = shown.map((row) => h("tr", {}, columns.map((column) => {
    const content = column.fmt ? column.fmt(row[column.key], row) : row[column.key];
    return h("td", { class: [column.num ? "num" : "", column.wrap ? "wrap" : ""].join(" ").trim() || null },
      content === null || content === undefined ? NA : content);
  })));
  return h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": caption },
    h("table", {}, h("caption", {}, caption), h("thead", {}, h("tr", {}, header)), h("tbody", {}, body)));
}

// --- Tooltip -------------------------------------------------------------------------------------

function tipContent(title, rows) {
  const box = document.getElementById("tooltip");
  box.replaceChildren(
    title ? h("div", { class: "tip-title" }, title) : "",
    ...rows.map((row) => h("div", { class: "tip-row" },
      row.key ? h("i", { class: `key line ${row.key}` }) : null,
      h("b", {}, row.value),
      row.label ? h("span", {}, row.label) : null)));
  box.hidden = false;
  return box;
}

function placeTip(box, x, y) {
  const rect = box.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - rect.width - 8);
  const top = y + 14 + rect.height > window.innerHeight ? y - rect.height - 10 : y + 14;
  box.style.left = `${Math.max(8, left)}px`;
  box.style.top = `${Math.max(8, top)}px`;
}

function hideTip() {
  document.getElementById("tooltip").hidden = true;
}

function withTip(node, title, rows) {
  node.setAttribute("tabindex", "0");
  node.addEventListener("pointermove", (event) => placeTip(tipContent(title, rows), event.clientX, event.clientY));
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", () => {
    const rect = node.getBoundingClientRect();
    placeTip(tipContent(title, rows), rect.left + rect.width / 2, rect.bottom);
  });
  node.addEventListener("blur", hideTip);
  return node;
}

// --- Charts --------------------------------------------------------------------------------------

function legend(items) {
  return h("div", { class: "legend" }, items.map((item) =>
    h("span", {}, h("i", { class: `key ${item.line ? "line " : ""}${item.cls}` }), item.label)));
}

function niceTicks(min, max, target = 5) {
  if (min === max) { min -= 1; max += 1; }
  const raw = (max - min) / Math.max(1, target - 1);
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = [1, 2, 2.5, 5, 10].find((m) => m * power >= raw) * power;
  const ticks = [];
  for (let t = Math.floor(min / unit) * unit; t <= Math.ceil(max / unit) * unit + unit / 1e6; t += unit) {
    ticks.push(Number(t.toFixed(10)));
  }
  return ticks;
}

function intervalChart(rows, format = pnl) {
  const values = rows.flatMap((r) => [r.mean, r.low ?? r.mean, r.high ?? r.mean]);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values));
  const [lo, hi] = [ticks[0], ticks[ticks.length - 1]];
  const at = (v) => `${((v - lo) / (hi - lo)) * 100}%`;
  const out = [];
  let group = null;
  for (const row of rows) {
    if (row.group && row.group !== group) {
      group = row.group;
      out.push(h("div", { class: "dw-group" }, group));
    }
    const [low, high] = [row.low ?? row.mean, row.high ?? row.mean];
    const position = high < 0 ? "below" : low > 0 ? "above" : "across";
    out.push(withTip(h("div", { class: `dw-row ${position}` },
      h("div", { class: "dw-label" }, row.label),
      h("div", { class: "dw-plot" },
        h("div", { class: "dw-zero", style: { left: at(0) } }),
        h("div", { class: "dw-line", style: { left: at(low), width: `${((high - low) / (hi - lo)) * 100}%` } }),
        h("div", { class: "dw-dot", style: { left: at(row.mean) } })),
      h("div", { class: "dw-value" }, format(row.mean))),
    row.label, [
      { value: format(row.mean), label: "mean per market" },
      { value: `${format(low)} to ${format(high)}`, label: "95% interval" },
      ...(row.extra || []),
    ]));
  }
  out.push(h("div", { class: "dw-axis" }, h("div", {}),
    h("div", { class: "dw-ticks" }, ticks.map((t) => h("span", { style: { left: at(t) } }, format(t, 0)))), h("div", {})));
  return h("div", { class: "dw", role: "group", "aria-label": "Mean result per market with 95 percent interval" }, out);
}

function barRows(rows, options = {}) {
  const max = options.max ?? Math.max(1, ...rows.map((r) => Math.abs(r.value)));
  const hasNegative = rows.some((r) => r.value < 0);
  const hasPositive = rows.some((r) => r.value > 0);
  const lowest = hasNegative ? -max : 0;
  const highest = hasPositive || !hasNegative ? max : 0;
  const range = highest - lowest || 1;
  const zero = ((0 - lowest) / range) * 100;
  return h("div", { class: options.stacked ? "bars stacked" : "bars" }, rows.map((row) => {
    const width = (Math.abs(row.value) / range) * 100;
    const bar = h("div", {
      class: `bar ${row.cls || (row.value < 0 ? "neg" : "pos")}`,
      style: row.value < 0
        ? { left: `${zero - width}%`, width: `${width}%`, borderRadius: "4px 0 0 4px" }
        : { left: `${zero}%`, width: `${width}%`, borderRadius: "0 4px 4px 0" },
    });
    return withTip(h("div", { class: "bar-row" },
      h("div", { class: "bar-label" }, row.label),
      h("div", { class: "bar-track" }, row.value !== 0 ? bar : null,
        hasNegative ? h("div", { class: "bar-zero", style: { left: `${zero}%` } }) : null),
      h("div", { class: "bar-value" }, row.display)),
    row.label, [{ value: row.display, label: row.tipLabel || "" }, ...(row.extra || [])]);
  }));
}

function meter(label, done, total) {
  const [d, t] = [num(done) ?? 0, num(total) ?? 0];
  const share = t > 0 ? d / t : 0;
  return h("div", { class: "meter" },
    h("div", { class: "meter-head" }, h("span", {}, label), h("b", {}, t > 0 ? pct(share, 2) : NA)),
    h("div", { class: "meter-track", role: "img", "aria-label": `${label}: ${count(d)} of ${count(t)}` },
      h("div", { class: "meter-fill", style: { width: `${share * 100}%` } })),
    h("div", { class: "meter-sub" }, `${count(d)} of ${count(t)}`));
}

function chartWidth(fallback = 760) {
  const main = document.getElementById("main");
  return Math.max(280, Math.min(fallback, (main.clientWidth || fallback) - 42));
}

function lineChart({ series, height = 260, width, formatX, formatY, label }) {
  const W = width || chartWidth(1200);
  const margin = { top: 12, right: series.length > 1 ? 66 : 72, bottom: 28, left: 62 };
  const xs = series.flatMap((one) => one.points.map((p) => p[0]));
  const ys = series.flatMap((one) => one.points.map((p) => p[1]));
  const [xMin, xMax] = [Math.min(...xs), Math.max(...xs)];
  const yTicks = niceTicks(Math.min(...ys), Math.max(...ys), 5);
  const [yMin, yMax] = [yTicks[0], yTicks[yTicks.length - 1]];
  const plotW = W - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const x = (v) => margin.left + (xMax === xMin ? plotW / 2 : ((v - xMin) / (xMax - xMin)) * plotW);
  const y = (v) => margin.top + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;
  const xTickCount = Math.max(2, Math.min(6, Math.floor(plotW / 110)));
  const xTicks = series.length && series[0].points.length <= 6
    ? series[0].points.map((p) => p[0])
    : Array.from({ length: xTickCount }, (_, i) => xMin + ((xMax - xMin) * i) / (xTickCount - 1));
  const svg = s("svg", {
    class: "chart", viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": label, tabindex: "0",
    width: W, height,
  });
  for (const tick of yTicks) {
    svg.append(s("line", { class: tick === yMin ? "axis" : "grid", x1: margin.left, x2: W - margin.right, y1: y(tick), y2: y(tick) }));
    const text = s("text", { x: margin.left - 8, y: y(tick) + 4, "text-anchor": "end" });
    text.textContent = formatY(tick, true);
    svg.append(text);
  }
  xTicks.forEach((tick, i) => {
    const anchor = xTicks.length > 1 && i === 0 ? "start" : xTicks.length > 1 && i === xTicks.length - 1 ? "end" : "middle";
    const text = s("text", { x: x(tick), y: height - 8, "text-anchor": anchor });
    text.textContent = formatX(tick);
    svg.append(text);
  });
  series.forEach((one) => {
    const path = one.points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`).join("");
    if (series.length === 1 && one.points.length > 1) {
      const lastX = x(one.points[one.points.length - 1][0]).toFixed(1);
      const firstX = x(one.points[0][0]).toFixed(1);
      svg.append(s("path", { class: `area fill-${one.slot}`, d: `${path}L${lastX} ${y(yMin)}L${firstX} ${y(yMin)}Z` }));
    }
    svg.append(s("path", { class: `line stroke-${one.slot}`, d: path }));
    const end = one.points[one.points.length - 1];
    svg.append(s("circle", { class: `ring fill-${one.slot}`, cx: x(end[0]), cy: y(end[1]), r: 4 }));
    if (series.length <= 4) {
      const text = s("text", { class: "end-label", x: x(end[0]) + 8, y: y(end[1]) + 4 });
      text.textContent = formatY(end[1]);
      svg.append(text);
    }
  });
  const cross = s("line", { class: "cross", y1: margin.top, y2: margin.top + plotH, visibility: "hidden" });
  const dots = series.map((one) => s("circle", { class: `ring fill-${one.slot}`, r: 4, visibility: "hidden" }));
  svg.append(cross, ...dots);

  const positions = [...new Set(xs)].sort((a, b) => a - b);
  let active = positions.length - 1;
  const nearestPoint = (one, target) => one.points.reduce((best, p) =>
    Math.abs(p[0] - target) < Math.abs(best[0] - target) ? p : best);
  const show = (index, clientX, clientY) => {
    active = index;
    const target = positions[index];
    cross.setAttribute("x1", x(target));
    cross.setAttribute("x2", x(target));
    cross.setAttribute("visibility", "visible");
    series.forEach((one, i) => {
      const point = nearestPoint(one, target);
      dots[i].setAttribute("cx", x(point[0]));
      dots[i].setAttribute("cy", y(point[1]));
      dots[i].setAttribute("visibility", "visible");
    });
    const box = tipContent(formatX(target), series.map((one) => ({
      value: formatY(nearestPoint(one, target)[1]), label: one.name, key: `c${one.slot}`,
    })));
    placeTip(box, clientX, clientY);
  };
  const clear = () => {
    cross.setAttribute("visibility", "hidden");
    dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
    hideTip();
  };
  svg.addEventListener("pointermove", (event) => {
    const rect = svg.getBoundingClientRect();
    const dataX = xMin + (((event.clientX - rect.left) * (W / rect.width) - margin.left) / (plotW || 1)) * (xMax - xMin);
    let index = 0;
    positions.forEach((p, i) => { if (Math.abs(p - dataX) < Math.abs(positions[index] - dataX)) index = i; });
    show(index, event.clientX, event.clientY);
  });
  svg.addEventListener("pointerleave", clear);
  svg.addEventListener("blur", clear);
  svg.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = Math.max(0, Math.min(positions.length - 1, active + (event.key === "ArrowRight" ? 1 : -1)));
    const rect = svg.getBoundingClientRect();
    show(next, rect.left + (x(positions[next]) / W) * rect.width, rect.top + 20);
  });
  return svg;
}

function reliabilityChart(series) {
  const size = 420;
  const m = { top: 10, right: 12, bottom: 42, left: 62 };
  const plot = size - m.left - m.right;
  const x = (v) => m.left + v * plot;
  const y = (v) => size - m.bottom - v * (size - m.top - m.bottom);
  const svg = s("svg", {
    class: "chart square-chart", viewBox: `0 0 ${size} ${size}`, role: "img",
    "aria-label": "Predicted probability against observed up rate. Points on the diagonal are well calibrated.",
  });
  for (const tick of [0, 0.25, 0.5, 0.75, 1]) {
    svg.append(
      s("line", { class: tick === 0 ? "axis" : "grid", x1: m.left, x2: x(1), y1: y(tick), y2: y(tick) }),
      s("line", { class: tick === 0 ? "axis" : "grid", x1: x(tick), x2: x(tick), y1: y(0), y2: y(1) }));
    const left = s("text", { x: m.left - 8, y: y(tick) + 4, "text-anchor": "end" });
    left.textContent = tick.toFixed(2);
    const bottom = s("text", { x: x(tick), y: y(0) + 18, "text-anchor": "middle" });
    bottom.textContent = tick.toFixed(2);
    svg.append(left, bottom);
  }
  const xLabel = s("text", { x: x(0.5), y: size - 6, "text-anchor": "middle" });
  xLabel.textContent = "Predicted probability of up";
  const yLabel = s("text", { x: 12, y: y(0.5), "text-anchor": "middle", transform: `rotate(-90 12 ${y(0.5)})` });
  yLabel.textContent = "Observed up rate";
  svg.append(xLabel, yLabel, s("line", { class: "diag", x1: x(0), y1: y(0), x2: x(1), y2: y(1) }));
  for (const one of series) {
    for (const bucket of one.buckets) {
      const [cx, cy] = [x(num(bucket.mean_predicted)), y(num(bucket.observed_up_rate))];
      const group = s("g", {}, s("circle", { class: `ring fill-${one.slot}`, cx, cy, r: 5 }), s("circle", { class: "hit", cx, cy, r: 12 }));
      svg.append(withTip(group, `${one.name}, ${count(bucket.count)} rows`, [
        { value: pct(bucket.mean_predicted, 1), label: "predicted" },
        { value: pct(bucket.observed_up_rate, 1), label: "observed" },
      ]));
    }
  }
  return svg;
}

// --- Shared data lookups -------------------------------------------------------------------------

function isFresh(ts) {
  const n = num(ts);
  return n !== null && Date.now() - n < RUNNING_WITHIN_MS;
}

function sessionRunning(session) {
  return Boolean(session.running && isFresh(session.ts_utc_ms));
}

function recorderRunning(recorder) {
  return isFresh(recorder.health.ts_utc_ms);
}

function compareRows() {
  const section = ui.state.compare;
  return section.available ? section.doc.rows : [];
}

function headlineRow() {
  return compareRows().find((r) => r.strategy === "passive_maker_no_loss_stop" && r.scenario === "central" && r.partition === "all");
}

function attentionItems() {
  const items = [];
  const { recorders, coverage, sessions, meta } = ui.state;
  for (const recorder of recorders) {
    const match = /sunset-(\d{4}-\d{2}-\d{2})/.exec(recorder.health.twap_status || "");
    if (match) {
      const days = Math.ceil((Date.parse(`${match[1]}T00:00:00Z`) - Date.now()) / 86400000);
      items.push({
        tone: days <= 30 ? "warning" : "neutral",
        title: days > 0 ? `Public TWAP feed shuts down in ${days} days (${match[1]})` : `Public TWAP feed shut down on ${match[1]}`,
        detail: "Recording has no TWAP after that without authenticated PolyBolt access, which needs credentials and your decision.",
      });
      break;
    }
  }
  const offsets = recorders.map((r) => num(r.health.clock_offset_ms)).filter((v) => v !== null);
  const offset = offsets.length ? offsets[0] : coverage.available ? num(coverage.doc.clock_offset_ms_latest) : null;
  const limit = num(meta.max_clock_skew_ms);
  if (offset !== null && limit && Math.abs(offset) > limit / 2) {
    items.push({
      tone: Math.abs(offset) > limit ? "critical" : "warning",
      title: `This computer's clock is ${count(Math.abs(offset))} ms ${offset > 0 ? "fast" : "slow"}`,
      detail: `The limit is ${count(limit)} ms. Replay corrects for it, but it should be fixed with Windows time sync.`,
    });
  }
  for (const session of sessions) {
    const shutdown = session.metrics.shutdown;
    if (shutdown && shutdown.clean === false && !sessionRunning(session)) {
      const open = (shutdown.orders_still_open || []).length;
      items.push({
        tone: "warning",
        title: `Session ${session.name} did not shut down cleanly`,
        detail: `${open} simulated order${open === 1 ? " was" : "s were"} still open when it stopped.`,
      });
    }
  }
  return items;
}

// --- Views ---------------------------------------------------------------------------------------

function viewOverview() {
  const { memo, scan, forecast, audit, coverage, sessions, recorders, meta } = ui.state;
  const parts = [];
  if (memo.available) {
    const doc = memo.doc;
    parts.push(h("section", { class: "card" },
      h("div", { class: "verdict" },
        h("div", { class: `verdict-word ${doc.tone}` }, icon(TONE_ICON[doc.tone] || "minus"), doc.verdict || NA),
        h("div", {},
          h("p", { class: "verdict-statement" }, doc.statement || ""),
          h("p", { class: "verdict-meta" }, `Readiness verdict${doc.date ? `, ${doc.date}` : ""}, from the readiness memo.`))),
      source(memo)));
  } else {
    parts.push(missing(memo));
  }
  const best = headlineRow();
  const tiles = [];
  if (best) {
    const dist = best.per_traded_market;
    tiles.push(tile("Passive quoting, per market", pnl(dist.mean), `95% interval ${pnl(dist.mean_95ci_low)} to ${pnl(dist.mean_95ci_high)}`));
  }
  if (scan.available) {
    const summary = scan.doc.summary;
    tiles.push(tile("Pair arbitrage checks passed", count(summary.passed_candidates), `of ${count(summary.evaluations)} evaluated`));
  }
  if (forecast.available) {
    const bench = forecast.doc.benchmark_vs_market_on_test;
    tiles.push(tile("Forecast score, lower is better", `${score(bench.market_price.brier)}`, `Market's price. Our best model: ${score(Math.min(num(bench.baseline.brier), num(bench.logistic.brier)))}`));
  }
  if (audit.available) {
    tiles.push(tile("Fair-value model audit", audit.doc.decision.status, audit.doc.decision.model_enabled_by_default ? "Model enabled" : "The model stays disabled"));
  }
  if (coverage.available) {
    tiles.push(tile("Recorded market data", `${compact(coverage.doc.events_total)} events`, `${count(coverage.doc.full_lifespans)} fully captured markets`));
  }
  tiles.push(tile("Real orders possible", meta.live_disabled_reasons.length ? "No" : "Check setup", `${meta.live_disabled_reasons.length} separate blocks in place`));
  parts.push(h("div", { class: "tiles" }, tiles));

  const scenarioRows = compareRows()
    .filter((r) => r.partition === "all" && r.strategy === "passive_maker_no_loss_stop" && num(r.per_traded_market.mean) !== null)
    .map((r) => ({
      label: SCENARIO[r.scenario] || words(r.scenario),
      mean: num(r.per_traded_market.mean),
      low: num(r.per_traded_market.mean_95ci_low),
      high: num(r.per_traded_market.mean_95ci_high),
    }));

  const attention = attentionItems();
  const running = [
    ...sessions.filter(sessionRunning).map((one) => ({ name: one.name, kind: words(one.kind) })),
    ...recorders.filter(recorderRunning).filter((r) => !r.name.startsWith("reports/")).map((r) => ({ name: r.name, kind: "Recorder" })),
  ];
  const now = card("Running now", null, running.length
    ? h("ul", { class: "check-list" }, running.map((one) => h("li", {}, h("span", {}, `${one.kind}: ${one.name}`), pill("good", "Running"))))
    : h("div", {}, h("p", { class: "empty" }, "Nothing is running. To watch a simulated session live, start one in a terminal:"), command(RUN_COMMANDS.paper)));

  parts.push(h("div", { class: "cols" },
    h("div", { class: "stack" },
      scenarioRows.length ? card(scenarioRows.every((r) => r.high < 0) ? "Passive quoting loses in every scenario" : "Passive quoting by scenario", "Mean result per traded market with its 95% interval. An interval entirely left of zero means a loss even at the top of the range.", intervalChart(scenarioRows)) : null,
      memo.available ? card("Readiness questions", null, table("questions", [
        { key: "question", label: "Question", wrap: true, noSort: true },
        { key: "status", label: "Answer", noSort: true, fmt: (v, row) => pill(row.tone, v) },
        { key: "detail", label: "Detail", wrap: true, noSort: true, fmt: (v) => v || "—" },
      ], memo.doc.questions, "Answers taken from the readiness memo")) : null),
    h("div", { class: "stack" }, now,
      card("Needs your attention", null, attention.length
        ? attention.map((item) => h("div", { class: `attention ${item.tone}` }, icon(item.tone === "neutral" ? "info" : "alert"),
          h("div", {}, h("strong", {}, item.title), h("span", {}, item.detail))))
        : h("p", { class: "empty" }, "Nothing right now.")),
      card("Why it cannot trade for real", "Every one of these has to clear before a real order could exist, and the first cannot clear in this release.",
        h("ul", { class: "check-list" }, meta.live_disabled_reasons.map((reason) => h("li", {}, h("span", {}, words(reason)), pill("neutral", "Blocked"))))))));
  return parts;
}

function viewRecorder() {
  const { recorders } = ui.state;
  if (!recorders.length) {
    return [card("No recordings found", "No health.json was found under data/ or reports/. Start a recording in a terminal:", command(RUN_COMMANDS.record))];
  }
  const chosen = recorders.find((r) => r.name === ui.recorder) || recorders[0];
  const health = chosen.health;
  const running = recorderRunning(chosen);
  const selector = h("label", { class: "field" }, "Recording", h("select", {
    id: "recorder-select",
    onchange: (event) => { ui.recorder = event.target.value; renderView(); },
  }, recorders.map((r) => h("option", { value: r.name, selected: r.name === chosen.name }, `${r.name}${recorderRunning(r) ? " (running)" : ""}`))));
  const sources = Object.entries(health.events_by_source || {}).map(([name, events]) => ({
    name, events, age: (health.source_age_ms || {})[name],
  }));
  const books = Object.entries(health.books || {}).map(([asset, book]) => ({ asset, ...book }));
  const invalidations = Object.values(health.invalidations || {}).reduce((sum, v) => sum + num(v), 0);
  return [
    h("div", { class: "filters" }, selector,
      running ? pill("good", "Running") : pill("neutral", "Stopped"),
      h("span", { class: "updated" }, `Last update ${when(health.ts_utc_ms)}`)),
    h("div", { class: "tiles" },
      tile("Rows written", compact(health.rows_written), `Run ${health.run_id || NA}`),
      tile("Markets seen", count(health.markets_seen), `${count(health.markets_resolved)} resolved`),
      tile("Data gaps", count(health.gaps), `${count(health.resnapshots)} book recoveries`),
      tile("Book invalidations", count(invalidations), invalidations ? "Books needed recovery" : "None"),
      tile("Clock offset", num(health.clock_offset_ms) === null ? NA : `${count(health.clock_offset_ms)} ms`, "Against Binance server time"),
      tile("Active market", slugTime(health.active_market), health.active_market || "None")),
    h("div", { class: "cols" },
      card("Events by source", null,
        barRows(sources.map((one) => ({ label: words(one.name), value: num(one.events), display: compact(one.events), tipLabel: "events" }))),
        h("div", { style: { marginTop: "16px" } }, table("sources", [
          { key: "name", label: "Source", fmt: words },
          { key: "events", label: "Events", num: true, fmt: count },
          { key: "age", label: "Age at last update", num: true, fmt: (v) => span(v) },
        ], sources, "Age should stay under a second for market data while recording"))),
      card("Status", null, h("ul", { class: "check-list" },
        (chosen.alerts.length ? chosen.alerts : []).map((alert) => h("li", {}, h("span", {}, alert.detail),
          pill(alert.severity === "critical" ? "critical" : "warning", words(alert.code)))),
        h("li", {}, h("span", {}, "Rollover failures"), pill((health.rollover_failures || []).length ? "critical" : "good", count((health.rollover_failures || []).length))),
        h("li", {}, h("span", {}, "TWAP feed"), h("span", {}, h("code", {}, health.twap_status || NA))),
        h("li", {}, h("span", {}, "Binance feed"), h("span", {}, h("code", {}, health.binance_status || NA))),
        h("li", {}, h("span", {}, "Mode"), h("span", {}, words(health.mode))),
        h("li", {}, h("span", {}, "Fee schedule"), h("span", {}, h("code", {}, health.active_fee_version || NA)))))),
    card("Order books at last update", null, table("books", [
      { key: "market_id", label: "Market" },
      { key: "asset", label: "Token", fmt: (v) => h("code", {}, `${v.slice(0, 6)}…${v.slice(-4)}`) },
      { key: "validity", label: "State", fmt: (v) => pill(v === "ready" ? "good" : "warning", words(v)) },
      { key: "age_ms", label: "Age", num: true, fmt: (v) => span(v) },
      { key: "snapshots", label: "Snapshots", num: true, fmt: count },
      { key: "invalid_reason", label: "Reason", fmt: (v) => v || "—" },
    ], books, "Each market has two tokens, one for Up and one for Down")),
    (health.recent_gaps || []).length ? card("Recent gaps", null, table("rec-gaps", [
      { key: "ts_utc_ms", label: "Time", fmt: when },
      { key: "reason", label: "Reason" },
      { key: "segment", label: "Segment" },
    ], health.recent_gaps, "Periods the recorder did not observe")) : null,
  ];
}

function sessionLive(session) {
  const market = session.active_market;
  const parts = [];
  if (market) {
    const left = num(market.end_utc_ms) - Date.now();
    parts.push(card(`Active market: ${slugTime(market.slug)}`, left > 0 ? `${span(left)} left in this 5-minute window.` : "This window has closed.",
      h("div", { class: "book" }, ["YES", "NO"].map((outcome) => {
        const side = market.sides[outcome];
        return h("div", { class: "book-side" }, h("div", {}, outcome === "YES" ? "Up (YES)" : "Down (NO)"),
          h("div", { class: "book-prices" },
            h("div", {}, h("small", {}, "Best bid"), side.bid ?? NA),
            h("div", {}, h("small", {}, "Best ask"), side.ask ?? NA)));
      }))));
  }
  parts.push(card("Open simulated orders", null, (session.open_orders || []).length
    ? table("open-orders", [
      { key: "outcome", label: "Outcome" },
      { key: "side", label: "Side" },
      { key: "price", label: "Price", num: true },
      { key: "size", label: "Size", num: true },
      { key: "matched", label: "Filled", num: true },
      { key: "state", label: "State", fmt: words },
      { key: "purpose", label: "Purpose", fmt: words },
    ], session.open_orders, "None of these orders exist on any exchange")
    : h("p", { class: "empty" }, "None right now.")));
  return h("div", { class: "cols" }, parts);
}

function viewSessions() {
  const { sessions } = ui.state;
  if (!sessions.length) {
    return [card("No sessions yet", "Start a simulated session in a terminal and it will appear here within seconds:", command(RUN_COMMANDS.paper), command(RUN_COMMANDS.shadow))];
  }
  const chosen = sessions.find((one) => one.name === ui.session) || sessions[0];
  const m = chosen.metrics;
  const running = sessionRunning(chosen);
  const shutdown = m.shutdown;
  const selector = h("label", { class: "field" }, "Session", h("select", {
    id: "session-select",
    onchange: (event) => { ui.session = event.target.value; renderView(); },
  }, sessions.map((one) => h("option", { value: one.name, selected: one.name === chosen.name },
    `${one.name} (${one.kind}${sessionRunning(one) ? ", running" : ""})`))));
  const states = Object.entries(m.orders.by_state || {}).sort((a, b) => num(b[1]) - num(a[1]));
  const perMarket = m.per_market || [];
  const settled = perMarket.filter((row) => row.settled && (num(row.yes_gross) > 0 || num(row.no_gross) > 0));
  const curve = (chosen.equity_curve || []).map(([t, equity]) => [num(t), num(equity)]);
  const kills = (m.safety.kill_events || []).map(([ts, action, reason]) => ({ ts, action, reason }));
  const checks = [
    ["Simulated order route", !m.safety.port_is_real, m.safety.port_is_real ? "Real" : "Simulated"],
    ["Reconciliation runs clean",
      num(m.reconciliation.runs) ? num(m.reconciliation.clean) === num(m.reconciliation.runs) : null,
      num(m.reconciliation.runs) ? `${count(m.reconciliation.clean)} of ${count(m.reconciliation.runs)}` : "None yet"],
    ["Ledger conservation errors", !(m.ledger.conservation_errors || []).length, count((m.ledger.conservation_errors || []).length)],
    ["Unknown orders remaining", !num(m.orders.unknown_remaining), count(m.orders.unknown_remaining)],
    ["Orphan fills", !num(m.orders.orphan_fills), count(m.orders.orphan_fills)],
    ["Future-data violations", !num(m.safety.visibility_violations), count(m.safety.visibility_violations)],
  ];
  if (shutdown) checks.unshift(["Clean shutdown", shutdown.clean, shutdown.clean ? "Yes" : `${(shutdown.orders_still_open || []).length} still open`]);
  const latency = Object.entries(m.latency_ms || {}).map(([name, row]) => ({ name, ...row }));
  const markouts = Object.entries(m.markout_avg_per_share || {}).map(([name, value]) => ({ name, value, samples: (m.markout_samples || {})[name] }));
  return [
    h("div", { class: "filters" }, selector,
      running ? pill("good", "Running") : chosen.running ? pill("warning", "Stopped without a final report") : pill("neutral", "Finished"),
      h("span", { class: "updated" }, `${words(m.scenario.name)} scenario, last update ${when(chosen.ts_utc_ms || chosen.modified_utc_ms)}`)),
    h("div", { class: "tiles" },
      tile("Realized, settled", pnl(m.pnl.realized_settled), "Simulated"),
      tile("Unrealized, unsettled", pnl(m.pnl.unrealized_mtm_unresolved), "At current midpoints"),
      tile("Cost in unsettled markets", money(m.pnl.unresolved_cost_at_risk), `${count(perMarket.filter((row) => !row.settled && num(row.cost) > 0).length)} traded markets with no result yet`),
      tile("Simulated fills", count(m.fills.count), `${count(m.fills.maker)} passive, ${count(m.fills.taker)} taker`),
      tile("Simulated orders", count(Object.values(m.orders.by_state || {}).reduce((sum, v) => sum + num(v), 0)), `${count(m.orders.risk_rejected)} refused by risk checks`),
      tile("Largest drawdown", money(m.equity.max_drawdown), `From ${money(m.equity.starting)} starting cash`)),
    running ? sessionLive(chosen) : null,
    card("Simulated equity", "Cash plus open positions at their last midpoint.", curve.length > 1
      ? lineChart({
        series: [{ name: "Equity", slot: 1, points: curve }],
        formatX: (v) => clock(v), formatY: (v) => money(v), label: `Simulated equity over time, ending at ${money(curve[curve.length - 1][1])}`,
      })
      : h("p", { class: "empty" }, "No equity history for this session. Sessions started with this version record one.")),
    h("div", { class: "cols" },
      card("Result by settled market", settled.length ? null : "No traded market has settled in this session yet.",
        settled.length ? barRows(settled.map((row) => ({ label: slugTime(row.slug), value: num(row.realized_pnl), display: pnl(row.realized_pnl), tipLabel: `Winner: ${row.winning ?? NA}` }))) : null),
      card("Integrity checks", null, h("ul", { class: "check-list" }, checks.map(([label, ok, value]) =>
        h("li", {}, h("span", {}, label), pill(ok === null ? "neutral" : ok ? "good" : "critical", value)))))),
    card("All markets in this session", null, table(`markets-${chosen.name}`, [
      { key: "slug", label: "Window", fmt: slugTime, sort: (row) => row.slug },
      { key: "settled", label: "State", fmt: (v) => pill(v ? "good" : "neutral", v ? "Settled" : "Unsettled") },
      { key: "winning", label: "Winner", fmt: (v) => v ?? "—" },
      { key: "cost", label: "Cost", num: true, fmt: (v) => money(v) },
      { key: "payout", label: "Payout", num: true, fmt: (v) => money(v) },
      { key: "realized_pnl", label: "Realized", num: true, fmt: (v) => pnl(v) },
      { key: "unrealized_mtm", label: "Unrealized", num: true, fmt: (v) => pnl(v) },
      { key: "unpaired_yes_at_close", label: "Unpaired Up", num: true, fmt: (v) => (v === null ? "—" : Number(v).toFixed(0)) },
      { key: "unpaired_no_at_close", label: "Unpaired Down", num: true, fmt: (v) => (v === null ? "—" : Number(v).toFixed(0)) },
    ], perMarket, "Unpaired shares at close are a one-way bet rather than a locked pair")),
    h("div", { class: "cols-3" },
      card("Orders by final state", null, states.length ? barRows(states.map(([name, value]) => ({ label: words(name), value: num(value), display: count(value), tipLabel: "orders" }))) : h("p", { class: "empty" }, "None yet.")),
      card("Price move after a fill", "Average per share. Negative means the market moved against the fill.", table("markouts", [
        { key: "name", label: "After", noSort: true },
        { key: "value", label: "Move", num: true, noSort: true, fmt: cents },
        { key: "samples", label: "Fills", num: true, noSort: true, fmt: count },
      ], markouts, "Measured on simulated fills")),
      card("Assumed latency", "Configured, not measured. No real order has been sent.", table("latency", [
        { key: "name", label: "Step", noSort: true, fmt: words },
        { key: "p50", label: "p50", num: true, noSort: true, fmt: (v) => (v === undefined ? NA : `${v} ms`) },
        { key: "p95", label: "p95", num: true, noSort: true, fmt: (v) => (v === undefined ? NA : `${v} ms`) },
        { key: "count", label: "Count", num: true, noSort: true, fmt: count },
      ], latency, "Simulated order round trips"))),
    h("div", { class: "cols" },
      card("Kill switch events", `${count(kills.length)} recorded. ${(m.safety.kill_active_at_end || []).length ? `Active at the end: ${m.safety.kill_active_at_end.join(", ")}.` : "None active at the end."}`,
        kills.length ? table("kills", [
          { key: "ts", label: "Time", fmt: clock },
          { key: "action", label: "Action", fmt: words },
          { key: "reason", label: "Reason", fmt: words },
        ], kills.slice(-12).reverse(), "Latest 12 events") : null),
      chosen.shadow ? card("Hypothetical order log", "Shadow mode only logs what it would have done. These orders never happened and are not evidence that they would have filled.",
        h("div", { class: "tiles" }, Object.entries(chosen.shadow.counts).map(([kind, value]) => tile(words(kind.replace("hypothetical_", "")), count(value)))),
        h("details", {}, h("summary", {}, "Latest records"), table("shadow", [
          { key: "ts_utc_ms", label: "Time", noSort: true, fmt: clock },
          { key: "kind", label: "Kind", noSort: true, fmt: (v) => words(String(v).replace("hypothetical_", "")) },
          { key: "outcome", label: "Outcome", noSort: true, fmt: (v) => v ?? "—" },
          { key: "side", label: "Side", noSort: true, fmt: (v) => v ?? "—" },
          { key: "price", label: "Price", noSort: true, num: true, fmt: (v) => v ?? "—" },
          { key: "size", label: "Size", noSort: true, num: true, fmt: (v) => v ?? "—" },
        ], chosen.shadow.recent, "Newest first"))) : null),
    h("p", { class: "source" }, m.disclaimer),
  ];
}

function viewStrategies() {
  const section = ui.state.compare;
  if (!section.available) return [missing(section, RUN_COMMANDS.compare)];
  const doc = section.doc;
  const strategies = [...new Set(doc.rows.map((r) => r.strategy))];
  const partitions = [...new Set(doc.rows.map((r) => r.partition))];
  const rows = doc.rows.filter((r) => r.partition === ui.partition && (ui.strategy === "any" || r.strategy === ui.strategy));
  const plotted = rows.filter((r) => num(r.per_traded_market.mean) !== null);
  const counts = Object.fromEntries((doc.partitions || []).map((p) => [p.name, p.markets]));
  const filters = h("div", { class: "filters" },
    h("label", { class: "field" }, "Markets", h("select", {
      id: "partition-select", onchange: (event) => { ui.partition = event.target.value; renderView(); },
    }, partitions.map((p) => h("option", { value: p, selected: p === ui.partition },
      `${PARTITION[p] || words(p)}${counts[p] ? ` (${counts[p]})` : ""}`)))),
    h("label", { class: "field" }, "Strategy", h("select", {
      id: "strategy-select", onchange: (event) => { ui.strategy = event.target.value; renderView(); },
    }, h("option", { value: "any", selected: ui.strategy === "any" }, "All strategies"),
    strategies.map((name) => h("option", { value: name, selected: name === ui.strategy }, STRATEGY[name] || words(name))))));
  return [
    filters,
    card("Mean result per market", "Dot is the mean, line is its 95% interval. Red intervals are entirely below zero, grey ones include zero.",
      plotted.length
        ? intervalChart(plotted.map((r) => ({
          group: STRATEGY[r.strategy] || words(r.strategy),
          label: SCENARIO[r.scenario] || words(r.scenario),
          mean: num(r.per_traded_market.mean),
          low: num(r.per_traded_market.mean_95ci_low ?? r.per_traded_market.mean),
          high: num(r.per_traded_market.mean_95ci_high ?? r.per_traded_market.mean),
          extra: [{ value: `${count(r.per_traded_market.winners)} / ${count(r.per_traded_market.losers)}`, label: "wins / losses" }],
        })))
        : h("p", { class: "empty" }, "None of these rows traded, so there is nothing to plot.")),
    note("The \"no stop\" rows lift the $10 daily loss limit so that every market gets evaluated. They exist only in research code. The rows with the stop on show the strategy as configured, which ends a run when the limit latches."),
    card("Every result", `${doc.coverage_grade}. ${doc.disclaimer}`, table("compare", [
      { key: "strategy", label: "Strategy", fmt: (v) => STRATEGY[v] || words(v) },
      { key: "scenario", label: "Scenario", fmt: (v) => SCENARIO[v] || words(v) },
      { key: "partition", label: "Markets", fmt: (v) => PARTITION[v] || words(v) },
      { key: "markets_traded", label: "Traded", num: true, fmt: count },
      { key: "settled", label: "Settled", num: true, fmt: (_, r) => count(r.per_traded_market.count), sort: (r) => r.per_traded_market.count },
      { key: "realized_pnl", label: "Realized", num: true, fmt: (v) => pnl(v) },
      { key: "mean", label: "Per market", num: true, fmt: (_, r) => pnl(r.per_traded_market.mean), sort: (r) => r.per_traded_market.mean },
      { key: "wl", label: "Win / loss", num: true, fmt: (_, r) => (r.per_traded_market.count ? `${r.per_traded_market.winners} / ${r.per_traded_market.losers}` : NA), sort: (r) => r.per_traded_market.winners },
      { key: "fills", label: "Fills", num: true, fmt: count },
      { key: "fees", label: "Fees", num: true, fmt: (v) => money(v) },
      { key: "max_drawdown", label: "Drawdown", num: true, fmt: (v) => money(v) },
      { key: "unpaired_close_fraction", label: "Unpaired", num: true, fmt: (v) => pct(v) },
      { key: "markout_5s", label: "5s move", num: true, fmt: cents },
      { key: "unresolved_cost", label: "Unsettled cost", num: true, fmt: (v) => money(v) },
    ], rows, "Select a column heading to sort"), source(section)),
  ];
}

function viewScanner() {
  const section = ui.state.scan;
  if (!section.available) return [missing(section, RUN_COMMANDS.scan)];
  const doc = section.doc;
  const summary = doc.summary;
  const sizes = Object.entries(summary.by_q_net || {}).map(([size, row]) => ({ size, ...row })).sort((a, b) => num(a.size) - num(b.size));
  const fillable = sizes.reduce((sum, row) => sum + num(row.fully_fillable), 0);
  const funnel = [
    ["Evaluated", summary.evaluations],
    ["Enough depth to fill", fillable],
    ["Positive before fees", summary.positive_gross_edge],
    ["Positive after fees", summary.positive_after_fees],
    ["Positive after safety reserve", summary.positive_after_fees_and_reserve],
    ["Passed every check", summary.passed_candidates],
  ];
  const reasons = Object.entries(summary.reject_reason_counts || {}).sort((a, b) => num(b[1]) - num(a[1]));
  return [
    h("div", { class: "tiles" },
      tile("Evaluations", count(summary.evaluations), "Across all order sizes"),
      tile("Passed every check", count(summary.passed_candidates), "An executable pair"),
      tile("Positive before fees", count(summary.positive_gross_edge), "Readings, not executable trades"),
      tile("Largest apparent edge", money(summary.best_edge_after_reserve), "Only real if it passed every check")),
    h("div", { class: "cols" },
      card("From evaluation to executable", "Each row is a stricter test than the one above it.",
        barRows(funnel.map(([label, value]) => ({ label, value: num(value), display: count(value), tipLabel: "evaluations" })), { max: num(summary.evaluations) })),
      card("Why evaluations were rejected", "One evaluation can fail for several reasons, so these add up to more than the total.",
        barRows(reasons.map(([name, value]) => ({ label: words(name), value: num(value), display: count(value), tipLabel: "times" }))))),
    card("By order size", words(doc.interpretation), table("scan-sizes", [
      { key: "size", label: "Shares", num: true },
      { key: "evaluated", label: "Evaluated", num: true, fmt: count },
      { key: "fully_fillable", label: "Enough depth", num: true, fmt: count },
      { key: "positive_gross", label: "Positive before fees", num: true, fmt: count },
      { key: "positive_after_fees", label: "After fees", num: true, fmt: count },
      { key: "positive_after_reserve", label: "After reserve", num: true, fmt: count },
    ], sizes, "Counts at each order size"), source(section)),
  ];
}

function viewForecast() {
  const section = ui.state.forecast;
  if (!section.available) return [missing(section, RUN_COMMANDS.forecast)];
  const doc = section.doc;
  const bench = doc.benchmark_vs_market_on_test;
  const entities = [
    { key: "market_price", name: "Market's price", slot: 1 },
    { key: "baseline", name: "Random-walk baseline", slot: 2 },
    { key: "logistic", name: "Logistic model", slot: 3 },
  ];
  const metrics = [
    { key: "brier", label: "Brier score" },
    { key: "log_loss", label: "Log loss" },
    { key: "ece", label: "Calibration error" },
  ];
  const evaluation = Object.entries(doc.evaluation || {}).flatMap(([partition, models]) =>
    Object.entries(models).map(([model, row]) => ({ partition, model, ...row })));
  const horizon = (name) => (doc.test_by_horizon?.[name] || []).map((row) => [num(row.elapsed_s), num(row.brier)]);
  const reliability = (name) => doc.evaluation?.test?.[name]?.reliability || [];
  const flags = Object.entries(doc.verdict || {}).filter(([, value]) => typeof value === "boolean");
  const bucketRows = [...reliability("baseline").map((b) => ({ model: "Random-walk baseline", ...b })), ...reliability("logistic").map((b) => ({ model: "Logistic model", ...b }))];
  return [
    h("div", { class: "tiles" },
      tile("Closed markets used", count(doc.markets), `Train ${doc.split.train}, validation ${doc.split.validation}, test ${doc.split.test}`),
      tile("Same-moment test rows", count(bench.rows), `${count(bench.market_price.markets)} markets`),
      tile("Base rate of up", pct(doc.base_rate_up, 1), "Across the sample"),
      tile("Preferred model", words(doc.verdict.preferred_forecast_model), "Among our own models")),
    card("Our forecasts against the market's own price", "Lower is better on all three, measured at the same moments on the untouched test set.",
      legend(entities.map((e) => ({ label: e.name, cls: `c${e.slot}` }))),
      h("div", { class: "cols-3" }, metrics.map((metric) => {
        const lowest = Math.min(...entities.map((e) => num(bench[e.key][metric.key])));
        return h("div", {}, h("h3", { class: "group-label" }, metric.label),
          barRows(entities.map((e) => ({
            label: e.name,
            cls: `c${e.slot}`,
            value: num(bench[e.key][metric.key]),
            display: `${score(bench[e.key][metric.key])}${num(bench[e.key][metric.key]) === lowest ? " best" : ""}`,
            tipLabel: metric.label,
          })), { stacked: true }));
      }))),
    h("div", { class: "cols" },
      card("Calibration on the test set", "A well-calibrated forecast sits on the diagonal: events given 30% happen about 30% of the time.",
        legend([{ label: "Random-walk baseline", cls: "c2" }, { label: "Logistic model", cls: "c3" }]),
        reliabilityChart([
          { name: "Random-walk baseline", slot: 2, buckets: reliability("baseline") },
          { name: "Logistic model", slot: 3, buckets: reliability("logistic") },
        ]),
        h("details", {}, h("summary", {}, "Show as a table"), table("reliability", [
          { key: "model", label: "Model", noSort: true },
          { key: "mean_predicted", label: "Predicted", num: true, noSort: true, fmt: (v) => pct(v, 1) },
          { key: "observed_up_rate", label: "Observed", num: true, noSort: true, fmt: (v) => pct(v, 1) },
          { key: "count", label: "Rows", num: true, noSort: true, fmt: count },
        ], bucketRows, "Calibration buckets"))),
      h("div", { class: "stack" },
        card("Accuracy through the 5-minute window", "Forecasts get sharper as the window runs. Lower is better.",
          legend([{ label: "Random-walk baseline", cls: "c2", line: true }, { label: "Logistic model", cls: "c3", line: true }]),
          lineChart({
            series: [
              { name: "Random-walk baseline", slot: 2, points: horizon("baseline") },
              { name: "Logistic model", slot: 3, points: horizon("logistic") },
            ],
            height: 240,
            width: Math.max(280, Math.min(640, (chartWidth(1200) - 20) / (window.innerWidth >= 1024 ? 2 : 1))),
            formatX: (v) => `${v} s`,
            formatY: (v) => Number(v).toFixed(3),
            label: "Brier score by seconds elapsed in the window",
          })),
        card("Verdict", doc.verdict.note, h("ul", { class: "check-list" }, flags.map(([name, value]) =>
          h("li", {}, h("span", {}, words(name)), pill(value ? "good" : "critical", value ? "Yes" : "No"))))))),
    card("Scores by partition", doc.caveat, table("evaluation", [
      { key: "partition", label: "Partition", fmt: words },
      { key: "model", label: "Model", fmt: words },
      { key: "brier", label: "Brier", num: true, fmt: score },
      { key: "log_loss", label: "Log loss", num: true, fmt: score },
      { key: "ece", label: "Calibration error", num: true, fmt: score },
      { key: "markets", label: "Markets", num: true, fmt: count },
      { key: "rows", label: "Rows", num: true, fmt: count },
    ], evaluation, "Lower is better"), source(section)),
  ];
}

function viewAudit() {
  const section = ui.state.audit;
  if (!section.available) return [missing(section, RUN_COMMANDS.audit)];
  const doc = section.doc;
  const decision = doc.decision;
  const variants = [...new Set(doc.rows.map((r) => r.variant))];
  const scenarios = [...new Set(doc.rows.map((r) => r.scenario))].slice(0, 3);
  const max = Math.max(...doc.rows.map((r) => Math.abs(num(r.realized_pnl))));
  const usage = (row) => {
    const sources = row.fair_value_sources || {};
    const total = Object.values(sources).reduce((sum, v) => sum + num(v), 0);
    return total ? num(sources.model || 0) / total : null;
  };
  const verdicts = Object.entries(decision.variants || {}).map(([name, row]) => ({ name, ...row }));
  return [
    h("section", { class: "card" }, h("div", { class: "verdict" },
      h("div", { class: `verdict-word ${decision.status === "NO-GO" ? "critical" : "warning"}` }, icon(decision.status === "NO-GO" ? "x" : "alert"), decision.status),
      h("div", {}, h("p", { class: "verdict-statement" }, words(decision.reason)), h("p", { class: "verdict-meta" }, decision.note)))),
    card("Realized result by variant", "Compare fills as well as the result: a variant that trades less also loses less, whether or not its signal is better. Cost in markets with no recorded result is excluded here and listed in the table.",
      legend(scenarios.map((name, i) => ({ label: SCENARIO[name] || words(name), cls: `c${i + 1}` }))),
      variants.map((variant) => h("div", { class: "bar-group" },
        h("div", { class: "group-label" }, VARIANT[variant] || words(variant)),
        barRows(scenarios.map((scenario, i) => {
          const row = doc.rows.find((r) => r.variant === variant && r.scenario === scenario);
          return row && {
            label: SCENARIO[scenario] || words(scenario), cls: `c${i + 1}`, value: num(row.realized_pnl), display: pnl(row.realized_pnl),
            tipLabel: "realized", extra: [{ value: count(row.fills), label: "fills" }],
          };
        }).filter(Boolean), { max })))),
    card("Gate for a candidate model", "A model counts only if it passes all three in every scenario.", table("gate", [
      { key: "name", label: "Variant", noSort: true, fmt: (v) => VARIANT[v] || words(v) },
      { key: "model_used_in_most_decisions", label: "Used in most decisions", noSort: true, fmt: (v) => pill(v ? "good" : "critical", v ? "Yes" : "No") },
      { key: "beats_midpoint_in_all_scenarios", label: "Beats midpoint", noSort: true, fmt: (v) => pill(v ? "good" : "critical", v ? "Yes" : "No") },
      { key: "profitable_in_all_scenarios", label: "Profitable", noSort: true, fmt: (v) => pill(v ? "good" : "critical", v ? "Yes" : "No") },
      ], verdicts, "Audit gate results")),
    decision.status === "NO-GO" && ui.state.coverage.available
      ? note(`A smaller loss is not a near-breakeven strategy. With ${count(ui.state.coverage.doc.full_lifespans)} fully captured markets, this sample may be too small to tell whether the difference is real.`, "alert")
      : null,
    card("Every audit run", doc.disclaimer, table("audit", [
      { key: "variant", label: "Variant", fmt: (v) => VARIANT[v] || words(v) },
      { key: "scenario", label: "Scenario", fmt: (v) => SCENARIO[v] || words(v) },
      { key: "realized_pnl", label: "Realized", num: true, fmt: (v) => pnl(v) },
      { key: "fills", label: "Fills", num: true, fmt: count },
      { key: "markets_traded", label: "Traded", num: true, fmt: count },
      { key: "usage", label: "Model used", num: true, fmt: (_, r) => pct(usage(r)), sort: usage },
      { key: "markout_5s", label: "5s move", num: true, fmt: cents },
      { key: "unpaired_close_fraction", label: "Unpaired", num: true, fmt: (v) => pct(v) },
      { key: "max_drawdown", label: "Drawdown", num: true, fmt: (v) => money(v) },
      { key: "unresolved_cost", label: "Unsettled cost", num: true, fmt: (v) => money(v) },
      { key: "ledger_errors", label: "Ledger errors", num: true, fmt: count },
    ], doc.rows, "Select a column heading to sort"), source(section)),
  ];
}

function viewData() {
  const { coverage, performance } = ui.state;
  const parts = [];
  if (coverage.available) {
    const doc = coverage.doc;
    const stream = doc.stream_snapshot_validation;
    const rest = doc.rest_validation;
    const invalid = Object.values(doc.book_invalidations || {}).reduce((sum, v) => sum + num(v), 0);
    parts.push(
      h("div", { class: "tiles" },
        tile("Events", compact(doc.events_total), `${count(doc.parts)} files`),
        tile("Markets", count((doc.markets || []).length), `${count(doc.full_lifespans)} fully captured`),
        tile("TWAP points", compact(doc.twap_points), `${count(doc.clock_probes)} clock checks`),
        tile("Gaps", count(doc.gaps_total), "Periods not observed"),
        tile("Book invalidations", count(invalid), invalid ? "See health" : "None"),
        tile("Checksum problems", count((doc.checksum_problems || []).length), (doc.checksum_problems || []).length ? "See the coverage report" : "Every file verified")),
      h("div", { class: "cols" },
        card("Rebuilt order books against independent snapshots", stream.note,
          meter("Stream snapshots, top of book", stream.top_of_book_match, stream.checks),
          meter("Stream snapshots, full depth", stream.exact_depth_match, stream.checks),
          meter("REST samples, top of book", rest.top_of_book_match, rest.samples),
          meter("REST samples, full depth", rest.exact_depth_match, rest.samples)),
        card("Known gaps", null, (doc.gaps || []).length ? table("gaps", [
          { key: "ts_utc_ms", label: "Time", fmt: when },
          { key: "reason", label: "Reason", wrap: true },
        ], doc.gaps, "Periods the recorder did not observe") : h("p", { class: "empty" }, "None."))),
      card("Recorded markets", null, table("markets", [
        { key: "slug", label: "Window", fmt: slugTime, sort: (row) => row.slug },
        { key: "full_lifespan_captured", label: "Captured", fmt: (v) => pill(v ? "good" : "warning", v ? "Full" : "Partial") },
        { key: "resolved", label: "Result", fmt: (v, row) => {
          if (!v) return pill("warning", "Missing");
          const yes = num(row.yes_price);
          return yes === 1 ? "Up" : yes === 0 ? "Down" : NA;
        } },
        { key: "price_to_beat", label: "Price to beat", num: true, fmt: (v) => money(v) },
        { key: "final_price", label: "Final price", num: true, fmt: (v) => money(v) },
        { key: "events", label: "Events", num: true, fmt: (v) => count(Object.values(v || {}).reduce((sum, n) => sum + num(n), 0)), sort: (r) => Object.values(r.events || {}).reduce((sum, n) => sum + num(n), 0) },
        { key: "market_gaps", label: "Gaps", num: true, fmt: count },
      ], doc.markets, "Select a column heading to sort"), source(coverage)));
  } else {
    parts.push(card("No coverage report yet", `${coverage.path} does not exist. Run the coverage command and save its coverage object to that file.`,
      command("uv run bone-reaper coverage --input data/raw")));
  }
  if (performance.available) {
    const doc = performance.doc;
    parts.push(card("Simulator performance", doc.not_measured,
      h("div", { class: "tiles" },
        tile("Replay speed", `${compact(doc.events_per_second)}/s`, `${compact(doc.events)} events`),
        tile("Decision, p50", `${doc.decision_ms.p50} ms`),
        tile("Decision, p95", `${doc.decision_ms.p95} ms`),
        tile("Decision, p99", `${doc.decision_ms.p99} ms`),
        tile("Peak memory", `${doc.peak_rss_gb} GB`, `On ${doc.cpu_count} cores, ${doc.ram_gb} GB RAM`)),
      source(performance)));
  }
  return parts;
}

// --- Shell ---------------------------------------------------------------------------------------

const VIEWS = [
  { id: "overview", label: "Overview", icon: "overview", sub: "The verdict, and what needs your attention", deps: ["memo", "compare", "scan", "forecast", "audit", "coverage", "sessions", "recorders"], render: viewOverview },
  { id: "recorder", label: "Recorder", icon: "activity", sub: "Health of market data recordings", deps: ["recorders"], render: viewRecorder },
  { id: "sessions", label: "Sessions", icon: "play", sub: "Paper, shadow and replay runs. All orders are simulated", deps: ["sessions"], render: viewSessions },
  { id: "strategies", label: "Strategies", icon: "chart", sub: "Simulated results by strategy, scenario and data split", deps: ["compare"], render: viewStrategies },
  { id: "scanner", label: "Scanner", icon: "filter", sub: "Whether a fee-aware pair arbitrage ever existed", deps: ["scan"], render: viewScanner },
  { id: "forecast", label: "Forecast", icon: "target", sub: "Forecast accuracy against the market's own price", deps: ["forecast"], render: viewForecast },
  { id: "audit", label: "Model audit", icon: "shield", sub: "Whether the fair-value model improves quoting", deps: ["audit"], render: viewAudit },
  { id: "data", label: "Data quality", icon: "database", sub: "What was captured, and how well it checks out", deps: ["coverage", "performance"], render: viewData },
];

function currentView() {
  const id = location.hash.slice(1);
  return VIEWS.find((view) => view.id === id) || VIEWS[0];
}

function renderNav() {
  const view = currentView();
  const anyRunning = ui.state && ui.state.sessions.some(sessionRunning);
  const recording = ui.state && ui.state.recorders.some(recorderRunning);
  document.getElementById("nav").replaceChildren(...VIEWS.map((item) => h("a", {
    href: `#${item.id}`, "aria-current": item.id === view.id ? "page" : null,
  }, icon(item.icon), item.label,
  (item.id === "sessions" && anyRunning) || (item.id === "recorder" && recording)
    ? h("span", { class: "nav-dot" }, icon("activity"), "Live") : null)));
  document.querySelector('#nav a[aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  document.getElementById("view-title").textContent = view.label;
  document.getElementById("view-sub").textContent = view.sub;
  document.title = `${view.label} · Bone Reaper`;
}

function renderView() {
  if (!ui.state) return;
  const view = currentView();
  const main = document.getElementById("main");
  const focusId = document.activeElement && document.activeElement.id;
  const scroll = window.scrollY;
  hideTip();
  const content = h("div", { class: "stack" }, view.render());
  main.replaceChildren(content);
  main.classList.remove("loading");
  window.scrollTo(0, scroll);
  if (focusId) {
    const again = document.getElementById(focusId);
    if (again && again !== document.activeElement) again.focus({ preventScroll: true });
  }
}

function signature() {
  const view = currentView();
  const parts = view.deps.map((dep) => JSON.stringify(ui.state[dep]));
  // Running status depends on the clock as well as the data, so it is part of the comparison.
  parts.push(ui.state.sessions.map(sessionRunning).join(), ui.state.recorders.map(recorderRunning).join());
  return `${view.id}|${parts.join("|")}`;
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    ui.state = await response.json();
    ui.lastOk = Date.now();
    ui.failed = false;
    document.getElementById("offline").hidden = true;
    const next = signature();
    renderNav();
    if (next !== ui.signature) {
      ui.signature = next;
      renderView();
    }
    renderFoot();
  } catch {
    ui.failed = true;
    const box = document.getElementById("offline");
    box.hidden = false;
    box.textContent = ui.lastOk
      ? `Can't reach the dashboard server. Showing data from ${clock(ui.lastOk)}. Is bone-reaper dashboard still running?`
      : "Can't reach the dashboard server. Is bone-reaper dashboard still running?";
  }
  tick();
}

function renderFoot() {
  const foot = document.getElementById("side-foot");
  foot.replaceChildren(h("strong", {}, icon("lock"), "Live trading disabled"),
    `${ui.state.meta.live_disabled_reasons.length} blocks in place. This page can only read files on this computer.`);
}

function tick() {
  const label = document.getElementById("updated");
  if (!ui.lastOk) { label.textContent = ""; return; }
  const seconds = Math.round((Date.now() - ui.lastOk) / 1000);
  label.textContent = ui.paused ? "Updates paused" : `Updated ${seconds < 2 ? "just now" : `${seconds} s ago`}`;
}

function renderControls() {
  const pause = document.getElementById("pause");
  pause.replaceChildren(icon(ui.paused ? "play" : "pause"), ui.paused ? "Resume" : "Pause");
  pause.setAttribute("aria-pressed", String(ui.paused));
  const theme = document.getElementById("theme");
  const isLight = document.documentElement.dataset.theme === "light";
  theme.replaceChildren(icon(isLight ? "moon" : "sun"));
  theme.setAttribute("aria-label", isLight ? "Switch to dark theme" : "Switch to light theme");
  theme.title = theme.getAttribute("aria-label");
}

function init() {
  document.getElementById("sim-banner").replaceChildren(icon("alert"),
    h("span", {}, "All profit and loss here is simulated. It is not earnings and is not financial advice. No real order has ever been placed, and this software cannot place one."));
  document.getElementById("pause").addEventListener("click", () => {
    ui.paused = !ui.paused;
    renderControls();
    tick();
    if (!ui.paused) refresh();
  });
  document.getElementById("theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("br-theme", next); } catch { /* Not saved, still applied. */ }
    renderControls();
  });
  window.addEventListener("hashchange", () => {
    renderNav();
    ui.signature = ui.state ? signature() : "";
    window.scrollTo(0, 0);
    renderView();
    document.getElementById("main").focus({ preventScroll: true });
  });
  let resizeTimer;
  let lastWidth = window.innerWidth;
  window.addEventListener("resize", () => {
    if (window.innerWidth === lastWidth) return;
    lastWidth = window.innerWidth;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderView, 150);
  });
  renderControls();
  renderNav();
  document.getElementById("main").classList.add("loading");
  refresh();
  setInterval(() => { if (!ui.paused) refresh(); }, POLL_MS);
  setInterval(tick, 1000);
}

init();
