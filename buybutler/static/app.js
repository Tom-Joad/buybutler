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
  lists: null, // the user's lists with their role; null until loaded
  listId: null, // the list everything happens in (sent as a header)
  sentProducts: {}, // outbox job id -> product id, once its product was created
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
  if (res.status === 401 && path !== "/api/session") {
    state.user = null;
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

const Outbox = (() => {
  let opening = null;
  const open = () => {
    opening = opening || new Promise((resolve, reject) => {
      const req = indexedDB.open("buybutler", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("jobs", { keyPath: "id" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return opening;
  };
  const run = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("jobs", mode);
      const req = fn(tx.objectStore("jobs"));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  };
  return {
    all: () => run("readonly", (s) => s.getAll()),
    get: (id) => run("readonly", (s) => s.get(id)),
    put: (job) => run("readwrite", (s) => s.put(job)),
    remove: (id) => run("readwrite", (s) => s.delete(id)),
  };
})();

// Pictures are stored as bytes: older Safari versions can't keep a Blob in
// IndexedDB.
async function blobStep(ref, path, blob) {
  return { ref, method: "PUT", path, file: { type: blob.type || "image/jpeg", bytes: await blob.arrayBuffer() } };
}

function newJob(label, steps) {
  return { id: uid(), label, created: new Date().toISOString(), steps: steps.filter(Boolean), error: null };
}

function fillPath(path, job) {
  return path.replace(/\{(\w+)\}/g, (_, ref) => {
    const step = job.steps.find((s) => s.ref === ref);
    if (!step || !step.done) throw new Error(`step ${ref} not sent yet`);
    return String(step.id);
  });
}

async function runJob(job, retries) {
  for (const step of job.steps) {
    if (step.done) continue;
    const path = fillPath(step.path, job);
    const body = step.file ? new Blob([step.file.bytes], { type: step.file.type }) : step.body;
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

window.addEventListener("online", syncOutbox);
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

  if (parts[0] !== "scan") stopCamera();
  // A product saved offline waits for its rating only while that form is open.
  if (parts[0] !== "pending") state.holdJob = null;

  if (!state.user && parts[0] !== "login") {
    try {
      state.user = await api("GET", "/api/session");
    } catch (err) {
      if (err.offline) {
        show("view-scan", "scan");
        showError($("#scan-message"), err);
        return;
      }
      go("#/login");
      return;
    }
  }
  if (state.user && state.lists === null) {
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

function showLogin() {
  show("view-login");
  $("#login-form [name=username]").focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#login-message");
  showInfo(msg, "");
  try {
    state.user = await api("POST", "/api/session", {
      username: form.username.value,
      password: form.password.value,
    });
    state.lists = null;
    form.password.value = "";
    go("#/scan");
  } catch (err) {
    showError(msg, err);
  }
});

function showAccount() {
  show("view-account", "account");
  $("#a-user").textContent = `Logged in as ${state.user.username} (${state.user.role})`;
  $("#password-form").reset();
  showInfo($("#a-message"), "");
  updateOutboxBadge();
  $("#a-users-section").hidden = state.user.role !== "admin";
  if (state.user.role === "admin") loadUsers().catch((err) => showError($("#u-message"), err));
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
          renderUsers((await api("PATCH", `/api/users/${u.id}`, { role: role.value })).users);
        } catch (err) {
          role.value = u.role;
          showError($("#u-message"), err);
        }
      });
      actions.append(role);
      const reset = el("button", { type: "button", class: "edit linkish", text: "New password" });
      reset.addEventListener("click", async () => {
        const password = prompt(`New password for ${u.username} (at least 10 characters). Their sessions end.`);
        if (!password) return;
        try {
          await api("POST", `/api/users/${u.id}/password`, { password });
          showInfo($("#u-message"), `Password of ${u.username} changed.`);
        } catch (err) {
          showError($("#u-message"), err);
        }
      });
      actions.append(reset);
      if (!me) {
        const remove = el("button", { type: "button", class: "edit linkish", text: "Delete" });
        remove.addEventListener("click", async () => {
          if (!confirm(`Delete ${u.username}? Lists only they use are deleted too.`)) return;
          try {
            await api("DELETE", `/api/users/${u.id}`);
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
        el("p", { class: "stamps", text: `${plural(u.lists, "list")} · created ${stamp(u.created_at)}` }));
    }),
  );
}

$("#user-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  try {
    const { users } = await api("POST", "/api/users", {
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
  state.user = null;
  state.lists = null;
  go("#/login");
});

// ---- Scanner --------------------------------------------------------------

const FORMATS = ["EAN13", "EAN8", "UPCA", "UPCE"];
let camera = null; // { stream, timer }

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
    $("#scan-state").textContent = camera ? "Point at a barcode" : "Camera off";
  }
}

async function startCamera() {
  if (camera) return;
  const msg = $("#scan-message");
  showInfo(msg, "");
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
  const video = $("#video");
  video.srcObject = stream;
  await video.play().catch(() => {});
  camera = { stream, busy: false, timer: null };
  $("#scan-start").hidden = true;
  $("#scan-stop").hidden = false;
  $("#scan-state").textContent = "Point at a barcode";

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
        if (navigator.vibrate) navigator.vibrate(60);
        stopCamera();
        await lookup(hit.text);
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
  clearInterval(camera.timer);
  for (const t of camera.stream.getTracks()) t.stop();
  camera = null;
  $("#video").srcObject = null;
  $("#scan-start").hidden = false;
  $("#scan-stop").hidden = true;
  $("#scan-state").textContent = "Camera off";
}

$("#scan-start").addEventListener("click", startCamera);
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
      state.suggestion = { barcode, lookup: "offline" };
      go(`#/new?barcode=${encodeURIComponent(barcode)}`);
      return;
    }
    showError(msg, err);
  }
}

// ---- Product view ---------------------------------------------------------

async function showProduct(id) {
  const p = await api("GET", `/api/products/${id}`);
  state.product = p;
  show("view-product", "lists");
  showInfo($("#p-message"), "");
  takeFlash($("#p-message"));
  // Viewers see everything, but no way to change it.
  const editable = p.role !== "viewer";
  for (const sel of ["#p-rate", "#p-edit", "#p-add-price", "#p-delete"]) $(sel).hidden = !editable;
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
    ...p.ratings.map((r) =>
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
  if (!p.ratings.length) list.append(el("li", { class: "meta", text: "No ratings yet." }));

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
  $("#pf-submit").textContent = "Save and rate";
  $("#pf-cancel").href = "#/scan";
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
    job = newJob(`New product: ${body.name}`, [
      // The list is fixed when the product is saved, not when it is sent.
      { ref: "product", method: "POST", path: "/api/products", body, list: state.listId },
      file && (await blobStep("image", "/api/products/{product}/image", await shrink(file))),
    ]);
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
      go(`#/product/${job.steps[0].id}/rate`);
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
    p = await api("GET", `/api/products/${productId}`);
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
  const p = await api("GET", `/api/products/${productId}`);
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
  const { products } = await api("GET", `/api/products?${q}`);
  $("#l-count").textContent = `${products.length} ${products.length === 1 ? "product" : "products"}`;
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
