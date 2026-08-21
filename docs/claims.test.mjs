// node --test docs/claims.test.mjs
//
// The claims page is an evidence page: every card asserts that the record says
// something, and the whole value of it is that the assertion can be clicked
// through to. So what is worth testing is not the styling — it is that the
// sourcing rules the page states about itself are actually true of the data it
// ships, and that a claim's citations point at meetings this site really has.
//
// Same method as staffing.test.mjs and tax.test.mjs: the page's own <script> is
// evaluated in a sandbox with a stub DOM, so these exercise the source the
// browser runs rather than a copy of it.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import vm from "node:vm";

const DOCS = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(DOCS, "..");
const html = readFileSync(join(DOCS, "claims.html"), "utf8");
const data = JSON.parse(readFileSync(join(DOCS, "data.claims.json"), "utf8"));
const record = JSON.parse(readFileSync(join(DOCS, "data.json"), "utf8"));
const transcripts = JSON.parse(readFileSync(join(DOCS, "transcripts", "index.json"), "utf8"));
const upcoming = JSON.parse(readFileSync(join(DOCS, "data.upcoming.json"), "utf8"));

function loadPage() {
  const source = html.match(/<script>\n([\s\S]*?)\n {4}<\/script>\s*<\/body>/)[1];
  const el = () => ({
    innerHTML: "",
    classList: { add() {}, remove() {}, contains: () => false },
    dataset: {},
    addEventListener() {},
    removeAttribute() {},
    setAttribute() {},
    querySelector: () => el(),
    querySelectorAll: () => [],
    closest: () => null,
    scrollIntoView() {},
    appendChild() {},
    remove() {},
    click() {},
  });
  const schemas = [];
  const ctx = {
    document: {
      baseURI: "https://civicrollcall.com/claims.html",
      getElementById: () => el(),
      querySelector: () => el(),
      querySelectorAll: () => [],
      createElement: () => el(),
      body: el(),
      head: el(),
    },
    location: { origin: "https://civicrollcall.com", pathname: "/claims.html", hash: "" },
    navigator: {},
    fetch: () => Promise.reject(new Error("no network in tests")),
    URL: globalThis.URL,
    setTimeout,
    console,
    CT: {
      currentBody: () => "city-council",
      escapeHtml: (s) =>
        String(s ?? "").replace(/[&<>"']/g, (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
      fmtDate: (iso) => String(iso ?? ""),
      addSchema: (node) => schemas.push(node),
    },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  ctx.__schemas = schemas;
  // `const` at the top of the script is a lexical binding, not a property of
  // the context object, so the two lookup tables have to be read out of the
  // scope rather than off `ctx`.
  ctx.__esc = vm.runInContext("esc", ctx);
  ctx.__TONE = vm.runInContext("TONE", ctx);
  ctx.__RATING = vm.runInContext("RATING", ctx);
  return ctx;
}

const page = loadPage();

/* ---------------------------------------------------------------------------
   The rules the page states about itself
   --------------------------------------------------------------------------- */

test("every claim carries a verdict the page defines", () => {
  const known = new Set(data.verdicts.map((v) => v.key));
  assert.ok(known.size >= 5, "the verdict vocabulary has collapsed");
  for (const c of data.claims) {
    assert.ok(known.has(c.verdict), `${c.id}: verdict "${c.verdict}" is not in verdicts[]`);
  }
});

test("every verdict has a colour and a rating", () => {
  // An unmapped verdict renders as neutral and rates as a 3, which would quietly
  // publish "Not accurate" as an even-handed shrug.
  for (const v of data.verdicts) {
    assert.ok(page.__TONE[v.key], `verdict "${v.key}" has no tone in claims.html`);
    assert.ok(page.__RATING[v.key], `verdict "${v.key}" has no ClaimReview rating`);
  }
});

test("every claim is filed under a topic the page defines", () => {
  // The filter is only as honest as this: a claim with no topic is reachable
  // from "Everything" and from nowhere else, which reads as "not about roads"
  // to a reader who filtered to roads.
  const known = new Set(data.categories.map((c) => c.key));
  assert.ok(known.size >= 4, "the topic vocabulary has collapsed");
  for (const c of data.claims) {
    assert.ok(c.topics && c.topics.length, `${c.id} has no topics`);
    assert.equal(new Set(c.topics).size, c.topics.length, `${c.id} repeats a topic`);
    for (const t of c.topics) {
      assert.ok(known.has(t), `${c.id}: topic "${t}" is not in categories[]`);
    }
  }
});

test("every topic has a label, a note, and something filed under it", () => {
  const used = new Set(data.claims.flatMap((c) => c.topics || []));
  for (const cat of data.categories) {
    assert.match(cat.key, /^[a-z0-9-]+$/, `bad topic key "${cat.key}" — it goes in a URL`);
    assert.ok(cat.label && cat.label.length < 24, `${cat.key}: no label, or one too long for a button`);
    assert.ok(cat.note && cat.note.length > 20, `${cat.key}: no note for the legend`);
    // A button that filters twenty claims down to none is a dead end the reader
    // has to undo. filters() drops empty topics from the bar; this catches the
    // legend, which prints the vocabulary whole.
    assert.ok(used.has(cat.key), `topic "${cat.key}" is defined but nothing is filed under it`);
  }
});

test("every claim is sourced", () => {
  // The page's first rule. A card with no citation is an assertion, and this
  // site's whole argument is that it doesn't publish those.
  for (const c of data.claims) {
    assert.ok(c.sources && c.sources.length, `${c.id} has no sources`);
    for (const s of c.sources) {
      const cited = s.meeting != null || s.motion != null || s.doc || s.date;
      assert.ok(cited, `${c.id}: a source with no meeting, document or date`);
      // A quotation must say who said it, or it is a sentence with quote marks.
      if (s.quote) assert.ok(s.speaker, `${c.id}: a quotation with no speaker`);
      if (s.doc) assert.match(s.doc.url, /^https:\/\//, `${c.id}: document without an https URL`);
    }
  }
});

test("every cited meeting exists in the record", () => {
  // The citation is the point of the card. An id the site can't resolve renders
  // a link to a meeting page that will not load — and a meeting is resolvable
  // from any of the three files meetings.html reads, not just the minuted ones.
  // The August hearings are exactly that case: #747 exists only as a transcript
  // and #728 only as a calendar entry, because neither has been minuted yet.
  const known = new Set((record.meetings || []).map((m) => m.id));
  assert.ok(known.size > 100, "data.json didn't load the way this test expects");
  for (const list of Object.values(transcripts)) {
    for (const t of list) known.add(t.id);
  }
  for (const list of Object.values(upcoming.bodies || {})) {
    for (const u of list) known.add(u.id);
  }
  const motions = new Set((record.motions || []).map((m) => m.id));
  assert.ok(motions.size > 500, "data.json didn't load the way this test expects");
  for (const c of data.claims) {
    for (const s of c.sources || []) {
      if (s.motion != null) {
        assert.ok(motions.has(s.motion), `${c.id} cites vote #${s.motion}, which is not on file`);
      }
      if (s.meeting == null) continue;
      assert.ok(known.has(s.meeting), `${c.id} cites meeting #${s.meeting}, which is not on file`);
    }
  }
});

test("every claim has an id, a ruling and a checked date", () => {
  const seen = new Set();
  for (const c of data.claims) {
    assert.match(c.id, /^[a-z0-9-]+$/, `bad id "${c.id}" — it is a URL fragment`);
    assert.ok(!seen.has(c.id), `duplicate id ${c.id}; two cards would share one permalink`);
    seen.add(c.id);
    assert.ok(c.claim && c.claim.length > 10, `${c.id}: no claim text`);
    assert.ok(c.ruling && c.ruling.length > 20, `${c.id}: no ruling`);
    assert.match(c.checked, /^\d{4}-\d{2}-\d{2}$/, `${c.id}: checked date is not ISO`);
    assert.ok(c.checked <= data.updated, `${c.id}: checked after the page's own updated date`);
  }
});

test("every in-site link a card offers points at a real page", () => {
  for (const c of data.claims) {
    for (const m of c.read_more || []) {
      const file = m.href.split("#")[0].split("?")[0];
      assert.ok(existsSync(join(DOCS, file)), `${c.id}: read_more points at missing ${file}`);
    }
  }
});

/* ---------------------------------------------------------------------------
   Rendering
   --------------------------------------------------------------------------- */

test("every claim renders, and renders its citation", () => {
  const verdicts = Object.fromEntries(data.verdicts.map((v) => [v.key, v]));
  const cats = Object.fromEntries(data.categories.map((c) => [c.key, c]));
  for (const [i, c] of data.claims.entries()) {
    const out = page.card(c, verdicts, i + 1, cats);
    assert.ok(out.includes(`id="${c.id}"`), `${c.id}: card lost its anchor`);
    assert.ok(out.includes(verdicts[c.verdict].label), `${c.id}: verdict label missing`);
    assert.ok(out.includes(`data-copy="${c.id}"`), `${c.id}: no copy-link button`);
    for (const s of c.sources || []) {
      if (s.motion != null) {
        assert.ok(out.includes(`motions.html?id=${s.motion}`), `${c.id}: vote #${s.motion} not linked`);
      } else if (s.meeting != null) {
        assert.ok(out.includes(`meetings.html?id=${s.meeting}`), `${c.id}: meeting #${s.meeting} not linked`);
      }
    }
    // No un-substituted template expressions, which is what a renamed key looks
    // like in the browser.
    assert.ok(!out.includes("${"), `${c.id}: unrendered template literal`);
    assert.ok(!out.includes("undefined"), `${c.id}: rendered the string "undefined"`);
  }
});

test("a quotation is escaped, not injected", () => {
  const hostile = {
    id: "x",
    claim: "<script>alert(1)</script>",
    verdict: "accurate",
    ruling: "r".repeat(30),
    checked: "2026-01-01",
    sources: [{ speaker: "<img onerror=x>", date: "2026-01-01", quote: "\"&\"" }],
  };
  const out = page.card(hostile, { accurate: { label: "Accurate" } }, 1);
  assert.ok(!out.includes("<script>"), "claim text is not escaped");
  assert.ok(!out.includes("<img onerror"), "speaker is not escaped");
});

test("the whole page renders, and publishes one ClaimReview per claim", () => {
  const root = { innerHTML: "", addEventListener() {} };
  page.render(data, root);
  const out = root.innerHTML;
  for (const c of data.claims) assert.ok(out.includes(`id="${c.id}"`), `${c.id} missing from the page`);
  assert.ok(out.includes(page.__esc(data.title)), "the page lost its title");
  for (const r of data.how.rules) assert.ok(out.includes(page.__esc(r)), "a standing rule went missing");

  const reviews = page.__schemas.filter((s) => s["@type"] === "ClaimReview");
  assert.equal(reviews.length, data.claims.length, "one ClaimReview per claim");
  for (const r of reviews) {
    assert.match(r.url, /^https:\/\/civicrollcall\.com\/claims\.html#/);
    assert.ok(r.reviewRating.ratingValue >= 1 && r.reviewRating.ratingValue <= 5);
    assert.ok(r.claimReviewed, "a ClaimReview with no claim");
  }
});

test("the docket lists every claim, verdict first", () => {
  const root = { innerHTML: "", addEventListener() {} };
  page.render(data, root);
  const docket = root.innerHTML.match(/<nav class="cl-docket"[\s\S]*?<\/nav>/);
  assert.ok(docket, "the page has no docket");
  const hrefs = [...docket[0].matchAll(/href="#([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(hrefs, data.claims.map((c) => c.id),
    "the docket and the cards disagree about what is on the page");
});

test("the filter bar offers every topic, with its count", () => {
  const cats = Object.fromEntries(data.categories.map((c) => [c.key, c]));
  const out = page.filters(data.claims, cats);
  for (const cat of data.categories) {
    const n = data.claims.filter((c) => (c.topics || []).includes(cat.key)).length;
    assert.ok(out.includes(`data-topic="${cat.key}"`), `${cat.key} is missing from the filter bar`);
    const button = out.slice(out.indexOf(`data-topic="${cat.key}"`));
    assert.match(button.slice(0, 220), new RegExp(`<span class="cl-filter-n">${n}</span>`),
      `${cat.key}: the button's count disagrees with the claims`);
  }
  // "Everything" is the way back, and it starts pressed.
  assert.match(out, /data-topic=""\s+aria-pressed="true"/, "no unpressed-by-default reset button");
});

test("every card and every docket row carries what the filter reads", () => {
  const root = { innerHTML: "", addEventListener() {} };
  page.render(data, root);
  const out = root.innerHTML;
  for (const c of data.claims) {
    // Hidden-not-re-rendered filtering means the topics have to be on the
    // element itself; a card the filter can't read stays on screen under every
    // topic, which is worse than being missing.
    assert.ok(out.includes(`data-topics="${c.topics.join(" ")}"`),
      `${c.id}: nothing on the page carries its topics`);
    for (const t of c.topics) {
      assert.ok(out.includes(`data-topic="${t}"`), `${c.id}: topic "${t}" is not clickable anywhere`);
    }
  }
  // The card rail and the docket row both have to be hideable, so each claim's
  // topics appear twice: once on the <article>, once on the <li>.
  const cards = [...out.matchAll(/<article class="cl-card[\s\S]*?data-topics="/g)].length;
  const rows = [...out.matchAll(/<li data-topics="/g)].length;
  assert.equal(cards, data.claims.length, "a card lost its topics");
  assert.equal(rows, data.claims.length, "a docket row lost its topics");
});

test("the verdict colours are the ones that clear 4.5:1", () => {
  // Sage measures 4.41 on white and rust 3.79 on the dark card — both below AA
  // for text this size. The page defines deepened variants for exactly that
  // reason, and a revert to the shared tokens would silently drop the verdict —
  // the most load-bearing label here — under the contrast floor.
  const css = readFileSync(join(DOCS, "site.css"), "utf8");
  const block = css.slice(css.indexOf("/* =============================================================================\n   CLAIMS"));
  assert.match(block, /--cl-sage:/, "the claims page no longer defines its own sage");
  assert.match(block, /--cl-rust:/, "the claims page no longer defines its own rust");
  assert.match(block, /\.cl-verdict\.is-bad\s*\{\s*color: var\(--cl-rust\)/,
    "the 'bad' verdict is back on the low-contrast shared token");
  assert.match(block, /\.cl-verdict\.is-cool\s*\{\s*color: var\(--cl-sage\)/,
    "the 'cool' verdict is back on the low-contrast shared token");
});

/* ---------------------------------------------------------------------------
   The pipeline behind the page
   --------------------------------------------------------------------------- */

test("the intake queue is valid, and its published rows exist", () => {
  const inbox = join(ROOT, "data", "claims", "inbox.jsonl");
  if (!existsSync(inbox)) return;
  const ids = new Set(data.claims.map((c) => c.id));
  const lines = readFileSync(inbox, "utf8").split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#"));
  for (const line of lines) {
    const row = JSON.parse(line);
    assert.ok(row.claim, "an intake row with no claim");
    if (row.published_as) {
      assert.ok(ids.has(row.published_as),
        `inbox row published_as "${row.published_as}", which is not in data.claims.json`);
    }
  }
});

test("the corpus builder feeds the agent every claim", () => {
  // The widget answering "is it true that…" out of the raw transcript when the
  // page already carries a checked answer is the failure this guards.
  const src = readFileSync(join(ROOT, "ingest", "build_agent_corpus.py"), "utf8");
  assert.match(src, /def _claim_docs\(\)/, "no _claim_docs() in the corpus builder");
  assert.match(src, /docs \+= _claim_docs\(\)/, "_claim_docs() is defined but never called");

  const corpus = join(DOCS, "agent-corpus.json");
  if (!existsSync(corpus)) return;
  const chunks = JSON.parse(readFileSync(corpus, "utf8")).docs.filter((d) => d.kind === "claim");
  assert.equal(chunks.length, data.claims.length,
    "agent-corpus.json is stale — re-run: python -m ingest.build_agent_corpus");
  for (const c of chunks) {
    assert.match(c.url, /^claims\.html#/, "a claim chunk that doesn't deep-link to its card");
  }
});
