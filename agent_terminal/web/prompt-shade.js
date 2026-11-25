// Claude Code draws its input box as a row of `─` above and below the cursor and paints no
// background of its own (2.1.331 under ConPTY, plain or with WT_SESSION/TERM_PROGRAM set),
// so the prompt line is hard to find. Shade the rows between the two rules around the cursor.

export const PROMPT_BACKGROUND = "#2b2b35";

const RULE = /^─{20,}$/u;

// The rows, by index into `lines`, strictly between the nearest rules above and below
// `cursorRow`; null when the cursor is not boxed in by two.
export function promptRows(lines, cursorRow) {
  if (cursorRow < 0 || cursorRow >= lines.length || RULE.test(lines[cursorRow])) {
    return null;
  }
  let above = cursorRow - 1;
  while (above >= 0 && !RULE.test(lines[above])) above -= 1;
  let below = cursorRow + 1;
  while (below < lines.length && !RULE.test(lines[below])) below += 1;
  if (above < 0 || below >= lines.length) {
    return null;
  }
  return { start: above + 1, end: below - 1 };
}

export class PromptShade {
  constructor(term) {
    this.term = term;
    this.decoration = null;
    this.rows = null;
    this.pending = false;
    this.subscriptions = [
      term.onWriteParsed(() => this.schedule()),
      term.onResize(() => this.schedule()),
      // A marker belongs to one buffer, but a decoration paints on whichever is shown.
      term.buffer.onBufferChange(() => {
        this.clear();
        this.schedule();
      }),
    ];
  }

  schedule() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      this.update();
    });
  }

  update() {
    const { term } = this;
    const buffer = term.buffer.active;
    const lines = [];
    for (let row = 0; row < term.rows; row += 1) {
      lines.push(buffer.getLine(buffer.baseY + row)?.translateToString(true) ?? "");
    }
    const rows = promptRows(lines, buffer.cursorY);
    if (!rows) {
      this.clear();
      return;
    }
    const line = buffer.baseY + rows.start;
    const height = rows.end - rows.start + 1;
    const current = this.decoration;
    if (
      current &&
      !current.isDisposed &&
      current.marker.line === line &&
      this.height === height &&
      this.width === term.cols
    ) {
      return;
    }
    this.clear();
    const marker = term.registerMarker(rows.start - buffer.cursorY);
    if (!marker) return;
    this.decoration =
      term.registerDecoration({
        marker,
        width: term.cols,
        height,
        backgroundColor: PROMPT_BACKGROUND,
      }) ?? null;
    if (!this.decoration) {
      marker.dispose();
      return;
    }
    this.rows = rows;
    this.height = height;
    this.width = term.cols;
  }

  // Viewport rows shaded now, for the end-to-end tests.
  shaded() {
    return this.decoration && !this.decoration.isDisposed ? this.rows : null;
  }

  clear() {
    if (this.decoration) {
      this.decoration.marker.dispose();
      this.decoration.dispose();
    }
    this.decoration = null;
    this.rows = null;
  }

  dispose() {
    this.clear();
    for (const subscription of this.subscriptions) subscription.dispose();
  }
}
