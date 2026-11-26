// Every script error on the page goes to the app's log. In the window they would only
// reach a console nobody has open.

// Held until the socket opens, so an error during start-up is not lost; bounded, so a
// page failing in a loop cannot grow it without end.
const MAX_HELD = 50;
const held = [];
let sendNow = null;

function report(entry) {
  const message = { type: "page_error", ...entry };
  if (sendNow) sendNow(message);
  else if (held.length < MAX_HELD) held.push(message);
}

window.addEventListener("error", (event) => {
  report({
    message: event.message || String(event.error),
    source: event.filename || "",
    line: event.lineno ?? null,
    column: event.colno ?? null,
    stack: event.error?.stack || "",
  });
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  report({
    message: `unhandled rejection: ${reason?.message ?? String(reason)}`,
    stack: reason?.stack || "",
  });
});

// Called once the socket is open: sends what was held, then reports as errors happen.
export function reportErrorsTo(send) {
  sendNow = send;
  held.splice(0).forEach(send);
}
