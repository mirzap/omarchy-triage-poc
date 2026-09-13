/* Render-blocking, same-origin bootstrap: apply layout before the first paint. */
(() => {
  // The sidebar is collapsed by default at every width; drawers overlay the
  // diff instead of resizing it.  A saved preference still wins.
  let visible = false;
  try {
    const saved = window.localStorage.getItem("triage.context-sidebar.v1");
    if (saved === "shown" || saved === "hidden") visible = saved === "shown";
  } catch (_) { /* Storage may be unavailable; retain the responsive default. */ }
  document.documentElement.dataset.contextSidebar = visible ? "shown" : "hidden";
  let layout = "auto";
  try {
    const saved = window.localStorage.getItem("triage.diff-layout.v1");
    if (["auto", "side", "stacked"].includes(saved)) layout = saved;
  } catch (_) { /* Retain automatic layout when storage is unavailable. */ }
  document.documentElement.dataset.diffLayout = layout;
})();
