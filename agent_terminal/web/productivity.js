// The tally above the groups, today's prompts and PRs against the average and the record,
// and the graph by day it opens.

const SVG = "http://www.w3.org/2000/svg";
const SERIES = [
  ["prompts", "Prompts"],
  ["prs", "PRs"],
];
const RANGES = [
  [30, "30 days"],
  [90, "90 days"],
  [Infinity, "All"],
];

const WIDTH = 920;
const HEIGHT = 300;
const LEFT = 36;
const TOP = 12;
const BOTTOM = 24;
const PLOT_WIDTH = WIDTH - LEFT;
const PLOT_HEIGHT = HEIGHT - TOP - BOTTOM;
const MAX_BAR = 14;
const DAY_LABEL_WIDTH = 64;

function element(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function svg(tag, attributes, text = "") {
  const node = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  node.textContent = text;
  return node;
}

// "2026-11-25" as a local date: alone, it would be read as midnight UTC, the day before.
export function dayLabel(day) {
  const date = new Date(`${day}T00:00:00`);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function average(summary) {
  return summary.average === null ? "–" : String(summary.average);
}

// The gridlines: round steps, whole numbers, at most five, the last at or above `max`.
export function ticks(max) {
  const top = Math.max(max, 1);
  let step = 1;
  for (let scale = 1; top / step > 5; scale *= 10) {
    step = [1, 2, 5].map((n) => n * scale).find((s) => top / s <= 5) ?? scale * 10;
  }
  const found = [];
  for (let value = 0; value < top + step; value += step) found.push(value);
  return found;
}

// Square on the baseline, rounded at the end that says the value.
function barPath(x, y, width, height) {
  const r = Math.min(4, width / 2, height);
  return (
    `M${x} ${y + height}v${r - height}q0 ${-r} ${r} ${-r}h${width - 2 * r}` +
    `q${r} 0 ${r} ${r}v${height - r}z`
  );
}

function tipText(entry) {
  const lines = SERIES.map(([key, name]) => `${name}: ${entry[key] ?? "not recorded"}`);
  return [dayLabel(entry.day), ...lines].join("\n");
}

export class Productivity {
  constructor() {
    this.message = null;
    this.range = RANGES[0][0];
    this.tally = document.getElementById("tally");
    this.dialog = document.getElementById("productivity");
    this.ranges = document.getElementById("productivity-ranges");
    this.summary = document.getElementById("productivity-summary");
    this.chart = document.getElementById("productivity-chart");
    this.table = document.getElementById("productivity-table");
    this.tally.addEventListener("click", () => this.open());
    document
      .getElementById("productivity-close")
      .addEventListener("click", () => this.dialog.close());
    for (const [days, label] of RANGES) {
      const button = element("button", "", label);
      button.type = "button";
      button.dataset.days = days;
      button.addEventListener("click", () => {
        this.range = days;
        this.renderDialog();
      });
      this.ranges.append(button);
    }
  }

  set(message) {
    this.message = message;
    this.renderTally();
    if (this.dialog.open) this.renderDialog();
  }

  open() {
    this.dialog.showModal();
    this.renderDialog();
  }

  renderTally() {
    const cells = SERIES.flatMap(([key, name]) => {
      const summary = this.message[key];
      return [
        element("span", `tally-today ${key}`, summary ? String(summary.today) : "–"),
        element("span", `tally-name ${key}`, `${name} today`),
        element("span", "tally-stat", summary ? `avg ${average(summary)}` : ""),
        element("span", "tally-stat", summary ? `max ${summary.record}` : ""),
      ];
    });
    this.tally.replaceChildren(...cells);
    this.tally.hidden = false;
  }

  renderDialog() {
    for (const button of this.ranges.children) {
      button.setAttribute("aria-pressed", String(Number(button.dataset.days) === this.range));
    }
    const days = this.message.days.slice(-Math.min(this.range, this.message.days.length));
    this.summary.replaceChildren(...SERIES.map(([key, name]) => this.legend(key, name)));
    this.renderChart(days);
    this.renderTable(days);
  }

  legend(key, name) {
    const summary = this.message[key];
    const node = element("div", "");
    node.append(element("span", `legend-name ${key}`, name));
    let text = "waiting for GitHub";
    if (summary) {
      const on = summary.record_day ? ` (${dayLabel(summary.record_day)})` : "";
      text = `${summary.today} today · ${average(summary)} average · ${summary.record} record${on}`;
    }
    node.append(element("span", "legend-stats", text));
    return node;
  }

  renderChart(days) {
    const root = svg("svg", { viewBox: `0 0 ${WIDTH} ${HEIGHT}`, role: "img" });
    root.setAttribute("aria-label", "Prompts and PRs per day");
    const values = days.flatMap((entry) => SERIES.map(([key]) => entry[key] ?? 0));
    const marks = ticks(Math.max(...values));
    const top = marks.at(-1);
    const y = (value) => TOP + PLOT_HEIGHT * (1 - value / top);
    for (const value of marks) {
      root.append(svg("line", { class: "chart-grid", x1: LEFT, x2: WIDTH, y1: y(value), y2: y(value) }));
      const at = { class: "chart-label", x: LEFT - 8, y: y(value) + 4, "text-anchor": "end" };
      root.append(svg("text", at, String(value)));
    }

    const slot = PLOT_WIDTH / days.length;
    const gap = slot >= 10 ? 2 : 0;
    const bar = Math.min(MAX_BAR, (slot * 0.8 - gap) / 2);
    const labelEvery = Math.ceil(DAY_LABEL_WIDTH / slot);
    const tip = element("div", "chart-tip");
    tip.hidden = true;
    days.forEach((entry, index) => {
      const center = LEFT + slot * (index + 0.5);
      SERIES.forEach(([key], which) => {
        const value = entry[key];
        if (!value) return;
        const x = which === 0 ? center - gap / 2 - bar : center + gap / 2;
        const d = barPath(x, y(value), bar, y(0) - y(value));
        root.append(svg("path", { class: `chart-bar ${key}`, d }));
      });
      // Counted back from today, so the last label is always today's.
      const fromEnd = days.length - 1 - index;
      if (fromEnd % labelEvery === 0) {
        // One against an edge is anchored to it, not cut off by it.
        const anchor = WIDTH - center < 24 ? "end" : "middle";
        const x = anchor === "end" ? WIDTH : center;
        const at = { class: "chart-label", x, y: HEIGHT - 6, "text-anchor": anchor };
        root.append(svg("text", at, dayLabel(entry.day)));
      }
      const hit = svg("rect", {
        class: "chart-hit",
        x: center - slot / 2,
        y: TOP,
        width: slot,
        height: PLOT_HEIGHT,
      });
      hit.dataset.day = entry.day;
      hit.addEventListener("pointerenter", () => {
        tip.textContent = tipText(entry);
        tip.hidden = false;
        const right = center > WIDTH / 2;
        tip.style.left = right ? "" : `${((center + slot) / WIDTH) * 100}%`;
        tip.style.right = right ? `${(1 - (center - slot) / WIDTH) * 100}%` : "";
      });
      hit.addEventListener("pointerleave", () => {
        tip.hidden = true;
      });
      root.append(hit);
    });
    this.chart.replaceChildren(root, tip);
  }

  renderTable(days) {
    const table = element("table", "");
    const head = table.insertRow();
    for (const name of ["Day", ...SERIES.map(([, label]) => label)]) {
      head.append(element("th", "", name));
    }
    for (const entry of [...days].reverse()) {
      const row = table.insertRow();
      row.append(element("td", "", entry.day));
      for (const [key] of SERIES) row.append(element("td", "", String(entry[key] ?? "–")));
    }
    this.table.replaceChildren(table);
  }
}
