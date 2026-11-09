// OCR Service: every page's behaviour. Pages run no inline script (see the Content-Security-Policy).
//
// Pages open without a full reload: links and forms inside the app are fetched and only the page
// area is swapped; a list beside an open record stays where it was. Parts marked data-live are
// refreshed every few seconds while the tab is visible. Without this script every page still works.

const $$ = (root, selector) => [...(root.matches?.(selector) ? [root] : []), ...root.querySelectorAll(selector)];

// ── times: stored in UTC, shown in the viewer's time zone (UTC stays in the tooltip) ──────────

const pad = n => String(n).padStart(2, "0");
const localTime = (iso, short = false) => {
  const d = new Date(iso);
  if (isNaN(d)) return null;
  if (short) {
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: "short", day: "numeric" });
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  const zone = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d)
    .find(p => p.type === "timeZoneName")?.value || "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${zone}`.trim();
};
const localTimes = text => text.replace(/(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)( UTC)?/g,
  (m, day, time) => localTime(`${day}T${time}Z`) || m);

// ── behaviours of one page area: run on load, and on every area swapped in ─────────────────────

const served = new WeakMap();  // live area -> the HTML the server sent, to tell when it changed
let leaveGuard = null;         // a record form with unsaved changes

function initToast(t) {
  t.querySelector("[data-dismiss]")?.addEventListener("click", () => t.remove());
  if (!t.classList.contains("error")) setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 220); }, 4500);
}

function init(root) {
  $$(root, "[data-live][id]").forEach(el => served.set(el, el.outerHTML));
  $$(root, "time[datetime]").forEach(t => {
    const shown = localTime(t.getAttribute("datetime"), t.hasAttribute("data-short"));
    if (shown) t.textContent = shown;
  });
  $$(root, ".toast").forEach(initToast);

  // Forms that ask before they act, such as Delete.
  $$(root, "form[data-confirm]").forEach(f =>
    f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

  // Record forms: the save bar shows once something changed; leaving asks first.
  $$(root, "#record-form").forEach(form => {
    const bar = document.getElementById("savebar");
    let dirty = false, submitting = false;
    const markDirty = () => { dirty = true; bar?.classList.add("show"); };
    form.addEventListener("input", markDirty);
    form.addEventListener("change", markDirty);
    form.addEventListener("submit", () => { submitting = true; });
    leaveGuard = () => dirty && !submitting && document.contains(form);
    form.querySelector("[data-discard]")?.addEventListener("click", () => { dirty = false; visit(location.href, { push: false }); });
  });

  // Saved secrets: Replace opens an empty field; Cancel closes it again (empty means keep).
  $$(root, "[data-replace]").forEach(b => b.addEventListener("click", () => {
    const name = b.dataset.replace;
    document.querySelector(`[data-saved="${name}"]`).hidden = true;
    document.querySelector(`[data-input="${name}"]`).hidden = false;
    document.getElementById(name).focus();
  }));
  $$(root, "[data-keep]").forEach(b => b.addEventListener("click", () => {
    const name = b.dataset.keep;
    document.getElementById(name).value = "";
    document.querySelector(`[data-input="${name}"]`).hidden = true;
    document.querySelector(`[data-saved="${name}"]`).hidden = false;
  }));
  $$(root, "[data-reveal]").forEach(b => b.addEventListener("click", () => {
    const input = document.getElementById(b.dataset.reveal);
    input.type = input.type === "password" ? "text" : "password";
    b.textContent = input.type === "password" ? "Show" : "Hide";
  }));

  // Searchable Odoo lists: the visible box searches by name, the hidden field carries the Odoo ID.
  $$(root, "[data-combo]").forEach(box => {
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
  $$(root, ".tip-btn").forEach(b => b.addEventListener("click", e => {
    e.preventDefault();
    const tip = b.closest(".tip");
    const open = !tip.classList.contains("open");
    document.querySelectorAll(".tip.open").forEach(t => t.classList.remove("open"));
    tip.classList.toggle("open", open);
  }));

  // Instant filter on short lists; a group's header goes when none of its rows is left.
  $$(root, "[data-filter]").forEach(box => {
    box.addEventListener("input", () => {
      const target = document.querySelector(box.dataset.filter);
      if (!target) return;
      const rows = target.matches("table") ? [...target.querySelectorAll("tbody tr:not(.o-group-row)")]
        : [...target.children].filter(row => !row.matches(".list-group"));
      const empty = document.querySelector("[data-filter-empty]");
      const words = box.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      rows.forEach(row => {
        const hit = words.every(w => row.textContent.toLowerCase().includes(w));
        row.hidden = !hit;
        shown += hit;
      });
      target.querySelectorAll("tbody[data-group]").forEach(body => {
        body.hidden = !body.querySelector("tr:not(.o-group-row):not([hidden])");
      });
      let head = null;
      [...target.children].forEach(row => {
        if (row.matches(".list-group")) { head = row; row.hidden = true; }
        else if (head && !row.hidden) head.hidden = false;
      });
      if (empty) empty.hidden = shown > 0;
    });
  });

  $$(root, "[data-copy]").forEach(b => b.addEventListener("click", async () => {
    const label = b.innerHTML;
    try { await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent); b.textContent = "Copied"; }
    catch { b.textContent = "Select and copy"; }
    setTimeout(() => { b.innerHTML = label; }, 2000);
  }));

  // Long lists: the next page loads as the end comes into view.
  $$(root, "[data-more]").forEach(more => {
    const io = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) { io.disconnect(); loadMore(more); }
    }, { root: more.closest(".pane"), rootMargin: "600px" });
    io.observe(more);
  });

  // Settings: the section menu follows the scroll.
  $$(root, ".section-nav").forEach(nav => {
    const links = new Map([...nav.querySelectorAll("a")].map(a => [a.hash.slice(1), a]));
    const io = new IntersectionObserver(entries => entries.forEach(e => {
      if (!e.isIntersecting) return;
      links.forEach(a => a.classList.remove("on"));
      links.get(e.target.id)?.classList.add("on");
    }), { rootMargin: "-20% 0px -70% 0px" });
    links.forEach((_a, id) => { const section = document.getElementById(id); if (section) io.observe(section); });
  });
}

async function loadMore(more) {
  const list = more.parentElement;
  try {
    const page = await load(more.dataset.more);
    const fresh = page.html && new DOMParser().parseFromString(page.html, "text/html").getElementById(list.id);
    if (!fresh) throw new Error("no list");
    const items = [...fresh.children].filter(li => !li.classList.contains("list-empty"));
    more.replaceWith(...items);
    items.forEach(init);
    list.dataset.pages = Number(list.dataset.pages || 1) + 1;
  } catch {
    more.querySelector("a")?.removeAttribute("hidden");
  }
}

// ── soft navigation ───────────────────────────────────────────────────────────────────────────

const progress = document.getElementById("progress");
let progressTimer = null;
const startProgress = () => {
  clearTimeout(progressTimer);
  progress?.classList.remove("done");
  progressTimer = setTimeout(() => progress?.classList.add("on"), 120);
};
const endProgress = () => {
  clearTimeout(progressTimer);
  if (!progress?.classList.contains("on")) return;
  progress.classList.replace("on", "done");
  setTimeout(() => progress.classList.remove("done"), 500);
};

const load = (url, options = {}) => fetch(url, { credentials: "same-origin", ...options }).then(async r => ({
  url: r.url, status: r.status,
  html: (r.headers.get("content-type") || "").includes("text/html") ? await r.text() : null,
}));

const prefetched = new Map();  // url -> {at, page promise}; links in lists are fetched on hover
const takePrefetched = url => {
  const hit = prefetched.get(url);
  prefetched.delete(url);
  return hit && Date.now() - hit.at < 10000 ? hit.page : null;
};

let visitId = 0;
async function visit(url, { method = "GET", body = null, push = true } = {}) {
  if (leaveGuard?.() && !confirm("Leave without saving your changes?")) return;
  const id = ++visitId;
  document.body.classList.remove("menu-open");
  startProgress();
  let page;
  try {
    page = await ((method === "GET" && takePrefetched(url)) || load(url, { method, body }));
  } catch {
    endProgress();
    if (method === "GET") location.href = url;
    else showError("The service did not answer. Check your connection and try again.");
    return;
  }
  if (id !== visitId) return;
  endProgress();
  if (method !== "GET") prefetched.clear();
  const doc = page.html && new DOMParser().parseFromString(page.html, "text/html");
  if (!doc?.getElementById("main") || new URL(page.url).origin !== location.origin) {
    // Not an app page (signed out, or an error): load it the ordinary way.
    if (method === "GET" || page.status < 400) location.href = page.url;
    else showError(`Something went wrong (error ${page.status}). Reload the page and try again.`);
    return;
  }
  apply(doc, page.url, { push: push && page.url !== location.href, method });
}

function showError(text) {
  const toast = document.createElement("div");
  toast.className = "toast error";
  toast.setAttribute("role", "alert");
  toast.innerHTML = '<span class="t-icon"></span><span></span><button type="button" aria-label="Dismiss" data-dismiss>×</button>';
  toast.children[1].textContent = text;
  document.getElementById("toasts").append(toast);
  initToast(toast);
}

function apply(doc, url, { push, method }) {
  const main = document.getElementById("main");
  const next = doc.getElementById("main");
  document.title = doc.title;
  const rail = document.getElementById("rail");
  const nextRail = doc.getElementById("rail");
  if (rail && nextRail) { rail.replaceWith(nextRail); syncTheme(); }

  // Typing in a list's search box: keep the caret there when the list comes back.
  const active = document.activeElement;
  const typing = active?.closest?.("[role=search]") && active.name ? { name: active.name, at: active.selectionStart } : null;

  const list = main.querySelector("[data-list-key]");
  const nextList = next.querySelector("[data-list-key]");
  const sameList = list && nextList && list.dataset.listKey === nextList.dataset.listKey;
  const extended = Number(list?.querySelector("[data-pages]")?.dataset.pages || 1) > 1;
  if (sameList && (extended || list.querySelector("[data-group]")) && method === "GET") {
    // A longer list than the server sends, or groups opened in it: keep it, move the highlight, swap the rest.
    const target = new URL(url);
    const current = nextList.querySelector('[aria-current="true"]')?.getAttribute("href") ?? target.pathname + target.search;
    list.querySelectorAll(".mail-row").forEach(a =>
      a.getAttribute("href") === current ? a.setAttribute("aria-current", "true") : a.removeAttribute("aria-current"));
    main.querySelector(".split")?.setAttribute("class", next.querySelector(".split")?.getAttribute("class") || "split");
    main.querySelector("#detail")?.replaceWith(next.querySelector("#detail"));
    main.querySelector(".env-bar")?.replaceWith(next.querySelector(".env-bar"));
  } else {
    const scroll = sameList ? list.scrollTop : 0;
    main.className = next.className;
    main.replaceChildren(...next.childNodes);
    if (sameList) main.querySelector("[data-list-key]").scrollTop = scroll;
  }
  const toasts = document.getElementById("toasts");
  doc.querySelectorAll("#toasts .toast").forEach(t => { toasts.append(t); initToast(t); });

  if (push) history.pushState({}, "", url);
  else history.replaceState({}, "", url);
  init(main);

  if (typing) {
    const box = main.querySelector(`[role=search] [name="${typing.name}"]`);
    box?.focus();
    box?.setSelectionRange?.(typing.at, typing.at);
  } else if (push || method !== "GET") {
    const hash = new URL(url).hash;
    const target = hash && document.getElementById(hash.slice(1));
    if (target) target.scrollIntoView();
    else if (!main.classList.contains("is-split")) window.scrollTo(0, 0);
    if (method === "GET") main.querySelector("#detail-title")?.focus({ preventScroll: true });
  }
  main.querySelector(".mail-row[aria-current]")?.scrollIntoView({ block: "nearest" });
}

const inApp = () => document.getElementById("main");
const ownPage = url => url.origin === location.origin && !url.pathname.startsWith("/static/") && !url.pathname.startsWith("/oauth");

document.addEventListener("click", e => {
  if (!inApp() || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const more = e.target.closest("[data-more] a");
  if (more) { e.preventDefault(); more.hidden = true; loadMore(more.closest("[data-more]")); return; }
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a, button, form, summary, input")) { visit(new URL(row.dataset.href, location.href).href); return; }
  const a = e.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download") || a.hasAttribute("data-full")) return;
  if (a.matches("[data-group-toggle], [data-group-page]")) return;  // opened in place (Group By, below)
  const url = new URL(a.href, location.href);
  if (!ownPage(url)) return;
  if (url.hash && url.pathname === location.pathname && url.search === location.search) return;  // in-page anchor
  e.preventDefault();
  if (a.closest("#palette")) closePalette();
  visit(url.href);
});

document.addEventListener("submit", e => {
  const form = e.target;
  if (!inApp() || e.defaultPrevented || form.hasAttribute("data-full") || form.target) return;
  const method = (form.getAttribute("method") || "get").toUpperCase();
  const action = new URL(form.getAttribute("action") || location.href, location.href);
  if (!ownPage(action)) return;
  e.preventDefault();
  let data;
  try { data = new FormData(form, e.submitter); } catch { data = new FormData(form); }
  if (form.closest("#palette")) closePalette();
  if (method === "GET") {
    action.search = new URLSearchParams(data).toString();
    visit(action.href);
  } else {
    e.submitter?.setAttribute("aria-busy", "true");
    visit(action.href, { method, body: data });
  }
});

window.addEventListener("popstate", () => { if (inApp()) visit(location.href, { push: false }); });
window.addEventListener("beforeunload", e => { if (leaveGuard?.()) { e.preventDefault(); e.returnValue = ""; } });

// Hovering a row in a list fetches its page, so the click opens it at once.
let hoverTimer = null;
document.addEventListener("pointerover", e => {
  const a = e.target.closest?.("[data-prefetch] a[href]");
  if (!a || a.hasAttribute("data-full")) return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    const url = new URL(a.href, location.href).href;
    if (!prefetched.has(url)) prefetched.set(url, { at: Date.now(), page: load(url) });
  }, 70);
});

// The email search box searches as you type.
let typingTimer = null;
document.addEventListener("input", e => {
  const box = e.target.closest?.(".pane-list form[role=search] input[type=search]");
  if (!box) return;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => box.form.requestSubmit(), 350);
});

// ── live areas ────────────────────────────────────────────────────────────────────────────────

let polling = false;
async function poll() {
  if (polling || document.hidden || !inApp() || !document.querySelector("[data-live][id]")) return;
  polling = true;
  const here = location.href, visiting = visitId;
  try {
    const page = await load(here, { headers: { "X-Live": "1" } });
    if (location.href !== here || visitId !== visiting) return;  // moved on meanwhile: this answer is stale
    const doc = page.html && page.url === here.split("#")[0] && new DOMParser().parseFromString(page.html, "text/html");
    document.querySelectorAll(".live-pill").forEach(p => p.classList.toggle("paused", !doc));
    if (!doc) return;
    doc.querySelectorAll("[data-live][id]").forEach(fresh => {
      const old = document.getElementById(fresh.id);
      if (!old || served.get(old) === fresh.outerHTML) return;
      const focus = document.activeElement;
      if (old.contains(focus) && focus.matches("input, select, textarea")) return;
      if (Number(old.dataset.pages || 1) > 1) { mergeList(old, fresh); return; }
      const seen = new Set([...old.querySelectorAll("[data-key]")].map(el => el.dataset.key));
      const open = new Set([...old.querySelectorAll("details[id]")].filter(d => d.open).map(d => d.id));
      const shut = new Set([...old.querySelectorAll("details[id]")].filter(d => !d.open).map(d => d.id));
      const top = old.scrollTop;  // a list that scrolls on its own stays where it was
      const folded = foldedGroups(old);
      old.replaceWith(fresh);
      refold(fresh, folded);
      fresh.scrollTop = top;
      init(fresh);
      fresh.querySelectorAll("details[id]").forEach(d => {
        if (open.has(d.id)) d.open = true;
        if (shut.has(d.id)) d.open = false;
      });
      if (seen.size) fresh.querySelectorAll("[data-key]").forEach(el => { if (!seen.has(el.dataset.key)) el.classList.add("fresh"); });
    });
  } catch {
    document.querySelectorAll(".live-pill").forEach(p => p.classList.add("paused"));
  } finally {
    polling = false;
  }
}

// A list with more pages loaded than the server sends: update its rows in place, add new ones on top.
function mergeList(old, fresh) {
  const first = old.querySelector(":scope > [data-key]");
  fresh.querySelectorAll(":scope > [data-key]").forEach(item => {
    const existing = old.querySelector(`:scope > [data-key="${item.dataset.key}"]`);
    if (existing) {
      if (existing.innerHTML !== item.innerHTML) { existing.replaceWith(item); init(item); }
    } else {
      old.insertBefore(item, first);
      init(item);
      item.classList.add("fresh");
    }
  });
}

setInterval(poll, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

// ── keyboard: move through a list ─────────────────────────────────────────────────────────────

document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey || !inApp() || !document.getElementById("palette")?.hidden) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
  const list = document.querySelector(".pane-list .mail-list");
  if (!list) return;
  const down = e.key === "j" || e.key === "ArrowDown", up = e.key === "k" || e.key === "ArrowUp";
  if (!down && !up) return;
  // Arrow keys still scroll the open record when it has focus.
  if (e.key.startsWith("Arrow") && e.target.closest?.(".pane-detail")) return;
  const rows = [...list.querySelectorAll(".mail-row")].filter(r => !r.closest(".group-shut, [hidden]"));
  if (!rows.length) return;
  e.preventDefault();
  const at = rows.findIndex(r => r.getAttribute("aria-current") === "true");
  const target = rows[Math.min(Math.max(at + (down ? 1 : -1), 0), rows.length - 1)] || rows[0];
  if (target.getAttribute("aria-current") === "true") return;
  rows.forEach(r => r.removeAttribute("aria-current"));
  target.setAttribute("aria-current", "true");
  target.scrollIntoView({ block: "nearest" });
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => visit(target.href), 120);
});

// ── Group By: a group's header folds its rows away and opens them again ───────────────────────

const isFolded = group => group.querySelector("[data-group-toggle]")?.getAttribute("aria-expanded") === "false";

// Hide the rows of folded groups: in a table each group is its own <tbody>; in a list the rows
// follow their group's header.
function foldGroups(root) {
  $$(root, "tbody[data-group]").forEach(body => body.classList.toggle("group-shut", isFolded(body)));
  $$(root, ".mail-list").forEach(list => {
    let folded = false;
    [...list.children].forEach(li => {
      if (li.matches(".list-group")) li.classList.toggle("group-folded", folded = isFolded(li));
      else li.classList.toggle("group-shut", folded);
    });
  });
}
const foldedGroups = root => new Set([...root.querySelectorAll("[data-group]")].filter(isFolded).map(g => g.dataset.group));
function refold(root, folded) {
  if (!folded?.size) return;
  root.querySelectorAll("[data-group]").forEach(g => {
    if (folded.has(g.dataset.group)) g.querySelector("[data-group-toggle]")?.setAttribute("aria-expanded", "false");
  });
  foldGroups(root);
}

// A group's header: its <tbody>, or in a list the header item its rows follow.
function groupOf(el) {
  const body = el.closest("tbody[data-group]");
  if (body) return body;
  let li = el.closest("li");
  while (li && !li.matches(".list-group")) li = li.previousElementSibling;
  return li;
}

// As in Odoo a grouped list comes folded; a group's page of rows is fetched when it opens or its pager
// turns (the list with ?group= set sends that group open), and the group is swapped for it: its header
// (with the pager) and rows in a table's <tbody>, or the header item and the items after it in a list.
async function loadGroup(group, url) {
  const box = group.closest("table, .mail-list");
  try {
    const page = await load(url);
    const doc = page.html && new DOMParser().parseFromString(page.html, "text/html");
    const fresh = doc?.querySelector(`[data-group="${CSS.escape(group.dataset.group)}"][data-loaded]`);
    if (!fresh) throw new Error("no group");
    if (fresh.matches("tbody")) {
      group.replaceWith(fresh);
    } else {
      const items = [];
      for (let li = fresh.nextElementSibling; li && !li.matches(".list-group"); li = li.nextElementSibling) items.push(li);
      while (group.nextElementSibling && !group.nextElementSibling.matches(".list-group")) group.nextElementSibling.remove();
      group.replaceWith(fresh, ...items);
      items.forEach(init);
    }
    init(fresh);
    foldGroups(box);
  } catch {
    location.href = url;  // the group as a page of its own
  }
}

document.addEventListener("click", e => {
  if (!inApp() || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // a new tab opens it as a page
  const link = e.target.closest("[data-group-page], [data-group-toggle]");
  if (!link) return;
  e.preventDefault();
  const group = groupOf(link);
  if (link.matches("[data-group-toggle]") && group.hasAttribute("data-loaded")) {
    link.setAttribute("aria-expanded", isFolded(group) ? "true" : "false");
    foldGroups(group.closest("table, .mail-list"));
    return;
  }
  if (group.hasAttribute("aria-busy")) return;
  group.setAttribute("aria-busy", "true");
  loadGroup(group, link.href).finally(() => group.removeAttribute("aria-busy"));
});

// ── notebooks: a record's child tables as tabs, one shown at a time ───────────────────────────

function selectTab(tab) {
  const book = tab.closest(".o-notebook");
  book.querySelectorAll(":scope > .o-tabs [role=tab]").forEach(t => {
    const on = t === tab;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
}
document.addEventListener("click", e => {
  const tab = e.target.closest(".o-tabs [role=tab]");
  if (tab) selectTab(tab);
});
document.addEventListener("keydown", e => {
  const tab = e.target.closest?.(".o-tabs [role=tab]");
  if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const tabs = [...tab.parentElement.querySelectorAll("[role=tab]")];
  const at = tabs.indexOf(tab);
  const next = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: tabs.length - 1 }[e.key];
  const target = tabs[(next + tabs.length) % tabs.length];
  e.preventDefault();
  selectTab(target);
  target.focus();
});

// ── menu, theme ───────────────────────────────────────────────────────────────────────────────

const setMenu = open => {
  document.body.classList.toggle("menu-open", open);
  document.querySelector("[data-menu]")?.setAttribute("aria-expanded", open);
};
const THEMES = { system: "Theme: match Windows", light: "Theme: light", dark: "Theme: dark" };
const currentTheme = () => document.documentElement.dataset.theme || "system";
function syncTheme() {
  const theme = currentTheme();
  document.querySelectorAll("[data-theme-icon]").forEach(i => { i.hidden = i.dataset.themeIcon !== theme; });
  document.querySelectorAll("[data-theme-label]").forEach(l => { l.textContent = THEMES[theme]; });
  document.querySelectorAll("[data-theme-toggle]").forEach(b => { b.title = THEMES[theme]; });
}
document.addEventListener("click", e => {
  if (e.target.closest("[data-menu]")) setMenu(!document.body.classList.contains("menu-open"));
  else if (e.target.closest("[data-menu-close]")) setMenu(false);
  if (e.target.closest("[data-theme-toggle]")) {
    const order = Object.keys(THEMES);
    const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
    if (next === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem("theme");
      else localStorage.setItem("theme", next);
    } catch { /* storage blocked: the choice lasts until the page closes */ }
    syncTheme();
  }
  if (e.target.closest("[data-search-open]")) openPalette();
  if (!e.target.closest(".tip")) document.querySelectorAll(".tip.open").forEach(t => t.classList.remove("open"));
  // Top-bar menus close on a click anywhere else, or on choosing an entry.
  document.querySelectorAll("details.o-dropdown[open]").forEach(d => {
    if (!d.contains(e.target) || e.target.closest(".o-dropdown-menu a")) d.open = false;
  });
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  setMenu(false);
  document.querySelectorAll("details.o-dropdown[open]").forEach(d => { d.open = false; });
});

// ── search everything: Ctrl+K or / opens it; results arrive as you type ───────────────────────

const palette = document.getElementById("palette");
let openPalette = () => {}, closePalette = () => {};
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

  openPalette = () => {
    lastFocus = document.activeElement;
    setMenu(false);
    palette.hidden = false;
    document.body.classList.add("palette-open");
    input.focus();
    input.select();
    run();
  };
  closePalette = () => {
    palette.hidden = true;
    document.body.classList.remove("palette-open");
    lastFocus?.focus?.();
  };

  palette.addEventListener("mousedown", e => { if (e.target === palette) closePalette(); });
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 150); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); select(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); select(active - 1); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); options()[active].click(); }
    else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
  });
  document.addEventListener("keydown", e => {
    const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
    if ((e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing)) {
      e.preventDefault();
      if (palette.hidden) openPalette();
      else closePalette();
    }
  });
}

syncTheme();
init(document);
