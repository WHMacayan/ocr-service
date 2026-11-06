// OCR Service: every page's behaviour. Pages run no inline script (see the Content-Security-Policy).

// Success notices leave on their own; errors stay until dismissed.
document.querySelectorAll(".toast").forEach(t => {
  t.querySelector("[data-dismiss]").addEventListener("click", () => t.remove());
  if (!t.classList.contains("error")) setTimeout(() => t.remove(), 4500);
});

// Times are stored in UTC; show them in the viewer's own time zone (UTC stays in the tooltip).
const pad = n => String(n).padStart(2, "0");
const localTime = iso => {
  const d = new Date(iso);
  if (isNaN(d)) return null;
  const zone = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d)
    .find(p => p.type === "timeZoneName")?.value || "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${zone}`.trim();
};
const localTimes = text => text.replace(/(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)( UTC)?/g,
  (m, day, time) => localTime(`${day}T${time}Z`) || m);
document.querySelectorAll("time[datetime]").forEach(t => {
  const shown = localTime(t.getAttribute("datetime"));
  if (shown) t.textContent = shown;
});

// Whole table rows open their record.
document.querySelectorAll("tr[data-href]").forEach(tr =>
  tr.addEventListener("click", e => { if (!e.target.closest("a, button, form")) location = tr.dataset.href; }));

// Forms that ask before they act, such as Delete.
document.querySelectorAll("form[data-confirm]").forEach(f =>
  f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

// Phone menu.
const menuBtn = document.querySelector("[data-menu]");
const setMenu = open => {
  document.body.classList.toggle("menu-open", open);
  if (menuBtn) menuBtn.setAttribute("aria-expanded", open);
};
if (menuBtn) menuBtn.addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open")));
document.querySelector("[data-menu-close]")?.addEventListener("click", () => setMenu(false));
document.addEventListener("keydown", e => { if (e.key === "Escape") setMenu(false); });

// Record forms: the save bar shows once something changed; leaving asks first.
const form = document.getElementById("record-form");
if (form) {
  const bar = document.getElementById("savebar");
  let dirty = false, submitting = false;
  const markDirty = () => { dirty = true; bar.classList.add("show"); };
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
  form.addEventListener("submit", () => { submitting = true; });
  window.addEventListener("beforeunload", e => { if (dirty && !submitting) { e.preventDefault(); e.returnValue = ""; } });
  document.querySelector("[data-discard]")?.addEventListener("click", () => { dirty = false; location.reload(); });
}

// Saved secrets: Replace opens an empty field; Cancel closes it again (empty means keep).
document.querySelectorAll("[data-replace]").forEach(b => b.addEventListener("click", () => {
  const name = b.dataset.replace;
  document.querySelector(`[data-saved="${name}"]`).hidden = true;
  document.querySelector(`[data-input="${name}"]`).hidden = false;
  document.getElementById(name).focus();
}));
document.querySelectorAll("[data-keep]").forEach(b => b.addEventListener("click", () => {
  const name = b.dataset.keep;
  document.getElementById(name).value = "";
  document.querySelector(`[data-input="${name}"]`).hidden = true;
  document.querySelector(`[data-saved="${name}"]`).hidden = false;
}));
document.querySelectorAll("[data-reveal]").forEach(b => b.addEventListener("click", () => {
  const input = document.getElementById(b.dataset.reveal);
  input.type = input.type === "password" ? "text" : "password";
  b.textContent = input.type === "password" ? "Show" : "Hide";
}));

// Searchable Odoo lists: the visible box searches by name, the hidden field carries the Odoo ID.
document.querySelectorAll("[data-combo]").forEach(box => {
  const name = box.dataset.combo;
  const hidden = document.getElementById(name);
  const miss = document.querySelector(`[data-combo-miss="${name}"]`);
  const ids = new Map([...document.getElementById(`${name}-list`).options].map(o => [o.value, o.dataset.id]));
  const sync = () => {
    hidden.value = ids.get(box.value) || "";
    const unmatched = box.value.trim() !== "" && !hidden.value;
    box.classList.toggle("invalid", unmatched);
    miss.hidden = !unmatched;
  };
  box.addEventListener("input", sync);
  box.addEventListener("change", sync);
});

// Tooltips open on hover and keyboard focus (CSS); a tap toggles them on touch screens.
document.querySelectorAll(".tip-btn").forEach(b => b.addEventListener("click", e => {
  e.preventDefault();
  const tip = b.closest(".tip");
  const open = !tip.classList.contains("open");
  document.querySelectorAll(".tip.open").forEach(t => t.classList.remove("open"));
  tip.classList.toggle("open", open);
}));
document.addEventListener("click", e => {
  if (!e.target.closest(".tip")) document.querySelectorAll(".tip.open").forEach(t => t.classList.remove("open"));
});

// ── Search everything: Ctrl+K or / opens it; results arrive as you type ──────────

const palette = document.getElementById("palette");
if (palette) {
  const input = document.getElementById("palette-q");
  const list = document.getElementById("palette-results");
  const ICONS = { emails: "inbox", mailboxes: "mail", vendors: "users", formats: "type", settings: "sliders",
                  activity: "activity", pages: "chevron" };
  let active = -1, timer = null, controller = null, lastFocus = null;

  // Text with the search words marked, built from text nodes so result data never becomes HTML.
  const marked = (text, terms) => {
    const span = document.createElement("span");
    const words = terms.filter(Boolean).sort((a, b) => b.length - a.length)
      .map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!words.length) { span.textContent = text; return span; }
    const re = new RegExp(words.join("|"), "gi");
    let last = 0;
    for (const m of text.matchAll(re)) {
      span.append(text.slice(last, m.index));
      const mark = document.createElement("mark");
      mark.textContent = m[0];
      span.append(mark);
      last = m.index + m[0].length;
    }
    span.append(text.slice(last));
    return span;
  };

  const options = () => [...list.querySelectorAll("[role=option]")];
  const select = i => {
    const opts = options();
    if (!opts.length) { active = -1; return; }
    active = (i + opts.length) % opts.length;
    opts.forEach((o, n) => o.setAttribute("aria-selected", n === active));
    opts[active].scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", opts[active].id);
  };

  const hint = text => {
    list.replaceChildren();
    const p = document.createElement("p");
    p.className = "palette-empty";
    p.textContent = text;
    list.append(p);
    active = -1;
  };

  const render = data => {
    list.replaceChildren();
    let n = 0;
    for (const group of data.groups) {
      const head = document.createElement("div");
      head.className = "palette-group";
      head.textContent = group.label + (group.more ? ` (${group.items.length + group.more})` : "");
      list.append(head);
      for (const item of group.items) {
        const a = document.createElement("a");
        a.href = item.url;
        a.id = `palette-opt-${n++}`;
        a.setAttribute("role", "option");
        if (item.url.startsWith("http")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
        const icon = document.querySelector(`#palette-icons [data-icon="${ICONS[group.key]}"]`);
        if (icon) a.append(icon.cloneNode(true));
        const text = document.createElement("span");
        text.className = "p-text";
        const title = marked(item.title, data.terms);
        title.className = "p-title";
        const sub = marked(localTimes(item.sub || ""), data.terms);
        sub.className = "p-sub";
        text.append(title, sub);
        a.append(text);
        if (item.badge) {
          const badge = document.createElement("span");
          badge.className = `badge ${item.badge[0]}`;
          badge.textContent = item.badge[1];
          a.append(badge);
        }
        list.append(a);
      }
    }
    if (!n) { hint(`Nothing matches “${data.query}”.`); return; }
    const all = document.createElement("a");
    all.href = `/search?q=${encodeURIComponent(data.query)}`;
    all.id = `palette-opt-${n}`;
    all.className = "palette-all";
    all.setAttribute("role", "option");
    all.textContent = `See all results for “${data.query}”`;
    list.append(all);
    select(0);
  };

  const run = () => {
    const q = input.value.trim();
    if (!q) { hint("Type a booking or container number, a sender, a vendor or a setting."); return; }
    controller?.abort();
    controller = new AbortController();
    fetch(`/search?format=json&q=${encodeURIComponent(q)}`, { signal: controller.signal, credentials: "same-origin" })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(render)
      .catch(e => { if (e.name !== "AbortError") hint("Search is not available right now. Press Enter for the full search."); });
  };

  const open = () => {
    lastFocus = document.activeElement;
    palette.hidden = false;
    document.body.classList.add("palette-open");
    input.focus();
    input.select();
    run();
  };
  const close = () => {
    palette.hidden = true;
    document.body.classList.remove("palette-open");
    lastFocus?.focus?.();
  };

  document.querySelectorAll("[data-search-open]").forEach(b => b.addEventListener("click", open));
  palette.addEventListener("mousedown", e => { if (e.target === palette) close(); });
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 150); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); select(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); select(active - 1); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); options()[active].click(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  document.addEventListener("keydown", e => {
    const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
    if ((e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) {
      e.preventDefault();
      palette.hidden ? open() : close();
    }
  });
}

// ── Instant filter on list pages ─────────────────────────────────────────────

document.querySelectorAll("[data-filter]").forEach(box => {
  const rows = [...document.querySelectorAll(`${box.dataset.filter} tbody tr`)];
  const empty = document.querySelector("[data-filter-empty]");
  box.addEventListener("input", () => {
    const words = box.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    rows.forEach(tr => {
      const hit = words.every(w => tr.textContent.toLowerCase().includes(w));
      tr.hidden = !hit;
      shown += hit;
    });
    if (empty) empty.hidden = shown > 0;
  });
});

document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", async () => {
  const label = b.innerHTML;
  try { await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent); b.textContent = "Copied"; }
  catch { b.textContent = "Select and copy"; }
  setTimeout(() => { b.innerHTML = label; }, 2000);
}));
