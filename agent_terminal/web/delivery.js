// How far a tab's work has got: the button that ships it, and an icon per thing that judges
// it. Both are the backend's to decide (`delivery.py`); this only draws them.

import { icon } from "./icons.js";

const LAND = {
  create: ["Create PR", "Have this tab's agent ship its changes with the land skill"],
  push: ["Push", "Have this tab's agent add its new changes to the open PR with the land skill"],
  follow_up: [
    "New PR",
    "The PR has merged. Have this tab's agent ship what it changed since as a new one",
  ],
};

// Each names a symbol in icons/ui.svg.
const GATE_ICON = { ci: "ci", tests: "tests", e2e: "e2e", live: "live" };

// Typed into the agent, so only while it is at its prompt to read it.
export function landButton(tab, land) {
  const [text, title] = LAND[tab.land];
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tab-chip land";
  button.append(icon("ship"), text);
  button.disabled = tab.status !== "idle";
  button.title = button.disabled ? `${title}, once it is at its prompt` : title;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    button.disabled = true;
    land(tab.id);
  });
  return button;
}

export function gateIcons(gates) {
  const row = document.createElement("span");
  row.className = "tab-gates";
  for (const gate of gates) {
    const mark = document.createElement("span");
    mark.className = `gate ${gate.state}`;
    mark.dataset.gate = gate.name;
    mark.title = gate.detail;
    mark.setAttribute("role", "img");
    mark.setAttribute("aria-label", gate.detail);
    mark.append(icon(GATE_ICON[gate.name] ?? "pending"));
    row.append(mark);
  }
  return row;
}
