"use strict";
// Runs before first paint, so a saved light theme does not flash dark.
try {
  const saved = localStorage.getItem("br-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch {
  // Browser storage can be blocked. The default dark theme still works.
}
