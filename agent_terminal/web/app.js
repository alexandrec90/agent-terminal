// First, so its listeners are in place before any other module runs.
import { reportErrorsTo } from "./errors.js";
import { AppBar } from "./appbar.js";
import { confirmAction } from "./confirm.js";
import { connect } from "./connection.js";
import { NewTabDialog } from "./dialog.js";
import { Productivity } from "./productivity.js";
import { ReviewPanel } from "./review.js";
import { refreshTimes, renderSidebar } from "./sidebar.js";
import { Terminals } from "./terminals.js";

const groups = document.getElementById("groups");
const empty = document.getElementById("empty");

let tabs = [];
let prs = [];
let groupOrder = [];
let activeId = null;
let folded = new Set();
let hidden = new Set();
// Groups that stay with no tab in them, `{path, name}` each.
let kept = [];
let synced = false;
let appStatus = null;
// Mid-drag, a redraw would take the dragged row away: hold it until the drop.
let dragging = false;
let renderHeld = false;

// Each "… ago" in the sidebar moves on by itself; a minute is its smallest step.
const TIME_REFRESH_MS = 30_000;

function plural(count, word) {
  return count === 1 ? `${count} ${word}` : `${count} ${word}s`;
}

// Closing sessions ends their agents; they resume only from the agent's own history.
function closeGroup(entry) {
  const close = () => send({ type: "close_group", path: entry.path });
  if (entry.tabs.length === 0) {
    close();
    return;
  }
  const working = entry.tabs.filter((tab) => tab.status === "working").length;
  const lines = [`Close ${plural(entry.tabs.length, "session")} in ${entry.name}?`];
  if (working) lines.push(`${plural(working, "agent")} still working will be stopped.`);
  confirmAction(lines, "Close group", close);
}

const actions = {
  activate: (id) => {
    review.close();
    send({ type: "activate", id });
    render();
    terminals.focusActive();
  },
  close: (id) => send({ type: "close", id }),
  togglePark: (id) => send({ type: "toggle_park", id }),
  moveTab: (id, before) => send({ type: "move_tab", id, before }),
  orderGroups: (paths) => send({ type: "order_groups", paths }),
  dragging: (active) => {
    dragging = active;
    if (!active && renderHeld) render();
  },
  newInFolder: (entry) => dialog.open({ path: entry.path, name: entry.name }),
  toggleGroup: (path) => send({ type: "fold_group", path, folded: !folded.has(path) }),
  closeGroup,
  land: (id) => send({ type: "land", id }),
  review: (pr) => {
    // Out of the terminal underneath, or typing here would reach its agent unseen.
    document.activeElement?.blur();
    review.open(pr);
    render();
  },
};

const dialog = new NewTabDialog({
  start: (choice) => send({ type: "create", ...choice }),
  inspect: (path) => send({ type: "inspect_folder", path }),
  browse: () => send({ type: "pick_folder" }),
  prepare: (path) => send({ type: "prepare_worktree", path }),
  listPast: (path) => send({ type: "past_sessions", path }),
  resume: (choice) => send({ type: "resume_session", ...choice }),
  listRemote: () => send({ type: "list_remote_repos" }),
  clone: (fullName) => send({ type: "clone_repo", full_name: fullName }),
  describeDeletion: (path) => send({ type: "describe_deletion", path }),
  deleteProject: (path, force) => send({ type: "delete_project", path, force }),
  listRetired: () => send({ type: "list_retired" }),
  restore: (name) => send({ type: "restore_project", name }),
  listPresets: () => send({ type: "list_presets" }),
  newProject: (choice) => send({ type: "new_project", ...choice }),
});

const review = new ReviewPanel(document.getElementById("review"), {
  view: (pr) => send({ type: "pr_view", ...pr }),
  merge: (pr) => send({ type: "pr_merge", ...pr }),
  closePr: (pr) => send({ type: "pr_close", ...pr }),
  resume: (pr) => {
    review.close();
    send({ type: "pr_resume", ...pr });
  },
  showTab: (id) => actions.activate(id),
  openUrl: (url) => send({ type: "open_url", url }),
});

const appBar = new AppBar(document.getElementById("app-bar"), {
  restart: (choice) => send({ type: "app_restart", ...choice }),
});

const productivity = new Productivity();

const send = connect(onMessage, () => {
  reportErrorsTo(send);
  send({ type: "hello", ...terminals.dimensions() });
  send({ type: "app_status" });
  send({ type: "productivity" });
});
const terminals = new Terminals(document.getElementById("terminals"), {
  send,
  openUrl: (url) => send({ type: "open_url", url }),
});

function linkedTab() {
  const { pr } = review;
  if (!pr) return null;
  const holder = tabs.find((tab) => tab.pr?.repo === pr.repo && tab.pr?.number === pr.number);
  return holder?.id ?? null;
}

function render() {
  renderHeld = dragging;
  if (dragging) return;
  const view = {
    activeId: review.pr ? null : activeId,
    reviewing: review.pr,
    folded,
    hidden,
    kept,
  };
  renderSidebar(groups, tabs, prs, view, actions, groupOrder);
  review.setLinkedTab(linkedTab());
  empty.hidden = tabs.length > 0 || review.pr !== null;
}

function applyState(state) {
  const known = new Set(state.tabs.map((tab) => tab.id));
  for (const id of [...terminals.entries.keys()]) {
    if (!known.has(id)) terminals.remove(id);
  }
  for (const tab of state.tabs) {
    if (!terminals.entries.has(tab.id)) terminals.create(tab.id);
  }
  tabs = state.tabs;
  prs = state.prs;
  groupOrder = state.group_order ?? [];
  folded = new Set(state.folded);
  hidden = new Set(state.hidden);
  kept = state.kept ?? [];
  if (state.active_id !== activeId) review.close();
  activeId = state.active_id;
  synced = true;
  terminals.show(activeId);
  render();
}

const dialogMessages = {
  projects: (message) => dialog.setProjects(message.projects),
  folder: (message) => dialog.setFolder(message),
  past_sessions: (message) => dialog.setPastSessions(message),
  picked: (message) => dialog.setPicked(message.path),
  remote_repos: (message) => dialog.remote.setRepos(message),
  cloned: (message) => dialog.remote.setCloned(message),
  deletion: (message) => dialog.setDeletion(message),
  deleted: (message) => dialog.setDeleted(message),
  retired: (message) => dialog.retired.setRetired(message),
  restored: (message) => dialog.retired.setRestored(message),
  presets: (message) => dialog.newProject.setPresets(message),
  new_project_output: (message) => dialog.newProject.addLine(message.line),
  new_project_done: (message) => dialog.newProject.setDone(message),
};

function onMessage(message) {
  if (Object.hasOwn(dialogMessages, message.type)) {
    dialogMessages[message.type](message);
    return;
  }
  switch (message.type) {
    case "state":
      applyState(message);
      break;
    case "output":
      if (message.replay) terminals.replay(message.id, message.data);
      else terminals.write(message.id, message.data);
      break;
    case "reset":
      terminals.reset(message.id);
      break;
    case "paste":
      terminals.paste(message.id, message.text);
      break;
    case "pr_detail":
      review.setDetail(message);
      break;
    case "pr_error":
      review.setError(message);
      break;
    case "app":
      appStatus = message;
      appBar.set(message);
      break;
    case "productivity":
      productivity.set(message);
      break;
    default:
      console.warn("unknown message", message);
  }
}

// What the end-to-end tests drive the page through; nothing in the app uses it.
window.__agentTerminal = {
  ready: () => synced,
  screen: (id) => terminals.screenText(id),
  promptShade: (id) => terminals.promptShade(id),
  mouseMode: (id) => terminals.mouseMode(id),
  send,
  tabs: () => tabs,
  prs: () => prs,
  app: () => appStatus,
};

setInterval(() => refreshTimes(groups), TIME_REFRESH_MS);

document.getElementById("new-group").addEventListener("click", () => dialog.open());
document.getElementById("empty-new").addEventListener("click", () => dialog.open());
document.getElementById("productivity").addEventListener("close", () => terminals.focusActive());
document.getElementById("new-tab").addEventListener("close", () => terminals.focusActive());
