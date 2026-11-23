// The sessions a group's "+" offers to bring back: those on disk in the project's folders
// that no tab has open. Titles are what the user typed, so they only ever go in as text.

import { icon, prChipFace, shipChip, span } from "./sidebar.js";

const AGENT_NAMES = { claude: "Claude", codex: "Codex" };
const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function ago(seconds) {
  if (seconds < MINUTE) return "just now";
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)}m ago`;
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)}h ago`;
  return `${Math.floor(seconds / DAY)}d ago`;
}

export function sessionMeta(session, now) {
  const parts = [AGENT_NAMES[session.agent] ?? session.agent];
  parts.push(session.branch ?? session.tree_name);
  parts.push(ago(Math.max(0, now - session.last_active)));
  return parts.join(" · ");
}

// Where the session's work stands, in the order it gets there: intent, PR, deployed.
export function stateChips(session) {
  const chips = [];
  if (session.ship) chips.push(shipChip(session.ship));
  const { pr } = session;
  if (!pr) return chips;
  const [name, text] = prChipFace(pr);
  const chip = span(`tab-chip pr ${pr.state.toLowerCase()} checks-${pr.checks}`, "");
  chip.append(icon(name), text);
  chip.title = pr.title;
  chips.push(chip);
  if (pr.state === "MERGED") {
    const deployed = span(`tab-chip deploy${pr.deployed ? " deployed" : ""}`, "");
    deployed.append(icon(pr.deployed ? "check" : "pending"));
    deployed.append(pr.deployed ? "deployed locally" : "not pulled locally");
    deployed.title = pr.deployed
      ? "The main checkout has this merge"
      : "The main checkout has not pulled this merge yet";
    chips.push(deployed);
  }
  return chips;
}

function untitled(session) {
  return `${AGENT_NAMES[session.agent] ?? session.agent} session`;
}

export function renderPastSession(session, now, resume) {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "past-session";
  row.dataset.session = session.session_id;
  row.title = session.tree;
  const main = span("tab-main", "");
  main.append(
    span("tab-title", session.title || untitled(session)),
    span("tab-meta", sessionMeta(session, now)),
  );
  const chips = stateChips(session);
  if (chips.length) {
    const holder = span("past-chips", "");
    holder.append(...chips);
    main.append(holder);
  }
  row.append(main);
  row.addEventListener("click", () => resume(session));
  return row;
}

export function renderPastSessions(container, sessions, now, resume) {
  if (sessions === null) {
    container.replaceChildren(span("past-note", "Looking for sessions…"));
    return;
  }
  if (sessions.length === 0) {
    container.replaceChildren(span("past-note", "No other sessions here."));
    return;
  }
  container.replaceChildren(...sessions.map((s) => renderPastSession(s, now, resume)));
}
