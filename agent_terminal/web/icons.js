const SVG = "http://www.w3.org/2000/svg";

// One of the interface icons in icons/ui.svg, colored by the CSS `color` of whatever holds
// it. Drawn for 16px: `.icon` keeps it there.
export function icon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.dataset.icon = name;
  const use = document.createElementNS(SVG, "use");
  use.setAttribute("href", `icons/ui.svg#${name}`);
  svg.append(use);
  return svg;
}
