// One xterm per tab, stacked in the same place so each stays laid out while hidden and
// keeps parsing output. Every terminal is the same size, so the backend tracks just one.

const { Terminal } = window;
const { FitAddon } = window.FitAddon;
const { WebglAddon } = window.WebglAddon;
const { WebLinksAddon } = window.WebLinksAddon;
const { Unicode11Addon } = window.Unicode11Addon;

const OPTIONS = {
  allowProposedApi: true,
  cursorBlink: true,
  fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, monospace',
  fontSize: 14,
  scrollback: 10000,
  theme: { background: "#0c0c0f", foreground: "#e4e5e9" },
  windowsPty: { backend: "conpty" },
};

// Returning false from a key handler keeps the key away from the agent. For Ctrl+V that
// leaves it to the browser, whose paste event xterm picks up.
function clipboardKey(event, term, copy) {
  if (event.type !== "keydown" || !event.ctrlKey || event.altKey) {
    return true;
  }
  if (event.code === "KeyC" && term.hasSelection()) {
    copy(term);
    return false;
  }
  return event.code !== "KeyV";
}

export class Terminals {
  constructor(container, { send, openUrl }) {
    this.container = container;
    this.send = send;
    this.openUrl = openUrl;
    this.entries = new Map();
    this.activeId = null;
    this.webgl = null;
    this.size = null;
    this.fit = this.createMeasure();
    new ResizeObserver(() => this.refit()).observe(container);
  }

  // A terminal that always exists, so the size is known before the first tab does.
  createMeasure() {
    const host = document.createElement("div");
    host.className = "term-host";
    this.container.append(host);
    const term = new Terminal(OPTIONS);
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    return fit;
  }

  dimensions() {
    const proposed = this.fit.proposeDimensions();
    if (!proposed || !Number.isFinite(proposed.rows) || !Number.isFinite(proposed.cols)) {
      return { rows: 32, cols: 120 };
    }
    return { rows: Math.max(proposed.rows, 2), cols: Math.max(proposed.cols, 20) };
  }

  refit() {
    const { rows, cols } = this.dimensions();
    if (this.size && this.size.rows === rows && this.size.cols === cols) {
      return;
    }
    this.size = { rows, cols };
    for (const { term } of this.entries.values()) {
      term.resize(cols, rows);
    }
    this.send({ type: "resize", rows, cols });
  }

  create(id) {
    const host = document.createElement("div");
    host.className = "term-host";
    this.container.append(host);
    const { rows, cols } = this.dimensions();
    const term = new Terminal({ ...OPTIONS, rows, cols });
    term.loadAddon(new Unicode11Addon());
    term.unicode.activeVersion = "11";
    term.loadAddon(new WebLinksAddon((_event, url) => this.openUrl(url)));
    term.open(host);
    term.attachCustomKeyEventHandler((event) => clipboardKey(event, term, this.copy));
    term.onData((data) => this.send({ type: "input", id, data }));
    host.addEventListener("contextmenu", (event) => this.onContextMenu(event, id, term));
    this.entries.set(id, { term, host });
  }

  copy = (term) => {
    this.send({ type: "copy", text: term.getSelection() });
    term.clearSelection();
  };

  // As Windows Terminal: right-click copies a selection, and pastes if there is none.
  onContextMenu(event, id, term) {
    event.preventDefault();
    if (term.hasSelection()) {
      this.copy(term);
    } else {
      this.send({ type: "paste", id });
    }
  }

  paste(id, text) {
    if (text) this.entries.get(id)?.term.paste(text);
  }

  remove(id) {
    const entry = this.entries.get(id);
    if (!entry) {
      return;
    }
    if (this.activeId === id) {
      this.dropWebgl();
      this.activeId = null;
    }
    entry.term.dispose();
    entry.host.remove();
    this.entries.delete(id);
  }

  write(id, data) {
    this.entries.get(id)?.term.write(data);
  }

  // A tab's process was replaced, so its screen belongs to something that has gone.
  reset(id) {
    this.entries.get(id)?.term.reset();
  }

  dropWebgl() {
    this.webgl?.dispose();
    this.webgl = null;
  }

  // Browsers cap live WebGL contexts, so only the visible terminal holds one.
  attachWebgl(term) {
    try {
      const addon = new WebglAddon();
      addon.onContextLoss(() => {
        if (this.webgl === addon) {
          this.dropWebgl();
        }
      });
      term.loadAddon(addon);
      this.webgl = addon;
    } catch (error) {
      console.warn("WebGL unavailable; using the DOM renderer", error);
    }
  }

  show(id) {
    if (id === this.activeId) {
      return;
    }
    this.dropWebgl();
    this.entries.get(this.activeId)?.host.classList.remove("shown");
    this.activeId = id;
    const entry = this.entries.get(id);
    if (!entry) {
      return;
    }
    entry.host.classList.add("shown");
    this.attachWebgl(entry.term);
    entry.term.focus();
  }

  focusActive() {
    this.entries.get(this.activeId)?.term.focus();
  }

  // The text on a tab's screen. WebGL paints to a canvas, so the DOM does not hold it.
  screenText(id) {
    const entry = this.entries.get(id);
    if (!entry) return "";
    const buffer = entry.term.buffer.active;
    const lines = [];
    for (let row = 0; row < buffer.length; row += 1) {
      lines.push(buffer.getLine(row)?.translateToString(true) ?? "");
    }
    return lines.join("\n");
  }
}
