// The review panel: one PR, over the terminals. Everything from GitHub goes in as text.

import { renderDiff } from "./diff.js";

const OUTCOME_MARK = { passing: "✓", failing: "✗", pending: "…" };
const STATE_WORD = { OPEN: "open", MERGED: "merged", CLOSED: "closed" };

function element(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function button(label, className, onClick) {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function mergeWarning(detail) {
  if (detail.checks === "failing") return "Checks are failing on this PR.";
  if (detail.checks === "pending") return "Checks are still running on this PR.";
  return "";
}

function subtitle(detail) {
  const parts = [`#${detail.number}`, `${detail.base} ← ${detail.head}`];
  if (detail.author) parts.push(detail.author);
  if (detail.draft) parts.push("draft");
  if (detail.automerge) parts.push("automerge");
  return parts.join(" · ");
}

function changeSummary(detail) {
  const files = detail.changed_files === 1 ? "1 file" : `${detail.changed_files} files`;
  return `${files}, +${detail.additions} −${detail.deletions}`;
}

function checksSection(detail) {
  if (detail.check_rows.length === 0) return null;
  const section = element("section", "review-section");
  section.append(element("h3", "", "Checks"));
  for (const row of detail.check_rows) {
    section.append(element("div", `check ${row.outcome}`, `${OUTCOME_MARK[row.outcome]} ${row.name}`));
  }
  return section;
}

function commentsSection(detail) {
  if (detail.comments.length === 0) return null;
  const section = element("section", "review-section");
  section.append(element("h3", "", "Comments"));
  for (const comment of detail.comments) {
    section.append(element("div", "comment-author", comment.author));
    section.append(element("div", "review-body", comment.body));
  }
  return section;
}

export class ReviewPanel {
  constructor(root, actions) {
    this.root = root;
    this.actions = actions;
    this.pr = null;
    this.detail = null;
    this.hasPrompt = false;
    this.linkedTab = null;
    this.busy = false;
    this.confirmDialog = document.getElementById("confirm");
    this.confirmText = document.getElementById("confirm-text");
    this.confirmOk = document.getElementById("confirm-ok");
    document.getElementById("confirm-cancel").addEventListener("click", () => {
      this.confirmDialog.close();
    });
    this.confirmOk.addEventListener("click", () => {
      this.confirmDialog.close();
      this.pending?.();
    });
  }

  isShowing(pr) {
    return this.pr?.repo === pr.repo && this.pr?.number === pr.number;
  }

  open(pr) {
    this.pr = { repo: pr.repo, number: pr.number };
    this.detail = null;
    this.busy = false;
    this.root.hidden = false;
    this.root.replaceChildren(element("p", "review-note", `Loading PR #${pr.number}…`));
    this.actions.view(this.pr);
  }

  close() {
    this.pr = null;
    this.detail = null;
    this.root.hidden = true;
    this.root.replaceChildren();
  }

  // Which tab holds this PR, if any: what the panel offers to go back to.
  setLinkedTab(tabId) {
    if (tabId === this.linkedTab) return;
    this.linkedTab = tabId;
    if (this.detail) this.render();
  }

  setDetail(message) {
    if (!this.isShowing(message)) return;
    this.detail = message.detail;
    this.hasPrompt = message.has_prompt;
    this.busy = false;
    this.render();
  }

  setError(message) {
    if (!this.isShowing(message)) return;
    this.busy = false;
    if (this.detail) this.render(message.message);
    else this.root.replaceChildren(element("p", "review-error", message.message));
  }

  ask(text, label, run) {
    this.confirmText.textContent = text;
    this.confirmOk.textContent = label;
    this.pending = () => {
      this.busy = true;
      this.render();
      run();
    };
    this.confirmDialog.showModal();
  }

  merge() {
    const { detail } = this;
    const warning = mergeWarning(detail);
    const text = `Squash and merge #${detail.number} into ${detail.base}? ${warning}`.trim();
    this.ask(text, "Merge", () => this.actions.merge(this.pr));
  }

  closePr() {
    const text = `Close #${this.detail.number} without merging?`;
    this.ask(text, "Close PR", () => this.actions.closePr(this.pr));
  }

  toolbar() {
    const { detail } = this;
    const bar = element("div", "review-actions");
    if (this.linkedTab) {
      bar.append(button("Back to terminal", "", () => this.actions.showTab(this.linkedTab)));
    } else if (this.hasPrompt) {
      bar.append(button("Open prompt", "", () => this.actions.resume(this.pr)));
    }
    bar.append(button("Open on GitHub", "", () => this.actions.openUrl(detail.url)));
    if (detail.state === "OPEN") {
      const merge = button("Merge", "primary", () => this.merge());
      merge.disabled = this.busy || detail.mergeable === "CONFLICTING" || detail.draft;
      const close = button("Close PR", "danger", () => this.closePr());
      close.disabled = this.busy;
      bar.append(close, merge);
    }
    return bar;
  }

  header() {
    const { detail } = this;
    const head = element("header", "review-head");
    const state = element("span", `review-state ${detail.state.toLowerCase()}`);
    state.textContent = STATE_WORD[detail.state] ?? detail.state;
    const title = element("h2", "");
    title.append(state, document.createTextNode(` ${detail.title}`));
    head.append(title, element("p", "review-sub", subtitle(detail)), this.toolbar());
    return head;
  }

  // Why the merge button is off, or what just went wrong.
  notices(error) {
    const { detail } = this;
    const notes = [];
    if (error) notes.push(element("p", "review-error", error));
    if (detail.state === "OPEN" && detail.mergeable === "CONFLICTING") {
      notes.push(element("p", "review-note", "This PR conflicts with its base."));
    }
    if (detail.state === "OPEN" && detail.draft) {
      notes.push(element("p", "review-note", "This PR is a draft."));
    }
    return notes;
  }

  filesSection() {
    const { detail } = this;
    const section = element("section", "review-section");
    section.append(element("h3", "", `Changes: ${changeSummary(detail)}`));
    if (detail.diff_truncated) {
      section.append(element("p", "review-note", "Too long to show whole; the rest is on GitHub."));
    }
    const files = element("div", "diff");
    renderDiff(files, detail.diff);
    section.append(files);
    return section;
  }

  render(error = "") {
    const scroll = this.root.scrollTop;
    const { detail } = this;
    const parts = [this.header(), ...this.notices(error)];
    if (detail.body.trim()) parts.push(element("div", "review-body", detail.body));
    parts.push(checksSection(detail), commentsSection(detail), this.filesSection());
    this.root.replaceChildren(...parts.filter(Boolean));
    this.root.scrollTop = scroll;
  }
}
