// Shared utilities for every page on the public site.
// Loads docs/data.json once and exposes helpers via `window.CT`.

(() => {
  const DEFAULT_BODY = "city-council";
  const BODY_KEY = "ct_body";

  // Resolve a site file against the site root, not against the current page.
  //
  // Every fetch below used to resolve against document.baseURI. That is the
  // same thing on the flat top-level pages, and wrong on the several hundred
  // pages under /meetings/, where "bodies.json" resolved to
  // /meetings/bodies.json and 404'd — which silently cost those pages their
  // body switcher and their Ask widget.
  //
  // site.js is always loaded from the root, so its own URL is the one reliable
  // anchor here. Resolved once, on first use rather than at load: this file is
  // also evaluated in the test harness's stub browser, which has a location but
  // no script tags to read.
  let siteRoot = null;
  function siteUrl(path) {
    if (!siteRoot) {
      let src = null;
      try {
        src =
          (document.currentScript && document.currentScript.src) ||
          (document.querySelector &&
            document.querySelector('script[src*="site.js"]')?.src) ||
          null;
      } catch {}
      // No script tag to read: fall back to the page's own base, which is
      // correct everywhere except /meetings/, and /meetings/ always has one.
      siteRoot = new URL(src || document.baseURI, document.baseURI);
    }
    return new URL(path, siteRoot).toString();
  }

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
    const url = siteUrl("bodies.json");
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
      let r = await fetch(siteUrl(file));
      // Unknown body or missing file → fall back to the default body's data.
      if (!r.ok && body !== DEFAULT_BODY) {
        const defFile = await dataFileFor(DEFAULT_BODY);
        r = await fetch(siteUrl(defFile));
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
    const url = siteUrl("transcripts/index.json");
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

  // Meetings whose only surviving public record is the posted agenda —
  // docs/data.agenda-only.json, written by ingest.build_agenda_only. Same
  // graceful-degradation contract as the transcripts: a missing file means the
  // list is simply built from minutes and recordings, as it was before.
  let agendaOnlyPromise = null;
  async function loadAgendaOnly() {
    if (agendaOnlyPromise) return agendaOnlyPromise;
    const url = siteUrl("data.agenda-only.json");
    agendaOnlyPromise = fetch(url)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
    return agendaOnlyPromise;
  }

  // Agenda-only entries for a body (default: the active one), newest first.
  async function agendaOnlyForBody(body = currentBody()) {
    const feed = await loadAgendaOnly();
    const list = feed && feed.bodies && feed.bodies[body];
    return Array.isArray(list) ? list : [];
  }

  // The forward-looking calendar (docs/data.upcoming.json, written by
  // ingest.build_upcoming from the city's portal). Cached; resolves to null if
  // the file is missing so a page simply shows nothing scheduled.
  let upcomingPromise = null;
  async function loadUpcoming() {
    if (upcomingPromise) return upcomingPromise;
    const url = siteUrl("data.upcoming.json");
    upcomingPromise = fetch(url)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
    return upcomingPromise;
  }

  // Today's date in Eagle Mountain, not in whatever timezone the reader is in.
  // A resident checking from a trip east must not be told a meeting is over
  // while it is still hours away back home.
  function cityToday(tz = "America/Denver") {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
      }).format(new Date());
    } catch {
      return new Date().toISOString().slice(0, 10);
    }
  }

  // Whole days from today (city time) to an ISO day: 0 today, 1 tomorrow.
  function daysUntil(iso, tz) {
    const day = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
    return Math.round((day(iso) - day(cityToday(tz))) / 86400000);
  }

  // The body's next scheduled meeting, or null when the calendar is empty, too
  // stale to hold anything future, or missing entirely. A meeting stays "next"
  // for the whole of its own day: to-the-minute precision would only mean the
  // card disappears mid-meeting, which is when it is most worth showing.
  async function nextMeeting(body = currentBody()) {
    const feed = await loadUpcoming();
    const list = feed && feed.bodies && feed.bodies[body];
    if (!Array.isArray(list) || !list.length) return null;
    const tz = (feed && feed.timezone) || "America/Denver";
    const today = cityToday(tz);
    const ahead = list
      .filter((e) => e.date && e.date >= today)
      .sort((a, b) => a.date.localeCompare(b.date) || (a.start || "").localeCompare(b.start || ""));
    if (!ahead.length) return null;
    return { ...ahead[0], timezone: tz, days_away: daysUntil(ahead[0].date, tz) };
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

  // Load the "ask about this site" chat widget once per page. Kept in its own
  // file (agent.js) so the widget can evolve without touching this core script.
  function mountAgent() {
    if (document.getElementById("ct-agent-js")) return;
    const s = document.createElement("script");
    s.id = "ct-agent-js";
    s.src = siteUrl("agent.js?v=20260817k");
    s.defer = true;
    document.body.appendChild(s);
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

  const MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                       "july", "august", "september", "october", "november",
                       "december"];

  // Every spelling of one day, joined into a single lower-cased haystack a
  // search box can substring-match against.
  //
  // The record stores ISO days and the search matched them literally, so a
  // reader looking for "July 2" — or "7/2/2024", or "2 July 2024" — was told
  // there was no such meeting while its page sat in the list underneath. A date
  // is the one thing people type in their own notation, so accept all of them.
  function dateForms(iso) {
    if (!iso) return "";
    const [y, mm, dd] = iso.split("-");
    const month = MONTH_NAMES[parseInt(mm, 10) - 1] || "";
    const d = String(parseInt(dd, 10));
    const m = String(parseInt(mm, 10));
    return [
      iso,
      `${month} ${d}, ${y}`, `${month} ${d} ${y}`, `${month.slice(0, 3)} ${d} ${y}`,
      `${d} ${month} ${y}`,
      `${m}/${d}/${y}`, `${mm}/${dd}/${y}`, `${m}/${d}/${y.slice(2)}`,
      `${m}-${d}-${y}`,
    ].join(" ").toLowerCase();
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

  // A top-level nav item can own more than one page. `data-nav-alias` lists the
  // extra filenames (space-separated) that should still light it up — that is
  // how projections.html keeps "Tax" active while its own sub-nav says which
  // page inside the section you are on. Only the top-level item gets
  // aria-current; the sub-nav sets its own, and two "current page" markers in
  // one document would be a lie to a screen reader.
  function highlightActiveNav() {
    const here = location.pathname.split("/").pop() || "index.html";
    document.querySelectorAll("[data-nav]").forEach((a) => {
      const own = a.dataset.nav === here || (here === "" && a.dataset.nav === "index.html");
      const alias = (a.dataset.navAlias || "").split(/\s+/).filter(Boolean).includes(here);
      if (!own && !alias) return;
      a.classList.add("active");
      if (own) a.setAttribute("aria-current", "page");
    });

    // Inside an open section menu, show which row is the page you are on. A
    // class rather than aria-current: on these pages the sub-nav in the reading
    // column already claims that, and it should stay the only one.
    document.querySelectorAll(".nav-menu a").forEach((a) => {
      const href = (a.getAttribute("href") || "").split("/").pop();
      if (href === here) a.classList.add("here");
    });
  }

  // The menus open on hover and on focus-within, both of which CSS handles on
  // its own. The one thing it can't do is close one on demand: a keyboard user
  // who has opened a menu by tabbing to its trigger needs a way out that isn't
  // "tab through every item in it". Escape returns focus to the trigger, which
  // is what closes the panel.
  function wireMenuEscape() {
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const menu = e.target.closest && e.target.closest(".nav-menu");
      if (!menu) return;
      const trigger = menu.parentElement.querySelector("a");
      if (trigger) trigger.focus();
    });
  }

  // Inject the mobile bottom tab bar once per page. Hidden via CSS on desktop.
  //
  // Top-level sections only, no overflow sheet. The bar used to carry three
  // items plus a "More" button covering five more, which meant the mobile
  // taxonomy and the desktop one disagreed about what the site contains — and
  // the sheet buried the money pages two taps down on the devices most people
  // read this on. Both lists now come from ingest/nav.py. The sections not on
  // the bar are reachable without it: About from the footer of every page,
  // Claims from its card on the front page, and Votes from the Meetings
  // section's own strip, which is where it now lives.
  //
  // Hrefs are root-absolute. This bar is injected into docs/meetings/*.html
  // too, where a relative "meetings.html" resolved to /meetings/meetings.html
  // and 404'd on every one of several hundred pages.
  const TABBAR = [
    // BEGIN generated:nav (ingest/build_nav.py)
    { href: "/index.html", label: "Home" },
    { href: "/meetings.html", label: "Meetings" },
    { href: "/members.html", label: "Members" },
    { href: "/claims.html", label: "Claims", body: "city-council" },
    { href: "/finances.html", label: "Finances", body: "city-council" },
    // END generated:nav
  ];

  // A section some bodies don't have. The Planning Commission levies no tax and
  // adopts no budget, so carrying Finances into that view would offer figures
  // that aren't theirs. Everything unmarked belongs to every body.
  function inThisBody(item) {
    return !item.body || item.body === currentBody();
  }

  function row(t) {
    // data-nav stays the bare filename: highlightActiveNav() compares it
    // against the last path segment, and the CSS icon masks are keyed on it.
    const nav = t.href.replace(/^\//, "");
    return `<a href="${t.href}" data-nav="${nav}"><span class="tab-ico" aria-hidden="true"></span><span class="tab-lbl">${t.label}</span></a>`;
  }

  function mountTabbar() {
    if (document.querySelector("nav.tabbar")) return;
    const nav = document.createElement("nav");
    nav.className = "tabbar";
    nav.setAttribute("aria-label", "Primary (mobile)");
    nav.innerHTML = TABBAR.filter(inThisBody).map(row).join("");
    document.body.appendChild(nav);
  }

  // Drop the body-scoped items from the desktop nav for a body that has no such
  // section. applyBodyChrome() rewrites labels but never touched the nav, so a
  // Planning Commission visitor was offered a tax rate and a budget that belong
  // to the council. Marked in the markup with data-nav-body.
  function applyBodyNav() {
    const body = currentBody();
    document.querySelectorAll("[data-nav-body]").forEach((a) => {
      if (a.dataset.navBody !== body) a.remove();
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

  // The URL a page wants to be indexed under.
  //
  // One shell serves many pages behind a query string, so the canonical has to
  // be computed rather than written into the HTML. member.html?id=rich-wood is
  // genuinely its own page and has to say so — a hard-coded canonical would
  // point all fourteen members at the bare shell and tell Google to drop
  // thirteen of them. motions.html?q=sewer&outcome=passed is the opposite case:
  // one filtered view of a single page, which must fold back to the bare URL or
  // every filter combination becomes a duplicate in the index.
  //
  // CONTENT_PARAMS is the whitelist that separates the two. Anything not on it —
  // filter state, utm_*, fbclid, a stray fragment of session junk — is dropped.
  // Order matters: it's the order build_sitemap.py writes these URLs in, and the
  // canonical has to match the sitemap byte for byte to be believed.
  const CONTENT_PARAMS = ["id", "body"];

  // Pinned rather than read from location.origin so a page opened from a preview
  // host, a github.io fallback or a file:// checkout can't publish a canonical
  // pointing somewhere nobody can fetch. Same reasoning as DEFAULT_HOST in
  // ingest/build_sitemap.py, and the two have to agree.
  const SITE_ORIGIN = "https://civicrollcall.com";

  function canonicalUrl(href = location.href) {
    let u;
    try { u = new URL(href); } catch { return null; }

    const keep = new URLSearchParams();
    for (const key of CONTENT_PARAMS) {
      const value = u.searchParams.get(key);
      if (!value) continue;
      // ?body=city-council is the default and renders the bare URL exactly, so
      // carrying it would manufacture a duplicate of every council page.
      if (key === "body" && value === DEFAULT_BODY) continue;
      keep.set(key, value);
    }

    // /index.html and / are the same page. The sitemap lists the bare origin.
    const path = u.pathname.replace(/\/index\.html$/, "/");
    const qs = keep.toString();
    return SITE_ORIGIN + path + (qs ? "?" + qs : "");
  }

  // Upsert <link rel="canonical"> and keep og:url pointing at the same place —
  // a share card that disagrees with the canonical is a mixed signal.
  function setCanonical(href) {
    // 404.html answers on every missing URL and is noindex for that reason.
    // Stamping a canonical onto it would name whatever path the reader mistyped
    // as a real page.
    const robots = document.head.querySelector('meta[name="robots"]');
    if (robots && /noindex/i.test(robots.content || "")) return;

    // A page that ships its own canonical and marks it data-fixed keeps it. The
    // generated meeting pages need this: decorateBodyLinks() appends ?body= to
    // in-site links when a non-default body is active, so a reader who last
    // looked at the planning commission would otherwise have every council
    // meeting page canonicalize itself to a ?body= variant of its own URL.
    let link = document.head.querySelector('link[rel="canonical"]');
    if (link && link.hasAttribute("data-fixed")) return;

    const url = href || canonicalUrl();
    if (!url) return;
    if (!link) {
      link = document.createElement("link");
      link.setAttribute("rel", "canonical");
      document.head.appendChild(link);
    }
    link.setAttribute("href", url);
    setMetaTag('meta[property="og:url"]', "property", "og:url", url);
  }

  // Where a meeting's own page lives.
  //
  // ingest/build_meeting_pages.py writes one static page per meeting and names it
  // <date>-<body id>-<event id>.html. This has to produce byte-identical output
  // or the index links somewhere that doesn't exist, so the two are deliberately
  // kept trivial: no slugifying, no lookups, just the three fields joined.
  function meetingHref(meeting, bodyId = currentBody()) {
    if (!meeting || !meeting.date || meeting.id == null) return null;
    return `/meetings/${meeting.date}-${bodyId}-${meeting.id}.html`;
  }

  // Append a JSON-LD node to the document.
  //
  // Only for schema that can't be known until the data lands — the Person behind
  // member.html?id=, the Event behind meetings.html?id=. Everything describable
  // without the dataset is written inline in the page <head> instead, because
  // Bing and the AI answer engines read the HTML without running any of this.
  function addSchema(node) {
    if (!node) return;
    const el = document.createElement("script");
    el.type = "application/ld+json";
    el.textContent = JSON.stringify(node);
    document.head.appendChild(el);
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

  // The bare noun a body goes by in prose — "council", "commission", "board".
  // The last word of the label carries it for every body the city seats.
  function bodyNoun(label) {
    return String(label).trim().split(/\s+/).pop().toLowerCase();
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
    // Copy written for the council calls it by its bare noun — the homepage
    // headline, the members section head. Swap the noun, not the sentence, so
    // the surrounding markup (the highlighted span in the hero) survives.
    const noun = bodyNoun(label);
    document.querySelectorAll("[data-body-noun]").forEach((el) => {
      el.textContent = noun;
    });
    // Tab and share titles spell the body out in full.
    const desc = document.head.querySelector('meta[name="description"]');
    setMeta({
      title: document.title.replace(/City Council/g, label),
      description: (desc?.content || "").replace(/City Council/g, label),
    });
  }

  // Boot every page: mount switcher + mobile nav, paint nav highlight, register SW.
  document.addEventListener("DOMContentLoaded", () => {
    setCanonical();
    // Before the highlight, so a removed item can't be the one lit up.
    applyBodyNav();
    mountBodySwitch();
    mountTabbar();
    highlightActiveNav();
    decorateBodyLinks();
    applyBodyChrome();
    wireSheetDismiss();
    wireMenuEscape();
    registerServiceWorker();
    mountAgent();
  });

  window.CT = {
    loadData,
    loadTranscripts,
    transcriptsForBody,
    loadAgendaOnly,
    agendaOnlyForBody,
    loadUpcoming,
    nextMeeting,
    cityToday,
    currentBody,
    linkBody,
    escapeHtml,
    memberName,
    tagLabel,
    cleanTitle,
    isCurrentMember,
    fmtDate,
    dateForms,
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
    setCanonical,
    canonicalUrl,
    addSchema,
    meetingHref,
  };
})();
