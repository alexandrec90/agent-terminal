// What the app says about itself, above the groups: an update to apply, a session host
// to replace, how the last update went, or that this copy cannot update at all. Hidden
// when there is nothing to say.

function element(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function button(label, onClick) {
  const node = element("button", "app-action", label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function row(text, ...actions) {
  const node = element("div", "app-row");
  node.append(element("span", "app-text", text), ...actions);
  return node;
}

function commits(count) {
  return count === 1 ? "1 commit" : `${count} commits`;
}

export function noticeText(notice) {
  if (notice.ok) return `Updated: ${notice.detail}`;
  const said = (notice.detail || "").split("\n")[0];
  const where = notice.log ? ` Full output: ${notice.log}` : "";
  return `Update failed at ${notice.step}: ${said}${where}`;
}

export class AppBar {
  constructor(root, { restart }) {
    this.root = root;
    this.restart = restart;
    this.status = null;
    this.dismissed = false;
    this.unmanagedDismissed = false;
    // "Restart sessions" ends every agent's turn, so it takes a second click.
    this.armed = false;
  }

  set(status) {
    this.status = status;
    this.render();
  }

  rows() {
    const { update, sessions, notice, restarting, cannot_update } = this.status;
    if (restarting) return [row("Restarting…")];
    const rows = [];
    if (cannot_update && !this.unmanagedDismissed) {
      const close = button("×", () => {
        this.unmanagedDismissed = true;
        this.render();
      });
      close.title = "Dismiss until the app restarts";
      rows.push(row(cannot_update, close));
    }
    if (notice && !this.dismissed) {
      const close = button("×", () => {
        this.dismissed = true;
        this.render();
      });
      close.title = "Dismiss";
      const line = row(noticeText(notice), close);
      line.classList.add(notice.ok ? "ok" : "failed");
      rows.push(line);
    }
    if (update?.behind > 0) {
      const text = `Update available: ${commits(update.behind)}.`;
      if (update.blocked) {
        rows.push(row(`${text} ${update.blocked}`));
      } else {
        const label = sessions?.kept ? "Update" : "Update (ends sessions)";
        const go = button(label, () => this.restart({ update: true, keep_sessions: true }));
        go.title = sessions?.kept
          ? "Pull, sync and restart the app. Running agents carry on."
          : "Pull, sync and restart the app. Agents are resumed afterwards.";
        rows.push(row(text, go));
      }
    }
    if (sessions?.stale) {
      const label = this.armed ? "Click again to end them" : "Restart sessions";
      const go = button(label, () => {
        if (!this.armed) {
          this.armed = true;
          this.render();
          return;
        }
        this.restart({ update: false, keep_sessions: false });
      });
      go.title = "Ends every agent, restarts the app, and resumes each session.";
      rows.push(row("Running sessions use the previous session host.", go));
    }
    return rows;
  }

  render() {
    const rows = this.status ? this.rows() : [];
    this.root.replaceChildren(...rows);
    this.root.hidden = rows.length === 0;
  }
}
