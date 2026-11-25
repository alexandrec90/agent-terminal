// The one confirmation dialog, for anything that cannot be taken back: a merge, closing a
// group's sessions, deleting a project. Cancel, Escape or a click outside does nothing.

const dialog = document.getElementById("confirm");
const text = document.getElementById("confirm-text");
const ok = document.getElementById("confirm-ok");
let pending = null;

document.getElementById("confirm-cancel").addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => {
  pending = null;
});
ok.addEventListener("click", () => {
  const run = pending;
  dialog.close();
  run?.();
});

// `lines` is a string or several; each is its own paragraph, and only ever text.
export function confirmAction(lines, label, run) {
  const paragraphs = [lines].flat().map((line) => {
    const paragraph = document.createElement("p");
    paragraph.textContent = line;
    return paragraph;
  });
  text.replaceChildren(...paragraphs);
  ok.textContent = label;
  pending = run;
  dialog.showModal();
}
