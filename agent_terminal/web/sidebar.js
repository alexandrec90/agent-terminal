// The tab groups: one per folder with a session or an open PR, in the order they first
// appeared. PR text was written by whoever opened it, so it only ever goes in as text.

const STATUS_TEXT = {
  starting: "starting",
  working: "working",
  blocked: "needs your input",
  idle: "ready",
  shell: "PowerShell",
  exited: "exited",
};

const ATTENTION_TEXT = { input: "needs your input", finished: "finished" };

const CHECK_MARK = { passing: "✓", failing: "✗", pending: "…", none: "" };

function group(groups, path, name) {
  if (!groups.has(path)) {
    groups.set(path, { path, name, tabs: [], prs: [] });
  }
  return groups.get(path);
}

export function groupItems(tabs, prs) {
  const groups = new Map();
  for (const tab of tabs) {
    group(groups, tab.project_path, tab.project).tabs.push(tab);
  }
  for (const pr of prs) {
    group(groups, pr.repo, pr.project).prs.push(pr);
  }
  return [...groups.values()];
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

function prChipText(pr) {
  if (pr.state === "MERGED") return `PR #${pr.number} merged`;
  if (pr.state === "CLOSED") return `PR #${pr.number} closed`;
  if (pr.mergeable === "CONFLICTING") return `PR #${pr.number} conflicts`;
  return `PR #${pr.number} ${CHECK_MARK[pr.checks]}`.trim();
}

function prChip(pr, actions) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `tab-chip pr ${pr.state.toLowerCase()} checks-${pr.checks}`;
  chip.textContent = prChipText(pr);
  chip.title = pr.title;
  chip.addEventListener("click", (event) => {
    event.stopPropagation();
    actions.review(pr);
  });
  return chip;
}

function shipChip(ship) {
  const refused = ship === "refused";
  const chip = span(`tab-chip ship ${ship}`, refused ? "ship refused" : "ship queued");
  chip.title = refused
    ? "The fix pass tried this intent and could not ship it"
    : "Waiting for the fix pass to open a PR";
  return chip;
}

function tabChip(tab, actions) {
  if (tab.pr) return prChip(tab.pr, actions);
  return tab.ship ? shipChip(tab.ship) : null;
}

function renderTab(tab, activeId, actions) {
  const row = document.createElement("div");
  row.className = tabClasses(tab, activeId);
  row.dataset.id = tab.id;
  row.title = "Right-click to mark for later";

  const main = span("tab-main", "");
  main.append(span("tab-title", tab.title || tab.agent), span("tab-meta", tabMeta(tab)));
  const chip = tabChip(tab, actions);
  if (chip) main.append(chip);

  const close = document.createElement("button");
  close.type = "button";
  close.className = "tab-close";
  close.title = "Close tab";
  close.textContent = "×";
  close.addEventListener("click", (event) => {
    event.stopPropagation();
    actions.close(tab.id);
  });

  row.append(span("tab-dot", ""), main, close);
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
  row.append(span("tab-dot", ""), main);
  row.addEventListener("click", () => actions.review(pr));
  return row;
}

function renderGroup(entry, view, actions) {
  const section = document.createElement("section");
  section.className = "group";
  section.dataset.project = entry.name;
  section.dataset.path = entry.path;

  const head = document.createElement("div");
  head.className = "group-head";
  head.title = entry.path;
  const add = document.createElement("button");
  add.type = "button";
  add.className = "group-add";
  add.title = `New session in ${entry.name}`;
  add.textContent = "+";
  add.addEventListener("click", () => actions.newInFolder(entry));
  head.append(span("group-name", entry.name), add);

  section.append(
    head,
    ...entry.tabs.map((tab) => renderTab(tab, view.activeId, actions)),
    ...entry.prs.map((pr) => renderPr(pr, view.reviewing, actions)),
  );
  return section;
}

// `view` says what is on stage: the active tab, and any PR being reviewed over it.
export function renderSidebar(container, tabs, prs, view, actions) {
  const scroll = container.scrollTop;
  container.replaceChildren(
    ...groupItems(tabs, prs).map((entry) => renderGroup(entry, view, actions)),
  );
  container.scrollTop = scroll;
}
