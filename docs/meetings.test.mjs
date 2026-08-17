/* =============================================================================
   Tests for the Meetings page's two lookups — node --test docs/meetings.test.mjs

   Same harness as upcoming.test.mjs: site.js is a browser script, so it is
   evaluated inside a vm context holding just enough of a browser, with fetch
   serving whichever data files a test wants to exist.

   What is covered:
     · a date typed the way people write dates finds the meeting
       (the bug that started this: "July 2" matched nothing, while
       /meetings/2024-07-02-city-council-141.html was live the whole time)
     · a date search doesn't match a day it shouldn't
     · the agenda-only feed loads per body, and a missing file is not an error
   ============================================================================= */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("./site.js", import.meta.url), "utf8");

/* Load site.js with a fetch that serves `files` by filename and 404s the rest. */
function loadSite(files = {}) {
  const sandbox = {
    console,
    URL,
    Intl,
    Date,
    location: { href: "https://civicrollcall.com/meetings.html" },
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
      const name = String(url).split("/").pop();
      if (!(name in files)) return { ok: false, status: 404 };
      return { ok: true, json: async () => files[name] };
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return sandbox.CT;
}

const agendaFeed = (entries) => ({
  generated_at: "2026-08-17T00:00:00Z",
  source: "Eagle Mountain CivicClerk portal",
  timezone: "America/Denver",
  bodies: { "city-council": entries },
});

/* ========================================================================== */

test("a date typed the way people write dates matches the meeting", () => {
  const CT = loadSite();
  const haystack = CT.dateForms("2024-07-02");
  for (const typed of ["2024-07-02", "july 2", "July 2, 2024", "jul 2 2024",
                       "2 july 2024", "7/2/2024", "07/02/2024", "7/2/24"]) {
    assert.ok(haystack.includes(typed.toLowerCase()),
              `"${typed}" should find the 2 July 2024 meeting`);
  }
});

test("a date search doesn't match the wrong day", () => {
  const CT = loadSite();
  const haystack = CT.dateForms("2024-07-02");
  for (const typed of ["july 3", "8/2/2024", "2024-07-03", "3 july 2024"]) {
    assert.ok(!haystack.includes(typed), `"${typed}" is a different day`);
  }
});

test("an empty date is empty rather than a crash", () => {
  const CT = loadSite();
  assert.equal(CT.dateForms(""), "");
  assert.equal(CT.dateForms(undefined), "");
});

test("agenda-only meetings load for the body that has them", async () => {
  const CT = loadSite({
    "data.agenda-only.json": agendaFeed([
      { id: 473, date: "2024-11-14", title: "Strategic Planning Conference",
        agenda: [{ number: "2", title: "DISCUSSION ITEMS", items: [{}, {}] }] },
    ]),
  });
  const list = await CT.agendaOnlyForBody("city-council");
  assert.equal(list.length, 1);
  assert.equal(list[0].id, 473);
  // Length rather than deepEqual: arrays built inside the vm are cross-realm,
  // so they never compare reference-equal to one out here.
  assert.equal((await CT.agendaOnlyForBody("planning-commission")).length, 0);
});

test("a site published before the agenda-only build ran still works", async () => {
  const CT = loadSite();          // no data.agenda-only.json at all
  assert.equal((await CT.agendaOnlyForBody("city-council")).length, 0);
});
