// The user's GitHub repositories that are not in the projects root yet, each with a Clone
// button. Asked for when first opened in a dialog; descriptions are the owner's text, so
// they only ever go in as text.

export class RemoteRepos {
  // `cloned(path)` hears where a finished clone landed; `say(text)` reports a failure.
  constructor({ list, clone, cloned, say }) {
    this.requests = { list, clone };
    this.cloned = cloned;
    this.say = say;
    this.repos = null;
    // Full names asked to clone that have not answered yet.
    this.cloning = new Set();
    this.toggle = document.getElementById("remote-toggle");
    this.panel = document.getElementById("remote");
    this.status = document.getElementById("remote-status");
    this.list = document.getElementById("remote-list");
    this.toggle.addEventListener("click", () => this.show(this.panel.hidden));
  }

  show(shown) {
    this.panel.hidden = !shown;
    this.toggle.setAttribute("aria-expanded", String(shown));
    if (!shown) {
      // Asked afresh next time: a clone may have landed, from here or anywhere else.
      if (!this.cloning.size) this.repos = null;
      return;
    }
    if (this.repos === null) {
      this.status.textContent = "Asking GitHub…";
      this.list.replaceChildren();
      this.requests.list();
    }
  }

  setRepos(message) {
    if (message.error) {
      this.repos = null;
      this.status.textContent = message.error;
      this.list.replaceChildren();
      return;
    }
    this.repos = message.repos;
    this.render();
  }

  // A clone done or refused. A done one leaves the list.
  setCloned(message) {
    this.cloning.delete(message.full_name);
    if (message.error) {
      this.say(`Could not clone ${message.full_name}: ${message.error}`);
    } else {
      this.repos = this.repos?.filter((repo) => repo.full_name !== message.full_name) ?? null;
      this.cloned(message.path);
    }
    if (this.repos) this.render();
  }

  render() {
    this.status.textContent = this.repos.length
      ? ""
      : "Every one of your repositories is already here.";
    this.list.replaceChildren(...this.repos.map((repo) => this.row(repo)));
  }

  row(repo) {
    const row = document.createElement("div");
    row.className = repo.archived ? "remote-repo archived" : "remote-repo";
    row.dataset.repo = repo.full_name;
    const text = document.createElement("span");
    text.className = "remote-text";
    const name = document.createElement("span");
    name.className = "remote-name";
    name.textContent = repo.archived ? `${repo.full_name} (archived)` : repo.full_name;
    const about = document.createElement("span");
    about.className = "remote-about";
    about.textContent = repo.description;
    text.append(name, about);

    const clone = document.createElement("button");
    clone.type = "button";
    clone.className = "remote-clone";
    const busy = this.cloning.has(repo.full_name);
    clone.disabled = busy;
    clone.textContent = busy ? "Cloning…" : "Clone";
    clone.addEventListener("click", () => {
      this.say("");
      this.cloning.add(repo.full_name);
      this.render();
      this.requests.clone(repo.full_name);
    });
    row.append(text, clone);
    return row;
  }
}
