// node --test docs/tax.test.mjs
//
// The tax page states, in a dozen places, what the city will charge. Between 6
// and 18 August 2026 that number changed twice — adopted, repealed, adopted
// again — and the page went on rendering the old one anywhere a date or a rate
// had been typed into the copy rather than read from the data. These tests are
// the guard against that: the shipped data is the only source of the rate, and
// the rendered page has to agree with it.
//
// Same method as staffing.test.mjs: the page's own <script> is evaluated in a
// sandbox with a stub DOM, so this exercises the source the browser runs.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const DOCS = dirname(fileURLToPath(import.meta.url));
const read = (f) => JSON.parse(readFileSync(join(DOCS, f), "utf8"));
const html = readFileSync(join(DOCS, "tax.html"), "utf8");

const tax = read("data.tax.json");
const county = read("data.tax-county.json");
const bill = read("data.tax-bill.json");
const state = read("data.tax-state.json");
const kase = read("data.tax-case.json");

function loadPage() {
  const source = html.match(/<script>\n([\s\S]*?)\n {4}<\/script>\s*<\/body>/)[1];
  const el = () => ({
    innerHTML: "",
    style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    dataset: {},
    addEventListener() {},
    removeAttribute() {},
    setAttribute() {},
    getAttribute: () => null,
    querySelector: () => el(),
    querySelectorAll: () => [],
    closest: () => null,
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    appendChild() {},
    remove() {},
    focus() {},
    click() {},
  });
  const ctx = {
    document: {
      baseURI: "https://civicrollcall.com/tax.html",
      documentElement: el(),
      getElementById: () => el(),
      querySelector: () => el(),
      querySelectorAll: () => [],
      createElement: () => el(),
      addEventListener() {},
      body: el(),
    },
    window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
    fetch: () => Promise.reject(new Error("no network in tests")),
    URL: globalThis.URL,
    URLSearchParams: globalThis.URLSearchParams,
    location: { search: "", hash: "" },
    history: { replaceState() {} },
    requestAnimationFrame: (fn) => fn(),
    setTimeout,
    clearTimeout,
    console,
    CT: {
      currentBody: () => "city-council",
      escapeHtml: (s) =>
        String(s ?? "").replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
        ),
      fmtDate: (iso) => String(iso || ""),
      bodyLabel: () => ({ label: "City Council" }),
    },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx;
}

const page = loadPage();

const renderTo = (...args) => {
  let out = "";
  const root = {
    set innerHTML(v) { out = v; },
    get innerHTML() { return out; },
    removeAttribute() {},
    setAttribute() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  page.render(...args, root);
  return out;
};

/* ------------------------------------------------------------------ */
/* 1. The data says one thing about which rate is in force.            */
/* ------------------------------------------------------------------ */

test("exactly one rate is the rate in force, and the ones it replaced are kept", () => {
  const r = tax.rates;
  assert.ok(r.adopted, "the page needs an adopted rate to speak in the past tense");
  assert.ok(r.certified_2026, "the certified rate is the comparison basis");
  // A superseded rate never sits under `adopted`: everything that prices a bill
  // reads that key, so two rates under it would be two answers to one question.
  assert.notEqual(r.adopted.rate, (r.repealed || {}).rate, "the repealed rate is still the adopted one");
  if (r.repealed) {
    assert.equal(r.repealed.superseded_by, "adopted", "a repealed rate must say what replaced it");
    assert.ok(r.repealed.repealed_on, "a repealed rate must say when");
    assert.ok(r.repealed.motion, "the repealed rate's motion is the record and stays quotable");
  }
});

test("the adopted figures are arithmetic on the rates the city published", () => {
  const cert = tax.rates.certified_2026;
  const ad = tax.rates.adopted;
  // The taxable value the city's own published pairs imply, and the only base
  // any of these figures may be priced against.
  const base = 3_055_201 / cert.rate;
  assert.ok(Math.abs(ad.revenue - ad.rate * base) < 1, "adopted revenue is not rate × the published base");
  assert.ok(Math.abs(ad.revenue - ad.revenue_increase - 3_055_201) < 1, "the increase is not measured off the certified rate");
  const pct = (ad.rate / cert.rate - 1) * 100;
  assert.ok(Math.abs(pct - ad.pct) < 0.01, `pct ${ad.pct} is not ${pct.toFixed(2)}`);
  // The home the city's own notice uses, priced the way the notice prices it.
  const h = ad.home_example;
  const dollars = h.taxable_value * (ad.rate - cert.rate);
  assert.ok(Math.abs(dollars - h.increase) < 0.02, `home example ${h.increase} is not ${dollars.toFixed(2)}`);
  assert.equal(h.taxable_value, Math.round(h.market_value * tax.residential_ratio), "taxable value is not 55% of market");
});

test("a figure the city has not published is marked as derived, and carries no quotation", () => {
  const ad = tax.rates.adopted;
  if (ad.provisional) {
    assert.equal(ad.motion, null, "a provisional rate may not carry a motion it has not read");
    assert.equal(ad.vote, null, "a provisional rate may not carry a vote it has not read");
    assert.ok(ad.unverified, "a provisional rate must say what is still unverified");
    assert.ok(ad.pct_derived, "a percentage nobody published must be declared derived");
  }
});

test("every comparison file carries the same rate as the tax file", () => {
  const rate = tax.rates.adopted.rate;
  const date = tax.rates.adopted.date;
  assert.equal(county.eagle_mountain.adopted_rate, rate, "the county comparison is on a different rate");
  assert.equal(county.eagle_mountain.adopted_date, date, "the county comparison is on a different date");
  assert.equal(county.salt_lake.reference.adopted_rate, rate, "the Salt Lake reference bar is on a different rate");
  assert.equal(county.statewide.eagle_mountain.adopted_rate, rate, "the statewide ranking is on a different rate");
  assert.equal(state.eagle_mountain.adopted.rate, rate, "the statewide notice list is on a different rate");
  const em = county.proposals.find((p) => p.city === "Eagle Mountain");
  assert.equal(em.resulting_rate, rate, "Eagle Mountain's row in the county chart is on a different rate");
  assert.equal(bill.entities.find((e) => e.is_city).proposed, rate, "the whole-bill table is on a different rate");
});

test("the county and statewide ranks are the ones the rate actually earns", () => {
  const rate = tax.rates.adopted.rate;
  const resulting = new Map(county.proposals.map((p) => [p.city, p.resulting_rate]));
  const rank = (rates, r) => rates.filter((x) => x > r).length + 1;
  const utahCounty = county.cities
    .filter((c) => c.name !== "Eagle Mountain")
    .map((c) => (resulting.has(c.name) ? resulting.get(c.name) : c.current_rate));
  assert.equal(county.eagle_mountain.rank_adopted, rank(utahCounty, rate), "rank_adopted is stale");
  assert.equal(county.eagle_mountain.rank_current, rank(utahCounty, county.eagle_mountain.current_rate), "rank_current is stale");

  const sw = county.statewide.cities.filter((c) => c.name !== "Eagle Mountain").map((c) => c.rate);
  const em = county.statewide.eagle_mountain;
  assert.equal(em.rank_adopted_high, rank(sw, rate), "statewide rank_adopted_high is stale");
  assert.equal(em.rank_adopted_low, sw.filter((x) => x < rate).length + 1, "statewide rank_adopted_low is stale");
});

/* ------------------------------------------------------------------ */
/* 2. The page renders that data, and no other number.                 */
/* ------------------------------------------------------------------ */

test("the estimator's percent field is on the certified rate, not on today's", () => {
  // The two bases differ by less than a point and a half, which is exactly why
  // this needs pinning: a reader who types the adopted rate has to get back the
  // percentage the city published, not one that is nearly it.
  const cert = tax.rates.certified_2026.rate;
  const ad = tax.rates.adopted;
  assert.equal(page.pctBase(tax), cert, "the estimator is measuring against the wrong rate");
  const shown = (ad.rate / page.pctBase(tax) - 1) * 100;
  assert.ok(
    Math.abs(shown - ad.pct) < 0.01,
    `typing ${ad.rate_display} gives +${shown.toFixed(2)}%, but the city states +${ad.pct}%`
  );
  // The dollars are a different question and keep their own basis: nobody was
  // ever billed the certified rate.
  assert.notEqual(page.pctBase(tax), tax.rates.current.rate, "the two bases have collapsed into one");
});

test("the percent basis falls back when the county has not certified a rate", () => {
  const noCert = { ...tax, rates: { ...tax.rates, certified_2026: undefined } };
  assert.equal(page.pctBase(noCert), tax.rates.current.rate, "a year without a certified rate has no basis");
});

test("render() produces the whole page without throwing", () => {
  const out = renderTo(tax, county, bill, state, kase);
  assert.ok(out.includes("<h1"), "no heading rendered");
  assert.ok(!/undefined|\[object Object\]|NaN/.test(out), "placeholder leaked into the output");
});

test("render() survives every optional file failing to load", () => {
  const out = renderTo(tax, null, null, null, null);
  assert.ok(out.includes("<h1"), "the page did not render on the tax file alone");
  assert.ok(!/undefined|NaN/.test(out), "placeholder leaked when optional data was absent");
});

test("the rate on the page is the rate in the data, and the repealed one is labeled", () => {
  const out = renderTo(tax, county, bill, state, kase);
  const ad = tax.rates.adopted;
  assert.ok(out.includes(ad.rate_display), "the adopted rate is not on the page");
  // The old rate may still appear — the record is the point — but never as the
  // thing the reader is being told they will pay.
  const rep = tax.rates.repealed;
  if (rep) {
    const claims = out.match(/the council set the rate at[\s\S]{0,120}/g) || [];
    claims.forEach((c) => {
      assert.ok(!c.includes(rep.rate_display), `the hero still states the repealed rate: ${c.slice(0, 80)}`);
    });
    assert.ok(/repeal/i.test(out), "the repeal is not disclosed anywhere on the page");
  }
});

test("no sentence quotes a motion the record has not produced", () => {
  const out = renderTo(tax, county, bill, state, kase);
  const ad = tax.rates.adopted;
  if (ad.provisional) {
    assert.ok(
      /paperwork is not published yet/.test(out),
      "a provisional rate must say on the page that its resolution is not out"
    );
    assert.ok(!/“”|“ ”/.test(out), "an empty quotation was rendered");
  }
});
