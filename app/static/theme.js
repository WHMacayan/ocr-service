// Loaded before the stylesheet so a chosen light or dark theme paints without a flash.
// No choice saved: the page follows Windows (prefers-color-scheme).
try {
  const theme = localStorage.getItem("theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch { /* storage blocked: follow Windows */ }
