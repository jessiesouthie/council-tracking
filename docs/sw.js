// Council Tracking service worker.
// - App shell: precache + cache-first (versioned).
// - Pyodide CDN: stale-while-revalidate into a separate, long-lived bucket.

const SHELL_CACHE = "council-shell-v16";
const PYODIDE_CACHE = "council-pyodide-v1";
const DATA_CACHE = "council-data-v1";

// Static app-shell assets. data.json is handled separately (stale-while-revalidate)
// so the site picks up freshly-ingested data quickly without a SW bump.
const SHELL_ASSETS = [
  "./",
  "index.html",
  "meetings.html",
  "motions.html",
  "members.html",
  "member.html",
  "site.css",
  "site.js",
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
    // update each ingest run — prefer fresh, fall back to cached.
    if (/\/data(\.[\w-]+)?\.json$/.test(url.pathname) || /\/bodies\.json$/.test(url.pathname)) {
      event.respondWith(staleWhileRevalidate(req, DATA_CACHE));
      return;
    }
    event.respondWith(cacheFirst(req, SHELL_CACHE));
    return;
  }

  // Other cross-origin: just go to network.
});

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req, { ignoreSearch: false });
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    // If we have an entry for the navigation root, serve it as fallback.
    if (req.mode === "navigate") {
      const fallback = await cache.match("./");
      if (fallback) return fallback;
    }
    throw err;
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const network = fetch(req)
    .then((res) => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    })
    .catch(() => null);
  return cached || (await network) || Response.error();
}
