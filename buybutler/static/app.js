// Buybutler interface: a small hash router over the JSON API. Everything the
// user typed is inserted with textContent, never as HTML.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
};

const state = {
  user: null,
  suggestion: null, // Open Food Facts data for the barcode being added
  product: null, // the product shown or being rated
  facets: { stores: [], categories: [] },
  verdict: "buy",
  flash: null, // { text, error } shown once on the next view
  holdJob: null, // an outbox job the user is still adding to
  offline: false, // started without a connection: data from this phone only
  lists: null, // the user's lists with their role; null until loaded
  listId: null, // the list everything happens in (sent as a header)
  sentProducts: {}, // outbox job id -> product id, once its product was created
  shopScanning: false, // the shopping list's camera was on; it comes back after the product form
  shopAdd: null, // barcode of a new product that goes on the shopping list once saved
};

// ---- API ----------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// No answer from the server: the network dropped, timed out, or the reverse
// proxy couldn't reach Buybutler.
class NetworkError extends Error {
  constructor() {
    super("No connection to Buybutler. Check the network and try again.");
    this.offline = true;
  }
}

const TIMEOUT_MS = 20000;
const RETRY_DELAYS = [1000, 3000, 8000];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Sends a request with a time limit and repeats it after network errors and
// gateway errors. Every request is safe to repeat: new records carry a
// client_id, so the server never creates them twice.
async function send(method, path, payload, headers, retries) {
  for (let attempt = 0; ; attempt++) {
    if (navigator.onLine === false) throw new NetworkError();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    let res = null;
    try {
      res = await fetch(path, { method, headers, body: payload, credentials: "same-origin", signal: controller.signal });
    } catch {
      res = null;
    } finally {
      clearTimeout(timer);
    }
    if (res && ![502, 503, 504].includes(res.status)) return { res, repeated: attempt > 0 };
    if (attempt >= retries) throw new NetworkError();
    await sleep(RETRY_DELAYS[Math.min(attempt, RETRY_DELAYS.length - 1)]);
  }
}

async function api(method, path, body, { retries = RETRY_DELAYS.length, list } = {}) {
  const headers = { "X-Buybutler": "1" };
  const listId = list !== undefined ? list : state.listId;
  if (listId) headers["X-Buybutler-List"] = String(listId);
  let payload;
  if (body instanceof Blob) {
    payload = body;
    headers["Content-Type"] = body.type || "application/octet-stream";
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const { res, repeated } = await send(method, path, payload, headers, retries);
  // A repeated delete whose first copy got through finds nothing left.
  if (method === "DELETE" && repeated && res.status === 404) return null;
  // A wrong password or code during the login is no reason to go to the login.
  if (res.status === 401 && !path.startsWith("/api/session")) {
    state.user = null;
    forgetOffline();
    go("#/login");
    throw new ApiError(401, "Logged out");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let detail = data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ");
    throw new ApiError(res.status, detail || `Request failed (${res.status})`);
  }
  return data;
}

// ---- Offline copy ---------------------------------------------------------
//
// The rated products of the current list (verdict, stars, reason, prices)
// are kept on the phone and refreshed while online, so a scan shows the
// verdict at once, also without reception. Logging out clears the copy,
// the user kept for an offline start and the cached pictures.

const USER_KEY = "buybutler.user";
const LISTS_KEY = "buybutler.lists";

const Known = {
  copy: null, // { list_id, fetched, products } of the current list
  fetched: 0, // when the copy was last refreshed, in ms

  async load() {
    if (!state.listId) return null;
    if (this.copy && this.copy.list_id === state.listId) return this.copy;
    try {
      this.copy = (await idb("known", "readonly", (s) => s.get(state.listId))) || null;
    } catch {
      this.copy = null;
    }
    return this.copy;
  },

  // Refreshed at most once a minute unless forced (after a change).
  async refresh(force = false) {
    if (!state.user || !state.listId || state.offline) return;
    if (!force && Date.now() - this.fetched < 60000 && this.copy && this.copy.list_id === state.listId) return;
    this.fetched = Date.now();
    try {
      const data = await api("GET", "/api/offline", undefined, { retries: 0 });
      this.copy = { list_id: data.list_id, fetched: data.generated_at, products: data.products };
      await idb("known", "readwrite", (s) => s.put(this.copy));
    } catch {
      // Offline or no storage: the copy stays as it was.
    }
  },

  async byBarcode(barcode) {
    const copy = await this.load();
    return copy ? copy.products.find((p) => p.barcode === barcode) || null : null;
  },

  async byId(id) {
    const copy = await this.load();
    return copy ? copy.products.find((p) => p.id === id) || null : null;
  },
};

function remember(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private mode: no offline start, nothing else changes.
  }
}

function remembered(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}

function rememberUser(user) {
  remember(USER_KEY, { username: user.username, role: user.role, currency: user.currency });
}

async function forgetOffline() {
  Known.copy = null;
  try {
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(LISTS_KEY);
  } catch {
    // nothing stored
  }
  await idb("known", "readwrite", (s) => s.clear()).catch(() => {});
  await idb("shopping", "readwrite", (s) => s.clear()).catch(() => {});
  if (window.caches) await caches.delete("buybutler-pictures").catch(() => {});
}

function offlineNote() {
  const when = Known.copy ? ` from ${stamp(Known.copy.fetched)}` : "";
  return `Offline: showing the copy on this phone${when}.`;
}

// ---- Service worker and updates -------------------------------------------

let reloading = false;

function offerUpdate(worker) {
  const bar = $("#update-bar");
  bar.hidden = false;
  $("#update-reload").onclick = () => {
    reloading = true;
    worker.postMessage("activate");
  };
}

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloading) location.reload();
  });
  navigator.serviceWorker.register("/sw.js").then((reg) => {
    if (reg.waiting && navigator.serviceWorker.controller) offerUpdate(reg.waiting);
    reg.addEventListener("updatefound", () => {
      const worker = reg.installing;
      worker.addEventListener("statechange", () => {
        // With no controller yet, this is the first install, not an update.
        if (worker.state === "installed" && navigator.serviceWorker.controller) offerUpdate(worker);
      });
    });
    // An app on the home screen stays open for days; look for a new
    // version whenever it comes back to the front.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) reg.update().catch(() => {});
    });
  }).catch(() => {});
}

function uid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // Plain HTTP on the LAN has no randomUUID.
  const b = crypto.getRandomValues(new Uint8Array(16));
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// ---- Outbox -----------------------------------------------------------------
//
// New products, ratings and prices are written as a job of steps into
// IndexedDB first, then sent. When the network fails, the job stays on the
// phone and is sent later, in order. A step's path can name an earlier
// step ("/api/products/{product}/ratings"); its ID is filled in once known.
// A body field can name one the same way (`fill: { product_id: "product" }`).

// One database: "jobs" (the outbox), "known" (the offline copy of rated
// products) and "shopping" (the last shopping list seen), one record per list.
const idb = (() => {
  let opening = null;
  const open = () => {
    opening = opening || new Promise((resolve, reject) => {
      const req = indexedDB.open("buybutler", 3);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("jobs")) db.createObjectStore("jobs", { keyPath: "id" });
        if (!db.objectStoreNames.contains("known")) db.createObjectStore("known", { keyPath: "list_id" });
        if (!db.objectStoreNames.contains("shopping")) db.createObjectStore("shopping", { keyPath: "list_id" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return opening;
  };
  return async (store, mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const req = fn(tx.objectStore(store));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  };
})();

const Outbox = {
  all: () => idb("jobs", "readonly", (s) => s.getAll()),
  get: (id) => idb("jobs", "readonly", (s) => s.get(id)),
  put: (job) => idb("jobs", "readwrite", (s) => s.put(job)),
  remove: (id) => idb("jobs", "readwrite", (s) => s.delete(id)),
};

// Pictures are stored as bytes: older Safari versions can't keep a Blob in
// IndexedDB.
async function blobStep(ref, path, blob) {
  return { ref, method: "PUT", path, file: { type: blob.type || "image/jpeg", bytes: await blob.arrayBuffer() } };
}

function newJob(label, steps) {
  return { id: uid(), label, created: new Date().toISOString(), steps: steps.filter(Boolean), error: null };
}

function stepId(ref, job) {
  const step = job.steps.find((s) => s.ref === ref);
  if (!step || !step.done) throw new Error(`step ${ref} not sent yet`);
  return step.id;
}

function fillPath(path, job) {
  return path.replace(/\{(\w+)\}/g, (_, ref) => String(stepId(ref, job)));
}

async function runJob(job, retries) {
  for (const step of job.steps) {
    if (step.done) continue;
    const path = fillPath(step.path, job);
    let body = step.file ? new Blob([step.file.bytes], { type: step.file.type }) : step.body;
    if (step.fill) {
      body = { ...body };
      for (const [field, ref] of Object.entries(step.fill)) body[field] = stepId(ref, job);
    }
    let data;
    try {
      data = await api(step.method, path, body, { retries, list: step.list });
    } catch (err) {
      // Saved offline, but the barcode was added meanwhile (another phone,
      // or the lookup itself had failed): rate the existing product.
      if (err.status === 409 && step.ref === "product" && step.body.barcode) {
        const r = await api("GET", `/api/lookup?barcode=${encodeURIComponent(step.body.barcode)}`, undefined,
          { retries, list: step.list });
        if (!r.product) throw err;
        data = r.product;
      } else {
        throw err;
      }
    }
    step.done = true;
    step.id = data && data.id;
    step.file = null;
    if (step.ref === "product") state.sentProducts[job.id] = step.id;
    await Outbox.put(job).catch(() => {});
  }
  await Outbox.remove(job.id).catch(() => {});
  if (job.steps.some((step) => !step.path.startsWith("/api/shopping"))) Known.refresh(true);
}

// Sends a new job now. Returns "sent", or "queued" when the network failed
// and the job waits on the phone. Errors from the server (a bad value) are
// thrown; when nothing was created yet, the job is dropped so the form can
// be corrected.
async function submitJob(job) {
  let stored = true;
  try {
    await Outbox.put(job);
  } catch {
    stored = false; // private mode or no storage: send without a safety net
  }
  try {
    await runJob(job, 1);
    return "sent";
  } catch (err) {
    if (err.offline && stored) {
      updateOutboxBadge();
      return "queued";
    }
    if (err.status === 401 && stored) return "queued";
    if (stored) await Outbox.remove(job.id).catch(() => {});
    updateOutboxBadge();
    if (job.steps.some((s) => s.done)) err.partial = true;
    throw err;
  }
}

let syncing = false;

async function syncOutbox() {
  if (syncing || !state.user) return;
  syncing = true;
  try {
    const jobs = (await Outbox.all()).sort((a, b) => a.created.localeCompare(b.created));
    for (const job of jobs) {
      if (job.error || job.id === state.holdJob) continue;
      try {
        await runJob(job, 0);
      } catch (err) {
        if (err.offline || err.status === 401) break;
        job.error = err.message;
        await Outbox.put(job);
      }
    }
  } catch {
    // IndexedDB not available: nothing was ever queued.
  } finally {
    syncing = false;
    updateOutboxBadge();
  }
}

async function updateOutboxBadge() {
  let jobs = [];
  try {
    jobs = await Outbox.all();
  } catch {
    jobs = [];
  }
  const failed = jobs.filter((j) => j.error).length;
  const waiting = jobs.length - failed;
  const badge = $("#outbox-badge");
  badge.hidden = jobs.length === 0;
  badge.textContent = [waiting && `${waiting} waiting`, failed && `${failed} not sent`].filter(Boolean).join(" · ");
  badge.classList.toggle("is-failed", failed > 0);
  if (!$("#view-account").hidden) renderOutbox(jobs);
}

window.addEventListener("online", () => {
  // Started offline: check the session and load fresh data now.
  if (state.offline) {
    state.offline = false;
    state.user = null;
    state.lists = null;
    route();
  }
  syncOutbox();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) syncOutbox();
});
setInterval(syncOutbox, 30000);

function flash(text, error = false) {
  state.flash = { text, error };
}

function takeFlash(target) {
  if (!state.flash) return;
  if (state.flash.error) showError(target, new Error(state.flash.text));
  else showInfo(target, state.flash.text);
  state.flash = null;
}

const QUEUED = "No connection. Saved on this phone; it is sent when the connection is back.";

function showError(target, err) {
  target.textContent = err.message;
  target.classList.add("is-error");
}

function showInfo(target, text) {
  target.textContent = text;
  target.classList.remove("is-error");
}

// ---- Formatting -----------------------------------------------------------

function stamp(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function today() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function stars(count) {
  const box = el("span", { class: "stars", "aria-label": count ? `${count} of 5 stars` : "No stars" });
  for (let i = 1; i <= 5; i++) box.append(el("i", { class: count && i <= count ? "is-on" : "" }));
  box.append(el("b", { text: count ? `${count}/5` : "–" }));
  return box;
}

// ---- Prices --------------------------------------------------------------

function money(cents) {
  const currency = (state.user && state.user.currency) || "EUR";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(cents / 100);
  } catch {
    return `${(cents / 100).toFixed(2)} ${currency}`;
  }
}

// "2,49" or "2.49" -> 249; empty -> null.
function parseAmount(text) {
  const t = text.trim();
  if (!t) return null;
  if (!/^\d+([.,]\d{1,2})?$/.test(t)) throw new Error("Enter the price as a number, e.g. 2.49");
  const [whole, frac = ""] = t.replace(",", ".").split(".");
  return Number(whole) * 100 + Number(frac.padEnd(2, "0"));
}

function parseQuantity(text) {
  const t = text.trim();
  if (!t) return null;
  if (!/^\d+([.,]\d+)?$/.test(t) || Number(t.replace(",", ".")) <= 0) {
    throw new Error("Enter the quantity as a number, e.g. 500");
  }
  return Number(t.replace(",", "."));
}

// The price fields of a form as an API object; null when no price was entered.
function readPrice(form) {
  const amount = parseAmount(form.price.value);
  const quantity = parseQuantity(form.quantity.value);
  const unit = form.unit.value.trim();
  // The quantity is often prefilled; without a price nothing is stored.
  if (amount === null) return null;
  if (quantity !== null && !unit) throw new Error("Enter the unit for the quantity, e.g. g or l.");
  if (quantity === null && unit) throw new Error("Enter the quantity for the unit.");
  return { amount_cents: amount, quantity, unit, on_offer: form.on_offer.checked };
}

function fillPriceFields(form, pr) {
  form.price.value = pr ? (pr.amount_cents / 100).toFixed(2) : "";
  form.quantity.value = pr && pr.quantity ? String(pr.quantity) : "";
  form.unit.value = pr ? pr.unit : "";
  form.on_offer.checked = !!(pr && pr.on_offer);
}

// Quantity from the last price, or from the pack size ("400 g e") when it
// reads cleanly; the user can change it.
function suggestQuantity(form, product) {
  const last = product.price && product.price.last;
  if (last && last.quantity) {
    form.quantity.value = String(last.quantity);
    form.unit.value = last.unit;
    return;
  }
  const m = /^(\d+(?:[.,]\d+)?)\s*(g|kg|ml|cl|l)\b/i.exec(product.quantity || "");
  if (m) {
    form.quantity.value = m[1].replace(",", ".");
    form.unit.value = m[2].toLowerCase();
  }
}

function priceText(pr) {
  const parts = [money(pr.amount_cents)];
  if (pr.quantity) parts.push(`for ${pr.quantity} ${pr.unit}`);
  if (pr.unit_price) parts.push(`${money(pr.unit_price.amount_cents)} per ${pr.unit_price.per}`);
  return parts.join(" · ");
}

function verdictWord(buy) {
  return el("span", { class: "word" }, el("i", { class: `mark mark--${buy ? "buy" : "skip"}` }),
    document.createTextNode(buy ? "Buy again" : "Don't buy again"));
}

// ---- Router ---------------------------------------------------------------

const views = [...document.querySelectorAll(".view")];

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function show(id, nav) {
  for (const v of views) v.hidden = v.id !== id;
  $("#bar").hidden = id === "view-login";
  $("#listbar").hidden = id === "view-login" || !state.lists || state.lists.length === 0;
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === nav) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  window.scrollTo(0, 0);
}

async function route() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query || "");
  const parts = path.split("/").filter(Boolean);

  if (parts[0] !== "scan" && parts[0] !== "shop") stopCamera();
  // The shopping list's camera comes back after the form for a new product,
  // not after a visit anywhere else.
  if (parts[0] !== "shop" && parts[0] !== "new") {
    state.shopScanning = false;
    state.shopAdd = null;
  }
  // A product saved offline waits for its rating only while that form is open.
  if (parts[0] !== "pending") state.holdJob = null;

  if (!state.user && parts[0] !== "login") {
    try {
      state.user = await api("GET", "/api/session", undefined, { retries: 1 });
      state.offline = false;
      rememberUser(state.user);
    } catch (err) {
      const known = err.offline ? remembered(USER_KEY) : null;
      if (known) {
        // No connection: work with what this phone has, read-only, until
        // the connection is back (see goOnline).
        state.user = known;
        state.offline = true;
        state.lists = remembered(LISTS_KEY);
        state.listId = storedListId();
        renderListBar();
        await Known.load();
      } else if (err.offline) {
        show("view-scan", "scan");
        showError($("#scan-message"), err);
        return;
      } else {
        await forgetOffline();
        go("#/login");
        return;
      }
    }
  }
  if (state.user && state.lists === null && !state.offline) {
    try {
      await loadLists();
    } catch (err) {
      if (err.status === 401) return;
      // Offline: keep working in the list used last.
      state.lists = [];
      state.listId = storedListId();
    }
  }
  if (state.user && state.lists && state.lists.length === 0 && state.listId === null
      && !["manage", "account", "login"].includes(parts[0])) {
    flash("Create your first list to start. Lists can be shared with other users.");
    go("#/manage");
    return;
  }
  if (state.user) syncOutbox();
  if (state.user) Known.refresh();
  $("#offline-bar").hidden = !state.offline;

  try {
    switch (parts[0]) {
      case "login":
        return showLogin();
      case "lists":
      case "products":
        return await showLists(params);
      case "manage":
        return parts[1] ? await showListDetail(Number(parts[1])) : await showManage();
      case "account":
        return showAccount();
      case "shop":
        return await showShop();
      case "new":
        return await showProductForm(null, params.get("barcode"));
      case "pending":
        return await showRateForm(null, null, parts[1]);
      case "product": {
        const id = Number(parts[1]);
        if (parts[2] === "edit") return await showProductForm(id);
        if (parts[2] === "rate") return await showRateForm(id, null);
        if (parts[2] === "rating") return await showRateForm(id, Number(parts[3]));
        if (parts[2] === "price") return await showPriceForm(id, parts[3] ? Number(parts[3]) : null);
        return await showProduct(id);
      }
      default:
        return showScan();
    }
  } catch (err) {
    if (err.status === 401) return;
    show("view-scan", "scan");
    showError($("#scan-message"), err);
  }
}

window.addEventListener("hashchange", route);

async function loadFacets() {
  // Only suggestions: offline, the last ones (or none) will do.
  try {
    state.facets = await api("GET", "/api/facets", undefined, { retries: 0 });
  } catch (err) {
    if (!err.offline) throw err;
    return;
  }
  const fill = (list, values) => {
    list.replaceChildren(...values.map((v) => el("option", { value: v })));
  };
  fill($("#stores"), state.facets.stores);
  fill($("#categories"), state.facets.categories);
}

// ---- Lists ------------------------------------------------------------------

const LIST_KEY = "buybutler.list";

function storedListId() {
  try {
    const id = Number(localStorage.getItem(LIST_KEY));
    return id > 0 ? id : null;
  } catch {
    return null;
  }
}

async function loadLists() {
  const { lists } = await api("GET", "/api/lists");
  state.lists = lists;
  remember(LISTS_KEY, lists.map((l) => ({ id: l.id, name: l.name, role: l.role })));
  const wanted = state.listId || storedListId();
  const pick = lists.find((l) => l.id === wanted) || lists[0] || null;
  setList(pick ? pick.id : null);
}

function setList(id) {
  state.listId = id;
  try {
    if (id) localStorage.setItem(LIST_KEY, String(id));
    else localStorage.removeItem(LIST_KEY);
  } catch {
    // Private mode: the choice lasts until the page is closed.
  }
  renderListBar();
}

function currentList() {
  return (state.lists || []).find((l) => l.id === state.listId) || null;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function canEdit() {
  const list = currentList();
  return !list || list.role !== "viewer";
}

const ROLE_WORD = { owner: "Owner", editor: "Editor", viewer: "Read only" };

function renderListBar() {
  const select = $("#list-select");
  const lists = state.lists || [];
  select.replaceChildren(...lists.map((l) => el("option", { value: String(l.id), text: l.name })));
  if (state.listId) select.value = String(state.listId);
  const list = currentList();
  const tag = $("#list-role");
  tag.hidden = !list || list.role !== "viewer";
  tag.textContent = list ? ROLE_WORD[list.role] : "";
  const bar = $("#listbar");
  if (!$("#view-login").hidden || lists.length === 0) bar.hidden = true;
  else bar.hidden = false;
}

$("#list-select").addEventListener("change", (ev) => {
  setList(Number(ev.currentTarget.value));
  route();
});

async function showManage() {
  await loadLists();
  show("view-manage", "lists");
  showInfo($("#m-message"), "");
  takeFlash($("#m-message"));
  $("#new-list-form").reset();
  $("#m-lists").replaceChildren(
    ...state.lists.map((l) => {
      const open = el("button", { type: "button", class: "edit linkish", text: l.id === state.listId ? "In use" : "Use" });
      open.addEventListener("click", () => {
        setList(l.id);
        go("#/scan");
      });
      return el("li", {},
        el("a", { href: `#/manage/${l.id}` },
          el("span", { class: "text" },
            el("span", { class: "name", text: l.name }),
            el("span", { class: "sub" },
              el("span", { class: "word", text: ROLE_WORD[l.role] }),
              el("span", { text: `${plural(l.members, "member")} · ${plural(l.products, "product")}` })))),
        open);
    }),
  );
}

$("#new-list-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  try {
    const created = await api("POST", "/api/lists", { name: form.name.value });
    setList(created.id);
    await showManage();
    showInfo($("#m-message"), `Created ${created.name}. It is the list in use now.`);
  } catch (err) {
    showError($("#m-message"), err);
  }
});

async function showListDetail(id) {
  await loadLists();
  const list = state.lists.find((l) => l.id === id);
  if (!list) throw new ApiError(404, "list not found");
  const { members } = await api("GET", `/api/lists/${id}/members`);
  show("view-list", "lists");
  const owner = list.role === "owner";
  showInfo($("#l1-message"), "");
  $("#l1-role").textContent = `You: ${ROLE_WORD[list.role]}`;
  $("#l1-name").textContent = list.name;
  $("#l1-stamps").textContent = `Created ${stamp(list.created_at)} · changed ${stamp(list.updated_at)}`
    + ` · ${plural(list.products, "product")}`;
  $("#rename-form").hidden = !owner;
  $("#rename-form").name.value = list.name;
  $("#member-form").hidden = !owner;
  $("#member-form").reset();
  $("#delete-list-form").hidden = !owner;
  $("#delete-list-form").reset();
  $("#view-list").dataset.list = id;

  $("#l1-members").replaceChildren(
    ...members.map((m) => {
      const actions = el("div", { class: "member-actions" });
      if (owner) {
        const role = el("select", { "aria-label": `Role of ${m.username}` },
          ...["owner", "editor", "viewer"].map((r) => el("option", { value: r, text: ROLE_WORD[r] })));
        role.value = m.role;
        role.addEventListener("change", async () => {
          try {
            await api("PATCH", `/api/lists/${id}/members/${m.user_id}`, { role: role.value });
            await showListDetail(id);
          } catch (err) {
            role.value = m.role;
            showError($("#l1-message"), err);
          }
        });
        actions.append(role);
        if (!m.you) {
          const remove = el("button", { type: "button", class: "edit linkish", text: "Remove" });
          remove.addEventListener("click", async () => {
            if (!confirm(`Remove ${m.username} from ${list.name}?`)) return;
            try {
              await api("DELETE", `/api/lists/${id}/members/${m.user_id}`);
              await showListDetail(id);
            } catch (err) {
              showError($("#l1-message"), err);
            }
          });
          actions.append(remove);
        }
      } else {
        actions.append(el("span", { class: "word", text: ROLE_WORD[m.role] }));
      }
      return el("li", {},
        el("div", { class: "head" }, el("span", { class: "word", text: m.you ? `${m.username} (you)` : m.username })),
        actions,
        el("p", { class: "stamps", text: `Member since ${stamp(m.created_at)}` }));
    }),
  );
  $("#leave-list").dataset.user = members.find((m) => m.you).user_id;
}

$("#rename-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const id = Number($("#view-list").dataset.list);
  try {
    await api("PATCH", `/api/lists/${id}`, { name: ev.currentTarget.name.value });
    await showListDetail(id);
  } catch (err) {
    showError($("#l1-message"), err);
  }
});

$("#member-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const id = Number($("#view-list").dataset.list);
  const name = form.username.value.trim();
  try {
    await api("POST", `/api/lists/${id}/members`, { username: name, role: form.role.value });
    await showListDetail(id);
    showInfo($("#l1-message"), `${name} can use this list now.`);
  } catch (err) {
    showError($("#l1-message"), err);
  }
});

$("#leave-list").addEventListener("click", async () => {
  const id = Number($("#view-list").dataset.list);
  if (!confirm("Leave this list? You lose access until an owner adds you again.")) return;
  try {
    await api("DELETE", `/api/lists/${id}/members/${$("#leave-list").dataset.user}`);
    if (state.listId === id) setList(null);
    state.lists = null;
    go("#/manage");
  } catch (err) {
    showError($("#l1-message"), err);
  }
});

$("#delete-list-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const id = Number($("#view-list").dataset.list);
  const name = ev.currentTarget.confirm_name.value;
  if (!confirm("Delete this list with everything in it, for all members?")) return;
  try {
    await api("DELETE", `/api/lists/${id}`, { confirm_name: name });
    if (state.listId === id) setList(null);
    state.lists = null;
    flash("The list was deleted.");
    go("#/manage");
  } catch (err) {
    showError($("#l1-message"), err);
  }
});

// ---- Login and account ----------------------------------------------------

// ---- Passkeys (WebAuthn) ----------------------------------------------------
//
// The server sends options as JSON with binary fields in base64url, and
// expects the browser's answer the same way.

const b64url = {
  decode(text) {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(text.length / 4) * 4, "=");
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  },
  encode(buffer) {
    let text = "";
    for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

const credentialList = (list) => (list || []).map((c) => ({ ...c, id: b64url.decode(c.id) }));

// The site must be opened at the address passkeys are registered for.
function passkeysHere(origin) {
  return Boolean(origin && window.PublicKeyCredential && window.isSecureContext && location.origin === origin);
}

function credentialJSON(cred) {
  const r = cred.response;
  const response = { clientDataJSON: b64url.encode(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = b64url.encode(r.attestationObject);
    if (r.getTransports) response.transports = r.getTransports();
  } else {
    response.authenticatorData = b64url.encode(r.authenticatorData);
    response.signature = b64url.encode(r.signature);
    if (r.userHandle) response.userHandle = b64url.encode(r.userHandle);
  }
  return {
    id: cred.id,
    rawId: b64url.encode(cred.rawId),
    type: cred.type,
    response,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
  };
}

async function passkeyGet(options) {
  const cred = await navigator.credentials.get({ publicKey: {
    ...options, challenge: b64url.decode(options.challenge), allowCredentials: credentialList(options.allowCredentials),
  } });
  return credentialJSON(cred);
}

async function passkeyCreate(options) {
  const cred = await navigator.credentials.create({ publicKey: {
    ...options,
    challenge: b64url.decode(options.challenge),
    user: { ...options.user, id: b64url.decode(options.user.id) },
    excludeCredentials: credentialList(options.excludeCredentials),
  } });
  return credentialJSON(cred);
}

// The browser's own errors when the user cancels or the device refuses.
function passkeyError(err) {
  if (err instanceof ApiError || err.offline) return err;
  if (err.name === "NotAllowedError") return new Error("Cancelled, or the passkey wasn't available.");
  if (err.name === "InvalidStateError") return new Error("This device holds a passkey for Buybutler already.");
  return new Error(`The passkey didn't work: ${err.message}`);
}

// ---- Login and account ----------------------------------------------------

let pendingLogin = null; // { ticket, methods } between password and second step

function showLogin() {
  show("view-login");
  pendingLogin = null;
  $("#login-form").hidden = false;
  $("#second-form").hidden = true;
  $("#login-form [name=username]").focus();
  // Requests here go out once: a repeated login step finds its challenge used.
  api("GET", "/api/session/options", undefined, { retries: 0 })
    .then((o) => { $("#passkey-login").hidden = !passkeysHere(o.passkey_origin); })
    .catch(() => {});
}

function finishLogin(user) {
  const before = remembered(USER_KEY);
  // Another user on this phone never sees the previous one's copy.
  if (before && before.username !== user.username) forgetOffline();
  state.user = { username: user.username, role: user.role, currency: user.currency };
  state.offline = false;
  rememberUser(state.user);
  state.lists = null;
  pendingLogin = null;
  $("#login-form").password.value = "";
  if (user.recovery_codes_left !== undefined) {
    flash(`Logged in with a recovery code; ${plural(user.recovery_codes_left, "code")} left. `
      + "New ones are under Account, Two-factor login.");
  }
  go("#/scan");
}

function showSecondStep(step) {
  pendingLogin = step;
  $("#login-form").hidden = true;
  const form = $("#second-form");
  form.hidden = false;
  form.reset();
  showInfo($("#second-message"), "");
  const totp = step.methods.includes("totp");
  const passkey = step.methods.includes("passkey") && Boolean(window.PublicKeyCredential);
  $("#second-hint").textContent = totp
    ? "Enter the 6-digit code from your authenticator app, or a recovery code."
    : "Use your passkey, or enter a recovery code.";
  $("#second-passkey").hidden = !passkey;
  form.code.inputMode = totp ? "numeric" : "text";
  if (passkey && !totp) $("#second-passkey").focus();
  else form.code.focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#login-message");
  showInfo(msg, "");
  try {
    const result = await api("POST", "/api/session", {
      username: form.username.value,
      password: form.password.value,
    });
    if (result.second_factor) showSecondStep(result.second_factor);
    else finishLogin(result);
  } catch (err) {
    showError(msg, err);
  }
});

$("#second-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  try {
    finishLogin(await api("POST", "/api/session/second-factor",
      { ticket: pendingLogin.ticket, code: form.code.value.trim() }, { retries: 0 }));
  } catch (err) {
    form.code.value = "";
    showError($("#second-message"), err);
    if (err.status === 401 && /start again/.test(err.message)) setTimeout(showLogin, 2500);
  }
});

$("#second-cancel").addEventListener("click", showLogin);

async function loginWithPasskey(ticket, msg) {
  showInfo(msg, "");
  try {
    const start = await api("POST", "/api/session/passkey/options", ticket ? { ticket } : {}, { retries: 0 });
    const credential = await passkeyGet(start.options);
    finishLogin(await api("POST", "/api/session/passkey", { ticket: start.ticket, credential }, { retries: 0 }));
  } catch (err) {
    showError(msg, passkeyError(err));
  }
}

$("#passkey-login").addEventListener("click", () => loginWithPasskey(null, $("#login-message")));
$("#second-passkey").addEventListener("click", () => loginWithPasskey(pendingLogin.ticket, $("#second-message")));

function showAccount() {
  show("view-account", "account");
  $("#a-user").textContent = `Logged in as ${state.user.username} (${state.user.role})`;
  $("#password-form").reset();
  showInfo($("#a-message"), "");
  updateOutboxBadge();
  showSecurity();
  $("#a-users-section").hidden = state.user.role !== "admin";
  if (state.user.role === "admin") loadUsers().catch((err) => showError($("#u-message"), err));
}

// Changes to users ask for the admin's own password (see AdminConfirm on the server).
function adminPassword() {
  const password = $("#u-password").value;
  if (!password) {
    $("#u-password").focus();
    throw new Error("Enter your password first.");
  }
  return password;
}

async function adminChange(method, path, body) {
  const result = await api(method, path, { ...body, admin_password: adminPassword() }, { retries: 0 });
  $("#u-password").value = "";
  return result;
}

async function loadUsers() {
  const { users } = await api("GET", "/api/users");
  renderUsers(users);
}

function renderUsers(users) {
  $("#a-users").replaceChildren(
    ...users.map((u) => {
      const me = u.username === state.user.username;
      const actions = el("div", { class: "member-actions" });
      const role = el("select", { "aria-label": `Role of ${u.username}` },
        el("option", { value: "member", text: "Member" }), el("option", { value: "admin", text: "Admin" }));
      role.value = u.role;
      role.addEventListener("change", async () => {
        try {
          renderUsers((await adminChange("PATCH", `/api/users/${u.id}`, { role: role.value })).users);
        } catch (err) {
          role.value = u.role;
          showError($("#u-message"), err);
        }
      });
      actions.append(role);
      const reset = el("button", { type: "button", class: "edit linkish", text: "New password" });
      reset.addEventListener("click", async () => {
        try {
          adminPassword();
          const password = prompt(`New password for ${u.username} (at least 10 characters). Their sessions end.`);
          if (!password) return;
          await adminChange("POST", `/api/users/${u.id}/password`, { password });
          showInfo($("#u-message"), `Password of ${u.username} changed.`);
        } catch (err) {
          showError($("#u-message"), err);
        }
      });
      actions.append(reset);
      if (u.two_factor) {
        const reset2fa = el("button", { type: "button", class: "edit linkish", text: "Reset two-factor" });
        reset2fa.addEventListener("click", async () => {
          try {
            adminPassword();
            if (!confirm(`Remove the authenticator app, passkeys and recovery codes of ${u.username}? `
              + "Their password alone logs in again.")) return;
            renderUsers((await adminChange("POST", `/api/users/${u.id}/reset-2fa`, {})).users);
            showInfo($("#u-message"), `Two-factor login of ${u.username} removed.`);
          } catch (err) {
            showError($("#u-message"), err);
          }
        });
        actions.append(reset2fa);
      }
      if (!me) {
        const remove = el("button", { type: "button", class: "edit linkish", text: "Delete" });
        remove.addEventListener("click", async () => {
          try {
            adminPassword();
            if (!confirm(`Delete ${u.username}? Lists only they use are deleted too.`)) return;
            await adminChange("DELETE", `/api/users/${u.id}`, {});
            await loadUsers();
          } catch (err) {
            showError($("#u-message"), err);
          }
        });
        actions.append(remove);
      }
      return el("li", {},
        el("div", { class: "head" }, el("span", { class: "word", text: me ? `${u.username} (you)` : u.username })),
        actions,
        el("p", { class: "stamps", text: `${plural(u.lists, "list")}${u.two_factor ? " · two-factor" : ""}`
          + ` · created ${stamp(u.created_at)}` }));
    }),
  );
}

// ---- Shopping list ------------------------------------------------------------
//
// One shopping list per list. Every change is shown at once and sent as an
// outbox job, in order, so it also works without a connection. While the
// list is open, it is fetched again every 5 seconds; changes still waiting
// in the outbox are laid over what the server sent.

const STORE_KEY = "buybutler.store";
const shop = {
  data: null, // { list_id, store, stores, items } as the server sent it
  inFlight: 0, // changes being sent; a fetch meanwhile could be older
  dragging: false,
  editing: null, // the item whose edit form is open
  noteItem: null, // the item a scan found open on the list ("Tick off", "Raise quantity")
  local: [], // jobs made here that may not be in the outbox yet
};
let shopChain = Promise.resolve();

function shopStore() {
  try {
    return localStorage.getItem(STORE_KEY) || "";
  } catch {
    return "";
  }
}

const sameStore = (a, b) => (a || "").toLowerCase() === (b || "").toLowerCase();

// Changes still waiting on the phone, laid over the server's list.
function overlay(items, jobs, store) {
  for (const job of jobs.sort((a, b) => a.created.localeCompare(b.created))) {
    for (const step of job.steps) {
      if (step.done || step.list !== state.listId || !step.path.startsWith("/api/shopping")) continue;
      const id = step.path.split("/")[3];
      const item = items.find((i) => i.id === id);
      if (step.method === "POST" && step.path === "/api/shopping") {
        const known = items.find((i) => i.id === step.body.client_id
          || (step.body.product_id && i.product_id === step.body.product_id && !i.checked));
        if (!known) items.push({ ...step.local });
      } else if (step.method === "PATCH" && item) {
        Object.assign(item, step.body);
        if (step.body.text) item.name = step.body.text;
        if (step.body.checked === true && !item.checked_at) item.checked_at = job.created;
      } else if (step.method === "DELETE") {
        items.splice(0, items.length, ...items.filter((i) => i.id !== id));
      } else if (step.path === "/api/shopping/clear-checked") {
        items.splice(0, items.length, ...items.filter((i) => !i.checked));
      } else if (step.path === "/api/shopping/order" && sameStore(step.body.store, store)) {
        step.body.keys.forEach((key, n) => {
          for (const i of items) if (i.key === key) i.position = n - step.body.keys.length;
        });
      }
    }
  }
  return items;
}

async function loadShop() {
  const store = shopStore();
  let data;
  try {
    if (state.offline) throw new NetworkError();
    data = await api("GET", `/api/shopping?store=${encodeURIComponent(store)}`, undefined, { retries: 0 });
    idb("shopping", "readwrite", (s) => s.put(data)).catch(() => {});
  } catch (err) {
    if (!err.offline) throw err;
    data = await idb("shopping", "readonly", (s) => s.get(state.listId)).catch(() => null);
    if (!data) throw err;
    // The positions saved were those of the store chosen then.
    if (!sameStore(data.store, store)) data = { ...data, items: data.items.map((i) => ({ ...i, position: null })) };
  }
  shop.data = data;
  renderShop();
}

function shopItems() {
  const items = structuredClone(shop.data ? shop.data.items : []);
  return items;
}

// The list as shown: the server's, with the changes still on the phone.
async function currentShopItems(store) {
  const stored = await Outbox.all().catch(() => []);
  const jobs = [...stored, ...shop.local.filter((j) => !stored.some((x) => x.id === j.id))];
  return overlay(shopItems(), jobs, store);
}

async function renderShop() {
  if (!shop.data) return;
  const store = shopStore();
  const items = await currentShopItems(store);
  const editable = canEdit();

  const select = $("#shop-store");
  const stores = [...shop.data.stores];
  if (store && !stores.some((x) => sameStore(x, store))) stores.push(store);
  select.replaceChildren(el("option", { value: "", text: "Any store" }),
    ...stores.map((x) => el("option", { value: x, text: x })));
  select.value = stores.find((x) => sameStore(x, store)) || "";

  const open = items.filter((i) => !i.checked);
  const here = open.filter((i) => !i.store || !store || sameStore(i.store, store));
  const elsewhere = open.filter((i) => !here.includes(i));
  const byPlace = (a, b) => (a.position ?? Infinity) - (b.position ?? Infinity) || a.created_at.localeCompare(b.created_at);
  here.sort(byPlace);
  elsewhere.sort((a, b) => a.store.localeCompare(b.store) || byPlace(a, b));
  const done = items.filter((i) => i.checked).sort((a, b) => (b.checked_at || "").localeCompare(a.checked_at || ""));

  $("#shop-add").hidden = !editable;
  $("#shop-scan").hidden = !editable;
  $("#shop-open").replaceChildren(...here.map((i) => shopRow(i, editable, true)));
  $("#shop-empty").hidden = here.length > 0;
  $("#shop-hint").hidden = !editable || here.length < 2 || here.some((i) => i.position !== null);
  $("#shop-others").hidden = elsewhere.length === 0;
  $("#shop-others-title").textContent = `${plural(elsewhere.length, "item")} for other stores`;
  $("#shop-other-items").replaceChildren(...elsewhere.map((i) => shopRow(i, editable, false)));
  $("#shop-done-title").hidden = done.length === 0;
  $("#shop-done").replaceChildren(...done.map((i) => shopRow(i, editable, false)));
  $("#shop-clear").hidden = !editable || done.length === 0;
}

function shopRow(item, editable, draggable) {
  const li = el("li", { class: `shop-item${item.checked ? " is-checked" : ""}` });
  li.dataset.id = item.id;
  li.dataset.key = item.key;
  if (editable && draggable) {
    const handle = el("button", { type: "button", class: "handle", "aria-label": `Move ${item.name}`, text: "≡" });
    handle.addEventListener("pointerdown", (ev) => startDrag(ev, li));
    li.append(handle);
  }
  const sub = [item.quantity, item.brand, item.store && !draggable && !item.checked ? item.store : null,
    item.checked && item.checked_by ? `ticked by ${item.checked_by}` : null].filter(Boolean).join(" · ");
  const tick = el("button", { type: "button", class: "tick", "aria-pressed": String(item.checked) },
    el("span", { class: "box" }),
    el("span", { class: "text" },
      el("span", { class: "name", text: item.name }),
      sub ? el("span", { class: "sub", text: sub }) : null,
      item.verdict === false ? el("span", { class: "sub", text: `Don't buy again${item.reason ? `: ${item.reason}` : ""}` }) : null));
  tick.disabled = !editable;
  tick.addEventListener("click", () => {
    shopChange(item, { checked: !item.checked }, `${item.checked ? "Untick" : "Tick"}: ${item.name}`);
  });
  li.append(tick);
  const side = el("span", { class: "side" });
  if (editable && item.checked && item.product_id) {
    side.append(el("a", { class: "edit linkish", href: `#/product/${item.product_id}/price`, text: "Price" }));
  }
  if (editable) {
    const edit = el("button", { type: "button", class: "edit linkish", text: "Edit" });
    edit.addEventListener("click", () => toggleShopEdit(li, item));
    side.append(edit);
  }
  li.append(side);
  return li;
}

function toggleShopEdit(li, item) {
  const open = li.nextElementSibling && li.nextElementSibling.classList.contains("shop-edit");
  document.querySelectorAll(".shop-edit").forEach((f) => f.remove());
  shop.editing = null;
  if (open) return;
  shop.editing = item.id;
  const form = el("form", { class: "shop-edit" });
  const field = (label, name, value, extra = {}) => el("label", { class: `field${extra.wide ? " wide" : ""}` },
    el("span", { class: "label", text: label }),
    el("input", { name, value, maxlength: String(extra.max || 20), autocomplete: "off", ...(extra.list ? { list: extra.list } : {}) }));
  if (!item.product_id) form.append(field("Text", "text", item.text, { wide: true, max: 100 }));
  form.append(field("How many", "quantity", item.quantity),
    field("Only at store", "store", item.store, { list: "stores", max: 100 }));
  const remove = el("button", { type: "button", class: "btn btn--outline", text: "Remove" });
  remove.addEventListener("click", () => {
    shop.editing = null;
    shopSend(`Remove: ${item.name}`, { method: "DELETE", path: `/api/shopping/${item.id}` });
  });
  form.append(el("div", { class: "actions" }, remove, el("button", { type: "submit", class: "btn btn--secondary", text: "Save" })));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const changes = { quantity: form.quantity.value.trim(), store: form.store.value.trim() };
    if (form.text && form.text.value.trim()) changes.text = form.text.value.trim();
    shop.editing = null;
    shopChange(item, changes, `Change: ${item.name}`);
  });
  li.after(form);
  form.querySelector("input").focus();
}

function shopChange(item, changes, label) {
  shopSend(label, { method: "PATCH", path: `/api/shopping/${item.id}`, body: changes });
}

// Shown at once, sent in order. When older shopping changes still wait on
// the phone, this one waits behind them (an item added offline must exist
// before it can be ticked).
function shopSend(label, step) {
  const job = newJob(label, [{ ref: "shop", list: state.listId, ...step }]);
  shop.inFlight++;
  shop.local.push(job);
  shopChain = shopChain.then(async () => {
    try {
      const waiting = (await Outbox.all().catch(() => [])).some((j) => j.steps.some((s) => !s.done
        && s.path.startsWith("/api/shopping")));
      if (waiting) {
        await Outbox.put(job).catch(() => {});
        updateOutboxBadge();
        syncOutbox();
        return;
      }
      await submitJob(job);
    } catch (err) {
      showError($("#shop-message"), err);
    } finally {
      shop.inFlight--;
      shop.local = shop.local.filter((j) => j !== job);
    }
    if (!$("#view-shop").hidden && !state.offline) await loadShop().catch(() => {});
  });
  renderShop();
}

// The item as the shopping list shows it until the server has it. A product
// that is still waiting in the outbox has no ID yet.
function shopItemLocal(clientId, { product, text, quantity = "" }) {
  const now = new Date().toISOString();
  return {
    id: clientId, product_id: product ? product.id || null : null, name: product ? product.name : text, text: product ? "" : text,
    brand: product ? product.brand || "" : "", category: product ? product.category || "" : "", image: null,
    verdict: product && product.rating ? product.rating.buy_again : null, reason: product && product.rating ? product.rating.reason : "",
    quantity, store: "", checked: false, checked_at: null, checked_by: null, added_by: state.user.username,
    key: product ? `p:${product.id || clientId}` : `t:${text.trim().replace(/\s+/g, " ").toLowerCase()}`, position: null,
    created_at: now, updated_at: now,
  };
}

function addToShopping({ product, text, quantity = "" }) {
  const clientId = uid();
  const body = { client_id: clientId, quantity };
  if (product) body.product_id = product.id;
  else body.text = text;
  const local = shopItemLocal(clientId, { product, text, quantity });
  shopSend(`Shopping: ${local.name}`, { method: "POST", path: "/api/shopping", body, local });
}

// "2x" becomes "3x"; anything else that isn't a count gets one in front.
function raiseQuantity(quantity) {
  const q = (quantity || "").trim();
  if (!q) return "2x";
  const m = q.match(/^(\d+)\s*[x×]\s*(.*)$/i) || q.match(/^(\d+)()$/);
  const raised = m ? `${Number(m[1]) + 1}x${m[2] ? ` ${m[2]}` : ""}` : `2x ${q}`;
  return raised.slice(0, 20); // the field's limit
}

// A product marked "don't buy again" asks first, with the reason.
function shoppingOk(product) {
  if (!product.rating || product.rating.buy_again) return true;
  const why = product.rating.reason ? ` ("${product.rating.reason}")` : "";
  return confirm(`You noted "Don't buy again" for ${product.name}${why}. Add it anyway?`);
}

async function showShop() {
  if (camera && camera.target !== SCANNERS.shop) stopCamera();
  show("view-shop", "shop");
  showInfo($("#shop-message"), "");
  takeFlash($("#shop-message"));
  shopNote("");
  shop.editing = null;
  const start = $("#shop-scan-start");
  const usable = navigator.mediaDevices && window.isSecureContext && scannerReady();
  start.disabled = !usable;
  start.textContent = usable ? "Scan barcodes" : window.isSecureContext ? "Scanner not available" : "Scanning needs HTTPS";
  await loadFacets();
  if (shop.data && shop.data.list_id === state.listId) renderShop();
  else shop.data = null;
  try {
    await loadShop();
  } catch (err) {
    if (!err.offline) throw err;
    showError($("#shop-message"), new Error("No connection, and this list's shopping list was never opened on this phone."));
    return;
  }
  // Back from the form for a new product: keep scanning.
  if (state.shopScanning && usable && canEdit() && !$("#view-shop").hidden) startCamera(SCANNERS.shop);
}

// Live while open: another phone's ticks show within seconds.
setInterval(() => {
  if ($("#view-shop").hidden || document.hidden || state.offline || shop.dragging || shop.editing || shop.inFlight) return;
  loadShop().catch(() => {});
}, 5000);

$("#shop-store").addEventListener("change", (ev) => {
  try {
    localStorage.setItem(STORE_KEY, ev.currentTarget.value);
  } catch {
    // Private mode: the choice lasts until the page is closed.
  }
  loadShop().catch((err) => showError($("#shop-message"), err));
});

$("#shop-clear").addEventListener("click", () => {
  shopSend("Clear ticked", { method: "POST", path: "/api/shopping/clear-checked" });
});

// Dragging by the handle: the row follows the finger past its neighbours;
// on release the shown order is saved for this store. The listeners sit on
// the window: moving the row in the DOM ends a pointer capture.
function startDrag(ev, li) {
  ev.preventDefault();
  const list = li.parentElement;
  const pointer = ev.pointerId;
  shop.dragging = true;
  li.classList.add("is-dragging");
  const move = (e) => {
    if (e.pointerId !== pointer) return;
    e.preventDefault();
    const rows = [...list.children].filter((x) => x !== li);
    const before = rows.find((row) => {
      const box = row.getBoundingClientRect();
      return e.clientY < box.top + box.height / 2;
    }) || null;
    if (before !== li.nextElementSibling) list.insertBefore(li, before);
  };
  const end = (e) => {
    if (e.pointerId !== pointer) return;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    li.classList.remove("is-dragging");
    shop.dragging = false;
    const keys = [...list.children].map((row) => row.dataset.key);
    shopSend("Shopping order", { method: "PUT", path: "/api/shopping/order", body: { store: shopStore(), keys } });
  };
  window.addEventListener("pointermove", move, { passive: false });
  window.addEventListener("pointerup", end);
  window.addEventListener("pointercancel", end);
}

// Suggestions while typing: "Buy again" first, "Don't buy again" last.
let suggestTimer;
const VERDICT_RANK = (p) => (!p.rating ? 1 : p.rating.buy_again ? 0 : 2);

async function suggestProducts(q) {
  let products = [];
  try {
    if (state.offline) throw new NetworkError();
    ({ products } = await api("GET", `/api/products?${new URLSearchParams({ verdict: "all", q, sort: "name" })}`,
      undefined, { retries: 0 }));
  } catch (err) {
    if (!err.offline) throw err;
    const copy = await Known.load();
    products = copy ? filterKnown(copy.products, { verdict: "all", q, store: "", category: "", sort: "name" }) : [];
  }
  return products.sort((a, b) => VERDICT_RANK(a) - VERDICT_RANK(b)).slice(0, 8);
}

function renderSuggestions(q, products) {
  const box = $("#shop-suggestions");
  const form = $("#shop-add");
  const pick = (fn) => () => {
    fn();
    form.reset();
    box.hidden = true;
    form.q.focus();
  };
  box.replaceChildren(
    ...products.map((p) => {
      const b = el("button", { type: "button" },
        el("span", { class: "name", text: p.name }),
        p.rating ? verdictWord(p.rating.buy_again) : el("span", { class: "meta", text: "Not rated" }),
        p.brand ? el("span", { class: "meta", text: p.brand }) : null);
      b.addEventListener("click", () => {
        if (!shoppingOk(p)) return;
        pick(() => addToShopping({ product: p, quantity: form.quantity.value.trim() }))();
      });
      return el("li", {}, b);
    }),
    (() => {
      const b = el("button", { type: "button" }, el("span", { text: `Add "${q}" as text` }));
      b.addEventListener("click", pick(() => addToShopping({ text: q, quantity: form.quantity.value.trim() })));
      return el("li", {}, b);
    })(),
  );
  box.hidden = false;
}

$("#shop-add").q.addEventListener("input", (ev) => {
  clearTimeout(suggestTimer);
  const q = ev.currentTarget.value.trim();
  if (!q) {
    $("#shop-suggestions").hidden = true;
    return;
  }
  suggestTimer = setTimeout(async () => {
    try {
      const products = await suggestProducts(q);
      if ($("#shop-add").q.value.trim() === q) renderSuggestions(q, products);
    } catch (err) {
      showError($("#shop-message"), err);
    }
  }, 200);
});

// Enter: a product with exactly this name, otherwise the text.
$("#shop-add").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const q = form.q.value.trim();
  if (!q) return;
  const products = await suggestProducts(q).catch(() => []);
  const exact = products.find((p) => p.name.toLowerCase() === q.toLowerCase());
  if (exact && !shoppingOk(exact)) return;
  addToShopping(exact ? { product: exact, quantity: form.quantity.value.trim() } : { text: q, quantity: form.quantity.value.trim() });
  form.reset();
  $("#shop-suggestions").hidden = true;
});

// ---- Scanning into the shopping list ------------------------------------
//
// The camera stays on: a known product goes on the list at once, one that
// is open on the list already offers "Tick off" and "Raise quantity", and an
// unknown barcode opens the form for a new product, which then goes on the
// list too (state.shopAdd).

function shopNote(text, item = null) {
  shop.noteItem = item;
  $("#shop-scan-note").hidden = !text;
  $("#shop-scan-text").textContent = text;
  $("#shop-scan-actions").hidden = !item;
}

async function shopScanned(code) {
  let product = await Known.byBarcode(code);
  if (!product) {
    shopNote(`Looking up ${code}`);
    try {
      const r = await api("GET", `/api/lookup?barcode=${encodeURIComponent(code)}`, undefined,
        state.offline ? { retries: 0 } : {});
      product = r.product;
      if (!product) state.suggestion = r.suggestion ? { ...r.suggestion, lookup: r.lookup } : { barcode: code, lookup: r.lookup };
    } catch (err) {
      if (!err.offline) {
        shopNote("");
        showError($("#shop-message"), err);
        return;
      }
      // Not in the copy on this phone: not rated yet in this list, or never seen.
      state.suggestion = { barcode: code, lookup: "offline" };
    }
  }
  if (!camera || camera.target !== SCANNERS.shop) return; // stopped meanwhile
  if (!product) {
    state.shopAdd = code;
    go(`#/new?barcode=${encodeURIComponent(code)}`);
    return;
  }
  const open = (await currentShopItems(shopStore())).find((i) => !i.checked && i.product_id === product.id);
  if (open) {
    shopNote(`${product.name} is on the list already${open.quantity ? ` (${open.quantity})` : ""}.`, open);
    return;
  }
  if (!shoppingOk(product)) {
    shopNote(`${product.name} was not added.`);
    return;
  }
  addToShopping({ product });
  shopNote(`${product.name} added.`);
}

$("#shop-scan-start").addEventListener("click", () => {
  showInfo($("#shop-message"), "");
  state.shopScanning = true;
  startCamera(SCANNERS.shop);
});
$("#shop-scan-stop").addEventListener("click", () => {
  state.shopScanning = false;
  stopCamera();
  shopNote("");
});
$("#shop-scan-tick").addEventListener("click", () => {
  const item = shop.noteItem;
  if (!item) return;
  shopChange(item, { checked: true }, `Tick: ${item.name}`);
  shopNote(`${item.name} ticked off.`);
});
$("#shop-scan-more").addEventListener("click", () => {
  const item = shop.noteItem;
  if (!item) return;
  item.quantity = raiseQuantity(item.quantity);
  shopChange(item, { quantity: item.quantity }, `Change: ${item.name}`);
  // Another tap raises it once more.
  shopNote(`${item.name}: ${item.quantity}.`, item);
});

$("#p-shop").addEventListener("click", () => {
  const p = state.product;
  if (!p || !shoppingOk(p)) return;
  addToShopping({ product: p });
  showInfo($("#p-message"), `${p.name} is on the shopping list.`);
});

// ---- Two-factor login (account) -------------------------------------------

async function showSecurity() {
  $("#tf-password").value = "";
  $("#tf-totp-setup").hidden = true;
  $("#tf-codes").hidden = true;
  showInfo($("#tf-message"), "");
  try {
    renderSecurity(await api("GET", "/api/account/security"));
  } catch (err) {
    showError($("#tf-message"), err);
  }
}

function renderSecurity(sec) {
  const parts = [];
  if (sec.totp) parts.push("authenticator app");
  if (sec.passkeys.length) parts.push(plural(sec.passkeys.length, "passkey"));
  $("#tf-status").textContent = sec.enabled
    ? `On: ${parts.join(" and ")}. ${plural(sec.recovery_codes_left, "recovery code")} left.`
    : "Off: the password alone logs in. Add an authenticator app or a passkey to ask for more.";
  $("#tf-totp-start").hidden = sec.totp;
  $("#tf-totp-remove").hidden = !sec.totp;
  $("#tf-codes-new").hidden = !sec.enabled;
  if (sec.totp) $("#tf-totp-setup").hidden = true;

  $("#tf-passkeys").replaceChildren(...sec.passkeys.map((pk) => {
    const remove = el("button", { type: "button", class: "edit linkish", text: "Remove" });
    remove.addEventListener("click", () => securityChange("DELETE", `/api/account/passkeys/${pk.id}`,
      `Remove the passkey "${pk.name}"?`, "Passkey removed."));
    const used = pk.last_used_at ? ` · last used ${stamp(pk.last_used_at)}` : " · not used yet";
    return el("li", {},
      el("div", { class: "head" }, el("span", { class: "word", text: pk.name }), remove),
      el("p", { class: "stamps", text: `added ${stamp(pk.created_at)}${used}` }));
  }));
  const here = passkeysHere(sec.passkey_origin);
  $("#tf-passkey-add-row").hidden = !here;
  const note = $("#tf-passkey-note");
  note.hidden = here;
  if (!sec.passkey_origin) note.textContent = "Passkeys are off: the admin sets BUYBUTLER_PUBLIC_URL to turn them on.";
  else if (!window.PublicKeyCredential || !window.isSecureContext) note.textContent = "This browser can't use passkeys here.";
  else note.textContent = `Passkeys work when Buybutler is opened at ${sec.passkey_origin}.`;

  if (sec.recovery_codes) {
    $("#tf-codes").hidden = false;
    $("#tf-code-list").replaceChildren(...sec.recovery_codes.map((code) => el("li", { text: code })));
    $("#tf-code-list").dataset.codes = sec.recovery_codes.join("\n");
  }
}

function tfPassword() {
  const password = $("#tf-password").value;
  if (!password) {
    $("#tf-password").focus();
    throw new Error("Enter your current password first.");
  }
  return password;
}

async function securityChange(method, path, question, done) {
  const msg = $("#tf-message");
  try {
    const password = tfPassword();
    if (question && !confirm(question)) return;
    const sec = await api(method, path, { password }, { retries: 0 });
    $("#tf-password").value = "";
    renderSecurity(sec);
    showInfo(msg, done);
  } catch (err) {
    showError(msg, err);
  }
}

// The QR encoder is loaded only here. Its script defines the same global as
// the barcode reader, which is put back afterwards.
async function qrImage(text) {
  if (!qrImage.writer) {
    const reader = window.ZXingWASM;
    await new Promise((resolve, reject) => {
      const script = el("script", { src: "/static/vendor/zxing/writer.js" });
      script.addEventListener("load", resolve);
      script.addEventListener("error", () => reject(new Error("The QR code could not be loaded.")));
      document.head.append(script);
    });
    qrImage.writer = window.ZXingWASM;
    window.ZXingWASM = reader;
    qrImage.writer.prepareZXingModule({ overrides: { locateFile: (file) => `/static/vendor/zxing/${file}` } });
  }
  const result = await qrImage.writer.writeBarcode(text, { format: "QRCode", ecLevel: "M" });
  if (result.error) throw new Error(result.error);
  return URL.createObjectURL(new Blob([result.svg], { type: "image/svg+xml" }));
}

$("#tf-totp-start").addEventListener("click", async () => {
  const msg = $("#tf-message");
  showInfo(msg, "");
  try {
    const setup = await api("POST", "/api/account/totp", { password: tfPassword() }, { retries: 0 });
    $("#tf-password").value = "";
    $("#tf-totp-setup").hidden = false;
    $("#tf-secret").textContent = setup.secret.replace(/(.{4})/g, "$1 ").trim();
    $("#tf-code").value = "";
    const img = $("#tf-qr");
    img.hidden = true;
    try {
      img.src = await qrImage(setup.uri);
      img.hidden = false;
    } catch (err) {
      showError(msg, new Error(`${err.message} Type in the key by hand.`));
    }
    $("#tf-code").focus();
  } catch (err) {
    showError(msg, err);
  }
});

$("#tf-totp-confirm").addEventListener("click", async () => {
  const msg = $("#tf-message");
  try {
    const sec = await api("POST", "/api/account/totp/confirm", { code: $("#tf-code").value.trim() }, { retries: 0 });
    renderSecurity(sec);
    showInfo(msg, "Authenticator app added. From now on the login asks for its code.");
  } catch (err) {
    showError(msg, err);
  }
});

$("#tf-totp-remove").addEventListener("click", () => securityChange("DELETE", "/api/account/totp",
  "Remove the authenticator app?", "Authenticator app removed."));

$("#tf-codes-new").addEventListener("click", () => securityChange("POST", "/api/account/recovery-codes",
  "Make new recovery codes? The old ones stop working.", "New recovery codes made; the old ones no longer work."));

$("#tf-passkey-add").addEventListener("click", async () => {
  const msg = $("#tf-message");
  showInfo(msg, "");
  try {
    const start = await api("POST", "/api/account/passkeys/options", { password: tfPassword() }, { retries: 0 });
    $("#tf-password").value = "";
    const credential = await passkeyCreate(start.options);
    const name = $("#tf-passkey-name").value.trim() || "Passkey";
    renderSecurity(await api("POST", "/api/account/passkeys", { ticket: start.ticket, credential, name }, { retries: 0 }));
    $("#tf-passkey-name").value = "";
    showInfo(msg, "Passkey added.");
  } catch (err) {
    showError(msg, passkeyError(err));
  }
});

$("#tf-codes-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#tf-code-list").dataset.codes);
    showInfo($("#tf-message"), "Recovery codes copied.");
  } catch {
    showError($("#tf-message"), new Error("Copying isn't allowed here; write the codes down."));
  }
});

$("#user-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  try {
    const { users } = await adminChange("POST", "/api/users", {
      username: form.username.value.trim(), password: form.password.value, role: form.role.value,
    });
    renderUsers(users);
    showInfo($("#u-message"), `Added ${form.username.value.trim()}. They create their own lists, or an owner adds them to one.`);
    form.reset();
  } catch (err) {
    showError($("#u-message"), err);
  }
});

function renderOutbox(jobs) {
  $("#a-outbox-section").hidden = jobs.length === 0;
  $("#a-outbox").replaceChildren(
    ...[...jobs].sort((a, b) => a.created.localeCompare(b.created)).map((job) => {
      const discard = el("button", { type: "button", class: "edit linkish", text: "Discard" });
      discard.addEventListener("click", async () => {
        if (!confirm(`Discard "${job.label}"? It was not sent.`)) return;
        await Outbox.remove(job.id);
        updateOutboxBadge();
      });
      return el("li", {},
        el("div", { class: "head" }, el("span", { class: "word", text: job.label }), discard),
        el("p", { class: job.error ? "message is-error" : "stamps",
          text: job.error ? `Not sent: ${job.error}` : `Saved ${stamp(job.created)} · waiting for the connection` }));
    }),
  );
}

$("#a-outbox-send").addEventListener("click", async () => {
  // Failed jobs get another try when sent by hand.
  for (const job of await Outbox.all()) {
    if (job.error) {
      job.error = null;
      await Outbox.put(job);
    }
  }
  await syncOutbox();
});

$("#password-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#a-message");
  try {
    await api("POST", "/api/session/password", { current: form.current.value, new: form.new.value });
    form.reset();
    showInfo(msg, "Password changed. Other devices have to log in again.");
  } catch (err) {
    showError(msg, err);
  }
});

$("#logout").addEventListener("click", async () => {
  await api("DELETE", "/api/session").catch(() => {});
  await forgetOffline();
  state.user = null;
  state.lists = null;
  state.offline = false;
  go("#/login");
});

// ---- Scanner --------------------------------------------------------------

const FORMATS = ["EAN13", "EAN8", "UPCA", "UPCE"];
// One camera at a time, for the Scan view or the shopping list.
let camera = null; // { stream, timer, busy, target, last: { code, at } }
// The same barcode only counts again after this pause, so a product held
// in front of the camera isn't added twice.
const SAME_CODE_MS = 3000;

const SCANNERS = {
  scan: {
    video: "#video", start: "#scan-start", stop: "#scan-stop", state: "#scan-state", msg: "#scan-message",
    onHit: async (code) => {
      stopCamera();
      await lookup(code);
    },
  },
  shop: {
    video: "#shop-video", start: "#shop-scan-start", stop: "#shop-scan-stop", state: "#shop-scan-state",
    msg: "#shop-message", frame: "#shop-viewfinder",
    onHit: (code) => shopScanned(code),
  },
};

function scannerReady() {
  if (!window.ZXingWASM) return false;
  if (!scannerReady.prepared) {
    // The decoder is served by Buybutler itself, never from a CDN.
    window.ZXingWASM.prepareZXingModule({
      overrides: { locateFile: (file) => `/static/vendor/zxing/${file}` },
    });
    scannerReady.prepared = true;
  }
  return true;
}

function showScan() {
  if (camera && camera.target !== SCANNERS.scan) stopCamera();
  show("view-scan", "scan");
  showInfo($("#scan-message"), "");
  takeFlash($("#scan-message"));
  $("#add-by-hand").hidden = !canEdit();
  $("#manual-form").reset();
  if (!navigator.mediaDevices || !window.isSecureContext) {
    $("#scan-start").disabled = true;
    $("#scan-state").textContent = "Camera needs HTTPS";
  } else if (!scannerReady()) {
    $("#scan-start").disabled = true;
    $("#scan-state").textContent = "Scanner not available";
  } else {
    $("#scan-start").disabled = false;
    $("#scan-state").textContent = camera && camera.target === SCANNERS.scan ? "Point at a barcode" : "Camera off";
  }
}

async function startCamera(target = SCANNERS.scan) {
  if (camera && camera.target === target) return;
  stopCamera();
  const msg = $(target.msg);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
  } catch (err) {
    showError(msg, new Error("No camera access. Allow it in the browser settings, or type the barcode."));
    return;
  }
  if (camera) stopCamera(); // a second tap while the first was waiting for access
  const video = $(target.video);
  if (target.frame) $(target.frame).hidden = false;
  video.srcObject = stream;
  await video.play().catch(() => {});
  camera = { stream, busy: false, timer: null, target, last: null };
  $(target.start).hidden = true;
  $(target.stop).hidden = false;
  $(target.state).textContent = "Point at a barcode";

  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  camera.timer = setInterval(async () => {
    if (!camera || camera.busy || video.readyState < 2) return;
    camera.busy = true;
    try {
      // Only the band behind the aiming frame is decoded: faster, and a
      // second barcode elsewhere in the picture doesn't get in the way.
      const w = video.videoWidth;
      const h = video.videoHeight;
      const bandH = Math.round(h * 0.4);
      canvas.width = w;
      canvas.height = bandH;
      ctx.drawImage(video, 0, Math.round((h - bandH) / 2), w, bandH, 0, 0, w, bandH);
      const results = await window.ZXingWASM.readBarcodes(ctx.getImageData(0, 0, w, bandH), {
        formats: FORMATS,
        tryHarder: true,
        maxNumberOfSymbols: 1,
      });
      const hit = results.find((r) => r.isValid && /^\d{8,14}$/.test(r.text));
      if (hit && camera) {
        const now = Date.now();
        const again = camera.last && camera.last.code === hit.text && now - camera.last.at < SAME_CODE_MS;
        camera.last = { code: hit.text, at: now };
        if (!again) {
          if (navigator.vibrate) navigator.vibrate(60);
          await target.onHit(hit.text);
        }
      }
    } catch (err) {
      console.error(err);
    } finally {
      if (camera) camera.busy = false;
    }
  }, 200);
}

function stopCamera() {
  if (!camera) return;
  const { target } = camera;
  clearInterval(camera.timer);
  for (const t of camera.stream.getTracks()) t.stop();
  camera = null;
  $(target.video).srcObject = null;
  if (target.frame) $(target.frame).hidden = true;
  $(target.start).hidden = false;
  $(target.stop).hidden = true;
  $(target.state).textContent = "Camera off";
}

$("#scan-start").addEventListener("click", () => {
  showInfo($("#scan-message"), "");
  startCamera(SCANNERS.scan);
});
$("#scan-stop").addEventListener("click", stopCamera);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopCamera();
});

$("#manual-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  await lookup(ev.currentTarget.barcode.value.trim());
});

async function lookup(barcode) {
  const msg = $("#scan-message");
  // A rated product of this list shows at once from the copy; its page
  // fetches the rest when there is a connection.
  const known = await Known.byBarcode(barcode);
  if (known) {
    go(`#/product/${known.id}`);
    return;
  }
  showInfo(msg, `Looking up ${barcode}`);
  try {
    const r = await api("GET", `/api/lookup?barcode=${encodeURIComponent(barcode)}`);
    if (r.product) {
      go(`#/product/${r.product.id}`);
      return;
    }
    if (!canEdit()) {
      showInfo(msg, `${barcode} is not in ${currentList().name}, and the list is read only for you.`);
      return;
    }
    state.suggestion = r.suggestion ? { ...r.suggestion, lookup: r.lookup } : { barcode, lookup: r.lookup };
    go(`#/new?barcode=${encodeURIComponent(barcode)}`);
  } catch (err) {
    if (err.offline && canEdit()) {
      // Not in the copy: not rated yet in this list, or never seen.
      state.suggestion = { barcode, lookup: "offline" };
      go(`#/new?barcode=${encodeURIComponent(barcode)}`);
      return;
    }
    showError(msg, err);
  }
}

// ---- Product view ---------------------------------------------------------

async function showProduct(id) {
  const known = await Known.byId(id);
  // Started offline, the bar at the top says so already.
  if (known) renderProduct({ ...known, ratings: null, prices: null }, "");
  if (state.offline && known) return;
  let p;
  try {
    p = await api("GET", `/api/products/${id}`, undefined, known ? { retries: 1 } : {});
  } catch (err) {
    if (err.offline && known) {
      showInfo($("#p-message"), offlineNote());
      return;
    }
    throw err;
  }
  if (location.hash.split("?")[0] !== `#/product/${id}`) return; // the user moved on meanwhile
  renderProduct(p, null);
}

// ratings and prices are null for a product from the offline copy: only the
// newest verdict and the price summary are kept on the phone.
function renderProduct(p, note) {
  state.product = p;
  show("view-product", "lists");
  showInfo($("#p-message"), note || "");
  takeFlash($("#p-message"));
  // Viewers see everything, but no way to change it.
  const editable = p.role !== "viewer";
  for (const sel of ["#p-rate", "#p-edit", "#p-add-price", "#p-delete"]) $(sel).hidden = !editable;
  // From the offline copy: new ratings and prices wait in the outbox, but
  // changing or deleting the product needs the connection.
  if (p.ratings === null) for (const sel of ["#p-edit", "#p-delete"]) $(sel).hidden = true;
  $("#p-shop").hidden = !editable;
  if (!editable && !$("#p-message").textContent) showInfo($("#p-message"), "This list is read only for you.");

  const box = $("#p-verdict");
  box.className = "verdict";
  box.replaceChildren();
  if (p.rating) {
    box.classList.add(p.rating.buy_again ? "verdict--buy" : "verdict--skip");
    const detail = [p.rating.rated_on, p.rating.store].filter(Boolean).join(" · ");
    box.append(
      el("i", { class: "mark" }),
      el("div", {},
        el("p", { class: "verdict-word", text: p.rating.buy_again ? "Buy again" : "Don't buy again" }),
        stars(p.rating.stars),
        p.rating.reason ? el("p", { class: "verdict-detail", text: p.rating.reason }) : null,
        el("p", { class: "verdict-detail", text: detail })),
    );
  } else {
    box.append(el("i", { class: "mark" }), el("p", { class: "verdict-word", text: "Not rated yet" }));
  }

  const img = $("#p-image");
  img.hidden = !p.image;
  // Offline, a picture that was never viewed isn't on the phone.
  img.onerror = () => { img.hidden = true; };
  if (p.image) img.src = p.image;
  $("#p-name").textContent = p.name;
  $("#p-meta").textContent = [p.brand, p.quantity, p.category, p.barcode].filter(Boolean).join(" · ");
  $("#p-rate").href = `#/product/${p.id}/rate`;
  $("#p-rate").textContent = p.rating ? "Rate again" : "Rate";
  $("#p-edit").href = `#/product/${p.id}/edit`;

  const summary = $("#p-price-summary");
  summary.replaceChildren();
  if (p.price) {
    const s = p.price;
    summary.append(el("p", { class: "price-last" }, el("strong", { text: money(s.last.amount_cents) }),
      document.createTextNode(s.last.unit_price ? ` · ${money(s.last.unit_price.amount_cents)} per ${s.last.unit_price.per}` : ""),
      s.last.on_offer ? el("span", { class: "tag", text: "On offer" }) : null));
    const range = [];
    const value = (pr) => (pr.unit_price ? `${money(pr.unit_price.amount_cents)}/${pr.unit_price.per}` : money(pr.amount_cents));
    if (s.lowest && s.highest && s.lowest.id !== s.highest.id) {
      range.push(`Lowest ${value(s.lowest)} · highest ${value(s.highest)}`);
    }
    if (range.length) summary.append(el("p", { class: "meta", text: `${range.join("")} (offers left out)` }));
  } else {
    summary.append(el("p", { class: "meta", text: "No prices yet." }));
  }
  $("#p-prices").replaceChildren(
    ...(p.prices || []).map((pr) =>
      el("li", {},
        el("div", { class: "head" },
          el("span", { class: "word", text: priceText(pr) }),
          pr.on_offer ? el("span", { class: "tag", text: "On offer" }) : null,
          editable ? el("a", { class: "edit", href: `#/product/${p.id}/price/${pr.id}`, text: "Edit" }) : null),
        el("p", {
          class: "stamps",
          text: [pr.bought_on, pr.store, pr.user && `by ${pr.user}`, `entered ${stamp(pr.created_at)}`,
            pr.updated_at !== pr.created_at ? `changed ${stamp(pr.updated_at)}` : null].filter(Boolean).join(" · "),
        }))),
  );
  $("#p-add-price").href = `#/product/${p.id}/price`;

  const list = $("#p-history");
  list.replaceChildren(
    ...(p.ratings || []).map((r) =>
      el("li", {},
        el("div", { class: "head" }, verdictWord(r.buy_again), stars(r.stars),
          editable ? el("a", { class: "edit", href: `#/product/${p.id}/rating/${r.id}`, text: "Edit" }) : null),
        r.reason ? el("p", { class: "reason", text: r.reason }) : null,
        r.photo ? el("img", { src: r.photo, alt: "Photo of this rating", loading: "lazy" }) : null,
        el("p", {
          class: "stamps",
          text: [r.rated_on, r.store, r.user && `by ${r.user}`,
            `entered ${stamp(r.created_at)}`,
            r.updated_at !== r.created_at ? `changed ${stamp(r.updated_at)}` : null]
            .filter(Boolean).join(" · "),
        }),
      )),
  );
  if (!p.ratings) list.append(el("li", { class: "meta", text: "The history is loaded with a connection." }));
  else if (!p.ratings.length) list.append(el("li", { class: "meta", text: "No ratings yet." }));

  $("#p-stamps").textContent = `Added ${stamp(p.created_at)} · changed ${stamp(p.updated_at)}`
    + (p.source === "openfoodfacts" ? " · data from Open Food Facts" : "");
}

$("#p-delete").addEventListener("click", async () => {
  const p = state.product;
  if (!p || !confirm(`Delete ${p.name} and all its ratings?`)) return;
  try {
    await api("DELETE", `/api/products/${p.id}`);
    go("#/lists");
  } catch (err) {
    showError($("#p-message"), err);
  }
});

// ---- Product form -----------------------------------------------------------

async function showProductForm(id, barcode) {
  await loadFacets();
  const form = $("#product-form");
  form.reset();
  showInfo($("#pf-message"), "");
  const source = $("#pf-source");
  showInfo(source, "");
  $("#pf-image-choice").hidden = true;

  if (id) {
    const p = await api("GET", `/api/products/${id}`);
    state.product = p;
    show("view-product-form", "lists");
    $("#pf-title").textContent = "Edit product";
    $("#pf-submit").textContent = "Save";
    $("#pf-cancel").href = `#/product/${id}`;
    for (const k of ["name", "brand", "category", "quantity", "barcode"]) form[k].value = p[k] || "";
    form.dataset.id = id;
    delete form.dataset.created;
    return;
  }

  show("view-product-form", "scan");
  delete form.dataset.id;
  delete form.dataset.created;
  $("#pf-title").textContent = "New product";
  const toShop = Boolean(barcode) && state.shopAdd === barcode;
  $("#pf-submit").textContent = toShop ? "Save and add to shopping" : "Save and rate";
  $("#pf-cancel").href = toShop ? "#/shop" : "#/scan";
  const s = state.suggestion && state.suggestion.barcode === barcode ? state.suggestion : null;
  form.barcode.value = barcode || "";
  if (s && s.lookup === "found") {
    for (const k of ["name", "brand", "category", "quantity"]) form[k].value = s[k] || "";
    $("#pf-image-choice").hidden = !s.has_image;
    showInfo(source, "Filled in from Open Food Facts. Check the name before saving.");
  } else if (s) {
    const why = {
      not_found: "Open Food Facts doesn't know this barcode. Enter the product by hand.",
      disabled: "The lookup is switched off. Enter the product by hand.",
      failed: "Open Food Facts can't be reached right now. Enter the product by hand.",
      offline: "No connection, so the barcode can't be looked up. Enter the product by hand; "
        + "it is saved on this phone and sent later. If it is known already, the rating goes there.",
    }[s.lookup];
    if (why) showInfo(source, why);
  }
  form.name.focus();
}

$("#product-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#pf-message");
  const body = {
    name: form.name.value,
    brand: form.brand.value,
    category: form.category.value,
    quantity: form.quantity.value,
    barcode: form.barcode.value.trim() || null,
  };
  const file = form.image.files[0];
  const toShop = !form.dataset.id && Boolean(body.barcode) && state.shopAdd === body.barcode;
  let job = null;
  try {
    if (form.dataset.id) {
      let p = await api("PATCH", `/api/products/${form.dataset.id}`, { ...body, barcode: body.barcode || "" });
      if (file) {
        showInfo(msg, "Uploading the picture");
        p = await api("PUT", `/api/products/${p.id}/image`, await shrink(file));
      }
      go(`#/product/${p.id}`);
      return;
    }
    body.use_suggested_image = !$("#pf-image-choice").hidden && form.use_suggested_image.checked;
    body.client_id = uid();
    // Scanned on the shopping list: it goes on the list in the same job,
    // before the picture, so a failed upload doesn't keep it off.
    const shopId = toShop ? uid() : null;
    job = newJob(`New product: ${body.name}`, [
      // The list is fixed when the product is saved, not when it is sent.
      { ref: "product", method: "POST", path: "/api/products", body, list: state.listId },
      toShop && {
        ref: "shop", method: "POST", path: "/api/shopping", body: { client_id: shopId, quantity: "" },
        fill: { product_id: "product" }, list: state.listId,
        local: shopItemLocal(shopId, { product: { name: body.name, brand: body.brand, category: body.category } }),
      },
      file && (await blobStep("image", "/api/products/{product}/image", await shrink(file))),
    ]);
    if (toShop) {
      showInfo(msg, "Saving");
      const result = await submitJob(job);
      state.shopAdd = null;
      state.suggestion = null;
      flash(result === "sent" ? `${body.name} is on the shopping list.`
        : `No connection. ${body.name} and its place on the shopping list are saved on this phone and sent later.`);
      go("#/shop");
      return;
    }
    showInfo(msg, "Saving");
    // Held from the start: if it ends up queued, its rating is added to the
    // same job before anything is sent.
    state.holdJob = job.id;
    const result = await submitJob(job);
    state.suggestion = null;
    if (result === "sent") {
      state.holdJob = null;
      go(`#/product/${job.steps[0].id}/rate`);
    } else {
      flash("No connection. The product is saved on this phone; rate it now, both are sent later.");
      go(`#/pending/${job.id}`);
    }
  } catch (err) {
    state.holdJob = null;
    if (err.partial) {
      flash(`The product was saved, but not the picture: ${err.message}`, true);
      if (toShop) state.shopAdd = null;
      go(toShop ? "#/shop" : `#/product/${job.steps[0].id}/rate`);
      return;
    }
    showError(msg, err);
  }
});

// ---- Rating form ------------------------------------------------------------

let chosenStars = null;

function renderStars() {
  const row = $("#r-stars");
  row.replaceChildren(
    ...[1, 2, 3, 4, 5].map((n) => {
      const b = el("button", {
        type: "button",
        class: chosenStars && n <= chosenStars ? "is-on" : "",
        "aria-label": `${n} of 5 stars`,
        "aria-pressed": String(chosenStars === n),
        text: String(n),
      });
      // A second tap on the chosen value clears it: stars are optional.
      b.addEventListener("click", () => {
        chosenStars = chosenStars === n ? null : n;
        renderStars();
      });
      return b;
    }),
  );
  $("#r-stars-value").textContent = chosenStars ? `${chosenStars} of 5` : "none";
}

async function showRateForm(productId, ratingId, pendingJobId = null) {
  let p;
  const form = $("#rate-form");
  delete form.dataset.pending;
  if (pendingJobId) {
    // The product exists only in the outbox so far.
    const job = await Outbox.get(pendingJobId).catch(() => null);
    if (!job) {
      const sent = state.sentProducts[pendingJobId];
      if (sent) return go(`#/product/${sent}/rate`);
      return go("#/scan");
    }
    state.holdJob = job.id;
    const body = job.steps[0].body;
    p = { id: null, name: body.name, quantity: body.quantity, price: null, rating: null, ratings: [] };
    form.dataset.pending = job.id;
  } else {
    await loadFacets();
    try {
      p = await api("GET", `/api/products/${productId}`, undefined, state.offline ? { retries: 0 } : {});
    } catch (err) {
      // A new rating also works offline; it waits in the outbox.
      const known = err.offline && !ratingId ? await Known.byId(productId) : null;
      if (!known) throw err;
      p = { ...known, ratings: [] };
    }
  }
  state.product = p;
  show("view-rate", "scan");
  form.reset();
  showInfo($("#r-message"), "");
  takeFlash($("#r-message"));
  $("#r-product").textContent = p.name;
  $("#r-cancel").href = p.id ? `#/product/${p.id}` : "#/scan";
  form.dataset.product = p.id || "";

  fillPriceFields(form, null);
  // Prices of an existing rating are edited in the price list.
  $("#r-price-fields").hidden = !!ratingId;
  if (!ratingId) suggestQuantity(form, p);

  const rating = ratingId ? p.ratings.find((r) => r.id === ratingId) : null;
  if (ratingId && !rating) throw new ApiError(404, "rating not found");
  if (rating) {
    $("#r-title").textContent = "Edit rating";
    form.dataset.rating = rating.id;
    form.querySelector(`[name=buy_again][value="${rating.buy_again ? 1 : 0}"]`).checked = true;
    form.reason.value = rating.reason;
    form.store.value = rating.store;
    form.rated_on.value = rating.rated_on;
    chosenStars = rating.stars;
    $("#r-delete").hidden = false;
  } else {
    $("#r-title").textContent = p.rating ? "Rate again" : "Rate";
    delete form.dataset.rating;
    form.rated_on.value = today();
    // The store of the last rating is the likeliest one.
    if (p.rating) form.store.value = p.rating.store;
    chosenStars = null;
    $("#r-delete").hidden = true;
  }
  renderStars();
}

$("#rate-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#r-message");
  const choice = form.querySelector("[name=buy_again]:checked");
  if (!choice) {
    showError(msg, new Error("Choose Buy again or Don't buy again."));
    return;
  }
  const body = {
    buy_again: choice.value === "1",
    stars: chosenStars,
    reason: form.reason.value,
    store: form.store.value,
    rated_on: form.rated_on.value || today(),
  };
  const file = form.photo.files[0];
  try {
    if (form.dataset.rating) {
      if (chosenStars === null) body.clear_stars = true;
      const r = await api("PATCH", `/api/ratings/${form.dataset.rating}`, body);
      if (file) {
        showInfo(msg, "Uploading the photo");
        await api("PUT", `/api/ratings/${r.id}/photo`, await shrink(file));
      }
      go(`#/product/${form.dataset.product}`);
      return;
    }
    const price = readPrice(form);
    if (price) body.price = price;
    body.client_id = uid();
    const photo = file && (await blobStep("photo", "/api/ratings/{rating}/photo", await shrink(file)));

    if (form.dataset.pending) {
      // Added to the product's job; both go out together, later.
      const job = await Outbox.get(form.dataset.pending);
      if (job) {
        job.steps.push({ ref: "rating", method: "POST", path: "/api/products/{product}/ratings", body });
        if (photo) job.steps.push(photo);
        job.label = `${job.label}, rated`;
        await Outbox.put(job);
        state.holdJob = null;
        syncOutbox();
        flash(QUEUED);
        go("#/scan");
        return;
      }
    }

    showInfo(msg, "Saving");
    const job = newJob(`Rating: ${state.product.name}`, [
      { ref: "rating", method: "POST", path: `/api/products/${form.dataset.product}/ratings`, body },
      photo,
    ]);
    if ((await submitJob(job)) === "queued") {
      flash(QUEUED);
      go("#/scan");
      return;
    }
    go(`#/product/${form.dataset.product}`);
  } catch (err) {
    if (err.partial) {
      flash(`The rating was saved, but not the photo: ${err.message}`, true);
      go(`#/product/${form.dataset.product}`);
      return;
    }
    showError(msg, err);
  }
});

$("#r-delete").addEventListener("click", async () => {
  const form = $("#rate-form");
  if (!form.dataset.rating || !confirm("Delete this rating?")) return;
  try {
    await api("DELETE", `/api/ratings/${form.dataset.rating}`);
    go(`#/product/${form.dataset.product}`);
  } catch (err) {
    showError($("#r-message"), err);
  }
});

// ---- Price form -------------------------------------------------------------

async function showPriceForm(productId, priceId) {
  await loadFacets();
  let p;
  try {
    p = await api("GET", `/api/products/${productId}`, undefined, state.offline ? { retries: 0 } : {});
  } catch (err) {
    // A new price also works offline; it waits in the outbox.
    const known = err.offline && !priceId ? await Known.byId(productId) : null;
    if (!known) throw err;
    p = { ...known, prices: [] };
  }
  state.product = p;
  show("view-price", "lists");
  const form = $("#price-form");
  form.reset();
  showInfo($("#pr-message"), "");
  $("#pr-product").textContent = p.name;
  $("#pr-cancel").href = `#/product/${p.id}`;
  form.dataset.product = p.id;
  const pr = priceId ? (p.prices || []).find((x) => x.id === priceId) : null;
  if (priceId && !pr) throw new ApiError(404, "price not found");
  if (pr) {
    $("#pr-title").textContent = "Edit price";
    form.dataset.price = pr.id;
    fillPriceFields(form, pr);
    form.store.value = pr.store;
    form.bought_on.value = pr.bought_on;
    $("#pr-delete").hidden = false;
  } else {
    $("#pr-title").textContent = "Add a price";
    delete form.dataset.price;
    fillPriceFields(form, null);
    suggestQuantity(form, p);
    if (p.price) form.store.value = p.price.last.store;
    form.bought_on.value = today();
    $("#pr-delete").hidden = true;
  }
  form.price.focus();
}

$("#price-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#pr-message");
  try {
    const price = readPrice(form);
    if (!price) throw new Error("Enter the price.");
    const body = { ...price, store: form.store.value, bought_on: form.bought_on.value || today() };
    if (form.dataset.price) {
      if (body.quantity === null) {
        delete body.quantity;
        delete body.unit;
        body.clear_quantity = true;
      }
      await api("PATCH", `/api/prices/${form.dataset.price}`, body);
    } else {
      body.client_id = uid();
      const job = newJob(`Price: ${state.product.name}`, [
        { ref: "price", method: "POST", path: `/api/products/${form.dataset.product}/prices`, body },
      ]);
      if ((await submitJob(job)) === "queued") {
        flash(QUEUED);
        go("#/scan");
        return;
      }
    }
    go(`#/product/${form.dataset.product}`);
  } catch (err) {
    showError(msg, err);
  }
});

$("#pr-delete").addEventListener("click", async () => {
  const form = $("#price-form");
  if (!form.dataset.price || !confirm("Delete this price?")) return;
  try {
    await api("DELETE", `/api/prices/${form.dataset.price}`);
    go(`#/product/${form.dataset.product}`);
  } catch (err) {
    showError($("#pr-message"), err);
  }
});

// Phones take photos of several megabytes; they are scaled to at most
// 1600 px and sent as JPEG. If the browser can't decode the file, it goes
// as it is and the server decides.
async function shrink(file, max = 1600) {
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    return blob || file;
  } catch {
    return file;
  }
}

// ---- Lists ------------------------------------------------------------------

async function showLists(params) {
  show("view-lists", "lists");
  if (params.has("verdict")) state.verdict = params.get("verdict");
  await loadFacets();
  const form = $("#filter-form");
  const refill = (select, values, label) => {
    const current = select.value;
    select.replaceChildren(el("option", { value: "", text: label }), ...values.map((v) => el("option", { value: v, text: v })));
    if (values.includes(current)) select.value = current;
  };
  refill(form.store, state.facets.stores, "Every store");
  refill(form.category, state.facets.categories, "Every category");
  await loadList();
}

async function loadList() {
  const form = $("#filter-form");
  for (const b of document.querySelectorAll(".tabs button")) {
    b.setAttribute("aria-selected", String(b.dataset.verdict === state.verdict));
  }
  const q = new URLSearchParams({
    verdict: state.verdict,
    q: form.q.value,
    store: form.store.value,
    category: form.category.value,
    sort: form.sort.value,
  });
  let products;
  let note = "";
  try {
    if (state.offline) throw new NetworkError();
    ({ products } = await api("GET", `/api/products?${q}`));
  } catch (err) {
    const copy = err.offline ? await Known.load() : null;
    if (!copy) throw err;
    products = filterKnown(copy.products, Object.fromEntries(q));
    note = ` · ${offlineNote()}`;
  }
  $("#l-count").textContent = `${products.length} ${products.length === 1 ? "product" : "products"}${note}`;
  $("#l-items").replaceChildren(
    ...products.map((p) =>
      el("li", {},
        el("a", { href: `#/product/${p.id}` },
          p.image ? el("img", { src: p.image, alt: "", loading: "lazy" }) : el("span", { class: "no-image" }),
          el("span", { class: "text" },
            el("span", { class: "name", text: p.name }),
            el("span", { class: "sub" },
              p.rating ? verdictWord(p.rating.buy_again) : el("span", { class: "word", text: "Not rated" }),
              p.rating ? stars(p.rating.stars) : null,
              el("span", { text: [p.brand, p.rating && p.rating.store].filter(Boolean).join(" · ") }),
              p.price ? el("span", { class: "list-price", text: money(p.price.last.amount_cents) }) : null),
            p.rating && p.rating.reason ? el("span", { class: "why", text: p.rating.reason }) : null)))),
  );
}

// The server's filters, applied to the offline copy (rated products only).
function filterKnown(products, f) {
  const q = f.q.trim().toLowerCase();
  const found = products.filter((p) => {
    const r = p.rating;
    if (f.verdict === "unrated") return false;
    if (f.verdict === "buy" && !r.buy_again) return false;
    if (f.verdict === "skip" && r.buy_again) return false;
    if (q && ![p.name, p.brand, p.category, p.barcode, r.reason].some((v) => (v || "").toLowerCase().includes(q))) return false;
    if (f.store && (r.store || "").toLowerCase() !== f.store.toLowerCase()) return false;
    if (f.category && (p.category || "").toLowerCase() !== f.category.toLowerCase()) return false;
    return true;
  });
  const byName = (a, b) => a.name.localeCompare(b.name);
  const last = (p) => (p.price ? p.price.last.amount_cents : Infinity);
  const sorts = {
    recent: (a, b) => b.rating.rated_on.localeCompare(a.rating.rated_on) || b.rating.created_at.localeCompare(a.rating.created_at),
    name: byName,
    stars: (a, b) => (b.rating.stars || 0) - (a.rating.stars || 0) || byName(a, b),
    price: (a, b) => last(a) - last(b) || byName(a, b),
  };
  return found.sort(sorts[f.sort] || sorts.recent);
}

for (const b of document.querySelectorAll(".tabs button")) {
  b.addEventListener("click", () => {
    state.verdict = b.dataset.verdict;
    loadList();
  });
}

let searchTimer;
$("#filter-form").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadList, 250);
});
$("#filter-form").addEventListener("submit", (ev) => ev.preventDefault());

route();
