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

  // Load the transcript manifest (docs/transcripts/index.json): body id -> list
  // of {id, date, summary_file, text_file, media_url, ...}. Cached; returns {}
  // if the file is missing so pages degrade gracefully to no-transcript.
  let transcriptsPromise = null;
  async function loadTranscripts() {
    if (transcriptsPromise) return transcriptsPromise;
    const url = new URL("transcripts/index.json", document.baseURI).toString();
    transcriptsPromise = fetch(url)
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
    return transcriptsPromise;
  }

  // Transcript entries for a body (default: the active one), newest first.
  async function transcriptsForBody(body = currentBody()) {
    const idx = await loadTranscripts();
    return Array.isArray(idx[body]) ? idx[body] : [];
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

  // The parser writes "(outcome not found)" when it can't read a result line out of
  // the minutes — a sentinel, not a fact about the meeting. It must never reach the
  // page as if it were the recorded outcome.
  function isUnknownOutcome(s) {
    const o = (s || "").trim();
    return !o || /not found/i.test(o);
  }

  // Turn the clerk's phrasing ("carried with a vote of 4:1.") into a label.
  function outcomeWord(outcome) {
    const cls = classifyOutcome(outcome);
    if (cls === "passed") return "Passed";
    if (cls === "failed") return "Failed";
    const raw = String(outcome || "").trim().replace(/\.$/, "");
    return raw ? raw.charAt(0).toUpperCase() + raw.slice(1) : "Decided";
  }

  function outcomePill(outcome) {
    if (isUnknownOutcome(outcome)) {
      return `<span class="pill other" title="The minutes don’t record a result for this motion">Not recorded</span>`;
    }
    const cls = classifyOutcome(outcome);
    const label = cls === "other" ? outcomeWord(outcome) : cls;
    return `<span class="pill ${cls}">${escapeHtml(label)}</span>`;
  }

  function voteChip(member, vote) {
    const v = (vote || "").toLowerCase();
    return `<span class="vote-chip">${escapeHtml(member)}<span class="v ${v}">${escapeHtml(vote)}</span></span>`;
  }

  // --- motion presentation ---
  //
  // A motion carries two registers: the legalese the clerk recorded ("An Ordinance
  // of Eagle Mountain City, Utah, amending Municipal Code 16.15…") and the
  // plain-English headline/summary/impact from ingest.summarize_motions. Residents
  // came for the second one, so it leads and the official wording is tucked behind
  // a disclosure. Motions the LLM pass hasn't reached yet have neither, and fall
  // back to the cleaned title with the recorded text shown outright — the page must
  // still work for all 1,098 motions, not just the summarized ones.

  // Where the parser's item/motion split failed, item_title can run to thousands of
  // characters of swallowed narrative ("…Mr. Pili read from Utah Code Title 53-2a-205…").
  // A summarized motion has a headline and never hits this; an un-summarized one must
  // still not render an <h3> the length of a page. The full text stays in the detail.
  const TITLE_MAX = 140;

  function titleIsOversized(m) {
    return !m.headline && (cleanTitle(m.item_title) || "").length > TITLE_MAX;
  }

  // Most council business is housekeeping. The summarizer flags each motion
  // "notable" or "routine" so the handful of decisions that actually reach a
  // resident's money, property, or neighborhood aren't buried under 900 board
  // appointments. Un-summarized motions are neither — we don't know yet, and
  // guessing "routine" would hide them.
  function isRoutine(m) {
    return m.significance === "routine";
  }
  function isNotable(m) {
    return m.significance === "notable";
  }

  function significanceChip(m) {
    if (isNotable(m)) return `<span class="sig-chip notable">Affects residents</span>`;
    if (isRoutine(m)) return `<span class="sig-chip routine">Routine</span>`;
    return "";
  }

  function motionTitle(m) {
    if (m.headline) return m.headline;
    const t = cleanTitle(m.item_title) || "";
    if (!t) return "(no title)";
    if (t.length <= TITLE_MAX) return t;
    const cut = t.slice(0, TITLE_MAX);
    const at = cut.lastIndexOf(" ");
    return (at > 60 ? cut.slice(0, at) : cut).replace(/[,;:.\s]+$/, "") + "…";
  }

  function voteTally(m) {
    let yes = 0, no = 0, other = 0;
    for (const v of m.votes || []) {
      const x = (v.vote || "").toLowerCase();
      if (x === "yes" || x === "aye") yes++;
      else if (x === "no" || x === "nay") no++;
      else other++;
    }
    return { yes, no, other, total: yes + no + other };
  }

  // "Passed unanimously, 5–0." — one line a reader can take at face value, instead
  // of a pill they have to decode against a bar chart. Where the minutes are silent,
  // it says so plainly rather than guessing or leaking a parser sentinel.
  function outcomeSentence(m) {
    const t = voteTally(m);
    const tally = `${t.yes}–${t.no}${t.other ? `–${t.other}` : ""}`;

    if (isUnknownOutcome(m.outcome)) {
      return t.total === 0
        ? "The minutes don’t record a result or a roll-call vote for this motion."
        : `The minutes don’t state the result. The roll call was ${tally}.`;
    }

    const cls = classifyOutcome(m.outcome);
    if (t.total === 0) return `${outcomeWord(m.outcome)} — no roll-call vote was recorded.`;
    if (cls === "passed" && t.no === 0 && t.other === 0) return `Passed unanimously, ${t.yes}–0.`;

    const parts = [`${t.yes} yes`, `${t.no} no`];
    if (t.other) parts.push(`${t.other} other`);
    return `${outcomeWord(m.outcome)} ${tally} (${parts.join(", ")}).`;
  }

  // The expanded body of a motion — shared by the motions list, the meeting page,
  // and the member page so a decision reads identically wherever it is clicked.
  function motionDetail(data, m, { highlightMember = "" } = {}) {
    const decided = m.summary
      ? `<p class="m-lead">${escapeHtml(m.summary)}</p>`
      : "";

    // The amber callout has to *mean* something. If every motion got one, including
    // the 900 that change nothing, it would stop reading as "pay attention" and
    // become page furniture. So the callout is reserved for motions explicitly flagged
    // notable — everything else, routine *or not yet classified*, states its impact
    // plainly and quietly. Loud is earned, never the default.
    const impact = m.impact
      ? (isNotable(m)
          ? `<div class="m-impact">
               <p class="m-impact-label">What it means for residents</p>
               <p class="m-impact-text">${escapeHtml(m.impact)}</p>
             </div>`
          : `<p class="m-impact-routine"><span class="m-impact-routine-label">What it means for you:</span> ${escapeHtml(m.impact)}</p>`)
      : "";

    // No plain-English pass yet: say so, so a blank space doesn't read as "this
    // decision had no effect".
    const pending = !m.summary && !m.impact
      ? `<p class="m-pending muted">A plain-English summary of this decision hasn’t been written yet. The official wording the clerk recorded is below.</p>`
      : "";

    const chips = (m.votes || [])
      .map((v) => {
        const on = highlightMember && v.member_id === highlightMember ? " is-focus" : "";
        const name = escapeHtml(memberName(data, v.member_id));
        const vote = escapeHtml(v.vote || "");
        return `<span class="vote-chip${on}">${name}<span class="v ${(v.vote || "").toLowerCase()}">${vote}</span></span>`;
      })
      .join(" ");

    const rollcall = `
      <div class="m-block">
        <p class="m-block-label">How they voted</p>
        <p class="m-outcome-line">${escapeHtml(outcomeSentence(m))}</p>
        ${chips ? `<div class="m-votes">${chips}</div>` : ""}
      </div>`;

    // Open by default only when it is the sole thing we have to show.
    const official = `
      <details class="m-official"${pending ? " open" : ""}>
        <summary>Official wording as recorded</summary>
        <p class="m-text">${escapeHtml(m.motion || "(no motion text)")}</p>
        ${m.item_title && (m.headline || titleIsOversized(m))
            ? `<p class="m-text m-title-raw"><span class="muted">Agenda title:</span> ${escapeHtml(m.item_title)}</p>`
            : ""}
      </details>`;

    return `<div class="motion-detail">${decided}${impact}${pending}${rollcall}${official}</div>`;
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
  // A bottom bar should hold at most ~5 destinations, so the primary pages sit
  // on the bar and the rest live in a "More" sheet — every page stays reachable.
  const TABBAR_PRIMARY = [
    { href: "index.html",    label: "Home" },
    { href: "meetings.html", label: "Meetings" },
    { href: "members.html",  label: "Members" },
  ];
  const TABBAR_MORE = [
    { href: "motions.html", label: "Motions" },
    { href: "tax.html",     label: "Tax" },
    { href: "budget.html",  label: "Budget" },
  ];

  function row(t) {
    return `<a href="${t.href}" data-nav="${t.href}"><span class="tab-ico" aria-hidden="true"></span><span class="tab-lbl">${t.label}</span></a>`;
  }

  function mountTabbar() {
    if (document.querySelector("nav.tabbar")) return;
    const here = location.pathname.split("/").pop() || "index.html";
    const onMorePage = TABBAR_MORE.some((t) => t.href === here);

    const nav = document.createElement("nav");
    nav.className = "tabbar";
    nav.setAttribute("aria-label", "Primary (mobile)");
    nav.innerHTML =
      TABBAR_PRIMARY.map(row).join("") +
      `<button type="button" class="tab-more${onMorePage ? " active" : ""}"
         aria-haspopup="true" aria-expanded="false" aria-controls="more-sheet">
         <span class="tab-ico" aria-hidden="true"></span><span class="tab-lbl">More</span>
       </button>`;
    document.body.appendChild(nav);

    // The overflow sheet + its backdrop. Both start hidden; the .open class is
    // added a frame after un-hiding so the slide/fade transitions actually run.
    const backdrop = document.createElement("div");
    backdrop.className = "more-backdrop";
    backdrop.hidden = true;

    const sheet = document.createElement("div");
    sheet.className = "more-sheet";
    sheet.id = "more-sheet";
    sheet.hidden = true;
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.setAttribute("aria-label", "More pages");
    sheet.innerHTML =
      `<div class="more-sheet-grip" aria-hidden="true"></div>` +
      TABBAR_MORE.map(row).join("");
    document.body.append(backdrop, sheet);

    const btn = nav.querySelector(".tab-more");
    const open = () => {
      backdrop.hidden = false;
      sheet.hidden = false;
      // Next frame → run transitions from the hidden starting state.
      requestAnimationFrame(() => {
        backdrop.classList.add("open");
        sheet.classList.add("open");
      });
      btn.setAttribute("aria-expanded", "true");
    };
    const close = () => {
      backdrop.classList.remove("open");
      sheet.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      const done = () => { backdrop.hidden = true; sheet.hidden = true; };
      // Hide after the slide-out; fall back if transitionend doesn't fire.
      sheet.addEventListener("transitionend", done, { once: true });
      setTimeout(done, 300);
    };
    const toggle = () => (btn.getAttribute("aria-expanded") === "true" ? close() : open());

    btn.addEventListener("click", toggle);
    backdrop.addEventListener("click", close);
    sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", close));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && btn.getAttribute("aria-expanded") === "true") close();
    });
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
    loadTranscripts,
    transcriptsForBody,
    currentBody,
    linkBody,
    escapeHtml,
    memberName,
    tagLabel,
    cleanTitle,
    isCurrentMember,
    fmtDate,
    classifyOutcome,
    isUnknownOutcome,
    isRoutine,
    isNotable,
    significanceChip,
    outcomePill,
    voteChip,
    voteTally,
    outcomeSentence,
    motionTitle,
    motionDetail,
    tagChips,
    paramsFromUrl,
    setMeta,
  };
})();
