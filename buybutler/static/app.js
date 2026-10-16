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
};

// ---- API ----------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body, raw = false) {
  const headers = { "X-Buybutler": "1" };
  let payload;
  if (body instanceof Blob) {
    payload = body;
    headers["Content-Type"] = body.type || "application/octet-stream";
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
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
  return raw ? res : data;
}

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

  if (!state.user && parts[0] !== "login") {
    try {
      state.user = await api("GET", "/api/session");
    } catch {
      go("#/login");
      return;
    }
  }

  try {
    switch (parts[0]) {
      case "login":
        return showLogin();
      case "lists":
        return await showLists(params);
      case "account":
        return showAccount();
      case "new":
        return await showProductForm(null, params.get("barcode"));
      case "product": {
        const id = Number(parts[1]);
        if (parts[2] === "edit") return await showProductForm(id);
        if (parts[2] === "rate") return await showRateForm(id, null);
        if (parts[2] === "rating") return await showRateForm(id, Number(parts[3]));
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
  state.facets = await api("GET", "/api/facets");
  const fill = (list, values) => {
    list.replaceChildren(...values.map((v) => el("option", { value: v })));
  };
  fill($("#stores"), state.facets.stores);
  fill($("#categories"), state.facets.categories);
}

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
}

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
    state.suggestion = r.suggestion ? { ...r.suggestion, lookup: r.lookup } : { barcode, lookup: r.lookup };
    go(`#/new?barcode=${encodeURIComponent(barcode)}`);
  } catch (err) {
    showError(msg, err);
  }
}

// ---- Product view ---------------------------------------------------------

async function showProduct(id) {
  const p = await api("GET", `/api/products/${id}`);
  state.product = p;
  show("view-product", "lists");

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

  const list = $("#p-history");
  list.replaceChildren(
    ...p.ratings.map((r) =>
      el("li", {},
        el("div", { class: "head" }, verdictWord(r.buy_again), stars(r.stars),
          el("a", { class: "edit", href: `#/product/${p.id}/rating/${r.id}`, text: "Edit" })),
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
  await api("DELETE", `/api/products/${p.id}`);
  go("#/lists");
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
  try {
    let p;
    if (form.dataset.id) {
      p = await api("PATCH", `/api/products/${form.dataset.id}`, { ...body, barcode: body.barcode || "" });
    } else {
      body.use_suggested_image = !$("#pf-image-choice").hidden && form.use_suggested_image.checked;
      showInfo(msg, "Saving");
      p = await api("POST", "/api/products", body);
      // From here on the form edits the new product, so a failed picture
      // upload followed by another Save doesn't create it twice.
      form.dataset.id = p.id;
      form.dataset.created = "1";
    }
    const file = form.image.files[0];
    if (file) {
      showInfo(msg, "Uploading the picture");
      p = await api("PUT", `/api/products/${p.id}/image`, await shrink(file));
    }
    state.suggestion = null;
    go(form.dataset.created ? `#/product/${p.id}/rate` : `#/product/${p.id}`);
  } catch (err) {
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

async function showRateForm(productId, ratingId) {
  await loadFacets();
  const p = await api("GET", `/api/products/${productId}`);
  state.product = p;
  show("view-rate", "scan");
  const form = $("#rate-form");
  form.reset();
  showInfo($("#r-message"), "");
  $("#r-product").textContent = p.name;
  $("#r-cancel").href = `#/product/${p.id}`;
  form.dataset.product = p.id;

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
  try {
    let r;
    if (form.dataset.rating) {
      if (chosenStars === null) body.clear_stars = true;
      r = await api("PATCH", `/api/ratings/${form.dataset.rating}`, body);
    } else {
      r = await api("POST", `/api/products/${form.dataset.product}/ratings`, body);
      // A failed photo upload followed by another Save edits this rating
      // instead of adding a second one.
      form.dataset.rating = r.id;
    }
    const file = form.photo.files[0];
    if (file) {
      showInfo(msg, "Uploading the photo");
      await api("PUT", `/api/ratings/${r.id}/photo`, await shrink(file));
    }
    go(`#/product/${form.dataset.product}`);
  } catch (err) {
    showError(msg, err);
  }
});

$("#r-delete").addEventListener("click", async () => {
  const form = $("#rate-form");
  if (!form.dataset.rating || !confirm("Delete this rating?")) return;
  await api("DELETE", `/api/ratings/${form.dataset.rating}`);
  go(`#/product/${form.dataset.product}`);
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
              el("span", { text: [p.brand, p.rating && p.rating.store].filter(Boolean).join(" · ") })),
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
