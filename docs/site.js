// Shared utilities for every page on the public site.
// Loads docs/data.json once and exposes helpers via `window.CT`.

(() => {
  const CACHE = {};
  let loadPromise = null;

  async function loadData() {
    if (CACHE.data) return CACHE.data;
    if (loadPromise) return loadPromise;
    const dataUrl = new URL("data.json", document.baseURI).toString();
    loadPromise = fetch(dataUrl)
      .then((r) => {
        if (!r.ok) throw new Error(`data.json HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => {
        // Build a few indexes the pages will reuse.
        d._byMemberId = Object.fromEntries(d.members.map((m) => [m.id, m]));
        d._byTagId = Object.fromEntries(d.tags.map((t) => [t.id, t]));
        d._byMeetingId = Object.fromEntries(d.meetings.map((m) => [m.id, m]));
        CACHE.data = d;
        return d;
      });
    return loadPromise;
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

  // Boot every page: mount mobile nav, paint nav highlight, register SW.
  document.addEventListener("DOMContentLoaded", () => {
    mountTabbar();
    highlightActiveNav();
    wireSheetDismiss();
    registerServiceWorker();
  });

  window.CT = {
    loadData,
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
