// Box Inventory service worker: makes the app installable, receives photos shared from other
// apps, and keeps the app's own page and icons for a fast start. Inventory data and photos are
// never cached here: they always come from the server, behind sign-in.
const SHELL = "box-shell-v3";
const SHARED = "box-shared";
// Everything the app needs to start without a connection (the offline copy itself lives in IndexedDB).
const SHELL_FILES = ["/", "/app/offline.js", "/vendor/qrcode.js", "/manifest.webmanifest", "/favicon.ico",
  "/icons/favicon-32.png", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("box-shell-") && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  // Photos shared from the gallery or camera: keep them until the page picks a box for them.
  if (url.pathname === "/share" && e.request.method === "POST") {
    e.respondWith((async () => {
      const files = (await e.request.formData()).getAll("photos").filter((f) => f && f.size);
      const cache = await caches.open(SHARED);
      for (const k of await cache.keys()) await cache.delete(k);
      await Promise.all(files.map((f, i) => cache.put(`/shared/${i}`, new Response(f, { headers: { "Content-Type": f.type || "image/jpeg" } }))));
      return Response.redirect(`/?shared=${files.length}`, 303);
    })());
    return;
  }
  if (e.request.method !== "GET") return;
  // The app page: always try the network first (newest version), fall back to the saved copy offline.
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) caches.open(SHELL).then((c) => c.put("/", r.clone()));
      return r;
    }).catch(() => caches.match("/")));
    return;
  }
  if (SHELL_FILES.includes(url.pathname)) e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
