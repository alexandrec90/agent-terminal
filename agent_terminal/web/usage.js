// Claude's plan limits under the tally: a bar each for how much is used, with a mark at
// how far through its window the clock is. A bar past its mark is ahead of an even pace.

// The mark moves with the clock, not only when a poll answers.
const REDRAW_MS = 60 * 1000;
// Within this of the mark is on pace, either way: the percentages are whole numbers.
const ON_PACE = 2;
const NEAR_LIMIT = 90;

function element(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

// How far through its window the clock is, 0 to 100, or null with no window known.
export function elapsedPercent(limit, nowSeconds) {
  if (limit.resets_at == null || limit.window_seconds == null) return null;
  const share = 1 - (limit.resets_at - nowSeconds) / limit.window_seconds;
  return Math.min(Math.max(share, 0), 1) * 100;
}

// "ahead", "behind" or "even" of the pace that would use it up as its window ends.
export function pace(percent, elapsed) {
  if (elapsed === null) return "even";
  if (percent > elapsed + ON_PACE) return "ahead";
  if (percent < elapsed - ON_PACE) return "behind";
  return "even";
}

function resetText(resetsAt, nowSeconds) {
  if (resetsAt == null) return "";
  const at = new Date(resetsAt * 1000);
  const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const sameDay = at.toDateString() === new Date(nowSeconds * 1000).toDateString();
  const when = sameDay ? time : `${at.toLocaleDateString([], { weekday: "short" })} ${time}`;
  return `resets ${when}`;
}

export function describeLimit(limit, nowSeconds) {
  const used = Math.round(limit.percent);
  const elapsed = elapsedPercent(limit, nowSeconds);
  const parts = [`${limit.label}: ${used}% used`];
  if (elapsed !== null) {
    const words = { ahead: "ahead of", behind: "behind", even: "on" };
    parts.push(`${Math.round(elapsed)}% of the window gone, ${words[pace(used, elapsed)]} pace`);
  }
  const reset = resetText(limit.resets_at, nowSeconds);
  if (reset) parts.push(reset);
  return parts.join(" · ");
}

export class UsageMeters {
  constructor(root) {
    this.root = root;
    this.limits = null;
    setInterval(() => this.render(), REDRAW_MS);
  }

  set(message) {
    this.limits = message.limits;
    this.render();
  }

  render() {
    const limits = this.limits ?? [];
    this.root.hidden = limits.length === 0;
    const now = Date.now() / 1000;
    this.root.replaceChildren(...limits.map((limit) => this.meter(limit, now)));
  }

  meter(limit, now) {
    const used = Math.min(Math.max(limit.percent, 0), 100);
    const elapsed = elapsedPercent(limit, now);
    const row = element("div", "meter");
    row.dataset.kind = limit.kind;
    row.dataset.pace = used >= NEAR_LIMIT ? "near" : pace(Math.round(used), elapsed);
    row.title = describeLimit(limit, now);

    const bar = element("span", "meter-bar");
    bar.setAttribute("role", "meter");
    bar.setAttribute("aria-label", limit.label);
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(Math.round(used)));
    bar.setAttribute("aria-valuetext", row.title);
    const fill = element("span", "meter-fill");
    fill.style.width = `${used}%`;
    bar.append(fill);
    if (elapsed !== null) {
      const mark = element("span", "meter-pace");
      mark.style.left = `${elapsed}%`;
      bar.append(mark);
    }

    row.append(
      element("span", "meter-name", limit.label),
      bar,
      element("span", "meter-value", `${Math.round(used)}%`),
    );
    return row;
  }
}
