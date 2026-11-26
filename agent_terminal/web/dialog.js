// The new-session dialog. A group's "+" arrives with the folder fixed, and offers the
// project's unopened sessions to resume first; otherwise any folder can be chosen: a
// project, a typed path, or the native picker. Agent and worktree are chosen either way.
// Whatever would stop a start is said here, before it happens.
//
// Without a fixed folder it also manages the projects root: a project can be deleted
// (retired through devkit, then to the Recycle Bin, after a confirmation saying what only
// exists here and what retiring stops), a retired one restored, and any of the user's
// GitHub repositories not yet in it can be cloned in, or a new project made from devkit's
// templates.

import { confirmAction } from "./confirm.js";
import { icon } from "./icons.js";
import { PastSessions } from "./past.js";
import { RemoteRepos } from "./remote.js";
import { RetiredProjects } from "./retired.js";
import { NewProject } from "./scaffold.js";

const AGENT_NAMES = { claude: "Claude", codex: "Codex" };
const TYPING_PAUSE_MS = 250;

function trustNote(agent, worktree) {
  const name = AGENT_NAMES[agent];
  if (agent === "claude") {
    const after = worktree ? " and the worktree starts right after" : "";
    return `First time here: ${name} will ask you to trust this folder. Choose “Yes”${after}.`;
  }
  return `First time here: ${name} will ask you to trust this folder.`;
}

// What stops the backend's account of a folder from taking a worktree, with the button
// that would fix it; null when nothing does.
function worktreeBlocker(folder) {
  if (!folder?.exists) return null;
  if (!folder.is_git) {
    return {
      note: "A worktree needs a git repository, and this folder isn't one.",
      action: "Initialize git and commit this folder",
    };
  }
  if (!folder.has_commits) {
    return {
      note: "A worktree needs a commit to start from, and this repository has none.",
      action: "Commit this folder",
    };
  }
  return null;
}

export class NewTabDialog {
  constructor(requests) {
    this.requests = requests;
    this.projects = [];
    this.past = new PastSessions(requests, () => this.dialog.close());
    this.requested = null;
    this.folder = null;
    this.error = "";
    this.wantsWorktree = true;
    this.dialog = document.getElementById("new-tab");
    this.title = document.getElementById("new-tab-title");
    this.projectField = document.getElementById("project-field");
    this.projectList = document.getElementById("project-list");
    this.pathInput = document.getElementById("folder-path");
    this.worktree = document.getElementById("worktree");
    this.worktreeLabel = document.getElementById("worktree-label");
    this.notes = document.getElementById("folder-notes");
    this.prepare = document.getElementById("prepare-worktree");
    this.start = document.getElementById("new-tab-start");
    this.form = document.getElementById("new-tab-form");
    this.message = document.getElementById("project-message");
    this.remote = new RemoteRepos({
      list: requests.listRemote,
      clone: requests.clone,
      cloned: (path) => this.onCloned(path),
      say: (text) => this.say(text),
    });
    this.retired = new RetiredProjects({
      list: requests.listRetired,
      restore: requests.restore,
      restored: (path) => this.onCloned(path),
      say: (text) => this.say(text),
    });
    this.newProject = new NewProject({
      listPresets: requests.listPresets,
      create: requests.newProject,
      created: (path) => this.onCloned(path),
    });
    this.wire();
  }

  wire() {
    // The user's own choice, kept apart from the box: a folder that cannot take a worktree
    // clears the box, and the next folder that can gets the choice back.
    this.worktree.addEventListener("change", () => {
      this.wantsWorktree = this.worktree.checked;
    });
    this.form.addEventListener("submit", () => this.submit());
    this.form.addEventListener("change", () => this.render());
    document.getElementById("new-tab-cancel").addEventListener("click", () => this.dialog.close());
    document.getElementById("folder-browse").addEventListener("click", () => this.requests.browse());
    this.prepare.addEventListener("click", () => {
      this.prepare.disabled = true;
      this.requests.prepare(this.requested);
    });
    this.dialog.addEventListener("click", (event) => {
      if (event.target === this.dialog) this.dialog.close();
    });
    this.pathInput.addEventListener("input", () => this.onTyped());
    this.pathInput.addEventListener("keydown", (event) => {
      // Enter here means "this path", not "start the session".
      if (event.key === "Enter") {
        event.preventDefault();
        this.choose(this.pathInput.value.trim());
      }
    });
  }

  onTyped() {
    clearTimeout(this.typing);
    this.typing = setTimeout(() => this.choose(this.pathInput.value.trim()), TYPING_PAUSE_MS);
  }

  setProjects(projects) {
    this.projects = projects;
    if (this.dialog.open) this.renderProjects();
  }

  // `fixed` is a group's folder, as { path, name }; without one, any folder can be chosen.
  open(fixed = null) {
    this.wantsWorktree = true;
    this.pathInput.value = "";
    this.projectField.hidden = fixed !== null;
    this.past.open(fixed);
    this.title.textContent = fixed ? `New session in ${fixed.name}` : "New session";
    this.say("");
    this.remote.show(false);
    this.retired.show(false);
    // A run under way stays in view: what it prints is still arriving.
    if (!this.newProject.running) this.newProject.show(false);
    this.renderProjects();
    this.choose(fixed?.path ?? "");
    this.dialog.showModal();
  }

  // A line under the project list: what a clone or delete came to, or, as `progress`,
  // what is still under way.
  say(text, progress = false) {
    this.message.textContent = text;
    this.message.hidden = !text;
    this.message.classList.toggle("progress", progress);
  }

  deleteProject(message, force) {
    this.say(`Retiring and deleting ${message.name}…`, true);
    this.requests.deleteProject(message.requested, force);
  }

  renderProjects() {
    const items = this.projects.map((project) => {
      const item = document.createElement("div");
      item.className = "project-item";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "project";
      button.textContent = project.name;
      button.title = project.path;
      button.dataset.path = project.path;
      button.addEventListener("click", () => {
        this.pathInput.value = "";
        this.choose(project.path);
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "project-delete";
      remove.title = `Delete ${project.name}`;
      remove.setAttribute("aria-label", remove.title);
      remove.append(icon("trash"));
      remove.addEventListener("click", () => {
        this.say("");
        this.requests.describeDeletion(project.path);
      });
      item.append(button, remove);
      return item;
    });
    this.projectList.replaceChildren(...items);
    this.markSelected();
  }

  // The backend's account of what deleting a project would lose; asked before every delete.
  setDeletion(message) {
    if (!this.dialog.open) return;
    if (message.error) {
      this.say(message.error);
      return;
    }
    const lines = [`Move ${message.name} to the Recycle Bin?`];
    if (message.risks.length) lines.push("Only on this machine:", ...message.risks);
    else lines.push("Everything in it is pushed to a remote.");
    const retire = message.retire ?? { steps: [], notes: [] };
    if (retire.steps.length) {
      lines.push("First, devkit's retire-project.py will:", ...retire.steps.map((s) => `• ${s}`));
    }
    lines.push(...retire.notes);
    confirmAction(lines, "Delete", () => this.deleteProject(message, false));
  }

  setDeleted(message) {
    if (!this.dialog.open) return;
    if (message.error && message.can_force) {
      // devkit's registry gate: unpushed work or a live box. Its words, then a choice.
      const lines = [
        `devkit refused to retire ${message.name}:`,
        message.error,
        "Delete anyway retires it over that (--force), then moves the folder to the Recycle Bin.",
      ];
      confirmAction(lines, "Delete anyway", () => this.deleteProject(message, true));
      this.say(message.error);
      return;
    }
    if (message.error) {
      this.say(message.error);
      return;
    }
    this.say("");
    if (this.requested === message.requested) this.choose("");
  }

  // A finished clone, or a created project, is chosen, ready to start a session in.
  onCloned(path) {
    if (!this.dialog.open || this.projectField.hidden) return;
    this.pathInput.value = "";
    this.choose(path);
  }

  choose(path) {
    this.requested = path;
    this.folder = null;
    this.error = "";
    this.markSelected();
    this.render();
    if (path) this.requests.inspect(path);
  }

  markSelected() {
    for (const button of this.projectList.querySelectorAll(".project")) {
      button.classList.toggle("selected", button.dataset.path === this.requested);
    }
  }

  // The native picker's answer.
  setPicked(path) {
    if (!this.dialog.open) return;
    this.pathInput.value = path;
    this.choose(path);
  }

  // A list for another group, or a closed dialog, is ignored.
  setPastSessions(message) {
    if (this.dialog.open) this.past.set(message);
  }

  // The backend's answer about a folder. One for a path since replaced is ignored.
  setFolder(folder) {
    if (folder.requested !== this.requested) return;
    this.folder = folder;
    this.error = folder.error ?? "";
    this.render();
  }

  agent() {
    return new FormData(this.form).get("agent");
  }

  noteLines(blocker) {
    if (!this.requested) return [];
    if (!this.folder) return [];
    if (!this.folder.exists) return ["That folder doesn't exist."];
    const lines = [];
    if (blocker) lines.push(blocker.note);
    if (!this.folder.trusted[this.agent()]) {
      lines.push(trustNote(this.agent(), this.worktree.checked));
    }
    if (this.error) lines.push(this.error);
    return lines;
  }

  render() {
    const blocker = worktreeBlocker(this.folder);
    this.worktree.disabled = blocker !== null;
    this.worktree.checked = blocker === null && this.wantsWorktree;
    this.worktreeLabel.classList.toggle("disabled", blocker !== null);
    this.prepare.hidden = blocker === null;
    this.prepare.disabled = false;
    this.prepare.textContent = blocker?.action ?? "";

    const lines = this.noteLines(blocker).map((line) => {
      const paragraph = document.createElement("p");
      paragraph.textContent = line;
      return paragraph;
    });
    this.notes.replaceChildren(...lines);
    this.notes.hidden = lines.length === 0;
    this.start.disabled = !this.folder?.exists;
  }

  submit() {
    if (!this.folder?.exists) return;
    this.requests.start({
      path: this.folder.path,
      agent: this.agent(),
      worktree: this.worktree.checked,
    });
  }
}
