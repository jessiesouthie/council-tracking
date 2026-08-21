// Council Tracking service worker.
// - Pages: network-first, cached copy kept as the offline fallback.
// - Shell assets and data: precache + stale-while-revalidate.
// - Pyodide CDN: stale-while-revalidate into a separate, long-lived bucket.
//
// The shell used to be cache-first, which meant a cached site.css/site.js/*.html was
// served forever and only ever replaced when SHELL_CACHE was renamed by hand. That
// bump got missed (b60b08e and aa7b701 both shipped CSS with no bump), so returning
// visitors kept being served old styling — the page would visibly snap back to the
// previous design on refresh. Stale-while-revalidate removed that footgun: the cached
// copy still answers instantly, but every load also refetches in the background, so a
// change lands on the next visit whether or not anyone remembers to touch this file.
//
// "On the next visit" is the catch, and it's why pages are no longer part of that
// deal. Applied to HTML, stale-while-revalidate means a visitor is served the
// previous deploy every single time and has to load twice to see the current one —
// the "it hasn't updated, reload again" complaint. Worse, the background refetch
// was a plain fetch(), which still reads the browser's HTTP cache: GitHub Pages
// sends max-age=600 on everything, so for ten minutes after a deploy the refetch
// re-stored the same stale bytes and the two caches multiplied into several
// reloads. Navigations now go to the network first and every refetch revalidates.
//
// SHELL_CACHE is therefore no longer load-bearing for correctness — it's just the
// precache namespace. Bumping it is now only a way to force a one-time flush, which
// is exactly what the bump below does for everyone still pinned to a stale v46.

const SHELL_CACHE = "council-shell-v47";
const PYODIDE_CACHE = "council-pyodide-v1";
const DATA_CACHE = "council-data-v1";

// Static app-shell assets. data.json is handled separately (stale-while-revalidate)
// so the site picks up freshly-ingested data quickly without a SW bump.
//
// Every path here has to exist in the deployed docs/ before this file ships:
// install() uses cache.addAll(), which rejects the whole batch if a single
// request 404s, and a rejected install means no precache at all for anyone.
// So a new page and its line below belong in the same commit — adding the line
// first, or the page in a branch that hasn't merged, silently disables the
// service worker for every visitor rather than just missing one file.
const SHELL_ASSETS = [
  "./",
  "index.html",
  "meetings.html",
  "motions.html",
  "members.html",
  "member.html",
  "finances.html",
  "tax.html",
  "projections.html",
  "budget.html",
  "staffing.html",
  "claims.html",
  "definitions.html",
  "about.html",
  "site.css",
  "fonts.css",
  "site.js",
  "projections.js",
  "agent.js",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  const keep = new Set([SHELL_CACHE, PYODIDE_CACHE, DATA_CACHE]);
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => !keep.has(n)).map((n) => caches.delete(n)));
      await self.clients.claim();
    })()
  );
});

function isPyodideAsset(url) {
  return url.hostname === "cdn.jsdelivr.net" && url.pathname.startsWith("/pyodide/");
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  if (isPyodideAsset(url)) {
    event.respondWith(staleWhileRevalidate(req, PYODIDE_CACHE));
    return;
  }

  if (url.origin === self.location.origin) {
    // Per-body datasets (data.json, data.<body>.json) and the bodies index
    // update each ingest run — prefer fresh, fall back to cached. The
    // transcripts manifest and the summary/transcript files it points to also
    // change when meetings are published or re-summarized, so treat the whole
    // transcripts/ tree the same way — otherwise cache-first pins a stale
    // manifest and newly published meetings never surface for returning users.
    if (
      /\/data(\.[\w-]+)?\.json$/.test(url.pathname) ||
      /\/bodies\.json$/.test(url.pathname) ||
      /\/transcripts\//.test(url.pathname)
    ) {
      event.respondWith(staleWhileRevalidate(req, DATA_CACHE));
      return;
    }
    // Navigations go to the network first. Stale-while-revalidate is wrong for
    // the page itself: it paints the cached HTML and only *then* fetches, so a
    // visitor always sees the previous deploy and has to load twice to see the
    // current one. That's the "it didn't update, reload again" report. The
    // cached copy is still kept as the offline fallback below.
    if (req.mode === "navigate") {
      event.respondWith(networkFirst(req, SHELL_CACHE));
      return;
    }
    // Everything else in the shell (css/js/icons) is fingerprinted by the ?v=
    // token in the markup, so serving it from cache can't go stale on its own.
    event.respondWith(staleWhileRevalidate(req, SHELL_CACHE));
    return;
  }

  // Other cross-origin: just go to network.
});

// Network-first with a cached fallback, for navigations. Requests are rebuilt
// from the URL rather than passed through: a navigate-mode Request can't be
// handed to fetch() with an init object, which is how the no-cache override is
// applied.
async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req.url, { cache: "no-cache", credentials: "same-origin" });
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch {
    // Offline: the cached page, or the precached root, so navigation still lands.
    const cached = (await cache.match(req)) || (await cache.match("./"));
    return cached || Response.error();
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  // cache: "no-cache" forces a conditional request. Without it this "revalidate"
  // is a lie: a plain fetch() inside a worker still reads the HTTP cache, and
  // GitHub Pages sends max-age=600 on everything, so for ten minutes after a
  // deploy the background refetch re-stored the exact stale bytes it already
  // had. That's what turned "one load behind" into "reload several times".
  // The server answers 304 when nothing changed, so this costs a round trip,
  // not a re-download — data.json is 2.6MB and must not be refetched blindly.
  const network = fetch(req, { cache: "no-cache" })
    .then((res) => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    })
    .catch(() => null);

  if (cached) return cached;

  const res = await network;
  if (res) return res;

  // Offline, and nothing cached for this exact URL. Fall back to the precached shell
  // root so a navigation still lands on something instead of erroring. (Only ever
  // true for navigations — data and Pyodide requests aren't navigate-mode.)
  if (req.mode === "navigate") {
    const fallback = await cache.match("./");
    if (fallback) return fallback;
  }
  return Response.error();
}
