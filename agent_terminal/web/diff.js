// A unified diff as a list of files to read. Built from text nodes: a diff is arbitrary
// content from a branch, so none of it may become markup.

// Past this many lines, files start folded: building every row at once stalls the window.
const OPEN_LINE_LIMIT = 4000;

export function splitFiles(diff) {
  const files = [];
  let current = null;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      current = { name: fileName(line), lines: [] };
      files.push(current);
    } else if (current) {
      current.lines.push(line);
    }
  }
  return files;
}

function fileName(header) {
  const match = header.match(/ b\/(.+)$/);
  return match ? match[1] : header;
}

function lineClass(line) {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "header";
  if (/^(index |new file|deleted file|old mode|new mode|similarity|rename |Binary )/.test(line)) {
    return "header";
  }
  if (line.startsWith("+")) return "added";
  if (line.startsWith("-")) return "removed";
  return "";
}

function fileBody(file) {
  const pre = document.createElement("pre");
  pre.className = "diff-lines";
  for (const line of file.lines) {
    const row = document.createElement("div");
    row.className = lineClass(line);
    row.textContent = line || " ";
    pre.append(row);
  }
  return pre;
}

function renderFile(file, open) {
  const details = document.createElement("details");
  details.className = "diff-file";
  const summary = document.createElement("summary");
  summary.textContent = file.name;
  details.append(summary);
  // Filled when first opened, so a large folded diff costs nothing to show.
  const fill = () => {
    if (details.open && details.childElementCount === 1) details.append(fileBody(file));
  };
  details.addEventListener("toggle", fill);
  details.open = open;
  return details;
}

export function renderDiff(container, diff) {
  const files = splitFiles(diff);
  const total = files.reduce((sum, file) => sum + file.lines.length, 0);
  container.replaceChildren(...files.map((file) => renderFile(file, total <= OPEN_LINE_LIMIT)));
}
