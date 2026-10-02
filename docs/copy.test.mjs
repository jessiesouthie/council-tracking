/* =============================================================================
   Tests that the words on the pages keep up with the record — node --test docs/copy.test.mjs

   house-style.test.mjs holds the model prompts to ingest/house_style.txt. This
   holds the hand-written copy to the same rules where a test can see them, and
   holds the figures typed into that copy to the data files they came from.
   Most of the site's numbers render from docs/data.*.json, but some are typed
   into a sentence, a share description or the Ask widget, and those are the
   ones that went stale after 18 August: pages still said "proposed" of a rate
   and a budget the council had adopted, and the budget page printed the
   agenda's total as the adopted one.

   What is covered:
     · no em dash in a page's share descriptions or its static sentences
     · nothing calls the FY2026–27 rate or budget "proposed" once it is adopted
     · the budget's adopted total is printed only once one is published
     · hand-typed figures agree with the data file that holds them
   ============================================================================= */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const DOCS = fileURLToPath(new URL("./", import.meta.url));
const read = (p) => readFileSync(path.join(DOCS, p), "utf8");
const json = (p) => JSON.parse(read(p));

const PAGES = readdirSync(DOCS).filter((f) => f.endsWith(".html"));
const tax = json("data.tax.json");
const next = json("data.budget-next.json");

/* The text a reader sees without running a script: the <body> minus comments,
   scripts and styles. Page scripts render plenty more, but their source is code,
   and the rendered output is the render tests' business. */
function staticText(html) {
  const body = html.slice(html.indexOf("<body"));
  return body
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<script[\s\S]*?<\/script>/g, "")
    .replace(/<style[\s\S]*?<\/style>/g, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&mdash;/g, "—");
}

const descriptions = (html) =>
  [...html.matchAll(/<meta (?:name|property)="(?:description|og:description|twitter:description)" content="([^"]*)"/g)]
    .map((m) => m[1]);

test("no em dash in a share description", () => {
  // The description is what Facebook and a texted link show under the title,
  // which makes it the most-read sentence on most pages.
  const bad = PAGES.flatMap((p) => descriptions(read(p)).filter((d) => d.includes("—")).map((d) => `${p}: ${d.slice(0, 60)}…`));
  assert.deepEqual(bad, []);
});

test("no em dash in a page's static sentences", () => {
  // A lone dash in a table cell stands for "no figure" and is not a sentence.
  const bad = [];
  for (const p of PAGES) {
    for (const line of staticText(read(p)).split("\n")) {
      const t = line.trim();
      if (t.includes("—") && t !== "—") bad.push(`${p}: ${t.slice(0, 80)}`);
    }
  }
  assert.deepEqual(bad, []);
});

test("the Ask widget's greeting has no em dash and no stale tense", () => {
  const src = read("agent.js");
  const greeting = src.match(/renderMarkdown\(\s*("Hi[\s\S]*?")\s*\)/)?.[1] || "";
  assert.ok(greeting, "agent.js greeting not found");
  assert.doesNotMatch(greeting, /—/);
  if (tax.rates.adopted) assert.doesNotMatch(greeting, /proposed/i);
});

test("once the rate is adopted, nothing static calls the year's tax or budget proposed", () => {
  if (!tax.rates.adopted) return;
  const stale = /proposed (?:FY2026[–-]27|tax change|property-tax revenue exactly)|FY2026[–-]27 proposed|increase passes/;
  const bad = [];
  for (const p of PAGES) {
    const html = read(p);
    const head = html.slice(0, html.indexOf("<body"));
    for (const s of [...descriptions(html), ...[...head.matchAll(/"(?:name|description)": "([^"]*)"/g)].map((m) => m[1]), staticText(html)]) {
      const m = s.match(stale);
      if (m) bad.push(`${p}: …${s.slice(Math.max(0, m.index - 30), m.index + 50)}…`);
    }
    // The budget and projections pages render their copy from script, so look
    // in the source too: these phrases only ever appear in sentences.
    if (/budget|projections/.test(p)) {
      for (const m of html.matchAll(new RegExp(stale, "g"))) bad.push(`${p} (script): ${m[0]}`);
    }
  }
  assert.deepEqual(bad, []);
});

test("the budget's adopted total is printed only once one is published", () => {
  const fa = next.final_adopted;
  assert.ok(fa, "data.budget-next.json has no final_adopted");
  if (fa.amendments && fa.amendments.length) {
    // Adopted with amendments: the agenda's figure is not the adopted one.
    assert.equal(fa.total, null, "an amended budget carries the agenda total as if adopted");
    assert.ok(fa.agenda_total, "the agenda's figure is missing");
  }
  const page = read("budget.html");
  if (fa.total == null) {
    assert.doesNotMatch(page, /final_adopted\.total/, "budget.html prints a total that is null");
  }
  // The struck amounts the page sums in prose.
  const struck = fa.amendments
    .map((a) => a.match(/^Struck \$([\d,]+)/)?.[1])
    .filter(Boolean)
    .reduce((s, d) => s + Number(d.replace(/,/g, "")), 0);
  assert.match(page, new RegExp(`striking\\s+\\$${(struck / 1e6).toFixed(1)}M`), `budget.html should say $${(struck / 1e6).toFixed(1)}M was struck`);
});

/* ---------------------------------------------------------------------------
   Counts typed into sentences. Each was right when it was written and went
   wrong when the thing it counts changed under it: the definitions page lost
   an entry with the Community Services Board, and the staffing comparison
   lost a peer city when its schedule failed the method's rule.
   --------------------------------------------------------------------------- */

const WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"];

test("the front page counts the definitions there are", () => {
  const entries = (read("definitions.html").match(/<dt[\s>]/g) || []).length;
  const said = read("index.html").match(/(\d+) plain-English definitions/)?.[1];
  assert.ok(said, "index.html no longer counts the definitions");
  assert.equal(Number(said), entries, `index.html says ${said}, definitions.html has ${entries}`);
});

test("the staffing page counts the peer cities the data compares", () => {
  const n = json("data.staffing.json").peers.length;
  const html = read("staffing.html");
  const said = [...html.matchAll(new RegExp(`\\b(${WORDS.join("|")})\\s+(?:similar-sized\\s+)?Utah cities`, "gi"))].map((m) => m[1].toLowerCase());
  assert.ok(said.length, "staffing.html no longer counts its peer cities");
  for (const w of said) {
    assert.equal(w, WORDS[n], `staffing.html says "${w}" Utah cities; data.staffing.json has ${n} peers`);
  }
});

test("every fact check page links to the full list with the right count", () => {
  const n = json("data.claims.json").claims.length;
  for (const f of readdirSync(path.join(DOCS, "claims")).filter((x) => x.endsWith(".html"))) {
    const said = read(`claims/${f}`).match(/See all (\d+) fact checks/)?.[1];
    if (said) assert.equal(Number(said), n, `claims/${f} says ${said}`);
  }
});

test("no motion summary calls the adopted rate the certified one", () => {
  // The 18 August minutes call 0.000900 "the new certified rate". It is the
  // adopted rate; the certified rate is 0.000530. A summary may quote the
  // minutes, but not repeat the mistake in its own words.
  const cert = tax.rates.certified_2026.rate_display;
  const summaries = Object.values(JSON.parse(readFileSync(path.join(DOCS, "..", "data/meta/motion_summaries.json"), "utf8")).summaries);
  const bad = summaries.filter((s) => /match the certified rate|newly certified/.test(`${s.headline} ${s.summary}`));
  assert.deepEqual(bad.map((s) => s.headline), [], `summaries treating the adopted rate as the certified one (${cert})`);
});
