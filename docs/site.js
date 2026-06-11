// Shared utilities for every page on the public site.
// Loads docs/data.json once and exposes helpers via `window.CT`.

(() => {
  const DEFAULT_BODY = "city-council";
  const BODY_KEY = "ct_body";

  // Per-body caches so switching bodies in-session reloads the right dataset
  // instead of returning the first one fetched.
  const DATA_CACHE = {};   // body id -> parsed dataset
  const DATA_PROMISE = {}; // body id -> in-flight fetch promise
  let bodiesPromise = null;

  // Which body is active: ?body= wins (and is remembered), else the stored
  // preference, else the default. Writing the param through to localStorage
  // keeps the selection sticky across links that drop the query string.
  function currentBody() {
    try {
      const p = new URL(location.href).searchParams.get("body");
      if (p) {
        try { localStorage.setItem(BODY_KEY, p); } catch {}
        return p;
      }
      const stored = localStorage.getItem(BODY_KEY);
      if (stored) return stored;
    } catch {}
    return DEFAULT_BODY;
  }

  // Load the body index (id -> data_file). Falls back to the legacy single
  // body so the site keeps working even if bodies.json is missing.
  async function loadBodies() {
    if (bodiesPromise) return bodiesPromise;
    const url = new URL("bodies.json", document.baseURI).toString();
    bodiesPromise = fetch(url)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((list) =>
        Array.isArray(list) && list.length
          ? list
          : [{ id: DEFAULT_BODY, label: "City Council", data_file: "data.json", default: true }]
      );
    return bodiesPromise;
  }

  async function dataFileFor(bodyId) {
    const list = await loadBodies();
    const hit = list.find((b) => b.id === bodyId);
    if (hit) return hit.data_file;
    const def = list.find((b) => b.default) || list[0];
    return def ? def.data_file : "data.json";
  }

  function indexData(d) {
    d._byMemberId = Object.fromEntries(d.members.map((m) => [m.id, m]));
    d._byTagId = Object.fromEntries(d.tags.map((t) => [t.id, t]));
    d._byMeetingId = Object.fromEntries(d.meetings.map((m) => [m.id, m]));
    return d;
  }

  async function loadData() {
    const body = currentBody();
    if (DATA_CACHE[body]) return DATA_CACHE[body];
    if (DATA_PROMISE[body]) return DATA_PROMISE[body];
    DATA_PROMISE[body] = (async () => {
      const file = await dataFileFor(body);
      let r = await fetch(new URL(file, document.baseURI).toString());
      // Unknown body or missing file → fall back to the default body's data.
      if (!r.ok && body !== DEFAULT_BODY) {
        const defFile = await dataFileFor(DEFAULT_BODY);
        r = await fetch(new URL(defFile, document.baseURI).toString());
      }
      if (!r.ok) throw new Error(`data file HTTP ${r.status}`);
      const d = indexData(await r.json());
      DATA_CACHE[body] = d;
      return d;
    })();
    return DATA_PROMISE[body];
  }

  async function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    try {
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    } catch (e1) {
      try {
        // Fallback to relative if absolute path 404s (e.g. site served at a sub-path).
        await navigator.serviceWorker.register("sw.js");
      } catch (e2) {
        console.warn("SW register failed:", e1, e2);
      }
    }
  }

  // --- helpers ---

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function memberName(data, id) {
    return data._byMemberId[id]?.name || id;
  }

  function tagLabel(data, id) {
    return data._byTagId[id]?.label || id;
  }

  // Strip Eagle Mountain's boilerplate prefix so titles read more naturally.
  // "A Resolution of Eagle Mountain City, Utah, Approving …" -> "A Resolution Approving …"
  // Also handles "An Ordinance …" and variants with/without "the City of",
  // with/without "City" after "Eagle Mountain", and missing commas.
  const BOILERPLATE = /^((?:A\s+)?Resolution|(?:An\s+)?Ordinance)\s+of(?:\s+the\s+City\s+of)?\s+Eagle\s+Mountain(?:\s+City)?,?\s*Utah,?\s*/i;
  function cleanTitle(s) {
    if (!s) return s;
    return String(s).replace(BOILERPLATE, "$1 ").replace(/\s+/g, " ").trim();
  }

  // A member is "current" if their most recent term hasn't ended yet.
  // ISO YYYY-MM-DD strings compare correctly lexicographically.
  function isCurrentMember(m) {
    if (!m?.tenure_end) return true;
    const today = new Date().toISOString().slice(0, 10);
    return m.tenure_end >= today;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso + "T12:00:00Z");
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch {
      return iso;
    }
  }

  function classifyOutcome(s) {
    const o = (s || "").toLowerCase();
    if (!o) return "other";
    if (/passed|approved|carried/.test(o)) return "passed";
    if (/fail|denied|not pass/.test(o)) return "failed";
    return "other";
  }

  function outcomePill(outcome) {
    const cls = classifyOutcome(outcome);
    const label = cls === "other" ? (outcome || "—") : cls;
    return `<span class="pill ${cls}">${escapeHtml(label)}</span>`;
  }

  function voteChip(member, vote) {
    const v = (vote || "").toLowerCase();
    return `<span class="vote-chip">${escapeHtml(member)}<span class="v ${v}">${escapeHtml(vote)}</span></span>`;
  }

  function tagChips(data, tagIds, accent = false) {
    if (!tagIds || tagIds.length === 0) return "";
    return tagIds
      .map(
        (t) =>
          `<a class="tag${accent ? " accent" : ""}" href="motions.html?tag=${encodeURIComponent(t)}">${escapeHtml(
            tagLabel(data, t)
          )}</a>`
      )
      .join("");
  }

  function highlightActiveNav() {
    const here = location.pathname.split("/").pop() || "index.html";
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === here || (here === "" && a.dataset.nav === "index.html")) {
        a.classList.add("active");
        a.setAttribute("aria-current", "page");
      }
    });
  }

  // Inject the mobile bottom tab bar once per page. Hidden via CSS on desktop.
  function mountTabbar() {
    if (document.querySelector("nav.tabbar")) return;
    const tabs = [
      { href: "index.html",    label: "Home" },
      { href: "meetings.html", label: "Meetings" },
      { href: "motions.html",  label: "Motions" },
      { href: "members.html",  label: "Members" },
    ];
    const nav = document.createElement("nav");
    nav.className = "tabbar";
    nav.setAttribute("aria-label", "Primary (mobile)");
    nav.innerHTML = tabs
      .map(
        (t) =>
          `<a href="${t.href}" data-nav="${t.href}"><span class="tab-ico" aria-hidden="true"></span><span class="tab-lbl">${t.label}</span></a>`
      )
      .join("");
    document.body.appendChild(nav);
  }

  // Close any open mobile filter sheet when the user taps outside it.
  // (Sheets are <details class="filter-group">; on mobile the panel is fixed
  // to the viewport bottom, and the backdrop is rendered by body::after.)
  function wireSheetDismiss() {
    document.addEventListener("click", (e) => {
      const open = document.querySelectorAll("details.filter-group[open]");
      if (!open.length) return;
      open.forEach((d) => {
        if (!d.contains(e.target)) d.removeAttribute("open");
      });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      document.querySelectorAll("details.filter-group[open]").forEach((d) => d.removeAttribute("open"));
    });
  }

  function paramsFromUrl() {
    const u = new URL(location.href);
    return Object.fromEntries(u.searchParams.entries());
  }

  // Update the document title + description on data-driven pages so deep links
  // (a member, a single meeting) share with meaningful titles instead of the
  // generic static one. Keeps og:/twitter: in sync for social previews.
  function setMetaTag(selector, attr, key, content) {
    let el = document.head.querySelector(selector);
    if (!el) {
      el = document.createElement("meta");
      el.setAttribute(attr, key);
      document.head.appendChild(el);
    }
    el.setAttribute("content", content);
  }
  function setMeta({ title, description } = {}) {
    if (title) {
      document.title = title;
      setMetaTag('meta[property="og:title"]', "property", "og:title", title);
      setMetaTag('meta[name="twitter:title"]', "name", "twitter:title", title);
    }
    if (description) {
      setMetaTag('meta[name="description"]', "name", "description", description);
      setMetaTag('meta[property="og:description"]', "property", "og:description", description);
      setMetaTag('meta[name="twitter:description"]', "name", "twitter:description", description);
    }
  }

  // Append ?body= to a same-origin in-site link, preserving any existing query.
  function linkBody(href, body = currentBody()) {
    try {
      const u = new URL(href, location.href);
      if (u.origin !== location.origin) return href;
      u.searchParams.set("body", body);
      return u.pathname + u.search + u.hash;
    } catch {
      return href;
    }
  }

  // When a non-default body is active, rewrite the static in-site links present
  // at boot (topbar nav, mobile tabbar, brand) so navigation stays in-body even
  // if the link author didn't add ?body=. Dynamically-rendered links rely on the
  // localStorage fallback in currentBody() instead.
  function decorateBodyLinks() {
    const body = currentBody();
    if (body === DEFAULT_BODY) return;
    document.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href");
      if (!href || /^(https?:|mailto:|tel:|#)/i.test(href)) return;
      try {
        const u = new URL(href, location.href);
        if (u.origin !== location.origin) return;
        const isPage = u.pathname.endsWith(".html") || u.pathname.endsWith("/");
        if (!isPage || u.searchParams.get("body")) return;
        u.searchParams.set("body", body);
        a.setAttribute("href", u.pathname + u.search + u.hash);
      } catch {}
    });
  }

  // Inject the body switcher into the topbar (between brand and primary nav).
  // Hidden when there's only one body so the single-body site is unchanged.
  async function mountBodySwitch() {
    const topbar = document.querySelector("header.topbar");
    if (!topbar || topbar.querySelector(".body-switch")) return;
    const list = await loadBodies();
    if (!list || list.length <= 1) return;
    const cur = currentBody();
    const sel = document.createElement("select");
    sel.className = "body-switch";
    sel.setAttribute("aria-label", "Choose government body");
    sel.innerHTML = list
      .map(
        (b) =>
          `<option value="${escapeHtml(b.id)}"${b.id === cur ? " selected" : ""}>${escapeHtml(
            b.label
          )}</option>`
      )
      .join("");
    sel.value = cur; // no-op if cur isn't a listed body; first option stays selected
    sel.addEventListener("change", () => {
      const id = sel.value;
      try { localStorage.setItem(BODY_KEY, id); } catch {}
      location.href = linkBody("index.html", id);
    });
    const nav = topbar.querySelector("nav.nav");
    topbar.insertBefore(sel, nav || null);
  }

  // Reflect the active body in page chrome for non-default bodies. The default
  // (City Council) view is left exactly as authored.
  async function applyBodyChrome() {
    if (currentBody() === DEFAULT_BODY) return;
    let data;
    try { data = await loadData(); } catch { return; }
    const label = data.body_label;
    if (!label) return;
    document.querySelectorAll(".topbar .city").forEach((el) => {
      el.textContent = `Eagle Mountain, UT · ${label}`;
    });
    // Homepage hero headline names the body explicitly; keep it accurate.
    document.querySelectorAll(".hero-title").forEach((el) => {
      if (/City Council/.test(el.textContent)) {
        el.textContent = el.textContent.replace(/City Council/g, label);
      }
    });
  }

  // Boot every page: mount switcher + mobile nav, paint nav highlight, register SW.
  document.addEventListener("DOMContentLoaded", () => {
    mountBodySwitch();
    mountTabbar();
    highlightActiveNav();
    decorateBodyLinks();
    applyBodyChrome();
    wireSheetDismiss();
    registerServiceWorker();
  });

  window.CT = {
    loadData,
    currentBody,
    linkBody,
    escapeHtml,
    memberName,
    tagLabel,
    cleanTitle,
    isCurrentMember,
    fmtDate,
    classifyOutcome,
    outcomePill,
    voteChip,
    tagChips,
    paramsFromUrl,
    setMeta,
  };
})();
