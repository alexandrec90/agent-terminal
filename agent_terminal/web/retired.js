// The projects this machine has retired through devkit, each with a Restore button.
// Restoring plugs the project back into devkit's registry, cloning it if its folder is
// gone, and starts again what retiring stopped here.

import { confirmAction } from "./confirm.js";

export class RetiredProjects {
  // `restored(path)` hears where a restored project's folder is; `say(text)` reports a failure.
  constructor({ list, restore, restored, say }) {
    this.requests = { list, restore };
    this.restored = restored;
    this.say = say;
    this.projects = null;
    this.restoring = null;
    this.toggle = document.getElementById("retired-toggle");
    this.panel = document.getElementById("retired");
    this.status = document.getElementById("retired-status");
    this.list = document.getElementById("retired-list");
    this.toggle.addEventListener("click", () => this.show(this.panel.hidden));
  }

  show(shown) {
    this.panel.hidden = !shown;
    this.toggle.setAttribute("aria-expanded", String(shown));
    if (!shown) return;
    this.status.textContent = "Reading devkit's record…";
    this.list.replaceChildren();
    this.requests.list();
  }

  setRetired(message) {
    if (message.error) {
      this.projects = null;
      this.status.textContent = message.error;
      this.list.replaceChildren();
      return;
    }
    this.projects = message.projects;
    this.render();
  }

  setRestored(message) {
    this.restoring = null;
    if (message.error) {
      this.say(`Could not restore ${message.name}: ${message.error}`);
    } else if (message.path) {
      this.restored(message.path);
    }
    if (this.projects) this.render();
  }

  render() {
    if (this.panel.hidden) return;
    this.status.textContent = this.projects.length ? "" : "Nothing is retired on this machine.";
    this.list.replaceChildren(...this.projects.map((project) => this.row(project)));
  }

  row(project) {
    const row = document.createElement("div");
    row.className = "remote-repo";
    row.dataset.project = project.name;
    const text = document.createElement("span");
    text.className = "remote-text";
    const name = document.createElement("span");
    name.className = "remote-name";
    name.textContent = project.name;
    const about = document.createElement("span");
    about.className = "remote-about";
    about.textContent = project.retired_at ? `retired ${project.retired_at.replace("T", " ")}` : "";
    text.append(name, about);

    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "remote-clone";
    const busy = this.restoring === project.name;
    restore.disabled = this.restoring !== null;
    restore.textContent = busy ? "Restoring…" : "Restore";
    restore.addEventListener("click", () => this.confirmRestore(project.name));
    row.append(text, restore);
    return row;
  }

  confirmRestore(name) {
    const lines = [
      `Restore ${name}?`,
      "devkit plugs it back into its registry and starts again what retiring stopped here.",
      "If its folder is gone, it is cloned from GitHub: work that was never pushed is only in the Recycle Bin.",
    ];
    confirmAction(lines, "Restore", () => {
      this.say("");
      this.restoring = name;
      this.render();
      this.requests.restore(name);
    });
  }
}
