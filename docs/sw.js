// Council Tracking service worker.
// - App shell: precache + stale-while-revalidate.
// - Pyodide CDN: stale-while-revalidate into a separate, long-lived bucket.
//
// The shell used to be cache-first, which meant a cached site.css/site.js/*.html was
// served forever and only ever replaced when SHELL_CACHE was renamed by hand. That
// bump got missed (b60b08e and aa7b701 both shipped CSS with no bump), so returning
// visitors kept being served old styling — the page would visibly snap back to the
// previous design on refresh. Stale-while-revalidate removes the footgun: the cached
// copy still answers instantly, but every load also refetches in the background, so a
// change lands on the next visit whether or not anyone remembers to touch this file.
//
// SHELL_CACHE is therefore no longer load-bearing for correctness — it's just the
// precache namespace. Bumping it is now only a way to force a one-time flush, which
// is exactly what the bump below does for everyone still pinned to a stale v32.

const SHELL_CACHE = "council-shell-v45";
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
    // The shell (html/css/js/icons) gets the same treatment as the data: serve the
    // cached copy for an instant paint, but always refetch so the next load is current.
    event.respondWith(staleWhileRevalidate(req, SHELL_CACHE));
    return;
  }

  // Other cross-origin: just go to network.
});

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const network = fetch(req)
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
