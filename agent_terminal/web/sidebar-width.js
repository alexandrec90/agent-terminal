// The sidebar's width: what the user last dragged its edge to, kept by the backend because
// the page's origin, and so its localStorage, changes with every run's port.

// As `#sidebar`'s limits in style.css and `MIN_SIDEBAR_WIDTH` in store.py.
const MIN_WIDTH = 180;
const MIN_STAGE_WIDTH = 320;

function clamp(width) {
  const most = Math.max(MIN_WIDTH, window.innerWidth - MIN_STAGE_WIDTH);
  return Math.round(Math.min(Math.max(width, MIN_WIDTH), most));
}

export class SidebarWidth {
  // `dragging` hears a drag start and end; `save` gets a width, or null for the default.
  constructor(sidebar, handle, { dragging, save }) {
    this.sidebar = sidebar;
    this.handle = handle;
    this.dragging = dragging;
    this.save = save;
    this.drag = null;
    // A press on the edge must not take the keyboard from the terminal.
    handle.addEventListener("mousedown", (event) => event.preventDefault());
    handle.addEventListener("pointerdown", (event) => this.begin(event));
    handle.addEventListener("pointermove", (event) => this.move(event));
    // After a release, and also when the system cancels the drag.
    handle.addEventListener("lostpointercapture", () => this.end());
    handle.addEventListener("dblclick", () => {
      this.apply(null);
      this.save(null);
    });
  }

  // The backend's width. Mid-drag, the pointer's outranks it.
  set(width) {
    if (!this.drag) this.apply(width);
  }

  apply(width) {
    const { style } = document.documentElement;
    if (width === null) style.removeProperty("--sidebar-width");
    else style.setProperty("--sidebar-width", `${width}px`);
  }

  begin(event) {
    if (event.button !== 0) return;
    this.handle.setPointerCapture(event.pointerId);
    this.drag = {
      x: event.clientX,
      from: this.sidebar.getBoundingClientRect().width,
      width: null,
    };
    this.handle.classList.add("active");
    this.dragging(true);
  }

  move(event) {
    if (!this.drag || (event.clientX === this.drag.x && this.drag.width === null)) return;
    this.drag.width = clamp(this.drag.from + event.clientX - this.drag.x);
    this.apply(this.drag.width);
  }

  end() {
    if (!this.drag) return;
    const { width } = this.drag;
    this.drag = null;
    this.handle.classList.remove("active");
    this.dragging(false);
    if (width !== null) this.save(width);
  }
}
