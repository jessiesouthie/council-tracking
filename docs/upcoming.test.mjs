/* =============================================================================
   Tests for the "next meeting" lookup — node --test docs/upcoming.test.mjs

   Node's built-in runner, no dependencies, same as projections.test.mjs. site.js
   is a browser script rather than a module, so it is evaluated inside a vm
   context holding just enough of a browser (a document that answers nothing, a
   localStorage that forgets, a fetch that serves one file) plus a Date pinned to
   a chosen instant. That way the calendar logic is tested against the exact file
   the site loads.

   What is covered:
     · the soonest meeting that has not happened yet is the one returned
     · a meeting today stays "next" all day, including after it starts
     · "today" is Eagle Mountain's day, not the reader's
     · a stale or missing calendar shows nothing rather than a past meeting
   ============================================================================= */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("./site.js", import.meta.url), "utf8");

const meeting = (date, over = {}) => ({
  id: 700 + Number(date.slice(8, 10)),
  title: "City Council Meeting",
  date,
  start: "16:00",
  start_label: "4:00 PM",
  location: "Eagle Mountain City Council Chambers, 1650 E Stagecoach Run",
  url: `https://eaglemountainut.portal.civicclerk.com/event/728/overview`,
  agenda_posted: false,
  sessions: [],
  ...over,
});

const feedOf = (dates, over = {}) => ({
  generated_at: "2026-08-16T19:29:24Z",
  timezone: "America/Denver",
  bodies: { "city-council": dates.map((d) => meeting(d)) },
  ...over,
});

/* A Date frozen at one instant, so "is this meeting still ahead?" is a question
   with a fixed answer rather than one that changes with the clock on the wall. */
function frozenDate(instant) {
  const Real = Date;
  return class extends Real {
    constructor(...args) {
      super(...(args.length ? args : [instant]));
    }
    static now() {
      return new Real(instant).getTime();
    }
  };
}

/* Load site.js against a given calendar file and moment. `feed` of null stands
   for the file being absent (a site published before ingest.build_upcoming ever
   ran, or a fetch that failed). */
function loadSite({ feed, now }) {
  const sandbox = {
    console,
    URL,
    Intl,
    Date: frozenDate(now),
    location: { href: "https://civicrollcall.com/index.html" },
    localStorage: { getItem: () => null, setItem() {} },
    navigator: {},
    document: {
      baseURI: "https://civicrollcall.com/",
      addEventListener() {},
      querySelectorAll: () => [],
      getElementById: () => null,
      createElement: () => ({ setAttribute() {} }),
      head: { appendChild() {} },
    },
    fetch: async (url) => {
      if (!String(url).endsWith("data.upcoming.json") || !feed) {
        return { ok: false, status: 404 };
      }
      return { ok: true, json: async () => feed };
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox.CT;
}

/* ========================================================================== */

test("returns the soonest meeting still ahead, not the first one listed", async () => {
  const CT = loadSite({
    feed: feedOf(["2026-09-01", "2026-08-04", "2026-08-18"]),
    now: "2026-08-16T18:00:00Z", // noon in Eagle Mountain
  });
  const next = await CT.nextMeeting("city-council");
  assert.equal(next.date, "2026-08-18");
  assert.equal(next.days_away, 2);
  assert.equal(next.start_label, "4:00 PM");
});

test("a meeting today stays the next meeting, even once it has started", async () => {
  const CT = loadSite({
    feed: feedOf(["2026-08-18", "2026-09-01"]),
    // 8:30 PM in Eagle Mountain — the 4 PM meeting is well under way.
    now: "2026-08-19T02:30:00Z",
  });
  const next = await CT.nextMeeting("city-council");
  assert.equal(next.date, "2026-08-18", "the card should not vanish mid-meeting");
  assert.equal(next.days_away, 0);
});

test("the day turns over in Eagle Mountain, not in the reader's timezone", async () => {
  const CT = loadSite({
    feed: feedOf(["2026-08-18"]),
    // Past midnight UTC on the 19th, but still 10 PM on the 18th in Utah.
    now: "2026-08-19T04:00:00Z",
  });
  assert.equal(CT.cityToday("America/Denver"), "2026-08-18");
  const next = await CT.nextMeeting("city-council");
  assert.equal(next.date, "2026-08-18");
});

test("a calendar that has run out shows nothing rather than a past meeting", async () => {
  const CT = loadSite({
    feed: feedOf(["2026-08-04", "2026-08-18"]),
    now: "2026-10-01T18:00:00Z",
  });
  assert.equal(await CT.nextMeeting("city-council"), null);
});

test("a missing calendar file, or an unknown body, is simply nothing", async () => {
  const absent = loadSite({ feed: null, now: "2026-08-16T18:00:00Z" });
  assert.equal(await absent.nextMeeting("city-council"), null);

  const present = loadSite({
    feed: feedOf(["2026-08-18"]),
    now: "2026-08-16T18:00:00Z",
  });
  assert.equal(await present.nextMeeting("planning-commission"), null);
});

test("the sessions the agenda names are carried through untouched", async () => {
  const sessions = [
    { start: "16:00", start_label: "4:00 PM", label: "Work Session", public_comment: false },
    { start: "19:00", start_label: "7:00 PM", label: "Policy Session", public_comment: true },
  ];
  const feed = feedOf(["2026-08-18"]);
  Object.assign(feed.bodies["city-council"][0], { sessions, agenda_posted: true });

  const CT = loadSite({ feed, now: "2026-08-16T18:00:00Z" });
  const next = await CT.nextMeeting("city-council");
  assert.deepEqual(next.sessions, sessions);
  assert.equal(next.sessions.filter((s) => s.public_comment).length, 1);
});
