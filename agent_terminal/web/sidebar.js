// The tab groups: one per folder with a session or an open PR, in the order the user
// dragged them into, then the order they first appeared. PR text was written by whoever
// opened it, so it only ever goes in as text.

const STATUS_TEXT = {
  starting: "starting",
  working: "working",
  blocked: "needs your input",
  idle: "ready",
  shell: "PowerShell",
  exited: "exited",
};

const ATTENTION_TEXT = { input: "needs your input", finished: "finished" };

// Each names a symbol in icons/ui.svg.
const CHECK_ICON = { passing: "check", failing: "failing", pending: "pending", none: "pr-open" };

const SVG = "http://www.w3.org/2000/svg";

// One of the interface icons, colored by the CSS `color` of whatever holds it. Drawn for
// 16px: `.icon` keeps it there.
function icon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.dataset.icon = name;
  const use = document.createElementNS(SVG, "use");
  use.setAttribute("href", `icons/ui.svg#${name}`);
  svg.append(use);
  return svg;
}

function group(groups, path, name) {
  if (!groups.has(path)) {
    groups.set(path, { path, name, tabs: [], prs: [] });
  }
  return groups.get(path);
}

export function groupItems(tabs, prs, order = []) {
  const groups = new Map();
  for (const tab of tabs) {
    group(groups, tab.project_path, tab.project).tabs.push(tab);
  }
  for (const pr of prs) {
    group(groups, pr.repo, pr.project).prs.push(pr);
  }
  const rank = (entry) => {
    const index = order.indexOf(entry.path);
    return index < 0 ? Infinity : index;
  };
  // Stable: the groups the order does not name keep the order they appeared in.
  return [...groups.values()].sort((a, b) => {
    const [ra, rb] = [rank(a), rank(b)];
    return ra === rb ? 0 : ra < rb ? -1 : 1;
  });
}

const MINUTE = 60;
const UNITS = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", MINUTE],
];

// "just now", "5 minutes ago", "1 day ago": the largest whole unit, rounded down. Both
// times are in seconds.
export function timeAgo(then, now) {
  const elapsed = Math.max(0, now - then);
  for (const [unit, seconds] of UNITS) {
    const count = Math.floor(elapsed / seconds);
    if (count >= 1) return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
  }
  return "just now";
}

function lastUpdated(tab) {
  return tab.updated_at ?? tab.created_at;
}

function timeLabel(tab) {
  const at = lastUpdated(tab);
  const label = span("tab-time", timeAgo(at, Date.now() / 1000));
  label.dataset.at = at;
  label.title = `Last updated ${new Date(at * 1000).toLocaleString()}`;
  return label;
}

// Keeps every "… ago" true without redrawing the sidebar.
export function refreshTimes(container) {
  const now = Date.now() / 1000;
  for (const label of container.querySelectorAll(".tab-time")) {
    label.textContent = timeAgo(Number(label.dataset.at), now);
  }
}

// ---- Dragging ----
//
// A tab moves only among its own group's tabs, and a group only among the groups. Only
// where a drop is allowed does `dragover` call `preventDefault`, so anywhere else the
// pointer shows that it is not. The sidebar is redrawn on every state message, which
// would take the dragged row away mid-drag: `actions.dragging` holds that off.

let dragged = null;

function clearMarks(container) {
  for (const marked of container.querySelectorAll(".drop-before, .drop-after")) {
    marked.classList.remove("drop-before", "drop-after");
  }
}

function lowerHalf(element, event) {
  const box = element.getBoundingClientRect();
  return event.clientY > box.top + box.height / 2;
}

function startDrag(event, what, actions) {
  dragged = what;
  event.dataTransfer.effectAllowed = "move";
  // Some engines start no drag without data.
  event.dataTransfer.setData("text/plain", "");
  actions.dragging(true);
}

function endDrag(element, actions) {
  dragged = null;
  const container = element.closest("#groups") ?? document;
  clearMarks(container);
  for (const lifted of container.querySelectorAll(".dragging")) {
    lifted.classList.remove("dragging");
  }
  actions.dragging(false);
}

// Over `element`, a `kind` drop is allowed where `accepts` says so: mark which side of it
// the dragged item would land.
function dropTarget(element, kind, accepts, land) {
  element.addEventListener("dragover", (event) => {
    if (dragged?.kind !== kind || !accepts(dragged)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "move";
    clearMarks(element.closest("#groups") ?? document);
    element.classList.add(lowerHalf(element, event) ? "drop-after" : "drop-before");
  });
  element.addEventListener("drop", (event) => {
    if (dragged?.kind !== kind || !accepts(dragged)) return;
    event.preventDefault();
    event.stopPropagation();
    land(dragged, lowerHalf(element, event));
  });
}

function draggableTab(row, tab, groupTabs, actions) {
  row.draggable = true;
  row.addEventListener("dragstart", (event) => {
    event.stopPropagation();
    row.classList.add("dragging");
    startDrag(event, { kind: "tab", id: tab.id, path: tab.project_path }, actions);
  });
  row.addEventListener("dragend", () => endDrag(row, actions));
  dropTarget(
    row,
    "tab",
    (what) => what.path === tab.project_path && what.id !== tab.id,
    (what, after) => {
      const before = after ? nextTabId(groupTabs, tab.id) : tab.id;
      if (before !== what.id) actions.moveTab(what.id, before);
    },
  );
}

// The tab after `id` in its group, or null when it is the last: what `move_tab` takes as
// `before` for a tab dropped below `id`.
export function nextTabId(groupTabs, id) {
  const index = groupTabs.findIndex((tab) => tab.id === id);
  return groupTabs[index + 1]?.id ?? null;
}

// Where a group dropped beside `target` puts every group: the paths, in their new order.
export function reorderedPaths(paths, moved, target, after) {
  const rest = paths.filter((path) => path !== moved);
  const index = rest.indexOf(target);
  if (index < 0) return paths;
  rest.splice(after ? index + 1 : index, 0, moved);
  return rest;
}

function draggableGroup(section, head, entry, paths, actions) {
  head.draggable = true;
  head.addEventListener("dragstart", (event) => {
    section.classList.add("dragging");
    startDrag(event, { kind: "group", path: entry.path }, actions);
  });
  head.addEventListener("dragend", () => endDrag(section, actions));
  dropTarget(
    section,
    "group",
    (what) => what.path !== entry.path,
    (what, after) => actions.orderGroups(reorderedPaths(paths, what.path, entry.path, after)),
  );
}

function needsInput(tab) {
  return tab.awaiting_trust || tab.attention === "input";
}

function tabClasses(tab, activeId) {
  const classes = ["tab", tab.status];
  if (tab.id === activeId) classes.push("active");
  if (tab.parked) classes.push("parked");
  else if (needsInput(tab)) classes.push("attention", "input");
  else if (tab.attention === "finished") classes.push("attention", "finished");
  return classes.join(" ");
}

function statusWord(tab) {
  if (tab.parked) return "for later";
  if (tab.awaiting_trust) return "trust this folder to go on";
  return ATTENTION_TEXT[tab.attention] ?? STATUS_TEXT[tab.status];
}

// The states with an icon of their own; the rest keep the plain dot.
function stateIcon(tab) {
  if (tab.parked) return "parked";
  if (needsInput(tab) || tab.status === "blocked") return "input";
  if (tab.attention === "finished") return "finished";
  if (tab.status === "exited") return "exited";
  if (tab.status === "working") return "working";
  return null;
}

function stateMark(name) {
  const mark = span("tab-dot", "");
  if (name) mark.append(icon(name));
  return mark;
}

function tabMeta(tab) {
  const parts = [tab.agent];
  if (tab.worktree) parts.push("worktree");
  parts.push(statusWord(tab));
  return parts.join(" · ");
}

function span(className, text) {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  return element;
}

// The icon says where the PR stands; the text says which PR, and names any state the
// icon alone would leave a guess.
function prChipFace(pr) {
  if (pr.state === "MERGED") return ["pr-merged", `PR #${pr.number} merged`];
  if (pr.state === "CLOSED") return ["pr-closed", `PR #${pr.number} closed`];
  if (pr.mergeable === "CONFLICTING") return ["conflict", `PR #${pr.number} conflicts`];
  return [CHECK_ICON[pr.checks] ?? "pr-open", `PR #${pr.number}`];
}

function prChip(pr, actions) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `tab-chip pr ${pr.state.toLowerCase()} checks-${pr.checks}`;
  const [name, text] = prChipFace(pr);
  chip.append(icon(name), text);
  chip.title = pr.title;
  chip.addEventListener("click", (event) => {
    event.stopPropagation();
    actions.review(pr);
  });
  return chip;
}

function shipChip(ship) {
  const refused = ship === "refused";
  const chip = span(`tab-chip ship ${ship}`, "");
  chip.append(icon("ship"), refused ? "ship refused" : "ship queued");
  chip.title = refused
    ? "The fix pass tried this intent and could not ship it"
    : "Waiting for the fix pass to open a PR";
  return chip;
}

function tabChip(tab, actions) {
  if (tab.pr) return prChip(tab.pr, actions);
  return tab.ship ? shipChip(tab.ship) : null;
}

function renderTab(tab, groupTabs, activeId, actions) {
  const row = document.createElement("div");
  row.className = tabClasses(tab, activeId);
  row.dataset.id = tab.id;
  row.title = "Right-click to mark for later; drag to reorder";
  draggableTab(row, tab, groupTabs, actions);

  const main = span("tab-main", "");
  const line = span("tab-line", "");
  line.append(span("tab-title", tab.title || tab.agent), timeLabel(tab));
  main.append(line, span("tab-meta", tabMeta(tab)));
  const chip = tabChip(tab, actions);
  if (chip) main.append(chip);

  const close = document.createElement("button");
  close.type = "button";
  close.className = "tab-close";
  close.title = "Close tab";
  close.setAttribute("aria-label", "Close tab");
  close.append(icon("close"));
  close.addEventListener("click", (event) => {
    event.stopPropagation();
    actions.close(tab.id);
  });

  row.append(stateMark(stateIcon(tab)), main, close);
  row.addEventListener("click", () => actions.activate(tab.id));
  row.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    actions.togglePark(tab.id);
  });
  return row;
}

function prMeta(pr) {
  const parts = [`#${pr.number}`];
  if (pr.draft) parts.push("draft");
  if (pr.mergeable === "CONFLICTING") parts.push("conflicts");
  if (pr.checks !== "none") parts.push(`checks ${pr.checks}`);
  if (pr.has_prompt) parts.push("prompt on disk");
  return parts.join(" · ");
}

function renderPr(pr, reviewing, actions) {
  const row = document.createElement("div");
  const selected = reviewing?.repo === pr.repo && reviewing?.number === pr.number;
  row.className = `tab pr-row checks-${pr.checks}${selected ? " active" : ""}`;
  row.dataset.pr = pr.number;
  row.title = pr.title;
  const main = span("tab-main", "");
  main.append(span("tab-title", pr.title), span("tab-meta", prMeta(pr)));
  row.append(stateMark("pr-open"), main);
  row.addEventListener("click", () => actions.review(pr));
  return row;
}

function renderGroup(entry, paths, view, actions) {
  const section = document.createElement("section");
  section.className = "group";
  section.dataset.project = entry.name;
  section.dataset.path = entry.path;

  const head = document.createElement("div");
  head.className = "group-head";
  head.title = `${entry.path}\nDrag to reorder`;
  draggableGroup(section, head, entry, paths, actions);
  const add = document.createElement("button");
  add.type = "button";
  add.className = "group-add";
  add.title = `New session in ${entry.name}`;
  add.setAttribute("aria-label", add.title);
  add.append(icon("add"));
  add.addEventListener("click", () => actions.newInFolder(entry));
  head.append(span("group-name", entry.name), add);

  section.append(
    head,
    ...entry.tabs.map((tab) => renderTab(tab, entry.tabs, view.activeId, actions)),
    ...entry.prs.map((pr) => renderPr(pr, view.reviewing, actions)),
  );
  return section;
}

// `view` says what is on stage: the active tab, and any PR being reviewed over it, and
// `order` how the user arranged the groups.
export function renderSidebar(container, tabs, prs, view, actions, order = []) {
  const scroll = container.scrollTop;
  const entries = groupItems(tabs, prs, order);
  const paths = entries.map((entry) => entry.path);
  container.replaceChildren(...entries.map((entry) => renderGroup(entry, paths, view, actions)));
  container.scrollTop = scroll;
}
