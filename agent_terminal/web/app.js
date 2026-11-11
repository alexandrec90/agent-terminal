import { connect } from "./connection.js";
import { NewTabDialog } from "./dialog.js";
import { ReviewPanel } from "./review.js";
import { renderSidebar } from "./sidebar.js";
import { Terminals } from "./terminals.js";

const groups = document.getElementById("groups");
const empty = document.getElementById("empty");

let tabs = [];
let prs = [];
let activeId = null;
let synced = false;

const actions = {
  activate: (id) => {
    review.close();
    send({ type: "activate", id });
    render();
    terminals.focusActive();
  },
  close: (id) => send({ type: "close", id }),
  togglePark: (id) => send({ type: "toggle_park", id }),
  newInFolder: (entry) => dialog.open({ path: entry.path, name: entry.name }),
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

const send = connect(onMessage, () => {
  send({ type: "hello", ...terminals.dimensions() });
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
  const view = { activeId: review.pr ? null : activeId, reviewing: review.pr };
  renderSidebar(groups, tabs, prs, view, actions);
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
  if (state.active_id !== activeId) review.close();
  activeId = state.active_id;
  synced = true;
  terminals.show(activeId);
  render();
}

function onMessage(message) {
  switch (message.type) {
    case "state":
      applyState(message);
      break;
    case "output":
      terminals.write(message.id, message.data);
      break;
    case "reset":
      terminals.reset(message.id);
      break;
    case "paste":
      terminals.paste(message.id, message.text);
      break;
    case "projects":
      dialog.setProjects(message.projects);
      break;
    case "folder":
      dialog.setFolder(message);
      break;
    case "picked":
      dialog.setPicked(message.path);
      break;
    case "pr_detail":
      review.setDetail(message);
      break;
    case "pr_error":
      review.setError(message);
      break;
    default:
      console.warn("unknown message", message);
  }
}

// What the end-to-end tests drive the page through; nothing in the app uses it.
window.__agentTerminal = {
  ready: () => synced,
  screen: (id) => terminals.screenText(id),
  send,
  tabs: () => tabs,
  prs: () => prs,
};

document.getElementById("new-group").addEventListener("click", () => dialog.open());
document.getElementById("empty-new").addEventListener("click", () => dialog.open());
document.getElementById("new-tab").addEventListener("close", () => terminals.focusActive());
