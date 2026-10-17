// Buybutler service worker: keeps the app itself on the phone, so it opens
// in a shop without reception. The data stays with the page: the API is
// never cached here; the page keeps its own copy of the rated products.
"use strict";

const VERSION = "__VERSION__";
const SHELL = `buybutler-shell-${VERSION}`;
const PICTURES = "buybutler-pictures";
const MAX_PICTURES = 300;
const FILES = __FILES__;

self.addEventListener("install", (event) => {
  // cache: "reload" skips the browser's HTTP cache, so a new version never
  // picks up a file of the old one.
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(FILES.map((f) => new Request(f, { cache: "reload" })))));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("buybutler-shell-") && key !== SHELL) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

// A new version waits until the user chooses to reload (the page asks).
self.addEventListener("message", (event) => {
  if (event.data === "activate") self.skipWaiting();
});

async function fromShell(path, request) {
  const cache = await caches.open(SHELL);
  return (await cache.match(path)) || fetch(request);
}

// Product pictures and rating photos never change under their name; they
// are kept as they are viewed, the oldest dropped beyond MAX_PICTURES.
async function picture(request) {
  const cache = await caches.open(PICTURES);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) {
    await cache.put(request, response.clone());
    const keys = await cache.keys();
    for (const old of keys.slice(0, Math.max(0, keys.length - MAX_PICTURES))) await cache.delete(old);
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate" && url.pathname === "/") {
    event.respondWith(fromShell("/", request));
  } else if (FILES.includes(url.pathname)) {
    event.respondWith(fromShell(url.pathname, request));
  } else if (url.pathname.startsWith("/images/")) {
    event.respondWith(picture(request));
  }
  // The API and everything else go to the network as usual.
});
