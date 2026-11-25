// The tab groups: one per folder with a session or an open PR, in the order they first
// appeared. PR text was written by whoever opened it, so it only ever goes in as text.

import { icon } from "./icons.js";

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

function headButton(className, iconName, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon(iconName));
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  return button;
}

// What a folded group still says: how much is in it, and whether any of it wants the user.
function foldedSummary(entry) {
  const parts = [];
  if (entry.tabs.length) parts.push(`${entry.tabs.length}`);
  if (entry.prs.length) parts.push(`${entry.prs.length} PR`);
  const summary = span("group-count", parts.join(" · "));
  if (entry.tabs.some((tab) => !tab.parked && needsInput(tab))) {
    summary.classList.add("input");
    summary.title = "A session in here needs your input";
  } else if (entry.tabs.some((tab) => !tab.parked && tab.attention === "finished")) {
    summary.classList.add("finished");
    summary.title = "A session in here has finished";
  }
  return summary;
}

function renderHead(entry, folded, actions) {
  const head = document.createElement("div");
  head.className = "group-head";
  head.title = entry.path;
  head.setAttribute("role", "button");
  head.setAttribute("aria-expanded", String(!folded));
  head.addEventListener("click", () => actions.toggleGroup(entry.path));

  const fold = span("group-fold", "");
  fold.append(icon("chevron"));
  head.append(fold, span("group-name", entry.name));
  if (folded) head.append(foldedSummary(entry));
  const closeLabel = entry.tabs.length ? `Close every session in ${entry.name}` : `Hide ${entry.name}`;
  head.append(
    headButton("group-add", "add", `New session in ${entry.name}`, () => actions.newInFolder(entry)),
    headButton("group-close", "close", closeLabel, () => actions.closeGroup(entry)),
  );
  return head;
}

function renderGroup(entry, view, actions) {
  const section = document.createElement("section");
  const folded = view.folded.has(entry.path);
  section.className = folded ? "group folded" : "group";
  section.dataset.project = entry.name;
  section.dataset.path = entry.path;
  section.append(renderHead(entry, folded, actions));
  if (!folded) {
    section.append(
      ...entry.tabs.map((tab) => renderTab(tab, view.activeId, actions)),
      ...entry.prs.map((pr) => renderPr(pr, view.reviewing, actions)),
    );
  }
  return section;
}

// A hidden group stays hidden only while it holds nothing but PRs: a session opened there
// brings it back.
export function visibleGroups(tabs, prs, hidden) {
  return groupItems(tabs, prs).filter((entry) => entry.tabs.length || !hidden.has(entry.path));
}

// `view` says what is on stage: the active tab, any PR being reviewed over it, which groups
// are folded (a Set of paths) and which are hidden.
export function renderSidebar(container, tabs, prs, view, actions) {
  const scroll = container.scrollTop;
  container.replaceChildren(
    ...visibleGroups(tabs, prs, view.hidden).map((entry) => renderGroup(entry, view, actions)),
  );
  container.scrollTop = scroll;
}
