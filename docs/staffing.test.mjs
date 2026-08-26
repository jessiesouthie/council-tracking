// node --test docs/staffing.test.mjs
//
// The staffing page makes an arithmetic claim about eight cities, so the things
// worth testing are the arithmetic and the rendering, not the styling.
//
// The render functions are lifted out of staffing.html rather than duplicated
// here. That is deliberate: a copy of the code would pass forever while the page
// broke. The page's own <script> is evaluated in a sandbox with a stub DOM, so
// these tests exercise the same source the browser runs, against the same data
// file it ships with. A thrown reference, a bad template literal or a renamed
// JSON key fails here.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const DOCS = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(DOCS, "staffing.html"), "utf8");
const data = JSON.parse(readFileSync(join(DOCS, "data.staffing.json"), "utf8"));
const taxCase = JSON.parse(readFileSync(join(DOCS, "data.tax-case.json"), "utf8"));
const projections = JSON.parse(readFileSync(join(DOCS, "data.projections.json"), "utf8"));

/** Evaluate the page's inline script with just enough DOM for it to define its
 *  functions. The top-level IIFE runs and bails at the first fetch, which is
 *  what we want — the render path is driven directly below. */
function loadPage() {
  const source = html.match(/<script>\n([\s\S]*?)\n {4}<\/script>\s*<\/body>/)[1];
  const el = () => ({
    innerHTML: "",
    dataset: {},
    addEventListener() {},
    removeAttribute() {},
    setAttribute() {},
    querySelector: () => el(),
    querySelectorAll: () => [],
    appendChild() {},
    remove() {},
    click() {},
  });
  const ctx = {
    document: {
      // The page's top-level IIFE runs on load and resolves its fetches against
      // document.baseURI. Without one, `new URL(...)` throws asynchronously
      // after the tests have finished and the runner reports an unhandled
      // rejection with every test green. A real base and a failing fetch send
      // it down its own "couldn't load" path instead, which is also worth
      // knowing does not throw.
      baseURI: "https://civicrollcall.com/staffing.html",
      getElementById: () => el(),
      querySelector: () => el(),
      querySelectorAll: () => [],
      createElement: () => el(),
      body: el(),
    },
    fetch: () => Promise.reject(new Error("no network in tests")),
    URL: globalThis.URL,
    Blob: class {},
    setTimeout,
    console,
    CT: { currentBody: () => "city-council" },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx;
}

const page = loadPage();

test("the data file carries every city the page needs", () => {
  assert.ok(data.eagle_mountain.total > 0);
  assert.ok(data.peers.length >= 5, "peer set should not silently shrink");
  for (const p of data.peers) {
    assert.ok(p.population > 0, `${p.city} has no population`);
    assert.ok(p.source.url.startsWith("https://"), `${p.city} has no source URL`);
  }
});

test("each city's function split adds back to its own total", () => {
  const cities = [data.eagle_mountain, ...data.peers];
  for (const c of cities) {
    const sum = Object.values(c.by_function).reduce((a, b) => a + b, 0);
    assert.ok(
      Math.abs(sum - c.total) < 0.02,
      `${c.city ?? "Eagle Mountain"}: functions sum to ${sum}, total says ${c.total}`
    );
  }
});

test("the three measures nest the way the page says they do", () => {
  // Every FTE excluded from `core` is also excluded from `excl_public_safety`,
  // so the measures must fall in that order for the page's argument to hold.
  for (const c of [data.eagle_mountain, ...data.peers]) {
    const name = c.city ?? "Eagle Mountain";
    assert.ok(c.core <= c.excl_public_safety, `${name}: core exceeds excl-public-safety`);
    assert.ok(c.excl_public_safety <= c.total, `${name}: excl-public-safety exceeds total`);
    assert.ok(
      Math.abs(c.excl_public_safety - (c.total - c.public_safety)) < 0.02,
      `${name}: excl-public-safety is not total minus public safety`
    );
  }
});

test("per-1,000 figures match headcount over population", () => {
  for (const c of [data.eagle_mountain, ...data.peers]) {
    const name = c.city ?? "Eagle Mountain";
    const want = c.total / (c.population / 1000);
    assert.ok(
      Math.abs(c.per_1k.raw - want) < 0.005,
      `${name}: per_1k.raw is ${c.per_1k.raw}, expected ${want.toFixed(3)}`
    );
  }
});

test("Eagle Mountain employs essentially nobody in public safety", () => {
  // This is the premise the whole comparison rests on. If it ever stops being
  // true — the city stands up its own department — the page needs rewriting,
  // and this test is where that should surface.
  assert.ok(
    data.eagle_mountain.public_safety < 10,
    "Eagle Mountain now reports real public safety staff; the page's framing needs revisiting"
  );
});

test("the claim comparison is computed against the peers actually shown", () => {
  for (const key of ["raw", "excl_public_safety", "core"]) {
    const vals = data.peers.map((p) => p.per_1k[key]).sort((a, b) => a - b);
    const n = vals.length;
    const med = n % 2 ? vals[(n - 1) / 2] : (vals[n / 2 - 1] + vals[n / 2]) / 2;
    assert.ok(
      Math.abs(data.claim[key].peer_median - med) < 0.005,
      `${key}: stored median ${data.claim[key].peer_median} but peers give ${med}`
    );
    const pct = Math.round((data.claim[key].eagle_mountain / med) * 100);
    assert.equal(data.claim[key].pct_of_median, pct, `${key}: percentage does not follow`);
  }
});

test("excluding public safety narrows the gap rather than widening it", () => {
  // The page's central sentence. If normalization ever stopped moving the
  // number toward the peers, the argument on the page would be wrong.
  assert.ok(
    data.claim.excl_public_safety.pct_of_median > data.claim.raw.pct_of_median,
    "normalization no longer closes the gap; the claim section needs rewriting"
  );
});

test("render() produces the whole page without throwing", () => {
  let out = "";
  const root = {
    set innerHTML(v) { out = v; },
    get innerHTML() { return out; },
    removeAttribute() {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  page.render(data, taxCase, projections, root);

  assert.ok(out.includes("<h1"), "no heading rendered");
  assert.ok(out.includes("Eagle Mountain"), "subject city missing");
  assert.ok(!/undefined|\[object Object\]|NaN/.test(out), "placeholder leaked into the output");
  for (const p of data.peers) {
    assert.ok(out.includes(p.city), `${p.city} missing from the rendered page`);
  }
  for (const x of data.excluded) {
    assert.ok(out.includes(x.city), `excluded city ${x.city} is not disclosed`);
  }
});

test("render() still works when the optional files are missing", () => {
  // data.tax-case.json and data.projections.json enrich the page but are not
  // required; a failed fetch passes null and must not take the page down.
  let out = "";
  const root = {
    set innerHTML(v) { out = v; },
    get innerHTML() { return out; },
    removeAttribute() {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  page.render(data, null, null, root);
  assert.ok(out.includes("<h1"), "page did not render without the optional data");
  assert.ok(!/undefined|NaN/.test(out), "placeholder leaked when optional data was absent");
});

test("every chart carries a text alternative", () => {
  const rows = data.peers.map((p) => ({
    city: p.city, raw: p.per_1k.raw, excl: p.per_1k.excl_public_safety,
  }));
  const chart = page.compareChart(rows, "t");
  assert.ok(chart.includes('role="img"'), "comparison chart is not exposed as an image");
  assert.ok(chart.includes("<desc"), "comparison chart has no description");

  const growth = page.growthChart(data.eagle_mountain.series, "g");
  assert.ok(growth.includes('role="img"'), "growth chart is not exposed as an image");
  assert.ok(growth.includes("<desc"), "growth chart has no description");
});

test("the page and the data agree on the headline figures", () => {
  // The <noscript> block and the meta description are hand-written, so they can
  // drift from the data the way no generated string can.
  const em = data.eagle_mountain;
  assert.ok(
    html.includes(String(em.total)),
    `the static markup does not mention ${em.total}; noscript and meta need updating`
  );
});

test("the headline percentages come from the data, not from the prose", () => {
  // These three numbers were hardcoded once and went stale the moment the Census
  // published a new vintage and every denominator moved. Rendering with doctored
  // data proves they are still being read rather than typed.
  const doctored = JSON.parse(JSON.stringify(data));
  for (const k of ["raw", "excl_public_safety", "core"]) doctored.claim[k].pct_of_median = 11;
  let out = "";
  const root = {
    set innerHTML(v) { out = v; },
    get innerHTML() { return out; },
    removeAttribute() {}, querySelector: () => null, querySelectorAll: () => [],
  };
  page.render(doctored, taxCase, projections, root);
  const shown = [...out.matchAll(/class="sf-verdict-n">(\d+)</g)].map((m) => m[1]);
  assert.equal(shown.length, 3, "expected three headline percentages");
  assert.deepEqual(shown, ["11", "11", "11"], "a percentage is hardcoded in the markup");
});

test("population figures in the static markup track the data", () => {
  // The <noscript> block and the meta description are hand-written and cannot be
  // generated, so they are the one place a stale number can survive a rebuild.
  const pop = data.eagle_mountain.population;
  const rounded = Math.round(pop / 100) * 100;
  const shown = [...html.matchAll(/roughly ([\d,]+) residents/g)].map((m) =>
    Number(m[1].replace(/,/g, ""))
  );
  assert.ok(shown.length > 0, "no population figure found in the static markup");
  for (const n of shown) {
    assert.ok(
      Math.abs(n - pop) < 1000,
      `static markup says ${n.toLocaleString()} residents, data says ${pop.toLocaleString()} (rounded ${rounded})`
    );
  }
});

test("every city inside the rank cut is either compared or explained", () => {
  // The defect this guards against actually happened: two cities from outside
  // the cut were added by hand and one inside it was dropped in silence when its
  // download failed. The builder now refuses to run in that state; this checks
  // the shipped file, and the next test checks the reader can see it.
  const used = new Set(data.peers.map((p) => p.city));
  const excused = new Map(data.excluded.map((x) => [x.city, x.reason]));
  for (const r of data.peer_rule.ranked) {
    const ok = used.has(r.city) || excused.has(r.city);
    assert.ok(ok, `${r.city} (rank ${r.rank}) is neither compared nor explained`);
    if (!used.has(r.city)) {
      assert.ok(
        (excused.get(r.city) || "").length > 20,
        `${r.city} is excluded without a real reason`
      );
    }
  }
  for (const p of data.peers) {
    assert.ok(
      data.peer_rule.ranked.some((r) => r.city === p.city),
      `${p.city} is compared but is not inside the rank cut`
    );
  }
});

test("the rendered page discloses the full selection, not just the winners", () => {
  let out = "";
  const root = {
    set innerHTML(v) { out = v; },
    get innerHTML() { return out; },
    removeAttribute() {}, querySelector: () => null, querySelectorAll: () => [],
  };
  page.render(data, taxCase, projections, root);
  for (const r of data.peer_rule.ranked) {
    assert.ok(out.includes(r.city), `${r.city} is inside the rank cut but never named on the page`);
  }
});
