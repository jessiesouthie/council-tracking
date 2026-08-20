/* =============================================================================
   Tests that the site tells one story about what is on it — node --test docs/nav.test.mjs

   Before ingest/nav.py there were four copies of the destination list: the
   <nav> block pasted into twelve pages, the NAV constant in
   build_meeting_pages.py, the TABBAR array in site.js, and 404.html's "where to
   go instead". No two agreed, because each was hand-edited at a different time
   for a different reason. ingest/build_nav.py now writes all four from one
   definition — and this is what notices if someone edits a copy by hand and the
   splicer hasn't been re-run.

   What is covered:
     · every generated block matches the canonical list, spliced or not
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

const PAGES = readdirSync(DOCS).filter((f) => f.endsWith(".html"));

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
    ["Home", "Meetings", "Votes", "Members", "Finances", "About"]
  );
});

/* ---------------------------------------------------------------------------
   The four copies
   --------------------------------------------------------------------------- */

/* The raw contents of one <nav class="nav"> block. */
function navBlock(html) {
  const m = html.match(/<nav class="nav" aria-label="Primary">([\s\S]*?)\n\s*<\/nav>/);
  return m ? m[1] : null;
}

const asLinks = (s) =>
  [...s.matchAll(/<a\s([^>]*)>([^<]*)<\/a>/g)].map((m) => ({
    attrs: m[1],
    label: m[2],
    href: (m[1].match(/href="([^"]*)"/) || [])[1],
    nav: (m[1].match(/data-nav="([^"]*)"/) || [])[1],
  }));

/* The top-level row: the bar's own items, with each section menu's contents
   taken out first so a menu link is never mistaken for a destination on the
   bar. That distinction is the whole point of the structure. */
function navLinks(html) {
  const block = navBlock(html);
  if (block === null) return null;
  return asLinks(block.replace(/<div class="nav-menu"[\s\S]*?<\/div>/g, ""));
}

/* The menus, keyed by the section they hang under. */
function navMenus(html) {
  const block = navBlock(html) || "";
  const out = {};
  for (const g of block.matchAll(
    /<div class="nav-group"([^>]*)>([\s\S]*?)<div class="nav-menu"([^>]*)>([\s\S]*?)<\/div>/g
  )) {
    const trigger = asLinks(g[2])[0];
    out[trigger.nav] = {
      groupAttrs: g[1],
      menuAttrs: g[3],
      links: asLinks(g[4]),
    };
  }
  return out;
}

for (const page of PAGES) {
  test(`${page} carries the canonical nav`, () => {
    const links = navLinks(read(`docs/${page}`));
    assert.ok(links, `${page} has no <nav class="nav"> block`);
    assert.deepEqual(links.map((l) => l.label), NAV.map((i) => i.label));
    assert.deepEqual(links.map((l) => l.nav), NAV.map((i) => i.href));

    // 404.html is served for whatever path was missed, so it — and only it —
    // uses root-absolute hrefs among the top-level pages.
    const wantRoot = page === "404.html";
    for (const l of links) {
      assert.equal(l.href.startsWith("/"), wantRoot,
        `${page}: ${l.label} href "${l.href}" should ${wantRoot ? "" : "not "}be root-absolute`);
    }
  });

  test(`${page} keeps its accessibility contract`, () => {
    const html = read(`docs/${page}`);
    assert.match(html, /<a href="#main" class="skip-link">/, `${page}: no skip link`);
    assert.match(html, /<main id="main"/, `${page}: no <main id="main"> for it to reach`);

    // Two "you are here" markers in one document is a lie to a screen reader.
    // The top-level item claims it; a sub-nav item claims it; never both, and
    // never twice at the same level.
    const inNav = (navLinks(html) || []).filter((l) => /aria-current/.test(l.attrs));
    assert.ok(inNav.length <= 1, `${page}: ${inNav.length} aria-current in the primary nav`);
  });
}

test("the meeting-page generator emits the same nav", () => {
  const src = read("ingest/build_meeting_pages.py");
  // It builds NAV by calling nav_links() rather than restating the list, which
  // is the whole point — assert it still does, and hasn't been forked back into
  // a literal that can drift.
  assert.match(src, /from \.nav import nav_links/);
  assert.match(src, /nav_links\(root=True, active="meetings\.html"\)/);
  assert.doesNotMatch(src, /<a href="\/tax\.html" data-nav=/,
    "build_meeting_pages.py has a hand-written nav link again");
});

test("the mobile tab bar matches, and fits", () => {
  const src = read("docs/site.js");
  const block = src.match(/const TABBAR = \[([\s\S]*?)\];/);
  assert.ok(block, "site.js has no TABBAR array");

  const rows = [...block[1].matchAll(/\{\s*href:\s*"([^"]+)",\s*label:\s*"([^"]+)"([^}]*)\}/g)]
    .map((m) => ({ href: m[1], label: m[2], bodyScoped: /body:/.test(m[3]) }));

  assert.deepEqual(rows.map((r) => r.label), TABBAR.map((i) => i.label));
  assert.deepEqual(rows.map((r) => r.bodyScoped), TABBAR.map((i) => i.bodyScoped));

  // A bottom bar past five items stops being readable, which is what the
  // retired "More" sheet was working around.
  assert.ok(rows.length <= 5, `tab bar holds ${rows.length} items; five is the maximum`);

  // The bar is injected into docs/meetings/*.html too, where a relative
  // "meetings.html" resolves to /meetings/meetings.html and 404s. That was live.
  for (const r of rows) {
    assert.ok(r.href.startsWith("/"), `tab bar href "${r.href}" must be root-absolute`);
  }

  assert.doesNotMatch(src, /TABBAR_MORE|tab-more/, "the More sheet is retired");
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
        new RegExp(`<nav class="subnav" aria-label="${item.label} section">([\\s\\S]*?)</nav>`)
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
   The hover/focus menus
   --------------------------------------------------------------------------- */

const WITH_MENUS = NAV.filter((i) => i.children.length);

test("the definition has menus to test", () => {
  assert.deepEqual(WITH_MENUS.map((i) => i.label), ["Finances", "About"]);
});

for (const page of PAGES) {
  test(`${page} carries the section menus`, () => {
    const html = read(`docs/${page}`);
    const menus = navMenus(html);
    const wantRoot = page === "404.html";

    assert.deepEqual(Object.keys(menus), WITH_MENUS.map((i) => i.href),
      `${page}: wrong set of section menus`);

    for (const item of WITH_MENUS) {
      const menu = menus[item.href];
      assert.deepEqual(menu.links.map((l) => l.label), item.children.map((c) => c.label),
        `${page}: ${item.label} menu labels`);
      assert.deepEqual(
        menu.links.map((l) => l.href),
        item.children.map((c) => (wantRoot ? `/${c.href}` : c.href)),
        `${page}: ${item.label} menu hrefs`);

      // The panel names itself for a screen reader, since it is a bare div.
      assert.match(menu.menuAttrs, new RegExp(`aria-label="${item.label} section"`),
        `${page}: ${item.label} menu has no accessible name`);

      // A body-scoped section marks the wrapper, not the trigger — otherwise
      // applyBodyNav() strips the link and leaves its menu behind.
      const scoped = /data-nav-body=/.test(menu.groupAttrs);
      assert.equal(scoped, item.bodyScoped,
        `${page}: ${item.label} body scoping is on the wrong element`);
    }
  });
}

test("the menu is real markup, not built at runtime", () => {
  // It has to be in the page for a crawler and for a reader with no JS, and
  // revealing it must need no script — that is what makes focus-within the
  // keyboard route rather than a keydown handler.
  // site.js may read the menus — it marks which row is the current page — but
  // it must not be the thing that puts them in the document.
  const src = read("docs/site.js");
  assert.doesNotMatch(src, /<a[^>]*>\$\{[^}]*\}<\/a>[\s\S]{0,80}nav-menu/,
    "site.js is building the section menus");
  assert.doesNotMatch(src, /class="nav-menu"/, "site.js is emitting menu markup");
  assert.doesNotMatch(src, /nav-group/, "site.js is emitting menu wrappers");

  const css = read("docs/site.css");
  assert.match(css, /\.nav-group:focus-within \.nav-menu/,
    "the menus don't open on keyboard focus");
  assert.match(css, /\.nav-group:hover \.nav-menu/, "the menus don't open on hover");
});

test(".nav is never a scroll container", () => {
  // The invariant behind the whole layout. overflow-x:auto computes overflow-y
  // to auto, not visible, so the moment .nav scrolls it clips its own menus off
  // at the height of the bar. The menus were switched off under 1000px for
  // exactly that reason once, which meant a browser window that wasn't
  // maximised silently lost them. The width is found by tightening the row
  // instead — see the 760–1199px band — and this is what stops the scroll from
  // creeping back in.
  // Comments stripped first — the rule below explains this very hazard in
  // prose, and matching the explanation instead of a declaration would fail on
  // its own documentation.
  const css = read("docs/site.css").replace(/\/\*[\s\S]*?\*\//g, "");
  const navRules = [...css.matchAll(/(^|\})\s*\.nav\s*\{([^}]*)\}/g)].map((m) => m[2]);
  assert.ok(navRules.length, "no .nav rule found");
  for (const rule of navRules) {
    assert.doesNotMatch(rule, /overflow[^:]*:\s*(auto|scroll|hidden)/,
      ".nav has been made a scroll container again; its section menus will be clipped");
  }

  // And nothing may hide the menus at a width — that was the old workaround.
  assert.doesNotMatch(css, /\.nav-menu[^{]*\{[^}]*display:\s*none/,
    "a breakpoint is hiding the section menus again");
});

test("the row fits without the Home pill under 1200px", () => {
  // How the width is found. Measured: brand + body switcher + six full-size
  // pills need about 500px of bar and there isn't that much until past 1200px
  // on a non-default body, whose name is appended to the brand line and is the
  // wider option in the switcher. The brand is a link to the same page, so the
  // pill is the redundant one.
  const css = read("docs/site.css");
  assert.match(css, /@media \(min-width: 760px\) and \(max-width: 1199px\)/,
    "the narrow-desktop band is gone");
  assert.match(css, /\.nav > a\[data-nav="index\.html"\] \{ display: none; \}/,
    "the Home pill no longer steps aside, so the row will overflow under 1200px");
});
