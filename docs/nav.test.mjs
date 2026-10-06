/* =============================================================================
   Tests that the site tells one story about what is on it — node --test docs/nav.test.mjs

   Before ingest/nav.py there were four copies of the destination list, and no
   two agreed, because each was hand-edited at a different time for a different
   reason. ingest/chrome_v2.py now draws the header, tab bar and footer from one
   definition, and ingest/build_nav.py splices them into every page. This is
   what notices if someone edits a copy by hand and the splicer hasn't been
   re-run.

   What is covered:
     · every page's header and tab bar match the canonical list
     · the mobile bar stays inside the five items a bottom bar can hold
     · every nav href points at a file that exists
     · every listed page is in the sitemap and precached by the service worker
     · the accessibility contract each page owes: one skip link, one <main id="main">,
       and at most one aria-current per document
   ============================================================================= */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const DOCS = fileURLToPath(new URL("./", import.meta.url));
const ROOT = path.resolve(DOCS, "..");
const read = (p) => readFileSync(path.join(ROOT, p), "utf8");

const ALL_PAGES = readdirSync(DOCS).filter((f) => f.endsWith(".html"));
// Every page carries its chrome between two wrappers that ingest/build_nav.py
// fills from ingest/chrome_v2.py.
const isV2 = (page) => /<div class="v2-chrome" data-chrome="top">/.test(readFileSync(path.join(DOCS, page), "utf8"));
const V2_PAGES = ALL_PAGES.filter(isV2);
const PAGES = ALL_PAGES.filter((p) => !isV2(p));

// site.css is gone (2026-10-02), so a page on the old chrome would render
// unstyled. Every top-level page must carry the redesign's chrome.
test("every page is on the redesign", () => {
  assert.deepEqual(PAGES, [], "pages still on the old topbar chrome");
});

/* ---------------------------------------------------------------------------
   The canonical list, parsed out of ingest/nav.py rather than restated here.
   A test that keeps its own copy of the thing under test is a fifth copy, and
   the fifth copy is exactly the problem this file exists to prevent.
   --------------------------------------------------------------------------- */
function canonical() {
  const src = read("ingest/nav.py");
  const block = src.slice(src.indexOf("NAV: tuple[Item, ...] = ("), src.indexOf("\n)\n", src.indexOf("NAV: tuple[Item, ...] = (")));
  const items = [];
  for (const m of block.matchAll(/Item\(([\s\S]*?)\n    \)/g)) {
    const body = m[1];
    const field = (name) => {
      const hit = body.match(new RegExp(`${name}="([^"]*)"`));
      return hit ? hit[1] : null;
    };
    const href = field("href");
    const children = [...body.matchAll(/Child\("([^"]+)",\s*"([^"]+)"\)/g)]
      .map((c) => ({ label: c[1], href: c[2] }));
    const extra = [...body.matchAll(/alias=\(([^)]*)\)/g)]
      .flatMap((a) => [...a[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]));
    // Item.aliases: children first, then the extras, minus the item's own href.
    const alias = [...new Set([...children.map((c) => c.href), ...extra])]
      .filter((h) => h !== href);
    items.push({
      label: field("label"),
      tabLabel: field("short") || field("label"),
      href,
      children,
      alias,
      mobile: !/mobile=False/.test(body),
      bodyScoped: /body_scoped=True/.test(body),
    });
  }
  return items;
}

const NAV = canonical();
const TABBAR = NAV.filter((i) => i.mobile);

test("the canonical list parsed, and is not empty", () => {
  assert.ok(NAV.length >= 5, `parsed only ${NAV.length} items from ingest/nav.py`);
  assert.deepEqual(
    NAV.map((i) => i.label),
    ["Home", "Meetings & Votes", "Members", "Fact Checks", "Taxes & Budget", "Topics", "About"]
  );
});

/* ---------------------------------------------------------------------------
   The four copies
   --------------------------------------------------------------------------- */

const asLinks = (s) =>
  [...s.matchAll(/<a\s([^>]*)>([^<]*)<\/a>/g)].map((m) => ({
    attrs: m[1],
    label: m[2],
    href: (m[1].match(/href="([^"]*)"/) || [])[1],
    nav: (m[1].match(/data-nav="([^"]*)"/) || [])[1],
  }));

test("the meeting-page generator emits the same nav", () => {
  const src = read("ingest/build_meeting_pages.py");
  // It takes the redesign's chrome from chrome_v2 rather than restating the
  // list, which is the whole point — assert it still does, and hasn't been
  // forked back into a literal that can drift.
  assert.match(src, /from \. import chrome_v2 as chrome/);
  assert.match(src, /chrome\.top\("meetings\.html"\)/);
  assert.match(src, /chrome\.bottom\("meetings\.html"\)/);
  assert.doesNotMatch(src, /<a href="\/tax\.html"/,
    "build_meeting_pages.py has a hand-written nav link again");
  const v = (f) => read(f).match(/^CSS_VERSION = "([0-9a-z]+)"/m)?.[1];
  assert.equal(v("ingest/build_meeting_pages.py"), v("ingest/build_claim_pages.py"),
    "meeting pages and claim pages link different v2.css versions");
});

test("site.js no longer builds navigation", () => {
  // The tab bar, the menus and the "you are here" marks are static markup from
  // chrome_v2.py now. A second, script-built copy is how the four lists drifted.
  const src = read("docs/site.js");
  assert.doesNotMatch(src, /const TABBAR|mountTabbar|highlightActiveNav/,
    "site.js is building the tab bar or the highlight again");
  assert.doesNotMatch(src, /topbar|nav-menu|nav-group/, "site.js still targets the old header");
});

test("404.html offers the same destinations", () => {
  const html = read("docs/404.html");
  const block = html.match(/<ul id="fallback">([\s\S]*?)<\/ul>/);
  assert.ok(block, "404.html has no #fallback list");
  const hrefs = [...block[1].matchAll(/href="([^"]+)"/g)].map((m) => m[1]);
  // Home is not offered — the brand already goes there.
  assert.deepEqual(hrefs, NAV.filter((i) => i.href !== "index.html").map((i) => `/${i.href}`));
});

/* ---------------------------------------------------------------------------
   Everything the list points at has to exist, and be registered
   --------------------------------------------------------------------------- */

test("every nav destination is a real page", () => {
  for (const item of NAV) {
    assert.ok(existsSync(path.join(DOCS, item.href)), `${item.href} does not exist`);
    for (const alias of item.alias) {
      assert.ok(existsSync(path.join(DOCS, alias)), `${item.href} aliases missing ${alias}`);
    }
  }
});

test("every section page is in the sitemap", () => {
  const src = read("ingest/build_sitemap.py");
  const listed = new Set([...src.matchAll(/"path": "([^"]*)"/g)].map((m) => m[1] || "index.html"));
  for (const item of NAV) {
    assert.ok(listed.has(item.href), `${item.href} is not in build_sitemap.py PAGES`);
    for (const alias of item.alias) {
      if (alias === "member.html") continue; // expanded per member from the dataset
      assert.ok(listed.has(alias), `${alias} is not in build_sitemap.py PAGES`);
    }
  }
});

test("every section page is precached by the service worker", () => {
  const src = read("docs/sw.js");
  const block = src.match(/const SHELL_ASSETS = \[([\s\S]*?)\];/);
  assert.ok(block, "sw.js has no SHELL_ASSETS");
  const listed = new Set([...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]));

  // cache.addAll() rejects the whole batch on a single 404, which disables the
  // service worker for every visitor rather than missing one file. So the list
  // must name real files, and must name every page the nav can reach.
  for (const asset of listed) {
    if (asset === "./") continue;
    assert.ok(existsSync(path.join(DOCS, asset)), `sw.js precaches missing ${asset}`);
  }
  for (const item of NAV) {
    assert.ok(listed.has(item.href), `${item.href} is not in sw.js SHELL_ASSETS`);
  }
});

/* ---------------------------------------------------------------------------
   Section sub-navs
   --------------------------------------------------------------------------- */

test("each section page carries its section's sub-nav", () => {
  // Driven from the same children as the menus. A sub-nav that gains a page the
  // menu doesn't have — or loses one it does — is the drift this file exists to
  // catch, and it was two hand-written copies before.
  for (const item of NAV.filter((i) => i.children.length)) {
    for (const child of item.children) {
      const html = read(`docs/${child.href}`);
      const block = html.match(
        new RegExp(`<nav class="(?:v2-)?subnav" aria-label="${item.label} section">([\\s\\S]*?)</nav>`)
      );
      assert.ok(block, `${child.href} has no "${item.label} section" sub-nav`);

      const links = [...block[1].matchAll(/<a href="([^"]+)"([^>]*)>([^<]*)</g)];
      assert.deepEqual(links.map((l) => l[1]), item.children.map((c) => c.href),
        `${child.href}: sub-nav destinations`);
      assert.deepEqual(links.map((l) => l[3]), item.children.map((c) => c.label),
        `${child.href}: sub-nav labels`);

      // Exactly one item marks itself current, and it is this page.
      const current = links.filter((l) => /aria-current/.test(l[2]));
      assert.equal(current.length, 1, `${child.href}: ${current.length} aria-current in the sub-nav`);
      assert.equal(current[0][1], child.href, `${child.href}: sub-nav marks the wrong page current`);
    }
  }
});

/* ---------------------------------------------------------------------------
   Redesigned pages (docs/v2.css, ingest/chrome_v2.py)
   --------------------------------------------------------------------------- */

for (const page of V2_PAGES) {
  test(`${page} carries the canonical nav in the redesign's header`, () => {
    const html = read(`docs/${page}`);
    const head = html.match(/<nav class="v2-nav" aria-label="Main">([\s\S]*?)<\/nav>/);
    assert.ok(head, `${page} has no v2 header nav: run python -m ingest.build_nav`);
    // The section links only; the dropdown's own links are class="v2-nav-sub".
    const links = asLinks(head[1]).filter((l) => !/v2-nav-sub/.test(l.attrs));
    assert.deepEqual(links.map((l) => l.label.replace(/&amp;/g, "&")), NAV.map((i) => i.label));
    assert.deepEqual(links.map((l) => l.href), NAV.map((i) => `/${i.href}`));
    assert.ok(links.filter((l) => /aria-current/.test(l.attrs)).length <= 1,
      `${page}: more than one aria-current in the header`);
    // A council-only section is marked so site.js can drop it for another body.
    for (const [i, l] of links.entries()) {
      assert.equal(/data-nav-body="city-council"/.test(l.attrs), NAV[i].bodyScoped,
        `${page}: ${l.label} body scoping`);
    }
    // Each section with pages inside it opens a menu of exactly those pages.
    const menus = [...head[1].matchAll(/<ul class="v2-nav-menu" aria-label="([^"]*)">([\s\S]*?)<\/ul>/g)];
    const withKids = NAV.filter((i) => i.children.length);
    assert.deepEqual(menus.map((m) => m[1].replace(/&amp;/g, "&")), withKids.map((i) => i.label),
      `${page}: header dropdowns`);
    for (const [i, m] of menus.entries()) {
      const subs = asLinks(m[2]);
      assert.deepEqual(subs.map((l) => l.href), withKids[i].children.map((c) => `/${c.href}`),
        `${page}: ${withKids[i].label} dropdown destinations`);
      assert.ok(subs.every((l) => !/aria-current/.test(l.attrs)), `${page}: aria-current in a dropdown`);
    }
  });

  test(`${page} carries the redesign's tab bar and strip`, () => {
    const html = read(`docs/${page}`);
    const bar = html.match(/<nav class="v2-tabbar"[^>]*>([\s\S]*?)<\/nav>/);
    assert.ok(bar, `${page} has no v2 tab bar`);
    const labels = [...bar[1].matchAll(/<span>([^<]*)<\/span><\/a>/g)].map((m) => m[1]);
    assert.deepEqual(labels, TABBAR.map((i) => i.tabLabel));
    // Five tabs share a 390px phone, about 78px each. Past a dozen characters
    // a label wraps or clips; that is what Item.short is for. Past five tabs
    // the bar stops being readable.
    assert.ok(labels.length <= 5, `${page}: tab bar holds ${labels.length} items`);
    for (const l of labels) {
      assert.ok(l.length <= 12, `tab label "${l}" is too long for the bar; give the item a short=`);
    }
    assert.match(html, /class="v2-strip"/, `${page}: no "not run by the city" strip`);
    assert.match(html, /<a class="v2-skip" href="#main">/, `${page}: no skip link`);
    assert.match(html, /<main id="main"/, `${page}: no <main id="main">`);
    assert.doesNotMatch(html, /href="site\.css/, `${page}: links site.css as well as v2.css`);
  });
}

// One v2.css, one cache-busting tag. A page left on an older ?v= can be served
// the browser's cached copy of that older stylesheet, which lacks the rules the
// page was moved onto v2 with — so every page, and the claim pages the
// generator writes, must carry the same tag.
test("every redesigned page links the same v2.css version", () => {
  const tagOf = (html) => [...html.matchAll(/v2\.css\?v=([0-9a-z]+)/g)].map((m) => m[1]);
  const claimDir = path.join(DOCS, "claims");
  const files = [
    ...V2_PAGES.map((p) => [p, read(`docs/${p}`)]),
    ...readdirSync(claimDir).filter((f) => f.endsWith(".html")).map((f) => [`claims/${f}`, readFileSync(path.join(claimDir, f), "utf8")]),
  ];
  const generator = read("ingest/build_claim_pages.py").match(/^CSS_VERSION = "([0-9a-z]+)"/m)?.[1];
  assert.ok(generator, "build_claim_pages.CSS_VERSION not found");
  const stale = files.flatMap(([f, html]) => tagOf(html).filter((t) => t !== generator).map((t) => `${f} (${t})`));
  assert.deepEqual(stale, [], `pages not on v2.css?v=${generator}`);
});
