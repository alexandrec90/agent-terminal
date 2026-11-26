// A new project from devkit's templates, as its "Project: New from devkit" task made one:
// a name, a description, a preset, and whether to make the public GitHub repository.
// Preview runs the script's dry run, which writes nothing; Create applies it. What the
// script prints is shown as it prints it, and a created project is chosen.

const MAX_OUTPUT_LINES = 400;

export class NewProject {
  // `create(choice)` runs the script; `created(path)` hears where a project landed.
  constructor({ listPresets, create, created }) {
    this.requests = { listPresets, create };
    this.created = created;
    this.presets = null;
    this.running = false;
    this.lines = [];
    this.toggle = document.getElementById("scaffold-toggle");
    this.panel = document.getElementById("scaffold");
    this.status = document.getElementById("scaffold-status");
    this.fields = document.getElementById("scaffold-fields");
    this.name = document.getElementById("scaffold-name");
    this.description = document.getElementById("scaffold-description");
    this.preset = document.getElementById("scaffold-preset");
    this.remote = document.getElementById("scaffold-remote");
    this.previewButton = document.getElementById("scaffold-preview");
    this.createButton = document.getElementById("scaffold-create");
    this.output = document.getElementById("scaffold-output");
    this.wire();
  }

  wire() {
    this.toggle.addEventListener("click", () => this.show(this.panel.hidden));
    this.previewButton.addEventListener("click", () => this.run(false));
    this.createButton.addEventListener("click", () => this.run(true));
    this.name.addEventListener("input", () => this.render());
    for (const input of [this.name, this.description]) {
      // Enter here would otherwise submit the dialog and start a session.
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") event.preventDefault();
      });
    }
  }

  show(shown) {
    this.panel.hidden = !shown;
    this.toggle.setAttribute("aria-expanded", String(shown));
    if (shown && this.presets === null) {
      this.status.textContent = "Asking devkit…";
      this.requests.listPresets();
    }
    this.render();
  }

  setPresets(message) {
    if (message.error) {
      this.presets = null;
      this.status.textContent = message.error;
      this.render();
      return;
    }
    this.presets = message.presets;
    const options = this.presets.map(({ value, label }) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      return option;
    });
    this.preset.replaceChildren(...options);
    this.status.textContent = "";
    this.render();
  }

  run(apply) {
    const name = this.name.value.trim();
    if (!name || this.running) return;
    this.running = true;
    this.lines = [];
    this.output.textContent = "";
    this.output.hidden = false;
    this.status.textContent = apply ? `Creating ${name}…` : `Planning ${name}…`;
    this.render();
    this.requests.create({
      name,
      description: this.description.value.trim(),
      preset: this.preset.value,
      remote: this.remote.checked,
      apply,
    });
  }

  addLine(line) {
    this.lines.push(line);
    if (this.lines.length > MAX_OUTPUT_LINES) this.lines.shift();
    this.output.textContent = this.lines.join("\n");
    this.output.scrollTop = this.output.scrollHeight;
  }

  setDone(message) {
    this.running = false;
    if (message.error) {
      this.status.textContent = message.error;
    } else if (message.applied) {
      this.status.textContent = `Created ${message.name}.`;
      this.name.value = "";
      this.description.value = "";
      this.created(message.path);
    } else {
      this.status.textContent = "That is the plan; nothing was written. Create to apply it.";
    }
    this.render();
  }

  render() {
    const ready = this.presets !== null && !this.running;
    this.fields.hidden = this.presets === null;
    for (const field of [this.name, this.description, this.preset, this.remote]) {
      field.disabled = !ready;
    }
    const named = this.name.value.trim() !== "";
    this.previewButton.disabled = !ready || !named;
    this.createButton.disabled = !ready || !named;
  }
}
