// The new-session dialog. A group's "+" arrives with the folder fixed; otherwise any folder
// can be chosen: a project, a typed path, or the native picker. Agent and worktree are
// chosen either way. Whatever would stop a start is said here, before it happens.

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

export class NewTabDialog {
  constructor({ start, inspect, browse, prepare }) {
    this.requests = { start, inspect, browse, prepare };
    this.projects = [];
    this.requested = null;
    this.folder = null;
    this.error = "";
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
    this.wire();
  }

  wire() {
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
    this.worktree.checked = false;
    this.pathInput.value = "";
    this.projectField.hidden = fixed !== null;
    this.title.textContent = fixed ? `New session in ${fixed.name}` : "New session";
    this.renderProjects();
    this.choose(fixed?.path ?? "");
    this.dialog.showModal();
  }

  renderProjects() {
    const buttons = this.projects.map((project) => {
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
      return button;
    });
    this.projectList.replaceChildren(...buttons);
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
    for (const button of this.projectList.children) {
      button.classList.toggle("selected", button.dataset.path === this.requested);
    }
  }

  // The native picker's answer.
  setPicked(path) {
    if (!this.dialog.open) return;
    this.pathInput.value = path;
    this.choose(path);
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

  worktreeBlocker() {
    if (!this.folder?.exists) return null;
    if (!this.folder.is_git) {
      return {
        note: "A worktree needs a git repository, and this folder isn't one.",
        action: "Initialize git and commit this folder",
      };
    }
    if (!this.folder.has_commits) {
      return {
        note: "A worktree needs a commit to start from, and this repository has none.",
        action: "Commit this folder",
      };
    }
    return null;
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
    const blocker = this.worktreeBlocker();
    this.worktree.disabled = blocker !== null;
    if (blocker) this.worktree.checked = false;
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
