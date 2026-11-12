"use strict";
const ADMIN = location.pathname.startsWith("/admin");
const API = ADMIN ? "/admin/api" : "/api";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
const S = { facets: null, q: "", cuisine: "", tag: "", author: "", publisher: "", ingredient: "", location_id: "", sort: "author", editor: ADMIN, ingredients: null, askThread: [], askDraft: "", askLeft: 5 };

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && !(o.body instanceof FormData)) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
  const r = await fetch((path.startsWith("/") ? "" : API + "/") + path, o);
  let data = null;
  try { data = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) {
    if (r.status === 401 && ADMIN) toast("Your sign-in expired. Reload the page to sign in again.");
    const e = new Error((data && data.error) || `Request failed (${r.status})`); e.status = r.status; e.data = data; throw e;
  }
  return data;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}
// Run a mutation with its button disabled until it settles. A failure shows the server's message
// (not an unhandled rejection), and the button comes back either way.
async function busy(btn, fn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  try { return await fn(); }
  catch (err) { toast(err.message); }
  finally { btn.disabled = false; }
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ---------- covers ---------- */
const SPINES = ["#7a3b2e", "#2f5d50", "#3f4e78", "#8a6a2c", "#5b3a5e", "#2d5a73", "#6b4a32", "#4a6a3a", "#8b3c4f", "#355c5c"];
function spine(t) { let h = 0; for (const c of t || "") h = (h * 31 + c.charCodeAt(0)) >>> 0; return SPINES[h % SPINES.length]; }
function coverHTML(b) {
  if (b.kind === "house" && !b.cover) return `<div class="cover ph house"><span class="t">${esc(b.title)}</span><span class="a">Our recipes</span></div>`;
  if (b.kind === "online" && !b.cover) return `<div class="cover ph web"><span class="t">${esc(b.title)}</span><span class="a">🌐 ${esc(b.site || "")}</span></div>`;
  if (b.cover) return `<div class="cover"><img loading="lazy" src="/covers/${esc(b.cover)}" alt=""></div>`;
  return `<div class="cover ph" style="background:${spine(b.title)}"><span class="t">${esc(b.title)}</span><span class="a">${esc((b.authors || [])[0] || "")}</span></div>`;
}
const byline = (b) => (b.authors || []).join(", ");

/* ---------- editor check (public view) ---------- */
// Only /admin is behind Cloudflare Access, so the server never sees a family member's sign-in on /api/*.
// /admin/api/me is: the browser sends its Access cookie, Access adds the identity header, the app answers 200.
// Anonymous visitors get Access's login redirect (redirect:"manual" keeps that quiet) or a 401. This is only a
// hint for showing the Ask button; the real gate is still Access plus @editor_required on every /admin/api route.
// The result is kept for the tab session so the check isn't repeated on every page load, and anonymous visitors
// don't hit /admin on every visit. A "not an editor" answer expires sooner, since someone may sign in meanwhile.
const EDITOR_KEY = "cb.editor", EDITOR_TTL = { yes: 30 * 60e3, no: 10 * 60e3 };
function cachedEditor() {
  try {
    const c = JSON.parse(sessionStorage.getItem(EDITOR_KEY));
    if (c && Date.now() - c.t < (c.editor ? EDITOR_TTL.yes : EDITOR_TTL.no)) return c;
  } catch { /* storage blocked or garbled: just check again */ }
  return null;
}
async function probeEditor() {
  const out = { editor: false, ask: false, t: Date.now() };
  try {
    const me = await fetch("/admin/api/me", { redirect: "manual", credentials: "same-origin" });
    if (me.ok) out.editor = (await me.json()).editor === true;
    if (out.editor) {   // one more call, editors only: is AI search switched on?
      const f = await fetch("/admin/api/features", { redirect: "manual", credentials: "same-origin" });
      if (f.ok) out.ask = (await f.json()).ask === true;
    }
  } catch (e) {
    if (e instanceof TypeError) return { editor: false, ask: false };   // offline: don't remember a guess
    /* anything else (a non-JSON reply from a proxy) counts as "not an editor" */
  }
  try { sessionStorage.setItem(EDITOR_KEY, JSON.stringify(out)); } catch { /* fine without it */ }
  return out;
}
function applyEditor(p) { S.editor = !!p.editor; S.askPublic = !!(p.editor && p.ask); }
const currentRoute = () => location.hash.replace(/^#\/?/, "").split("?")[0];

/* ---------- nav / router ---------- */
function renderNav(route) {
  const items = ADMIN
    ? [["#/", "Catalog"], ["#/photos", "Photos"], ["#/cook", "Cook"], ["#/add", "Add"], ["#/stats", "Stats"], ["#/more", "More"]]
    : [["#/", "Catalog"], ["#/photos", "Photos"]].concat(S.editor ? [["/admin#/", "Edit"]] : [["/admin#/", "Sign in"]]);
  $("#nav").innerHTML = items.map(([h, t]) =>
    `<a href="${h}" class="${(h === "#/" ? route === "" : h.startsWith("#/") && route.startsWith(h.slice(2))) ? "on" : ""}">${t}</a>`).join("");
  if (!ADMIN) $$("#nav a[href^='/admin']").forEach((a) => a.onclick = () => {   // coming back signed in: ask again
    try { sessionStorage.removeItem(EDITOR_KEY); } catch { /* ignore */ }
  });
  const slot = $("#ask-slot");
  const ask = ADMIN ? S.features && S.features.ask : S.askPublic;
  if (slot) slot.innerHTML = ask   // on the public view this is a plain link into the editor, where Ask lives
    ? `<a id="ask-top" href="${ADMIN ? "#/ask" : "/admin#/ask"}" class="${ADMIN && route === "ask" ? "on" : ""}" title="Ask about your cookbooks">${CHAT_ICON}Ask</a>` : "";
}
const PIN_ICON = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s-6.5-5.7-6.5-11a6.5 6.5 0 0 1 13 0c0 5.3-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/></svg>`;
const CHAT_ICON = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M5 4.5h14A1.5 1.5 0 0 1 20.5 6v9a1.5 1.5 0 0 1-1.5 1.5h-8.5L6 20v-3.5H5A1.5 1.5 0 0 1 3.5 15V6A1.5 1.5 0 0 1 5 4.5z"/></svg>`;
async function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const cut = hash.indexOf("?");   // everything after the first "?", so an unencoded ?return=…?x=1 stays whole
  const path = cut < 0 ? hash : hash.slice(0, cut), qs = cut < 0 ? "" : hash.slice(cut + 1);
  renderNav(path);
  if (S.wake) { S.wake.release().catch(() => {}); S.wake = null; }   // a recipe page's "keep screen on"
  const m = path.match(/^book\/(\d+)(?:\/(edit|index))?$/);
  const u = path.match(/^uid\/([0-9a-f]{32})(?:\/([0-9a-f]{32}))?$/);
  const rr = path.match(/^recipe\/(new|[0-9a-f]{32})(?:\/(edit))?$/);
  window.scrollTo(0, 0);
  try {
    if (u) return await uidView(u[1], u[2]);
    if (rr && !ADMIN && (rr[1] === "new" || rr[2])) return location.replace("/admin" + location.hash);  // editing is signed-in
    if (rr && rr[1] === "new") return await recipeEditView(null, new URLSearchParams(qs));
    if (rr && rr[2] === "edit") return await recipeEditView(rr[1], new URLSearchParams(qs));
    if (rr) return await recipeView(rr[1]);
    if (path === "house" && ADMIN) { const h = await api("house"); return location.replace(`#/book/${h.id}`); }
    if (m && m[2] === "edit" && ADMIN) return await editView(+m[1]);
    if (m && m[2] === "index" && ADMIN) return await indexView(+m[1]);
    if (m) return await bookView(+m[1], new URLSearchParams(qs || "").get("recipe"));
    if (path === "photos") return await photosView();
    if (path === "add" && ADMIN) return await addView();
    if (path === "try" && ADMIN) return await tryView();
    if (path === "review" && ADMIN) return await reviewView();
    if (path === "more" && ADMIN) return await moreView();
    if (path === "ask" && ADMIN) return askView();
    if (path === "cook" && ADMIN) return await cookView();
    if (path === "stats" && ADMIN) return await statsView();
    if (path === "eyb" && ADMIN) return await eybView();
    if (path === "links" && ADMIN) return await linksView();
    if (path === "changes" && ADMIN) return await changesView(new URLSearchParams(qs));
    if (qs) { const p = new URLSearchParams(qs); for (const k of ["q", "cuisine", "tag", "author", "publisher", "ingredient", "location_id"]) S[k] = p.get(k) || ""; if (p.get("sort")) S.sort = p.get("sort"); }
    else { S.author = S.publisher = ""; }
    return await homeView();
  } catch (e) {
    view.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", (e) => {
  // The recipe editor sets S.dirty: leaving it asks first, and staying puts the old address back without a reload.
  if (S.dirty && !confirm("Leave without saving your changes?")) { history.replaceState(null, "", e.oldURL); return; }
  S.dirty = false; window.onbeforeunload = null;
  if (S.capJob) { discardCapture(S.capJob); S.capJob = null; }   // left the editor without saving a captured recipe
  route();
});
window.addEventListener("pagehide", () => { if (S.capJob) discardCapture(S.capJob, true); });

/* Deep link by stable uid: #/uid/<book uid>[/<recipe uid>] (what Dinner Log stores). Resolves through the
   by-uid API and replaces itself with #/book/<id>[?recipe=<id>], so Back skips it and bookView does the
   scrolling and highlighting. A recipe uid the book doesn't have just opens the book. */
async function uidView(bookUid, recipeUid) {
  let b;
  try { b = await api("books/by-uid/" + bookUid); }
  catch (e) {
    if (e.status !== 404) throw e;
    throw new Error(ADMIN ? "That book isn't in this catalog."
      : "That book isn't in the public catalog. Recipe websites are editor-only: try the same link under /admin.");
  }
  const r = recipeUid && (b.recipes || []).find((x) => x.uid === recipeUid);
  location.replace(`#/book/${b.id}${r ? `?recipe=${r.id}` : ""}`);
}

/* ---------- home ---------- */
async function homeView() {
  if (!S.facets) S.facets = await api("facets");
  if (!S.ingredients) { try { S.ingredients = (await api("ingredients")).ingredients.filter((i) => i.n && !i.hint); } catch { S.ingredients = []; } }
  const f = S.facets;
  // Only shelves that hold books: the editor's list also has empty ones (for the picker), and a filter for those is a dead end.
  const shelves = (f.locations || []).filter((l) => l.n > 0);
  if (S.location_id && !shelves.some((l) => String(l.id) === String(S.location_id))) S.location_id = "";   // a stale link or bookmark
  const opt = (arr, cur, label) => `<option value="">${label}</option>` +
    arr.map((x) => `<option value="${esc(x.name)}" ${x.name === cur ? "selected" : ""}>${esc(x.name)} (${x.n})</option>`).join("");
  view.innerHTML = `
    <div class="controls${shelves.length ? " four" : ""}">
      <label class="search"><input id="q" type="search" placeholder="Search books, authors, recipes…" value="${esc(S.q)}" autocomplete="off" aria-label="Search"></label>
      <select id="cuisine" aria-label="Cuisine">${opt(f.cuisines, S.cuisine, "Cuisine")}</select>
      <select id="tag" aria-label="Tag">${opt(f.tags, S.tag, "Tag")}</select>
      ${shelves.length ? `<select id="location_id" aria-label="Location"><option value="">Location</option>${shelves.map((l) =>
        `<option value="${l.id}" ${String(l.id) === String(S.location_id) ? "selected" : ""}>${"\u00a0\u00a0".repeat(l.depth)}${esc(l.name)} (${l.n})</option>`).join("")}</select>` : ""}
      <select id="sort" aria-label="Sort">
        <option value="author">By author</option><option value="title">By title</option><option value="added">Recently added</option>
        ${ADMIN ? `<option value="cooked">Most cooked</option><option value="rating">Top rated</option>` : ""}
      </select>
    </div>
    ${S.ingredients.length ? `<div class="ingchips" role="group" aria-label="Recipes by ingredient">${S.ingredients.map((i) =>
      `<button class="ing ${i.name === S.ingredient ? "on" : ""}" data-ing="${esc(i.name)}" aria-pressed="${i.name === S.ingredient}">${esc(i.name)} <span>${i.n.toLocaleString()}</span></button>`).join("")}</div>` : ""}
    <div class="summary" id="summary"></div>
    <div id="recipes"></div>
    <div class="grid" id="grid"></div>
    <footer class="foot">${f.stats.books} cookbooks · ${f.stats.recipes.toLocaleString()} recipes indexed</footer>`;
  $("#sort").value = S.sort;
  let loadSeq = 0;
  const load = async () => {
    const seq = ++loadSeq;
    const p = new URLSearchParams();
    for (const k of ["q", "cuisine", "tag", "author", "publisher", "location_id", "sort"]) if (S[k]) p.set(k, S[k]);
    if (S.ingredient) p.set("ingredient", S.ingredient);
    const rp = new URLSearchParams({ limit: 60 });
    for (const k of ["q", "cuisine", "tag", "publisher", "location_id", "ingredient"]) if (S[k]) rp.set(k, S[k]);
    const wantRecipes = S.q.trim().length >= 2 || S.ingredient;
    // With an ingredient picked, start from the books that have it and how many recipes each; open one to list them.
    const byBook = !!S.ingredient;
    const [bk, rc] = await Promise.all([
      api("books?" + p),
      byBook ? api("recipes/books?" + rp) : wantRecipes ? api("recipes?" + rp) : Promise.resolve({ recipes: [], count: 0 }),
    ]);
    if (seq !== loadSeq || !$("#summary")) return;
    const chips = [];
    if (S.author) chips.push(`<button class="chip x" data-clear="author" aria-label="Remove filter: ${esc(S.author)}">${esc(S.author)}</button>`);
    if (S.publisher) chips.push(`<button class="chip x" data-clear="publisher" aria-label="Remove filter: ${esc(S.publisher)}">${esc(S.publisher)}</button>`);
    if (S.cuisine) chips.push(`<button class="chip x" data-clear="cuisine" aria-label="Remove filter: ${esc(S.cuisine)}">${esc(S.cuisine)}</button>`);
    if (S.tag) chips.push(`<button class="chip x" data-clear="tag" aria-label="Remove filter: ${esc(S.tag)}">${esc(S.tag)}</button>`);
    const shelf = S.location_id && shelves.find((l) => String(l.id) === String(S.location_id));
    if (shelf) chips.push(`<button class="chip x" data-clear="location_id" aria-label="Remove filter: ${esc(shelf.path)}">${esc(shelf.path)}</button>`);
    if (S.ingredient) chips.push(`<button class="chip x" data-clear="ingredient" aria-label="Remove filter: uses ${esc(S.ingredient.toLowerCase())}">Uses ${esc(S.ingredient.toLowerCase())}</button>`);
    const nb = bk.books.length === bk.total ? `${bk.total} books` : bk.books.length ? `${bk.books.length} of ${bk.total} books` : "No matching books";
    $("#summary").innerHTML = `<span>${rc.count ? `${rc.count.toLocaleString()} recipe${rc.count > 1 ? "s" : ""} · ` : ""}${nb}</span>${chips.join("")}`;
    $$("[data-clear]").forEach((b) => b.onclick = () => {
      S[b.dataset.clear] = "";
      if (location.hash.includes("?")) history.replaceState(null, "", "#/");
      homeView();
    });
    const recipeLine = (r) => `<li><div class="rn"><a href="${r.has_detail ? `#/recipe/${esc(r.uid)}` : `#/book/${r.book_id}`}">${r.heading ? `<span class="rh">${esc(r.heading)} · </span>` : ""}${esc(r.name)}</a></div>
        ${whereHTML(r)}</li>`;
    if (byBook) {
      const ing = esc(S.ingredient.toLowerCase());
      $("#recipes").innerHTML = rc.count ? `
        <div class="section-h"><h2>Recipes using ${ing}</h2><span class="muted small">${rc.count.toLocaleString()} in ${rc.books.length} book${rc.books.length > 1 ? "s" : ""}</span></div>
        <div class="bookrecs">${rc.books.map((b) => `<details data-id="${b.id}"><summary><span class="t">${esc(b.title)}</span>
          <span class="n">${b.n.toLocaleString()} recipe${b.n > 1 ? "s" : ""}</span></summary><ul class="rlist"></ul></details>`).join("")}</div>
        ${bk.books.length ? `<div class="section-h"><h2>Books</h2></div>` : ""}` : `<div class="empty">No ${ing} recipes match.</div>`;
      $$(".bookrecs details").forEach((d) => d.addEventListener("toggle", async () => {
        if (!d.open || d.dataset.loaded) return;
        d.dataset.loaded = "1";
        const q = new URLSearchParams(rp); q.set("book", d.dataset.id); q.set("limit", 500);
        const ul = $("ul", d);
        ul.innerHTML = `<li class="muted small">Loading…</li>`;
        try {
          const { recipes } = await api("recipes?" + q);
          ul.innerHTML = recipes.map(recipeLine).join("") + `<li class="open"><a href="#/book/${d.dataset.id}">Open the book →</a></li>`;
        } catch (err) { ul.innerHTML = ""; delete d.dataset.loaded; toast(err.message); }
      }));
    } else {
      // Search results come back ordered by book, so group them under a heading per cookbook (carried across "Show more").
      let lastBook = null;
      const row = (r) => {
        const head = r.book_id !== lastBook ? `<li class="grp"><a href="#/book/${r.book_id}">${esc(r.book_title)}</a></li>` : "";
        lastBook = r.book_id;
        return head + recipeLine(r);
      };
      $("#recipes").innerHTML = rc.count ? `
        <div class="section-h"><h2>Recipes</h2><span class="muted small" id="rcount">${rc.count > rc.recipes.length ? `${rc.recipes.length} of ${rc.count.toLocaleString()}` : rc.count.toLocaleString()}</span></div>
        <ul class="rlist" id="rl">${rc.recipes.map(row).join("")}</ul>
        ${rc.count > rc.recipes.length ? `<div class="actions"><button id="more">Show more</button></div>` : ""}
        ${bk.books.length ? `<div class="section-h"><h2>Books</h2></div>` : ""}` : "";
      let shown = rc.recipes.length;
      if ($("#more")) $("#more").onclick = async () => {
        rp.set("offset", shown); rp.set("limit", 200);
        const more = await api("recipes?" + rp);
        $("#rl").insertAdjacentHTML("beforeend", more.recipes.map(row).join(""));
        shown += more.recipes.length;
        $("#rcount").textContent = `${shown.toLocaleString()} of ${rc.count.toLocaleString()}`;
        if (shown >= rc.count || !more.recipes.length) $("#more").remove();
      };
    }
    $("#grid").innerHTML = bk.books.length ? bk.books.map((b) => `
      <a class="card" href="#/book/${b.id}">${coverHTML(b)}
        <div class="meta"><div class="ttl">${esc(b.title)}</div><div class="by">${esc(byline(b))}</div>
        ${b.location_path ? `<div class="loc" title="${esc(b.location_path)}">${PIN_ICON}<span>${esc(b.location_path)}</span></div>` : ""}
        ${ADMIN && b.rating != null ? starsHTML(b.rating) : ""}${progressHTML(b)}</div></a>`).join("")
      : (rc.count ? "" : `<div class="empty">Nothing matches.${ADMIN && S.features && S.features.ask && S.q ? ` <a id="ask-instead" href="#/ask">Ask instead →</a>` : ""}</div>`);
    // A keyword search is a new topic: start a fresh thread, unless a question is still being answered.
    if ($("#ask-instead")) $("#ask-instead").onclick = () => {
      if (!S.askBusy) { S.askThread = []; S.askLeft = ASK_MAX; S.askErr = ""; }
      S.askDraft = S.q;
    };
  };
  $("#q").addEventListener("input", debounce((e) => { S.q = e.target.value; load(); }, 220));
  for (const k of ["cuisine", "tag", "sort", "location_id"]) if ($("#" + k)) $("#" + k).onchange = (e) => { S[k] = e.target.value; load(); };
  $$(".ing").forEach((b) => b.onclick = () => {
    S.ingredient = S.ingredient === b.dataset.ing ? "" : b.dataset.ing;
    $$(".ing").forEach((x) => { x.classList.toggle("on", x.dataset.ing === S.ingredient); x.setAttribute("aria-pressed", x.dataset.ing === S.ingredient); });
    load();
  });
  await load();
  if (!S.q && !/Mobi|Android/.test(navigator.userAgent)) $("#q").focus();
}

/* ---------- book ---------- */
const STATUS = { owned: "On the shelf", out: "Off the shelf", loaned: "Loaned out", gone: "No longer owned", wanted: "Want to buy" };
const fmtDay = (d) => { if (!d) return ""; const t = new Date(d + "T12:00:00"); return isNaN(t) ? d : t.toLocaleDateString(undefined, { month: "short", day: "numeric", year: t.getFullYear() === new Date().getFullYear() ? undefined : "numeric" }); };
// Same folding as the server's fold(): "creme" finds "Crème", "oeufs" finds "Œufs".
const FOLD_EXTRA = { æ: "ae", œ: "oe", ø: "o", ł: "l", đ: "d", ð: "d", þ: "th", ı: "i", ß: "ss" };
const fold = (s) => (s || "").toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "").replace(/[æœøłđðþıß]/g, (c) => FOLD_EXTRA[c]);
const num = (x) => (+x % 1 ? (+x).toFixed(1) : String(Math.round(+x)));
// Book rating, 0–5 in half stars: outline stars with a filled layer clipped to the rating.
const starLabel = (v) => v == null ? "Not rated" : `${num(v)} star${v === 1 ? "" : "s"}`;
const starsHTML = (v, cls = "") => `<span class="stars ${cls}" role="img" title="${starLabel(v)}" aria-label="${starLabel(v)}">
  <span class="on" style="width:${(v || 0) * 20}%">★★★★★</span>☆☆☆☆☆</span>`;
function rateWidget(el, value, save) {
  const stars = $(".stars", el), on = $(".on", stars), txt = $(".ratev", el);
  const show = (v) => { on.style.width = (v || 0) * 20 + "%"; };
  const set = async (v) => {
    if (v === value) return;
    try { await save(v); } catch (err) { toast(err.message); show(value); return; }
    value = v; show(v); txt.textContent = v == null ? "Not rated" : num(v);
    stars.setAttribute("aria-valuenow", v ?? 0); stars.setAttribute("aria-valuetext", starLabel(v));
    $(".clear", el).hidden = v == null;
  };
  // Left half of a star = half star, right half = whole star.
  const at = (e) => { const r = stars.getBoundingClientRect(); return Math.min(5, Math.max(0.5, Math.ceil((e.clientX - r.left) / r.width * 10) / 2)); };
  stars.onpointermove = (e) => show(at(e));
  stars.onpointerleave = () => show(value);
  stars.onclick = (e) => set(at(e));
  stars.onkeydown = (e) => {
    const step = { ArrowRight: 0.5, ArrowUp: 0.5, ArrowLeft: -0.5, ArrowDown: -0.5 }[e.key];
    if (step) set(Math.min(5, Math.max(0, (value || 0) + step)));
    else if (e.key === "Home") set(0);
    else if (e.key === "End") set(5);
    else if (e.key === "Delete" || e.key === "Backspace") set(null);
    else return;
    e.preventDefault();
  };
  $(".clear", el).onclick = () => set(null);
}
// "made 12 of 140" with a thin bar. Editors only: the server sends made_count to nobody else, and only for cookbooks.
const progressHTML = (b) => ADMIN && b.made_count != null && b.recipe_count
  ? `<div class="prog" title="${b.made_count} of ${b.recipe_count} recipes made"><span class="bar"><span style="width:${Math.round((b.made_count / b.recipe_count) * 100)}%"></span></span>
    <span class="pt">made ${b.made_count} of ${b.recipe_count}</span></div>` : "";
// Where to find a recipe: the page in the book, or a link for one saved from a website.
const whereHTML = (r) => r.url ? `<a class="pg web" href="${esc(r.url)}" target="_blank" rel="noopener" title="Open the recipe on ${esc(r.book_title || S.bookTitle || "the site")}">Open ↗</a>`
  : r.has_detail ? `<a class="pg web" href="#/recipe/${esc(r.uid)}">Recipe →</a>`   // a house recipe: its own page
  : `<span class="pg">${r.page ? "p. " + esc(r.page) : ""}</span>`;
// Stable uids for Dinner Log to store beside the ids (ids are reused after a delete; uids never are).
const uidParams = (r) => [["cookbook_book_uid", r.book_uid || S.bookUid], ["cookbook_recipe_uid", r.uid]]
  .filter(([, v]) => v).map(([k, v]) => `&${k}=${encodeURIComponent(v)}`).join("");
function recipeRow(r) {
  // From Dinner Log: editors see "made 3× · last Oct 12 · 8/10" (average rating); the public view gets only the
  // count, because that is all the server sends it (PUBLIC_RECIPE_COOKED).
  const extra = [];
  if (r.cooked_count) extra.push(`<span class="cooked">made ${r.cooked_count}×${ADMIN && r.cooked_last ? ` · last ${fmtDay(r.cooked_last)}` : ""}${ADMIN && r.cooked_avg != null ? ` · <span class="rating">${num(r.cooked_avg)}/10</span>` : ""}</span>`);
  if (ADMIN && r.rating != null) extra.push(`<span class="rating">${num(r.rating)}/10</span>${r.cooked_count ? " (journal)" : ""}`);
  if (ADMIN && r.last_made) extra.push(`made ${fmtDay(r.last_made)}`);
  if (ADMIN && r.notes) extra.push(esc(r.notes));
  const nph = (S.bookPhotos || []).filter((p) => p.recipe_id === r.id).length;
  return `<li data-id="${r.id}"${r.made != null ? ` data-made="${r.made ? 1 : 0}"` : ""}><div class="rn">${r.heading ? `<span class="rh">${esc(r.heading)} · </span>` : ""}${esc(r.name)}
    ${extra.length ? `<div class="extra">${extra.join(" · ")}</div>` : ""}</div>
    ${whereHTML(r)}
    ${ADMIN ? `<span class="acts">${S.features && S.features.dinnerlog_url && S.bookTitle ? `<a class="log" title="Log this dinner in Dinner Log" aria-label="Log ${esc(r.name)} in Dinner Log" target="_blank" rel="noopener"
        href="${esc(S.features.dinnerlog_url)}/new?source_type=${r.has_detail ? "original" : "cookbook"}&source_title=${encodeURIComponent(S.bookTitle)}&source_detail=${encodeURIComponent(r.page || r.url || "")}&cookbook_book_id=${r.book_id}&cookbook_recipe_id=${r.id}${uidParams(r)}&name=${encodeURIComponent(r.heading && r.name[0] === r.name[0].toLowerCase() ? r.heading + ", " + r.name : r.name)}">🍽</a>` : ""}
      <button title="${nph ? `${nph} photo${nph > 1 ? "s" : ""}` : "Add photos"}" aria-label="${nph ? `Photos (${nph})` : "Add photos"}: ${esc(r.name)}" class="pho ${nph ? "on" : ""}"><span aria-hidden="true">📷${nph ? `<span class="n">${nph}</span>` : ""}</span></button>
      <button title="Want to try" aria-label="Want to try: ${esc(r.name)}" aria-pressed="${!!r.want_to_try}" class="want ${r.want_to_try ? "on" : ""}">${r.want_to_try ? "★" : "☆"}</button>
      <button title="Edit" aria-label="Edit ${esc(r.name)}" class="edit">✎</button></span>` : ""}</li>`;
}
async function bookView(id, focus) {
  const b = await api("books/" + id);
  S.bookTitle = b.title; S.bookUid = b.uid;
  S.bookPhotos = b.photos || [];
  const rows = [];
  if (b.subtitle) rows.push(["Subtitle", esc(b.subtitle)]);
  if (b.publisher) rows.push(["Publisher", esc(b.publisher) + (b.year ? `, ${b.year}` : "")]);
  else if (b.year) rows.push(["Published", b.year]);
  if (b.cuisine) rows.push(["Cuisine", `<a href="#/?cuisine=${encodeURIComponent(b.cuisine)}">${esc(b.cuisine)}</a>`]);
  if (b.isbn) rows.push(["ISBN", esc(b.isbn)]);
  if (b.format) rows.push(["Format", esc(b.format)]);
  if (b.location_path) rows.push(["Location", `<a class="loclink" href="#/?location_id=${encodeURIComponent(b.location_id)}">${PIN_ICON}${esc(b.location_path)}</a>`]);
  if (b.signed) rows.push(["", "Signed copy"]);
  if (b.kind === "online") rows.push(["Website", `<a href="https://${esc(b.site)}" target="_blank" rel="noopener">${esc(b.site)} ↗</a>`]);
  const priv = ADMIN ? [
    b.status && b.status !== "owned" ? `<div><strong>${STATUS[b.status]}</strong>${b.status === "out" && b.out_since ? " since " + fmtDay(b.out_since) : ""}${b.loaned_to ? " to " + esc(b.loaned_to) : ""}</div>` : "",
    b.cooked_count ? `<div>Cooked from ${b.cooked_count}× (Dinner Log), last ${fmtDay(b.cooked_last)}</div>` : "",
    (b.wishes || []).length ? `<div>On ${b.wishes.map((w) => esc(w.person_name) + "'s").join(", ")} wish list</div>` : "",
    b.private_notes ? `<div>${esc(b.private_notes)}</div>` : "",
    b.eyb_status ? `<div>Eat Your Books: ${esc(b.eyb_status.toLowerCase())}${b.recipe_count ? "" : " (export its recipes from EYB to fill the index)"}</div>` : "",
    b.needs_review ? `<div class="review">Needs review: ${esc(b.needs_review)}</div>` : "",
    b.visibility === "members" ? `<div>Only signed-in members see this book; it isn't in the public catalog.</div>` : "",
  ].join("") : "";
  view.innerHTML = `
    <a class="back" href="#/">← All books</a>
    <div class="book">
      <div>${coverHTML(b)}</div>
      <div>
        <h1>${esc(b.title)}</h1>
        <div class="muted authors">${(b.authors || []).map((a) => `<a href="#/?author=${encodeURIComponent(a)}">${esc(a)}</a>`).join(", ")}</div>
        ${ADMIN && !b.kind ? `<div class="rate" id="rate">
          <span class="stars big" role="slider" tabindex="0" aria-label="Book rating" aria-valuemin="0" aria-valuemax="5"
            aria-valuenow="${b.rating ?? 0}" aria-valuetext="${starLabel(b.rating)}"><span class="on" style="width:${(b.rating || 0) * 20}%">★★★★★</span>☆☆☆☆☆</span>
          <span class="ratev small muted">${b.rating == null ? "Not rated" : num(b.rating)}</span>
          <button class="link clear" ${b.rating == null ? "hidden" : ""}>Clear</button></div>` : ""}
        ${progressHTML(b)}
        <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
        ${b.tags.length ? `<div class="tags">${b.tags.map((t) => `<a class="chip" href="#/?tag=${encodeURIComponent(t)}">${esc(t)}</a>`).join("")}</div>` : ""}
        ${b.description ? `<p class="desc">${esc(b.description)}</p>` : ""}
        ${priv ? `<div class="private">${priv}</div>` : ""}
        ${ADMIN && b.kind === "online" ? `<p class="small muted">Recipes saved from this website: each one opens on the site, where you're signed in.</p>
          <div class="actions"><a class="btn primary" href="#/links">Add recipes from links</a><a class="btn" href="#/book/${id}/edit">Edit</a></div>` : ""}
        ${ADMIN && b.kind === "house" ? `<p class="small muted">Recipes we've worked out ourselves, with ingredients and steps.</p>
          <div class="actions"><a class="btn primary" href="#/recipe/new">New recipe</a><a class="btn" href="#/book/${id}/edit">Edit</a></div>` : ""}
        ${ADMIN && !b.kind ? `<div class="actions"><button id="shelf" class="${b.status === "out" ? "primary" : ""}">${b.status === "out" ? "Put back on shelf" : "Pulled out"}</button>
          ${b.status === "wanted" ? `<button id="wish" class="primary">Add to a wish list…</button>` : ""}
          <a class="btn" href="#/book/${id}/edit">Edit book</a>
          <a class="btn" href="#/book/${id}/index">Import index</a><button id="addr">Add recipe</button></div>` : ""}
      </div>
    </div>
    ${S.bookPhotos.length ? `<div class="section-h"><h2>What we've made <span class="muted small">${S.bookPhotos.length}</span></h2></div>
      <div class="photos">${S.bookPhotos.map((p, i) => `<button class="ph" data-i="${i}"><img loading="lazy" src="${esc(p.thumb)}" alt="${esc(p.recipe_name)}">
        <span class="pn">${esc(p.recipe_name)}</span><span class="pd">${fmtDay(p.taken_on)}${p.recipe_page ? " · p. " + esc(p.recipe_page) : ""}</span></button>`).join("")}</div>` : ""}
    <div class="section-h"><h2>Recipes <span class="muted small">${b.recipes.length || ""}</span></h2>
      ${b.recipes.length > 12 ? `<input id="rf" type="search" placeholder="Filter recipes" aria-label="Filter recipes" style="max-width:240px">` : ""}</div>
    ${ADMIN && b.made_count != null && b.recipes.length ? `<div class="seg mf" role="group" aria-label="Which recipes">${[
      ["all", `All ${b.recipes.length}`], ["made", `Made ${b.made_count}`], ["todo", `Not yet made ${b.recipes.length - b.made_count}`]].map(([k, t]) =>
      `<button type="button" data-f="${k}" aria-pressed="${k === "all"}" class="${k === "all" ? "on" : ""}">${t}</button>`).join("")}</div>` : ""}
    ${b.recipes.length ? `<ul class="rlist" id="rl">${b.recipes.map(recipeRow).join("")}</ul>`
      : b.kind === "house" ? `<p class="muted">No house recipes yet.${ADMIN ? " Use <em>New recipe</em> to write one down." : ""}</p>`
      : `<p class="muted">No recipes indexed for this book yet.${ADMIN ? " Use <em>Import index</em> to add the book's index." : ""}</p>`}
    ${ADMIN ? `<div id="dinners"></div>` : ""}`;
  const rf = $("#rf");
  let madeFilter = "all";  // All / Made / Not yet made (editors), combined with the text filter
  const applyFilter = () => {
    const q = rf ? fold(rf.value.trim()) : "";
    $$("#rl li").forEach((li) => {
      const text = !q || fold(li.textContent).includes(q);
      const made = madeFilter === "all" || (madeFilter === "made") === (li.dataset.made === "1");
      li.classList.toggle("hidden", !(text && made));
    });
  };
  if (rf) rf.oninput = applyFilter;
  $$(".mf button").forEach((btn) => btn.onclick = () => {
    madeFilter = btn.dataset.f;
    $$(".mf button").forEach((x) => { x.classList.toggle("on", x === btn); x.setAttribute("aria-pressed", x === btn); });
    applyFilter();
  });
  const hit = focus && $(`#rl li[data-id="${+focus}"]`);  // arrived from the Cook page: show which recipe
  if (hit) { hit.classList.add("hl"); hit.scrollIntoView({ block: "center" }); }
  if (!ADMIN) return;
  $$(".photos .ph").forEach((el) => el.onclick = () => lightbox(S.bookPhotos, +el.dataset.i));
  if ($("#rate")) rateWidget($("#rate"), b.rating, (v) => api("books/" + id, { method: "PUT", body: { rating: v } }));
  if ($("#addr")) $("#addr").onclick = () => recipeDialog(null, id, () => bookView(id));
  if ($("#shelf")) $("#shelf").onclick = () => busy($("#shelf"), async () => {  // cookbooks only, not websites
    await api(`books/${id}/shelf`, { method: "POST", body: { out: b.status !== "out" } });
    toast(b.status === "out" ? "Back on the shelf" : "Marked as pulled out"); bookView(id);
  });
  if ($("#wish")) $("#wish").onclick = () => wishDialog(b);
  dinnersPanel(b);
  $$("#rl li").forEach((li) => {
    const r = b.recipes.find((x) => x.id === +li.dataset.id);
    $(".want", li).onclick = () => busy($(".want", li), async () => {
      await api("recipes/" + r.id, { method: "PUT", body: { want_to_try: !r.want_to_try } });
      r.want_to_try = !r.want_to_try; const b2 = $(".want", li);
      b2.classList.toggle("on", !!r.want_to_try); b2.textContent = r.want_to_try ? "★" : "☆"; b2.setAttribute("aria-pressed", !!r.want_to_try);
    });
    $(".edit", li).onclick = () => b.kind === "house" ? (location.hash = `#/recipe/${r.uid}/edit`)
      : recipeDialog(r, id, () => bookView(id));
    $(".pho", li).onclick = () => photoDialog(r, () => bookView(id));
  });
}

/* ---------- ask (natural-language search) ---------- */
// The thread lives in S.askThread only (JS memory): [{q, a?, items?}]. Reloading starts fresh.
const ASK_MAX = 5;
const askWire = () => S.askThread.flatMap((t) => [{ q: t.q }].concat(t.a != null ? [{ a: t.a }] : []));
function askItemsHTML(items) {
  const recipes = items.filter((i) => i.kind === "recipe"), books = items.filter((i) => i.kind === "book");
  let last = null;
  const rows = recipes.map((r) => {
    S.bookTitle = r.book_title; S.bookUid = r.book_uid; S.bookPhotos = [];  // recipeRow reads these for the Dinner Log link
    const head = r.book_id !== last ? `<li class="grp"><a href="#/book/${r.book_id}">${esc(r.book_title)}</a></li>` : "";
    last = r.book_id;
    return head + recipeRow(r);
  }).join("");
  return (rows ? `<ul class="rlist">${rows}</ul>` : "")
    + (books.length ? `<div class="ask-books">${books.map((b) => `<a class="ask-book" href="#/book/${b.id}">${coverHTML(b)}
      <div><div class="ttl">${esc(b.title)}</div><div class="by">${esc(byline(b))}</div></div></a>`).join("")}</div>` : "");
}
function askView() {
  const T = S.askThread;
  const last = T[T.length - 1];
  const answered = T.filter((t) => t.a != null).length;
  const full = answered >= ASK_MAX || S.askLeft === 0;
  view.innerHTML = `<h1 style="margin-top:18px">Ask</h1>
    <p class="small muted" style="margin-top:0">Describe what you want in your own words. Answers come from your cookbook index.</p>
    <div id="ask-thread">${T.map((t, n) => `<div class="ask-q">${esc(t.q)}</div>` + (t.a != null
      ? `<div class="ask-a" data-turn="${n}"><p class="txt">${esc(t.a)}</p><div class="ask-items">${askItemsHTML(t.items || [])}</div></div>` : "")).join("")}
      ${last && last.a == null && S.askBusy ? `<div class="ask-a muted">Thinking…</div>` : ""}
      ${last && last.a == null && S.askErr ? `<div class="ask-err">${esc(S.askErr)} <button id="ask-retry">Retry</button></div>` : ""}</div>
    <form id="ask-form"><input id="ask-q" type="text" enterkeyhint="search" maxlength="500" autocomplete="off"
        placeholder="${full ? "Start a new question to keep going" : T.length ? "Ask a follow-up" : "What are you in the mood for?"}"
        ${full || S.askBusy ? "disabled" : ""}>
      ${T.length && !S.askBusy ? `<button type="button" id="ask-new">New question</button>` : ""}</form>`;
  const inp = $("#ask-q");
  inp.value = S.askDraft;
  if (!inp.disabled) S.askDraft = "";  // a draft that arrives while the input is disabled waits for the next render
  if (!inp.disabled && !/Mobi|Android/.test(navigator.userAgent)) inp.focus();
  if (T.length) $("#ask-form").scrollIntoView({ block: "end" });
  const send = async () => {
    const t = S.askThread[S.askThread.length - 1];
    if (!t) return;
    S.askBusy = true; S.askErr = "";
    askView();
    try {
      const r = await api("ask", { method: "POST", body: { thread: askWire() } });
      t.items = r.items || []; S.askLeft = r.turns_left;
      // Never store an empty answer: askWire would send it back as {a: ""} and the server refuses the thread.
      t.a = (r.answer || "").trim() || (t.items.length ? "Here's what I found." : "I couldn't find anything for that.");
    } catch (e) { S.askErr = e.message; }
    S.askBusy = false;
    if ($("#ask-form")) askView();  // skip if the user navigated away meanwhile
  };
  $("#ask-form").onsubmit = (e) => {
    e.preventDefault();
    const q = inp.value.trim();
    if (!q || inp.disabled) return;
    // After a failed send the last turn has no answer yet: the new wording replaces it, so the thread never has two q in a row.
    if (last && last.a == null) last.q = q; else S.askThread.push({ q });
    send();
  };
  if ($("#ask-new")) $("#ask-new").onclick = () => {
    S.askThread = []; S.askLeft = ASK_MAX; S.askErr = ""; S.askDraft = inp.value; askView();
  };
  if ($("#ask-retry")) $("#ask-retry").onclick = send;
  // Each answer binds its own rows: the same recipe can appear in several turns, each with its own star state.
  $$(".ask-a[data-turn] li[data-id]").forEach((li) => {
    const t = T[+li.closest(".ask-a").dataset.turn];
    const r = t && (t.items || []).find((i) => i.kind === "recipe" && i.id === +li.dataset.id);
    if (r) $(".want", li).onclick = () => busy($(".want", li), async () => {
      await api("recipes/" + r.id, { method: "PUT", body: { want_to_try: !r.want_to_try } });
      r.want_to_try = !r.want_to_try; const b2 = $(".want", li);
      b2.classList.toggle("on", !!r.want_to_try); b2.textContent = r.want_to_try ? "★" : "☆"; b2.setAttribute("aria-pressed", !!r.want_to_try);
    });
  });
}

/* ---------- photos of what we cooked ---------- */
function photoDialog(r, done) {
  const d = document.createElement("dialog");
  let changed = false;
  d.innerHTML = `<form method="dialog">
    <h2>Photos <span class="muted small">${esc(r.name)}</span></h2>
    <div class="pgrid"></div>
    <div class="row2">
      <label class="f">Cooked on<input name="taken_on" type="date"></label>
      <label class="f">Caption<input name="caption" placeholder="optional"></label></div>
    <p class="small muted" style="margin:0">Leave the date empty to use the day each photo was taken.</p>
    <div class="actions" style="justify-content:space-between;align-items:center">
      <label class="btn primary">📷 Add photos<input type="file" accept="image/*" multiple class="vh"></label>
      <span class="small muted" id="pst"></span><button value="close">Done</button></div>
  </form>`;
  document.body.append(d);
  const grid = $(".pgrid", d), form = $("form", d);
  const show = (list) => {
    grid.innerHTML = list.length ? list.map((p) => `<div class="pt" data-id="${p.id}"><img src="${esc(p.thumb)}" alt="">
      <span class="small muted">${fmtDay(p.taken_on)}${p.caption ? " · " + esc(p.caption) : ""}${p.from_dinner_log ? " · from Dinner Log" : ""}</span>
      <button type="button" class="x" title="Delete photo" aria-label="Delete photo">×</button></div>`).join("")
      : `<p class="muted small" style="margin:0">No photos yet. Add some of the dish you made.</p>`;
    $$(".pt", grid).forEach((el) => {
      $("img", el).onclick = () => lightbox(list.map((p) => ({ ...p, recipe_name: r.name, recipe_page: r.page })), list.findIndex((p) => p.id === +el.dataset.id));
      $(".x", el).onclick = async () => {
        const p = list.find((x) => x.id === +el.dataset.id);
        if (!confirm(p && p.from_dinner_log
          ? "Delete this photo? It came from Dinner Log and won't be imported again, even if it's shared there again."
          : "Delete this photo?")) return;
        try { await api("photos/" + el.dataset.id, { method: "DELETE" }); changed = true; show(list.filter((p) => p.id !== +el.dataset.id)); }
        catch (e) { toast(e.message); }
      };
    });
  };
  show((S.bookPhotos || []).filter((p) => p.recipe_id === r.id));
  $("input[type=file]", d).onchange = async (e) => {
    const files = [...e.target.files]; e.target.value = "";
    const st = $("#pst", d);
    for (const [i, f] of files.entries()) {
      st.textContent = files.length > 1 ? `Uploading ${i + 1} of ${files.length}…` : "Uploading…";
      const fd = new FormData(); fd.append("file", f);
      fd.append("taken_on", form.elements.taken_on.value); fd.append("caption", form.elements.caption.value);
      try { show((await api(`recipes/${r.id}/photos`, { method: "POST", body: fd })).photos); changed = true; }
      catch (err) { toast(`${f.name}: ${err.message}`); }
    }
    st.textContent = "";
  };
  d.addEventListener("close", () => { d.remove(); if (changed) done(); });
  d.showModal();
}

function lightbox(list, i) {
  const d = document.createElement("dialog");
  d.className = "lb";
  const draw = () => {
    const p = list[i];
    d.innerHTML = `<img src="${esc(p.url)}" alt="${esc(p.recipe_name)}">
      <div class="lbbar"><div class="rn"><strong>${esc(p.recipe_name)}</strong>${p.recipe_page ? ` <span class="muted">p. ${esc(p.recipe_page)}</span>` : ""}
        <div class="small muted">${fmtDay(p.taken_on)}${p.caption ? " · " + esc(p.caption) : ""}</div></div>
        ${list.length > 1 ? `<button data-go="-1" aria-label="Previous">‹</button><span class="small muted">${i + 1}/${list.length}</span><button data-go="1" aria-label="Next">›</button>` : ""}
        <button data-close aria-label="Close">✕</button></div>`;
    $$("[data-go]", d).forEach((b) => b.onclick = () => go(+b.dataset.go));
    $("[data-close]", d).onclick = () => d.close();
  };
  const go = (n) => { i = (i + n + list.length) % list.length; draw(); };
  d.onkeydown = (e) => { if (e.key === "ArrowLeft") go(-1); else if (e.key === "ArrowRight") go(1); };
  d.onclick = (e) => { if (e.target === d) d.close(); };  // tap outside the photo
  d.addEventListener("close", () => d.remove());
  document.body.append(d);
  draw();
  d.showModal();
}

/* ---------- photo gallery: everything we've cooked, newest first ---------- */
const photoYear = (p) => (p.taken_on || "").slice(0, 4);
async function photosView() {
  const all = (await api("photos")).photos;  // already newest first
  if (!S.photoFilter) S.photoFilter = { book: "", year: "" };
  const F = S.photoFilter;
  const books = [...new Map(all.map((p) => [p.book_id, p.book_title])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1], undefined, { sensitivity: "base" }));
  const years = [...new Set(all.map(photoYear).filter(Boolean))].sort().reverse();
  if (F.book && !books.some(([id]) => String(id) === F.book)) F.book = "";  // a filter left from an earlier visit
  if (F.year && !years.includes(F.year)) F.year = "";
  const count = (fn) => all.filter(fn).length;
  view.innerHTML = `
    <div class="section-h"><h2>What we've cooked <span class="muted small" id="pcount"></span></h2></div>
    ${all.length ? `<div class="controls">
      <select id="pbook" aria-label="Cookbook"><option value="">All cookbooks</option>${books.map(([id, t]) =>
        `<option value="${id}">${esc(t)} (${count((p) => p.book_id === id)})</option>`).join("")}</select>
      <select id="pyear" aria-label="Year"><option value="">All years</option>${years.map((y) =>
        `<option value="${y}">${y} (${count((p) => photoYear(p) === y)})</option>`).join("")}</select>
    </div>` : ""}
    <div class="photos gallery" id="pgal"></div>`;
  if (!all.length) {
    $("#pgal").outerHTML = `<div class="empty">No photos yet.${ADMIN ? " Add some from a recipe's 📷 on its book's page." : ""}</div>`;
    return;
  }
  const draw = () => {
    const list = all.filter((p) => (!F.book || String(p.book_id) === F.book) && (!F.year || photoYear(p) === F.year));
    $("#pcount").textContent = list.length === all.length ? all.length : `${list.length} of ${all.length}`;
    const gal = $("#pgal");
    gal.innerHTML = list.length ? list.map((p, i) => `<figure class="pcard">
        <button class="ph" data-i="${i}" aria-label="View ${esc(p.recipe_name)}"><img loading="lazy" src="${esc(p.thumb)}" alt="${esc(p.recipe_name)}"
          ${p.width && p.height ? `width="${p.width}" height="${p.height}"` : ""}></button>
        <figcaption><a class="pn" href="#/book/${p.book_id}">${esc(p.recipe_name)}</a>
          <a class="pb" href="#/book/${p.book_id}">${esc(p.book_title)}${p.recipe_page ? ` · p. ${esc(p.recipe_page)}` : ""}</a>
          <span class="pd">${fmtDay(p.taken_on)}</span>${p.caption ? `<span class="pc">${esc(p.caption)}</span>` : ""}</figcaption></figure>`).join("")
      : `<div class="empty">No photos match those filters. <button type="button" class="linkish" id="pclear">Show all</button></div>`;
    $$(".ph", gal).forEach((el) => el.onclick = () => lightbox(list, +el.dataset.i));
    if ($("#pclear")) $("#pclear").onclick = () => { F.book = F.year = ""; $("#pbook").value = $("#pyear").value = ""; draw(); };
  };
  $("#pbook").value = F.book; $("#pyear").value = F.year;
  $("#pbook").onchange = (e) => { F.book = e.target.value; draw(); };
  $("#pyear").onchange = (e) => { F.year = e.target.value; draw(); };
  draw();
}

function recipeDialog(r, bookId, done) {
  const d = document.createElement("dialog");
  const v = r || {};
  d.innerHTML = `<form method="dialog">
    <h2>${r ? "Edit recipe" : "Add recipe"}</h2>
    <label class="f">Name<input name="name" required value="${esc(v.name)}"></label>
    <div class="row2 wide">
      <label class="f">Index heading <input name="heading" value="${esc(v.heading)}" placeholder="e.g. Chicken"></label>
      <label class="f">Page<input name="page" value="${esc(v.page)}" inputmode="numeric"></label></div>
    <label class="chk"><input type="checkbox" name="want_to_try" ${v.want_to_try ? "checked" : ""}> Want to try</label>
    <div class="row2">
      <label class="f">Rating (0–10)<input name="rating" type="number" min="0" max="10" step="0.5" value="${v.rating ?? ""}"></label>
      <label class="f">Last made<input name="last_made" type="date" value="${esc(v.last_made)}"></label></div>
    <label class="f">Notes<textarea name="notes">${esc(v.notes)}</textarea></label>
    <div class="actions" style="justify-content:space-between">
      ${r ? `<button type="button" class="danger" id="del">Delete</button>` : "<span></span>"}
      <span style="display:flex;gap:8px"><button value="cancel" formnovalidate>Cancel</button><button class="primary" value="save">Save</button></span></div>
  </form>`;
  document.body.append(d);
  d.addEventListener("close", async () => {
    if (d.returnValue === "save") {
      const fd = new FormData($("form", d));
      const body = Object.fromEntries(fd); body.want_to_try = fd.has("want_to_try");
      try {
        await api(r ? "recipes/" + r.id : `books/${bookId}/recipes`, { method: r ? "PUT" : "POST", body });
        toast(r ? "Saved" : "Recipe added"); done();
      } catch (e) { toast(e.message); }
    }
    d.remove();
  });
  const del = $("#del", d);
  if (del) del.onclick = () => busy(del, async () => {
    if (!confirm(`Delete “${r.name}”? You can undo it under More → Recent changes, but its photos are deleted for good.`)) return;
    await api("recipes/" + r.id, { method: "DELETE" }); d.close(); toast("Deleted"); done();
  });
  d.showModal();
}

/* ---------- house recipes: the recipe page (cooking mode) and the editor ---------- */
const MINUTES = (m) => m == null ? "" : m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`;
const FRACS = [[1 / 8, "⅛"], [1 / 4, "¼"], [1 / 3, "⅓"], [3 / 8, "⅜"], [1 / 2, "½"], [5 / 8, "⅝"], [2 / 3, "⅔"], [3 / 4, "¾"], [7 / 8, "⅞"]];
// A scaled amount the way a cook writes it: 1.5 -> "1½", 0.333 -> "⅓", 750 -> "750", 2.4 -> "2.4".
function fmtQty(x) {
  if (x >= 10) return String(Math.round(x * 10) / 10);
  const whole = Math.floor(x), frac = x - whole;
  if (frac < 0.03) return String(whole);
  if (frac > 0.97) return String(whole + 1);
  const f = FRACS.find(([v]) => Math.abs(frac - v) < 0.03);
  return f ? `${whole || ""}${f[1]}` : String(Math.round(x * 100) / 100);
}
// The quantity to show: as typed at 1×, scaled from quantity_num (and quantity_max for "2-3") otherwise.
const qtyText = (l, k) => k === 1 || l.quantity_num == null ? (l.quantity_text || "")
  : fmtQty(l.quantity_num * k) + (l.quantity_max != null ? `–${fmtQty(l.quantity_max * k)}` : "");
const sectioned = (list, li) => { let sec = null; return list.map((x, i) => {
  const head = x.section && x.section !== sec ? `<li class="sec">${esc(x.section)}</li>` : "";
  sec = x.section; return head + li(x, i);
}).join(""); };

async function recipeView(uid) {
  let r;
  try { r = await api("recipes/by-uid/" + uid); }
  catch (e) {
    if (e.status !== 404) throw e;
    throw new Error(ADMIN ? "That recipe isn't in this catalog."
      : "That recipe isn't in the public catalog. House recipes are for signed-in members: try the same link under /admin.");
  }
  const d = r.detail || {}, base = d.servings || null, st = { k: 1, servings: base };
  const scalable = r.ingredients.some((l) => l.quantity_num != null);
  const facts = [d.yield_text && ["Makes", esc(d.yield_text)], base && ["Serves", base],
    d.prep_minutes != null && ["Prep", MINUTES(d.prep_minutes)], d.cook_minutes != null && ["Cook", MINUTES(d.cook_minutes)],
    d.total_minutes != null && ["Total", MINUTES(d.total_minutes)]].filter(Boolean);
  const chips = [...(d.methods || []), ...(d.equipment || [])];
  const log = ADMIN && S.features && S.features.dinnerlog_url ? `${esc(S.features.dinnerlog_url)}/new?source_type=original&source_title=${encodeURIComponent(r.book.title)}`
    + `&name=${encodeURIComponent(r.name)}&cookbook_book_id=${r.book.id}&cookbook_recipe_id=${r.id}`
    + `&cookbook_book_uid=${r.book.uid}&cookbook_recipe_uid=${r.uid}` : "";
  if (!r.detail && !r.ingredients.length && !r.steps.length && r.book.kind !== "house") return location.replace(`#/book/${r.book.id}`);
  view.innerHTML = `<a class="back" href="#/book/${r.book.id}">← ${esc(r.book.title)}</a>
    <article class="recipe">
      <h1>${esc(r.name)}</h1>
      <p class="print-only" id="psv"></p>
      ${facts.length ? `<dl class="facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>` : ""}
      ${d.description ? `<p class="desc">${esc(d.description)}</p>` : ""}
      ${chips.length ? `<div class="tags">${chips.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div>` : ""}
      <div class="cookctl">
        ${scalable ? (base ? `<div class="scale" role="group" aria-label="Servings"><button type="button" data-d="-1" aria-label="Fewer servings">−</button>
            <span id="sv" aria-live="polite">Serves ${base}</span><button type="button" data-d="1" aria-label="More servings">+</button></div>`
          : `<div class="seg" role="group" aria-label="Scale the recipe">${[0.5, 1, 2, 3].map((k) =>
            `<button type="button" data-k="${k}" class="${k === 1 ? "on" : ""}" aria-pressed="${k === 1}">${k === 0.5 ? "½" : k}×</button>`).join("")}</div>`) : ""}
        ${"wakeLock" in navigator ? `<label class="chk"><input type="checkbox" id="wake"> Keep screen on</label>` : ""}
        ${ADMIN ? `<span class="acts2">${log ? `<a class="btn" href="${log}" target="_blank" rel="noopener" title="Log this dinner in Dinner Log">🍽 Log</a>` : ""}
          <a class="btn" href="#/recipe/${r.uid}/edit">Edit</a></span>` : ""}
      </div>
      <div class="rcols">
        <section><h2>Ingredients</h2>${r.ingredients.length ? `<ul class="ings" id="ings"></ul>` : `<p class="muted">None listed.</p>`}</section>
        <section><h2>Steps</h2>${r.steps.length ? `<ol class="rsteps">${sectioned(r.steps, (x, i) => `<li data-i="${i}"><p>${esc(x.text)}</p>
          ${x.timer_minutes ? `<button type="button" class="timer" data-m="${x.timer_minutes}" aria-label="Timer, ${MINUTES(x.timer_minutes)}">⏱ ${MINUTES(x.timer_minutes)}</button>` : ""}</li>`)}</ol>`
          : `<p class="muted">None yet.</p>`}</section>
      </div>
      ${d.notes ? `<section class="rnotes"><h2>Notes</h2><p>${esc(d.notes)}</p></section>` : ""}
      ${d.inspiration ? `<p class="small muted">Inspired by ${esc(d.inspiration)}</p>` : ""}
      ${ADMIN && r.notes ? `<div class="private">${esc(r.notes)}</div>` : ""}
    </article>`;
  const drawIngs = () => {
    const ul = $("#ings"); if (!ul) return;
    const done = new Set($$("#ings input:checked").map((x) => +x.dataset.i));
    ul.innerHTML = sectioned(r.ingredients, (l, i) => `<li class="${done.has(i) ? "done" : ""}"><label><input type="checkbox" data-i="${i}" ${done.has(i) ? "checked" : ""}>
      <span><span class="q">${esc([qtyText(l, st.k), l.unit].filter(Boolean).join(" "))}</span> ${esc(l.item)}${l.prep_note ? `<span class="muted">, ${esc(l.prep_note)}</span>` : ""}${l.optional ? ` <span class="muted small">(optional)</span>` : ""}</span></label></li>`);
    $$("#ings input").forEach((x) => x.onchange = () => x.closest("li").classList.toggle("done", x.checked));
  };
  // The printed page has no controls, so it says how the recipe is scaled: "Serves 6", or "2× the recipe".
  const printServes = () => { $("#psv").textContent = st.servings ? `Serves ${st.servings}` : st.k !== 1 ? `${fmtQty(st.k)}× the recipe` : ""; };
  drawIngs(); printServes();
  $$(".scale button").forEach((b) => b.onclick = () => {
    st.servings = Math.max(1, Math.min(99, st.servings + +b.dataset.d));
    st.k = st.servings / base; $("#sv").textContent = `Serves ${st.servings}`; drawIngs(); printServes();
  });
  $$(".cookctl .seg button").forEach((b) => b.onclick = () => {
    st.k = +b.dataset.k; drawIngs(); printServes();
    $$(".cookctl .seg button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
  });
  $$(".rsteps li[data-i] p").forEach((p) => p.onclick = () => p.closest("li").classList.toggle("done"));
  $$(".timer").forEach((b) => b.onclick = () => stepTimer(b));
  const wake = $("#wake");
  if (wake) {
    const lock = async () => { try { S.wake = await navigator.wakeLock.request("screen"); } catch { wake.checked = false; } };
    wake.onchange = () => { if (wake.checked) lock(); else if (S.wake) { S.wake.release().catch(() => {}); S.wake = null; } };
    document.addEventListener("visibilitychange", () => {   // the browser drops the lock when the tab is hidden
      if (document.visibilityState === "visible" && wake.isConnected && wake.checked && !S.wake) lock();
    });
  }
}
// A step's countdown: tap to start, tap again to stop. Lives in the page only (nothing is saved).
function stepTimer(b) {
  if (b._t) { clearInterval(b._t); b._t = null; b.classList.remove("on"); b.textContent = `⏱ ${MINUTES(+b.dataset.m)}`; return; }
  const end = Date.now() + +b.dataset.m * 60e3;
  b.classList.add("on");
  const tick = () => {
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    b.textContent = `⏱ ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    if (!left) {
      clearInterval(b._t); b._t = null; b.classList.remove("on"); b.textContent = "⏱ Done";
      toast("Timer done"); if (navigator.vibrate) navigator.vibrate([300, 150, 300]);
    }
  };
  tick(); b._t = setInterval(tick, 1000);
}

// The editor's rows: section headings are rows of their own here, and become each line's `section` when saved.
function toRows(list) {
  const rows = []; let sec = null;
  for (const x of list) { if (x.section && x.section !== sec) rows.push({ sec: x.section }); sec = x.section; rows.push({ ...x }); }
  return rows;
}
function fromRows(rows) {
  let sec = null; const out = [];
  for (const x of rows) { if ("sec" in x) sec = (x.sec || "").trim() || null; else out.push({ ...x, section: sec }); }
  return out;
}
const UNITS = ["g", "kg", "ml", "l", "tsp", "tbsp", "cup", "cups", "oz", "lb", "pinch", "clove", "cloves", "can", "bunch", "slice", "slices"];
const blankLine = () => ({ quantity_text: "", unit: "", item: "", prep_note: "", optional: false });
const blankStep = () => ({ text: "", timer_minutes: "" });

/* ---------- start a house recipe from photos or pasted text ---------- */
// Photos (1 to 4, in order) are read on the server as a background job that this page polls, so a slow read never
// meets Cloudflare's 100-second limit. Pasted text is parsed on the server straight away when it has Ingredients and
// Method headings; otherwise it becomes a job too. Either way the result only fills the editor below. Everything the
// server sends back from the model is shown through esc() or as input values, never as markup.
const CAP_MAX = 4, CAP_BYTES = 24e6;   // the server's limits: 4 photos, a 25 MB request
function captureHTML(cap) {
  const canPhoto = S.features && S.features.photo_import;
  const note = cap ? `<div class="panel capnote" id="cap-note">
      <strong>Read from ${cap.source === "text" ? "your text" : `the photo${(cap.photos || 1) > 1 ? "s" : ""}`}. Check it against the original, then save.</strong>
      ${cap.missing.length ? `<p class="err">Not found: ${esc(cap.missing.join(", "))}. Fill ${cap.missing.length > 1 ? "them" : "it"} in below.</p>` : ""}
      ${cap.warnings.length ? `<ul class="small">${cap.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      ${cap.attachable ? `<label class="chk"><input type="checkbox" id="cap-attach"> Add the photo${cap.attachable > 1 ? "s" : ""} to this recipe when it's saved</label>
        <p class="small muted">Otherwise ${cap.attachable > 1 ? "they are" : "it is"} deleted from the server.</p>` : ""}</div>` : "";
  return `${note}<div class="panel capture" id="capture">
      <div class="actions" style="margin-top:0">
        ${canPhoto ? `<button type="button" id="cap-photo">📷 From photo</button>` : ""}
        <button type="button" id="cap-text">📋 From text</button></div>
      <div id="cap-photo-box" class="hidden">
        <p class="small muted">Up to ${CAP_MAX} photos of one recipe (a card, a notebook page, a printed page), in order. They are sent to
          Claude (Anthropic's API) to read; the result fills the form for you to check.</p>
        <div class="actions"><label class="btn">📷 Take photo<input id="cap-snap" type="file" accept="image/*" capture="environment" class="vh"></label>
          <label class="btn">Choose photos<input id="cap-pick" type="file" accept="image/*" multiple class="vh"></label>
          <button type="button" class="primary" id="cap-read" disabled>Read recipe</button></div>
        <div id="cap-pages" class="scanpages hidden"></div></div>
      <div id="cap-text-box" class="hidden">
        <textarea id="cap-txt" rows="8" aria-label="Recipe text" placeholder="Paste a recipe: its name, then an “Ingredients” heading and a “Method” heading."></textarea>
        <p class="small muted">With those two headings it's read here; otherwise ${canPhoto ? "it's sent to Claude to read." : "add them (Claude isn't set up on this server)."}</p>
        <div class="actions"><button type="button" class="primary" id="cap-go">Read text</button></div></div>
      <div id="cap-status" class="small"></div></div>`;
}
function discardCapture(job, keepalive = false) {
  if (!job) return;
  fetch(`${API}/house/capture/${encodeURIComponent(job)}`, { method: "DELETE", keepalive }).catch(() => {});
}
// After the recipe is saved: attach the kept photos, or delete them. Returns a toast for a failed attach, else "".
async function finishCapture(recipeId, attach) {
  const job = S.capJob;
  S.capJob = null;
  if (!job) return "";
  if (!attach) { discardCapture(job); return ""; }
  try { await api(`house/capture/${encodeURIComponent(job)}/attach`, { method: "POST", body: { recipe_id: recipeId } }); return ""; }
  catch (err) { return `Saved, but the photo wasn't added: ${err.message}`; }
}
function wireCapture(params, isDirty) {
  const box = $("#capture");
  if (!box) return;
  const here = location.hash;
  const pend = [];    // {file, url}, in the order taken
  let busyRead = false;
  const status = (html, bad) => { const st = $("#cap-status"); if (st) st.innerHTML = html ? `<div class="${bad ? "err" : "muted"}">${html}</div>` : ""; };
  const show = (which) => {
    $("#cap-photo-box").classList.toggle("hidden", which !== "photo");
    $("#cap-text-box").classList.toggle("hidden", which !== "text");
    if (which === "text") $("#cap-txt").focus();
  };
  if ($("#cap-photo")) $("#cap-photo").onclick = () => show("photo");
  $("#cap-text").onclick = () => show("text");
  const drawPages = (state) => {
    const strip = $("#cap-pages");
    strip.classList.toggle("hidden", !pend.length);
    const srv = Object.fromEntries(((state && state.photos) || []).map((p) => [p.page_no, p]));
    strip.innerHTML = pend.map((pg, i) => {
      const p = srv[i + 1];
      const pill = !p ? "" : p.status === "done" ? "read" : p.status === "error" ? "failed" : p.status === "running" ? `reading… ${p.elapsed ? p.elapsed + "s" : ""}` : "waiting";
      return `<div class="scanpage ${p ? p.status : ""}" data-i="${i}" title="${esc((p && p.error) || "")}">
        <img src="${pg.url}" alt="Photo ${i + 1}"><div class="pn">Photo ${i + 1}</div><div class="ps">${esc(pill)}</div>
        ${busyRead ? "" : `<button type="button" class="retry" data-rm="${i}" aria-label="Remove photo ${i + 1}">Remove</button>`}</div>`;
    }).join("");
    $$("[data-rm]", strip).forEach((b) => b.onclick = () => { const [x] = pend.splice(+b.dataset.rm, 1); URL.revokeObjectURL(x.url); drawPages(); });
    $("#cap-read").disabled = busyRead || !pend.length;
  };
  const addFiles = (e) => {
    const files = [...e.target.files]; e.target.value = "";
    for (const f of files) {
      if (pend.length >= CAP_MAX) { status(`At most ${CAP_MAX} photos of one recipe.`, true); break; }
      pend.push({ file: f, url: URL.createObjectURL(f) });
    }
    drawPages();
  };
  $("#cap-snap") && ($("#cap-snap").onchange = addFiles);
  $("#cap-pick") && ($("#cap-pick").onchange = addFiles);
  const load = (j, job) => {
    if (location.hash !== here) { discardCapture(job); return; }
    if (isDirty() && !confirm("Replace what's in the form with the recipe that was read?")) { discardCapture(job); return; }
    pend.forEach((p) => URL.revokeObjectURL(p.url));
    pend.length = 0;   // its previews are gone; nothing may draw them again
    if (S.capJob && S.capJob !== job) discardCapture(S.capJob);
    S.capJob = job || null;
    recipeEditView(null, params, { draft: j.draft, warnings: j.warnings || [], missing: j.missing || [],
      attachable: j.attachable || 0, source: j.source, photos: (j.photos || []).length })
      .then(() => window.scrollTo(0, 0)).catch((err) => toast(err.message));
  };
  const poll = async (job) => {
    const t0 = Date.now();
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 2500));
      if (location.hash !== here) { discardCapture(job); return; }
      let j;
      try { j = await api(`house/capture/${encodeURIComponent(job)}`); }
      catch (err) {
        if ([400, 401, 404].includes(err.status) || Date.now() - t0 > 20 * 60000) { status(esc(err.message), true); return null; }
        continue;   // a dropped connection: keep checking
      }
      drawPages(j);
      if (j.status === "running") { status(`Reading… ${Math.round((Date.now() - t0) / 1000)}s. A dense page can take a minute.`); continue; }
      if (j.status === "error") { status(esc(j.error), true); discardCapture(job); return null; }
      return j;
    }
  };
  $("#cap-read").onclick = async () => {
    if (busyRead || !pend.length) return;
    const total = pend.reduce((n, p) => n + p.file.size, 0);
    if (total > CAP_BYTES) { status("Those photos are too large to send together. Use fewer, or smaller ones.", true); return; }
    const fd = new FormData();
    pend.forEach((p) => fd.append("file", p.file));
    busyRead = true; drawPages(); status("Sending…");
    try {
      const r = await api("house/capture/photos", { method: "POST", body: fd });
      const j = await poll(r.job);
      if (j) load(j, r.job);
    } catch (err) { status(esc(err.message), true); }
    finally { busyRead = false; if ($("#cap-pages")) drawPages(); }
  };
  $("#cap-go").onclick = () => busy($("#cap-go"), async () => {
    const text = $("#cap-txt").value;
    if (!text.trim()) { status("Paste the recipe's text first.", true); return; }
    status("Reading…");
    try {
      const r = await api("house/capture/text", { method: "POST", body: { text } });
      if (r.job) { const j = await poll(r.job); if (j) load(j, r.job); }
      else load(r, null);
    } catch (err) { status(esc(err.message), true); }
  });
}

// `cap`: a recipe just read from photos or pasted text ({draft, warnings, missing, attachable, source}). It fills the
// new-recipe form as an unsaved draft; nothing is stored until Save. The capture's job id is S.capJob.
async function recipeEditView(uid, params, cap = null) {
  const r = uid ? await api("recipes/by-uid/" + uid) : null;
  if (r && r.book.kind !== "house") throw new Error("Only house recipes have ingredients and steps. Edit this one from its book.");
  const house = r ? r.book : await api("house");
  const src = r || (cap && cap.draft) || null;
  const d = (src && src.detail) || {};
  const ret = params.get("return") || "";
  const rows = { ing: toRows(src ? src.ingredients : []), step: toRows(src ? src.steps : []) };
  if (!rows.ing.length) rows.ing.push(blankLine());
  if (!rows.step.length) rows.step.push(blankStep());
  const cancelTo = r ? `#/recipe/${r.uid}` : `#/book/${house.id}`;
  const v = (k) => esc(d[k] ?? "");
  view.innerHTML = `<a class="back" href="${cancelTo}">← ${esc(r ? r.name : house.title)}</a>
    <h1>${r ? "Edit recipe" : "New recipe"}</h1>
    ${r ? "" : captureHTML(cap)}
    <form id="rf-edit" class="redit" autocomplete="off">
      <label class="f">Name<input name="name" required maxlength="200" value="${esc(r ? r.name : ((cap && cap.draft.name) || params.get("title") || "").slice(0, 200))}"></label>
      <label class="f">Description<textarea name="description" rows="2">${v("description")}</textarea></label>
      <div class="row3">
        <label class="f">Serves<input name="servings" inputmode="numeric" value="${v("servings")}"></label>
        <label class="f">Makes<input name="yield_text" placeholder="e.g. 1 loaf" value="${v("yield_text")}"></label></div>
      <div class="row3">
        <label class="f">Prep (min)<input name="prep_minutes" inputmode="numeric" value="${v("prep_minutes")}"></label>
        <label class="f">Cook (min)<input name="cook_minutes" inputmode="numeric" value="${v("cook_minutes")}"></label>
        <label class="f">Total (min)<input name="total_minutes" inputmode="numeric" value="${v("total_minutes")}"></label></div>
      <label class="f">Methods <span class="small">(comma separated)</span><input name="methods" placeholder="braise, grill" value="${esc((d.methods || []).join(", "))}"></label>
      <label class="f">Equipment <span class="small">(comma separated)</span><input name="equipment" placeholder="Dutch oven" value="${esc((d.equipment || []).join(", "))}"></label>
      <h2>Ingredients</h2>
      <ul class="erows" id="ing-rows"></ul>
      <div class="actions"><button type="button" data-add="ing">+ Ingredient</button><button type="button" data-add="ing-sec">+ Section</button></div>
      <h2>Steps</h2>
      <ol class="erows steps-e" id="step-rows"></ol>
      <div class="actions"><button type="button" data-add="step">+ Step</button><button type="button" data-add="step-sec">+ Section</button></div>
      <label class="f">Notes<textarea name="notes" rows="3">${v("notes")}</textarea></label>
      <label class="f">Inspiration <span class="small">(where the idea came from)</span><input name="inspiration" value="${v("inspiration")}"></label>
      <p class="small muted" id="ret-note"></p>
      <div class="savebar">
        ${r ? `<button type="button" class="danger" id="rdel">Delete</button>` : "<span></span>"}
        <span class="actions" style="margin:0"><a class="btn" href="${cancelTo}">Cancel</a><button class="primary" id="rsave">Save</button></span>
      </div>
      <datalist id="dl-units">${UNITS.map((u) => `<option value="${u}">`).join("")}</datalist>
    </form>`;
  const form = $("#rf-edit");
  form.addEventListener("keydown", (e) => {   // a phone's Go key in a one-line field must not save half a recipe
    if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault();
  });
  const ctl = (i, n) => `<div class="ectl"><button type="button" data-a="up" aria-label="Move up" ${i ? "" : "disabled"}>↑</button>
    <button type="button" data-a="down" aria-label="Move down" ${i < n - 1 ? "" : "disabled"}>↓</button><button type="button" data-a="del" aria-label="Remove">✕</button></div>`;
  const rowHTML = {
    ing: (x, i, n) => "sec" in x ? `<li class="erow sec" data-i="${i}"><div class="ef"><input data-k="sec" placeholder="Section, e.g. For the sauce" aria-label="Ingredient section" value="${esc(x.sec)}"></div>${ctl(i, n)}</li>`
      : `<li class="erow" data-i="${i}"><div class="ef iline">
          <input data-k="quantity_text" placeholder="Qty" aria-label="Quantity" value="${esc(x.quantity_text)}">
          <input data-k="unit" placeholder="Unit" aria-label="Unit" list="dl-units" value="${esc(x.unit)}">
          <input data-k="item" placeholder="Ingredient" aria-label="Ingredient" enterkeyhint="next" value="${esc(x.item)}">
          <input data-k="prep_note" placeholder="Note, e.g. finely chopped" aria-label="Note" value="${esc(x.prep_note)}">
          <label class="chk"><input type="checkbox" data-k="optional" ${x.optional ? "checked" : ""}> optional</label></div>${ctl(i, n)}</li>`,
    step: (x, i, n) => "sec" in x ? `<li class="erow sec" data-i="${i}"><div class="ef"><input data-k="sec" placeholder="Section, e.g. The day before" aria-label="Step section" value="${esc(x.sec)}"></div>${ctl(i, n)}</li>`
      : `<li class="erow" data-i="${i}"><div class="ef step"><textarea data-k="text" rows="2" placeholder="What to do" aria-label="Step">${esc(x.text)}</textarea>
          <input data-k="timer_minutes" inputmode="numeric" placeholder="Timer (min)" aria-label="Timer in minutes" value="${esc(x.timer_minutes ?? "")}"></div>${ctl(i, n)}</li>`,
  };
  const dirty = () => { S.dirty = true; window.onbeforeunload = () => true; };
  const draw = (kind, focus) => {
    const ul = $(kind === "ing" ? "#ing-rows" : "#step-rows"), list = rows[kind];
    ul.innerHTML = list.map((x, i) => rowHTML[kind](x, i, list.length)).join("");
    $$(".erow", ul).forEach((li) => {
      const i = +li.dataset.i;
      $$("[data-k]", li).forEach((el) => el.addEventListener(el.type === "checkbox" ? "change" : "input", () => {
        list[i][el.dataset.k] = el.type === "checkbox" ? el.checked : el.value; dirty();
      }));
      $$(".ectl button", li).forEach((b) => b.onclick = () => {
        const a = b.dataset.a, j = a === "up" ? i - 1 : i + 1;
        if (a === "del") list.splice(i, 1);
        else [list[i], list[j]] = [list[j], list[i]];
        if (!list.length) list.push(kind === "ing" ? blankLine() : blankStep());
        dirty(); draw(kind, a === "del" ? null : j);
      });
      // Enter in the last ingredient's name adds the next row, so a list can be typed without the mouse
      const item = $("[data-k=item]", li);
      if (item) item.onkeydown = (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        if (i === list.length - 1) { list.push(blankLine()); draw(kind, list.length - 1); }
        else { const nx = $(`.erow[data-i="${i + 1}"] input`, ul); if (nx) nx.focus(); }
      };
    });
    if (focus != null) { const el = $(`.erow[data-i="${focus}"] [data-k]`, ul); if (el) el.focus(); }
  };
  draw("ing"); draw("step");
  if (!r) wireCapture(params, () => S.dirty);
  if (cap) dirty();   // a draft that was read, not yet saved
  $$("[data-add]", form).forEach((b) => b.onclick = () => {
    const [kind, sec] = b.dataset.add.split("-");
    rows[kind].push(sec ? { sec: "" } : kind === "ing" ? blankLine() : blankStep());
    dirty(); draw(kind, rows[kind].length - 1);
  });
  $$("input[name], textarea[name]", form).forEach((el) => el.addEventListener("input", dirty));
  if (ret) {   // the server decides whether the return link may be followed; this only labels the button
    try {
      const c = await api("house/return-check?url=" + encodeURIComponent(ret));
      if (c.ok) $("#rsave").textContent = c.host ? `Save and return to ${c.host}` : "Save and go back";
      else $("#ret-note").textContent = "The link to return to isn't on the allowed list (RETURN_URL_ORIGINS), so saving opens the recipe here.";
    } catch { /* the label stays "Save" */ }
  }
  form.onsubmit = (e) => {
    e.preventDefault();
    busy($("#rsave"), async () => {
      const f = Object.fromEntries(new FormData(form));
      const body = { ...f, methods: f.methods.split(","), equipment: f.equipment.split(","),
        ingredients: fromRows(rows.ing), steps: fromRows(rows.step), ...(ret ? { return: ret } : {}) };
      const saved = await api(r ? `house/recipes/${r.id}` : "house/recipes", { method: r ? "PUT" : "POST", body });
      S.dirty = false; window.onbeforeunload = null;
      const photoNote = r ? "" : await finishCapture(saved.id, $("#cap-attach") && $("#cap-attach").checked);
      if (saved.return_url) { location.assign(saved.return_url); return; }
      toast(photoNote || (saved.return_refused ? "Saved. That return link isn't allowed, so here's the recipe." : "Saved"));
      location.hash = `#/recipe/${saved.uid}`;
    });
  };
  const del = $("#rdel");
  if (del) del.onclick = () => busy(del, async () => {
    if (!confirm(`Delete “${r.name}”? Its ingredients, steps and photos go too. Undo (More → Recent changes) brings back everything but the photos.`)) return;
    await api("recipes/" + r.id, { method: "DELETE" });
    S.dirty = false; window.onbeforeunload = null; toast("Deleted"); location.hash = `#/book/${house.id}`;
  });
}

/* ---------- shelf locations (editor) ---------- */
// <option>s for a location <select>: the full path is the label ("Kitchen › Pantry › Shelf 2"); `skip` is a Set of ids to leave out.
function locationOptions(list, selected, { none = "(none)", skip = null } = {}) {
  const cur = String(selected ?? "");
  return `<option value="">${esc(none)}</option>` + list.filter((l) => !skip || !skip.has(l.id)).map((l) =>
    `<option value="${l.id}" ${String(l.id) === cur ? "selected" : ""}>${esc(l.path)}</option>`).join("");
}
// The picker's "Manage" button: edit the tree in a dialog, then rebuild the <select> (a deleted shelf falls back to "(none)").
function wireLocationPicker(form) {
  const btn = $("#loc-manage", form);
  if (!btn) return;
  btn.onclick = () => locationsManager(async () => {
    const sel = $("#loc-pick", form);
    if (!sel) return;
    try { sel.innerHTML = locationOptions((await api("locations")).locations, sel.value); } catch (e) { toast(e.message); }
  });
}
// Add, rename, move, reorder and delete locations. Tap a location to see its actions. Feedback is drawn inside the
// dialog: the page's toast sits behind a modal. `changed()` runs once on close if anything was edited.
function locationsManager(changed = () => {}) {
  const d = document.createElement("dialog");
  d.className = "locmgr";
  let locs = [], sel = null, mode = null, err = "", note = "", dirty = false;
  const kids = (parent) => locs.filter((l) => l.parent_id === parent);          // tree order, so siblings are in sort order
  const below = (id) => { const s = new Set([id]); for (const l of locs) if (s.has(l.parent_id)) s.add(l.id); return s; };   // id and its descendants
  const books = (n) => `${n} book${n === 1 ? "" : "s"}`;
  const panel = (l) => {
    const sibs = kids(l.parent_id), i = sibs.findIndex((s) => s.id === l.id);
    if (mode === "rename") return `<div class="lm-act"><input id="lm-in" value="${esc(l.name)}" maxlength="80" aria-label="Name">
      <button type="button" class="primary" data-do="rename">Save</button><button type="button" data-do="cancel">Cancel</button></div>`;
    if (mode === "add") return `<div class="lm-act"><input id="lm-in" placeholder="New location inside ${esc(l.name)}" maxlength="80" aria-label="New location name">
      <button type="button" class="primary" data-do="add">Add</button><button type="button" data-do="cancel">Cancel</button></div>`;
    if (mode === "move") return `<div class="lm-act"><select id="lm-in" aria-label="Move to">${locationOptions(locs, l.parent_id, { none: "(top level)", skip: below(l.id) })}</select>
      <button type="button" class="primary" data-do="move">Move</button><button type="button" data-do="cancel">Cancel</button></div>`;
    return `<div class="lm-act">
      <button type="button" data-do="up" aria-label="Move up" ${i === 0 ? "disabled" : ""}>↑</button>
      <button type="button" data-do="down" aria-label="Move down" ${i === sibs.length - 1 ? "disabled" : ""}>↓</button>
      <button type="button" data-do="addform">Add inside</button><button type="button" data-do="renameform">Rename</button>
      <button type="button" data-do="moveform">Move</button><button type="button" class="danger" data-do="delete">Delete</button></div>`;
  };
  const row = (l) => `<li class="${l.id === sel ? "on" : ""}" style="--d:${+l.depth}">
    <button type="button" class="lm-name" data-sel="${l.id}" aria-expanded="${l.id === sel}"><span>${esc(l.name)}</span><span class="muted small">${books(l.n)}</span></button>
    ${l.id === sel ? panel(l) : ""}</li>`;
  const render = () => {
    d.innerHTML = `<div class="lm-head"><h2>Shelf locations</h2><button type="button" data-do="close">Done</button></div>
      <p class="small muted">Rooms, shelves, anything nested. A book is in one location. Tap a location to change it. Locations that hold a book show on the public catalog.</p>
      ${err ? `<p class="lm-err" role="alert">${esc(err)}</p>` : note ? `<p class="lm-note" role="status">${esc(note)}</p>` : ""}
      <ul class="lm-list">${locs.length ? locs.map(row).join("") : `<li class="empty">No locations yet. Add a room below.</li>`}</ul>
      <div class="lm-add"><input id="lm-top" placeholder="New top-level location" maxlength="80" aria-label="New top-level location">
        <button type="button" class="primary" data-do="addtop">Add</button></div>`;
    const f = $("#lm-in", d);
    if (f) { f.focus(); if (f.select) f.select(); }
    const on = $("li.on", d);
    if (on) on.scrollIntoView({ block: "nearest" });     // the action bar can sit below the fold of a long list
  };
  const reload = async () => { locs = (await api("locations")).locations; };
  const run = async (btn, fn) => {
    if (btn.disabled) return;
    btn.disabled = true;
    try { await fn(); dirty = true; err = ""; }
    catch (e) { err = e.message; note = ""; }
    render();
  };
  d.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.sel) { const id = +t.dataset.sel; sel = sel === id ? null : id; mode = null; err = note = ""; return render(); }
    const l = locs.find((x) => x.id === sel), v = ($("#lm-in", d) || {}).value, act = t.dataset.do;
    if (act === "close") return d.close();
    if (act === "cancel") { mode = null; err = ""; return render(); }
    if (act === "addform" || act === "renameform" || act === "moveform") { mode = act.replace("form", ""); err = note = ""; return render(); }
    if (act === "addtop") return run(t, async () => {
      const name = $("#lm-top", d).value;
      const r = await api("locations", { method: "POST", body: { name, parent_id: null } });
      await reload(); note = `Added “${r.location.name}”.`;
    });
    if (!l) return;
    if (act === "add") return run(t, async () => {
      const r = await api("locations", { method: "POST", body: { name: v, parent_id: l.id } });
      await reload(); mode = null; note = `Added “${r.location.name}” inside “${l.name}”.`;
    });
    if (act === "rename") return run(t, async () => {
      await api("locations/" + l.id, { method: "PUT", body: { name: v } });
      await reload(); mode = null; note = "Renamed.";
    });
    if (act === "move") return run(t, async () => {
      await api("locations/" + l.id, { method: "PUT", body: { parent_id: v === "" ? null : +v } });
      await reload(); mode = null; note = `Moved “${l.name}”.`;
    });
    if (act === "up" || act === "down") return run(t, async () => {
      const ids = kids(l.parent_id).map((s) => s.id), i = ids.indexOf(l.id), j = i + (act === "up" ? -1 : 1);
      if (j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      locs = (await api("locations/reorder", { method: "POST", body: { parent_id: l.parent_id, ids } })).locations; note = "";
    });
    if (act === "delete") {
      const subs = below(l.id).size - 1;
      const msg = `Delete “${l.path}”?` + (subs ? ` Its ${subs} sub-location${subs === 1 ? "" : "s"} will be deleted too.` : "")
        + (l.n ? ` ${books(l.n)} will become unshelved.` : " No books are shelved there.");
      if (!confirm(msg)) return;
      return run(t, async () => {
        const r = await api("locations/" + l.id, { method: "DELETE" });
        await reload(); sel = null; mode = null;
        note = `Deleted “${l.name}”${r.books_unshelved ? `: ${books(r.books_unshelved)} unshelved` : ""}.`;
      });
    }
  });
  d.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.target.tagName !== "INPUT") return;
    e.preventDefault();
    const go = e.target.id === "lm-top" ? $("[data-do=addtop]", d) : $("button.primary", e.target.parentElement);
    if (go) go.click();
  });
  d.addEventListener("cancel", (e) => { if (mode) { e.preventDefault(); mode = null; render(); } });   // Esc backs out of an open form first
  d.addEventListener("close", () => { d.remove(); if (dirty) changed(); });
  document.body.append(d);
  d.showModal();
  d.innerHTML = `<p class="muted">Loading…</p>`;
  reload().then(render).catch((e) => { err = e.message; render(); });
}

/* ---------- edit / add ---------- */
function bookForm(b = {}) {
  const f = S.facets || { cuisines: [], tags: [], publishers: [] };
  return `<form class="grid2" id="bf" autocomplete="off">
    <label class="f full">Title<input name="title" required value="${esc(b.title)}"></label>
    <label class="f full">Subtitle<input name="subtitle" value="${esc(b.subtitle)}"></label>
    <label class="f full">Authors <span class="small">(separate with semicolons)</span><input name="authors" value="${esc((b.authors || []).join("; "))}"></label>
    <label class="f">Publisher<input name="publisher" list="dl-pub" value="${esc(b.publisher)}"></label>
    <label class="f">Year<input name="year" inputmode="numeric" value="${esc(b.year)}"></label>
    <label class="f">Cuisine<input name="cuisine" list="dl-cuisine" value="${esc(b.cuisine)}"></label>
    <label class="f">ISBN<input name="isbn" inputmode="numeric" value="${esc(b.isbn)}"></label>
    <label class="f">Format<input name="format" list="dl-format" value="${esc(b.format)}"></label>
    <datalist id="dl-format"><option value="Hardcover"><option value="Paperback"><option value="Spiral-bound"><option value="Ebook"></datalist>
    <label class="f full">Tags <span class="small">(comma separated)</span><input name="tags" list="dl-tags" value="${esc((b.tags || []).join(", "))}"></label>
    <label class="f full">Description <span class="small">(public)</span><textarea name="description">${esc(b.description)}</textarea></label>
    <label class="f">Status<select name="status">${Object.entries(STATUS).map(([k, t]) => `<option value="${k}" ${(b.status || "owned") === k ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label class="f">Loaned to<input name="loaned_to" value="${esc(b.loaned_to)}"></label>
    <label class="f">Rating<select name="rating"><option value="">Not rated</option>${[0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5].map((v) =>
      `<option value="${v}" ${b.rating === v ? "selected" : ""}>${starLabel(v)}</option>`).join("")}</select></label>
    ${b.kind === "online" ? "" : `<label class="f">Visible to<select name="visibility">
      <option value="public" ${(b.visibility || "public") === "public" ? "selected" : ""}>Everyone (public catalog)</option>
      <option value="members" ${b.visibility === "members" ? "selected" : ""}>Signed-in members only</option></select></label>`}
    ${b.kind ? "" : `<div class="fld full"><label for="loc-pick">Location <span class="small">(shown on the public catalog)</span></label>
      <div class="locpick"><select id="loc-pick" name="location_id">${locationOptions(f.locations || [], b.location_id)}</select>
        <button type="button" id="loc-manage">Manage</button></div></div>`}
    <label class="chk" style="align-self:end"><input type="checkbox" name="signed" ${b.signed ? "checked" : ""}> Signed copy</label>
    <label class="f full">Private notes <span class="small">(only visible when signed in)</span><textarea name="private_notes">${esc(b.private_notes)}</textarea></label>
    ${b.needs_review ? `<label class="chk full review"><input type="checkbox" name="clear_review"> Mark reviewed (${esc(b.needs_review)})</label>` : ""}
    <input type="hidden" name="cover_url" value="">
    <datalist id="dl-pub">${(f.publishers || []).map((p) => `<option value="${esc(p)}">`).join("")}</datalist>
    <datalist id="dl-cuisine">${f.cuisines.map((c) => `<option value="${esc(c.name)}">`).join("")}</datalist>
    <datalist id="dl-tags">${f.tags.map((t) => `<option value="${esc(t.name)}">`).join("")}</datalist>
  </form>`;
}
function readForm(form) {
  const fd = new FormData(form);
  const o = Object.fromEntries(fd);
  o.authors = (o.authors || "").split(";").map((s) => s.trim()).filter(Boolean);
  o.tags = (o.tags || "").split(",").map((s) => s.trim()).filter(Boolean);
  o.signed = fd.has("signed");
  if (fd.has("clear_review")) o.needs_review = "";
  delete o.clear_review;
  if (!o.cover_url) delete o.cover_url;
  return o;
}
function fillForm(form, r, { overwrite = false } = {}) {
  const set = (n, v) => { const el = form.elements[n]; if (el && v != null && v !== "" && (overwrite || !el.value)) el.value = v; };
  set("title", r.title); set("subtitle", r.subtitle); set("publisher", r.publisher || (r.publishers || [])[0]);
  set("year", r.year); set("isbn", r.isbn || (r.isbns || [])[0]);
  if (r.authors && r.authors.length) set("authors", r.authors.join("; "));
  if (r.cover_url) { form.elements.cover_url.value = r.cover_url; showCoverChoice(r.cover_url); }
}
function showCoverChoice(url) {
  const el = $("#newcover");
  if (!el) return;
  el.innerHTML = `<div class="cover"><img src="${esc(url)}" alt=""></div><span class="small muted">Cover will be saved with the book</span>`;
  // a listener, not an inline onerror: the Content-Security-Policy allows no inline script
  $("img", el).addEventListener("error", () => { el.innerHTML = `<span class="muted">No cover image available</span>`; });
}

async function editView(id) {
  if (!S.facets) S.facets = await api("facets");
  const b = await api("books/" + id);
  view.innerHTML = `<a class="back" href="#/book/${id}">← ${esc(b.title)}</a><h1>Edit book</h1>
    <div class="panel"><div class="coverpick">${coverHTML(b)}
      <div style="display:grid;gap:8px"><label class="btn">Upload / photograph cover<input id="cf" type="file" accept="image/*" capture="environment" class="vh"></label>
      <button id="ol">Find matches on Open Library</button></div><div id="newcover" class="coverpick"></div></div>
      <div id="cands" class="cands"></div></div>
    ${bookForm(b)}
    <div class="actions"><button class="primary" id="save">Save</button><a class="btn" href="#/book/${id}">Cancel</a>
      <span style="flex:1"></span><button class="danger" id="delete">Delete book</button></div>`;
  const form = $("#bf");
  wireLocationPicker(form);
  $("#cf").onchange = async (e) => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    try { await api(`books/${id}/cover`, { method: "POST", body: fd }); toast("Cover updated"); editView(id); } catch (err) { toast(err.message); }
  };
  $("#ol").onclick = () => findMatches(form, $("#cands"));
  $("#save").onclick = () => busy($("#save"), async () => {
    if (!form.reportValidity()) return;
    try { await api("books/" + id, { method: "PUT", body: readForm(form) }); S.facets = null; toast("Saved"); location.hash = `#/book/${id}`; }
    catch (e) { toast(e.message); }
  });
  $("#delete").onclick = () => busy($("#delete"), async () => {
    if (!confirm(`Delete “${b.title}” and its ${b.recipes.length} recipes? You can undo it under More → Recent changes, but its cover and photos are deleted for good.`)) return;
    await api("books/" + id, { method: "DELETE" }); S.facets = null; toast("Deleted"); location.hash = "#/";
  });
}

async function findMatches(form, box) {
  const title = form.elements.title.value, author = form.elements.authors.value.split(";")[0] || "";
  box.innerHTML = `<span class="muted">Searching…</span>`;
  try {
    const { results } = await api(`lookup/search?title=${encodeURIComponent(title)}&author=${encodeURIComponent(author)}`);
    if (!results.length) { box.innerHTML = `<span class="muted">No matches. Try trimming the title or author.</span>`; return; }
    box.innerHTML = results.map((r, i) => `<div class="cand">
      ${r.cover_url ? `<img src="${esc(r.cover_url.replace("-L.", "-S."))}" alt="">` : `<div class="noimg"></div>`}
      <div><strong>${esc(r.title)}</strong>${r.subtitle ? `: ${esc(r.subtitle)}` : ""}<div class="small muted">${esc(r.authors.join(", "))}${r.year ? " · " + r.year : ""}
      ${r.publishers[0] ? " · " + esc(r.publishers[0]) : ""}${r.isbns.length ? ` · ${r.isbns.length} ISBN${r.isbns.length > 1 ? "s" : ""}` : ""}</div></div>
      <button data-i="${i}">Use</button></div>`).join("");
    $$("button[data-i]", box).forEach((btn) => btn.onclick = () => {
      fillForm(form, results[+btn.dataset.i]);
      box.innerHTML = `<span class="small muted">Filled empty fields from Open Library. Review, then Save.</span>`;
    });
  } catch (e) { box.innerHTML = `<span class="muted">${esc(e.message)}</span>`; }
}

async function addView() {
  if (!S.facets) S.facets = await api("facets");
  const canScan = "BarcodeDetector" in window && window.isSecureContext;
  view.innerHTML = `<h1 style="margin-top:18px">Add a book</h1>
    <div class="panel">
      <p class="small muted" style="margin-top:0">Scan the barcode on the back cover, or type the ISBN. Details and the cover are filled in for you.</p>
      <div class="isbnrow">${canScan ? `<button class="primary" id="scan">📷 Scan barcode</button>` : ""}
        <input id="isbn" inputmode="numeric" placeholder="ISBN (10 or 13 digits)" aria-label="ISBN"><button id="look">Look up</button></div>
      ${!canScan ? `<p class="small muted">Barcode scanning needs Chrome on Android over HTTPS (the site's public address, not the LAN IP).</p>` : ""}
      <div id="lookres" class="small" style="margin-top:10px"></div>
      <div id="newcover" class="coverpick" style="margin-top:10px"></div>
    </div>
    ${bookForm({})}
    <div class="actions"><button class="primary" id="save">Save book</button><button id="ol">Find matches by title</button></div>
    <div id="cands" class="cands panel hidden"></div>`;
  const form = $("#bf");
  wireLocationPicker(form);
  const look = async (raw) => {
    const out = $("#lookres"); out.textContent = "Looking up…";
    try {
      const r = await api("lookup/isbn/" + encodeURIComponent(raw));
      form.elements.isbn.value = r.isbn;
      if (r.existing) { out.innerHTML = `Already in the catalog: <a href="#/book/${r.existing.id}">${esc(r.existing.title)}</a>`; return; }
      if (!r.result) { out.innerHTML = `No details found for ${esc(r.isbn)}. Fill in the form by hand.`; return; }
      fillForm(form, r.result, { overwrite: true });
      out.innerHTML = `Found on ${esc(r.result.source)}. Check the details, add cuisine and tags, then save.`;
    } catch (e) { out.textContent = e.message; }
  };
  $("#look").onclick = () => look($("#isbn").value);
  $("#isbn").onkeydown = (e) => { if (e.key === "Enter") look(e.target.value); };
  if (canScan) $("#scan").onclick = () => scanBarcode().then((code) => { if (code) { $("#isbn").value = code; look(code); } });
  $("#ol").onclick = () => { $("#cands").classList.remove("hidden"); findMatches(form, $("#cands")); };
  $("#save").onclick = () => busy($("#save"), async () => {
    if (!form.reportValidity()) return;
    try {
      const b = await api("books", { method: "POST", body: readForm(form) });
      S.facets = null; toast("Added " + b.title); location.hash = `#/book/${b.id}`;
    } catch (e) {
      toast(e.message);
      if (e.status === 409 && e.data.book_id) $("#lookres").innerHTML = `<a href="#/book/${e.data.book_id}">Open the existing book</a>`;
    }
  });
}

async function scanBarcode() {
  const det = new BarcodeDetector({ formats: ["ean_13", "upc_a"] });
  const ov = document.createElement("div");
  ov.className = "scan";
  ov.innerHTML = `<video playsinline muted></video><div class="reticle"></div><div class="bar"><span>Point at the barcode</span><button>Cancel</button></div>`;
  document.body.append(ov);
  let stream, done = false, onNav;
  const stop = () => { done = true; window.removeEventListener("hashchange", onNav); stream && stream.getTracks().forEach((t) => t.stop()); ov.remove(); };
  return new Promise(async (resolve) => {
    onNav = () => { stop(); resolve(null); };
    window.addEventListener("hashchange", onNav);
    $("button", ov).onclick = () => { stop(); resolve(null); };
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 } } });
    } catch (e) { stop(); toast("Camera unavailable: " + e.message); return resolve(null); }
    if (done) { stream.getTracks().forEach((t) => t.stop()); return resolve(null); }
    const video = $("video", ov); video.srcObject = stream; await video.play();
    const tick = async () => {
      if (done) return;
      try {
        const codes = await det.detect(video);
        const hit = codes.map((c) => c.rawValue).find((v) => /^97[89]\d{10}$/.test(v));
        if (hit) { navigator.vibrate && navigator.vibrate(60); stop(); return resolve(hit); }
      } catch { /* frame not ready */ }
      setTimeout(tick, 180);
    };
    tick();
  });
}

/* ---------- index import ---------- */
async function indexView(id) {
  const b = await api("books/" + id);
  view.innerHTML = `<a class="back" href="#/book/${id}">← ${esc(b.title)}</a><h1>Import index</h1>
    <div class="panel">
      <p class="small muted" style="margin-top:0">Photograph every page of the book's index in order with <strong>📷 Take photo</strong>
      (or pick them all with <strong>Choose photos</strong>); the pages are read in the background and merged into one table to check
      and add in one go. Or paste the index text: Google Lens on Android can copy it from a photo. Lines without a page number
      become headings (like “Chicken”), and the lowercase or indented lines under them become sub-entries.</p>
      <div class="actions" style="margin-top:0"><span id="photobtns"><label class="btn primary">📷 Take photo<input id="snap" type="file" accept="image/*" capture="environment" class="vh"></label>
        <label class="btn">Choose photos<input id="photos" type="file" accept="image/*" multiple class="vh"></label></span></div>
      <div id="scanpages" class="scanpages hidden"></div>
      <div id="photostatus" class="small muted"></div>
      <details id="pastebox"><summary class="small muted">Paste index text instead</summary>
        <textarea id="txt" rows="8" aria-label="Index text" placeholder="Chicken&#10;  braised with figs, honey, and vinegar, 350&#10;  roast, with bread salad, 342&#10;Zuni Caesar Salad, 154"></textarea>
        <div class="actions"><button id="parse">Preview pasted text</button></div></details>
    </div>
    <div id="prev"></div>`;
  let feat = { photo_import: false };
  try { feat = await api("features"); } catch { /* ignore */ }
  if (!feat.photo_import) {
    $("#photobtns").classList.add("hidden");
    $("#pastebox").open = true;
    $("#photostatus").innerHTML = `Photo import is off. Add <code>ANTHROPIC_API_KEY</code> to <code>/etc/conf.d/cookbooks</code> to read index photos directly.`;
  }
  const here = () => location.hash.startsWith(`#/book/${id}/index`);
  // One scan per visit: a parent job on the server with a page per photo. The browser keeps each photo's File
  // (for the thumbnail and for "Retry"), polls the parent, and shows the merged rows once every page is read.
  const scan = { id: null, pages: [], state: null, polling: false };
  let pasted = [];        // rows from pasted text, kept apart from the scan
  const edits = {};       // key -> the fields the editor changed: corrections survive a re-render
  let rendered = {};      // key -> the row as last shown, to tell an edit from the server's own value
  const rowKey = (r, i) => r.from === "paste" ? `paste:${i}` : `${r.page_no}:${r.name}|${r.page}`;
  const keepEdits = () => {
    $$("#prev tbody tr").forEach((tr) => {
      const k = tr.dataset.k, r = rendered[k];
      if (!r) return;
      const e = edits[k] || {}, was = (f) => e[f] ?? (f === "checked" ? !r.duplicate : r[f] ?? "");
      for (const [f, v] of [["checked", $("input[type=checkbox]", tr).checked], ["heading", $("[name=heading]", tr).value],
        ["name", $("[name=name]", tr).value], ["page", $("[name=page]", tr).value]]) {
        if (v !== was(f)) e[f] = v;
      }
      if (Object.keys(e).length) edits[k] = e;
    });
  };
  const status = (html, bad) => { const st = $("#photostatus"); if (st) st.innerHTML = html ? `<div class="${bad ? "err" : ""}">${html}</div>` : ""; };
  const showPages = () => {
    const box = $("#scanpages");
    if (!box) return;
    box.classList.toggle("hidden", !scan.pages.length);
    const srv = Object.fromEntries((scan.state?.pages || []).map((p) => [p.page_no, p]));
    box.innerHTML = scan.pages.map((pg) => {
      const p = srv[pg.no] || { status: pg.status || "queued" };
      const pill = p.status === "done" ? `${p.rows} entr${p.rows === 1 ? "y" : "ies"}${p.warning ? " ⚠" : ""}`
        : p.status === "error" ? "failed" : p.status === "running" ? `reading… ${p.elapsed ? p.elapsed + "s" : ""}` : "waiting";
      return `<div class="scanpage ${p.status}" data-no="${pg.no}" title="${esc(p.error || p.warning || "")}">
        <img src="${pg.url}" alt="Page ${pg.no}"><div class="pn">Page ${pg.no}</div><div class="ps">${esc(pill)}</div>
        ${p.status === "error" || (p.status === "done" && p.warning) ? `<button type="button" class="retry">Retry</button>` : ""}</div>`;
    }).join("");
    $$("#scanpages .retry").forEach((el) => el.onclick = () => retryPage(+el.closest(".scanpage").dataset.no));
  };
  const summarize = () => {
    const st = scan.state;
    if (!st) return;
    const bad = st.pages.filter((p) => p.status === "error");
    const warned = st.pages.filter((p) => p.status === "done" && p.warning);
    const notes = [];
    if (st.status === "running") notes.push(`Reading ${st.pages.filter((p) => p.status !== "done" && p.status !== "error").length} of ${st.pages.length} pages… a full index page can take a minute or two. Take the next photo meanwhile.`);
    else if (st.pages.length) notes.push(`${st.pages.length} page${st.pages.length === 1 ? "" : "s"} read. Take the next photo, or check the entries below and add them.`);
    for (const p of bad) notes.push(`Page ${p.page_no}: ${p.error} Use Retry under its thumbnail.`);
    for (const p of warned) notes.push(`Page ${p.page_no}: ${p.warning}`);
    status(notes.map(esc).join("<br>"), bad.length > 0);
  };
  const poll = async () => {
    if (scan.polling) return;
    scan.polling = true;
    try {
      const t0 = Date.now();
      for (;;) {
        if (!here()) return;
        let r;
        try { r = await api(`index/scan/${scan.id}`); }
        catch (err) {
          if (err.status === 400 || err.status === 401 || err.status === 404 || Date.now() - t0 > 60 * 60000) { status(esc(err.message), true); return; }
          await new Promise((ok) => setTimeout(ok, 3000)); continue;  // a dropped connection: keep checking
        }
        scan.state = r; showPages(); summarize();
        if (r.status !== "running") { renderPreview(); return; }
        await new Promise((ok) => setTimeout(ok, 3000));
      }
    } finally { scan.polling = false; }
  };
  const sendPages = async (files, put) => {
    const fd = new FormData();
    for (const f of files) fd.append("file", f);
    try {
      if (!scan.id) scan.id = (await api(`books/${id}/index/scan`, { method: "POST" })).scan;
      const r = await api(put ? `index/scan/${scan.id}/pages/${put}` : `index/scan/${scan.id}/pages`, { method: put ? "PUT" : "POST", body: fd });
      return r.pages;
    } catch (err) {
      status(esc(err.message), true);  // the daily page limit, a bad scan id, or the server being away
      return null;
    }
  };
  const addPhotos = async (files) => {
    if (!files.length || !here()) return;
    const nos = await sendPages(files);
    if (!nos) return;
    nos.forEach((no, i) => scan.pages.push({ no, file: files[i], url: URL.createObjectURL(files[i]), status: "queued" }));
    scan.pages.sort((a, b) => a.no - b.no);
    showPages(); status("Reading…");
    poll();
  };
  const retryPage = async (no) => {
    const pg = scan.pages.find((p) => p.no === no);
    if (!pg || !(await sendPages([pg.file], no))) return;
    pg.status = "queued";
    if (scan.state) { const p = scan.state.pages.find((x) => x.page_no === no); if (p) { p.status = "queued"; p.error = null; } }
    showPages(); poll();
  };
  const readPhotos = (e) => { const files = [...e.target.files]; e.target.value = ""; addPhotos(files); };
  $("#snap").onchange = readPhotos;
  $("#photos").onchange = readPhotos;
  $("#parse").onclick = () => busy($("#parse"), async () => {
    const { rows } = await api(`books/${id}/index/parse`, { method: "POST", body: { text: $("#txt").value } });
    if (!rows.length && !scan.pages.length) { $("#prev").innerHTML = `<p class="muted">Couldn't find any entries with page numbers in that text.</p>`; return; }
    keepEdits();
    pasted = rows.map((r) => ({ ...r, from: "paste" }));  // new text replaces the earlier paste; photo rows stay
    renderPreview();
  });
  function renderPreview() {
    if (!here()) return;
    keepEdits();
    const rows = [...(scan.state?.rows || []), ...pasted];
    if (!rows.length) { $("#prev").innerHTML = ""; return; }
    rendered = Object.fromEntries(rows.map((r, i) => [rowKey(r, i), r]));
    const fresh = rows.filter((r) => !r.duplicate).length;
    const flagged = rows.filter((r) => r.warn).length;
    $("#prev").innerHTML = `<div class="section-h"><h2>${fresh} to add</h2><span class="muted small">${rows.length - fresh} already in this book${flagged ? ` · ${flagged} ⚠ page${flagged === 1 ? "" : "s"} to check` : ""}</span></div>
      ${rows.some((r) => r.carried) ? `<p class="small muted" style="margin:0 0 6px">Tinted headings were carried over from the page before; fix any that belong to a new section.</p>` : ""}
      <div class="overflow"><table class="preview"><thead><tr><th></th><th>Heading / recipe</th><th>Page</th></tr></thead><tbody>
      ${rows.map((r, i) => { const k = rowKey(r, i), e = edits[k] || {}; return `<tr class="${r.duplicate ? "dup" : ""} ${r.carried ? "carried" : ""}" data-k="${esc(k)}"><td><input type="checkbox" aria-label="Import this line" ${(e.checked ?? !r.duplicate) ? "checked" : ""}></td>
        <td class="hn"><input type="text" name="heading" placeholder="(no heading)" aria-label="Heading" value="${esc(e.heading ?? r.heading)}"><input type="text" name="name" aria-label="Recipe" value="${esc(e.name ?? r.name)}"></td>
        <td class="pg ${r.warn ? "warn" : ""}" ${r.warn ? `title="Unusual page number: check it"` : ""}><input type="text" name="page" aria-label="Page" value="${esc(e.page ?? r.page)}">${r.warn ? "<span>⚠</span>" : ""}</td></tr>`; }).join("")}</tbody></table></div>
      <div class="actions"><button class="primary" id="commit">Add checked entries</button><button id="again">Start over</button></div>`;
    $("#again").onclick = () => indexView(id);
    $("#commit").onclick = () => busy($("#commit"), async () => {
      const out = $$("#prev tbody tr").filter((tr) => $("input[type=checkbox]", tr).checked).map((tr) => ({
        heading: $("[name=heading]", tr).value, name: $("[name=name]", tr).value, page: $("[name=page]", tr).value }));
      const r = await api(`books/${id}/index/commit`, { method: "POST", body: { rows: out } });
      toast(`Added ${r.added} recipes${r.skipped ? `, skipped ${r.skipped} duplicates` : ""}`);
      S.facets = null;
      indexView(id);
    });
  }
}

/* ---------- editor lists ---------- */
async function tryView() {
  const { recipes, count } = await api("recipes?want=1&limit=500");
  const by = {};
  for (const r of recipes) (by[r.book_title] ||= { id: r.book_id, list: [] }).list.push(r);
  view.innerHTML = `<h1 style="margin-top:18px">Want to try <span class="muted small">${count}</span></h1>
    ${Object.entries(by).map(([t, g]) => `<div class="section-h"><h2><a href="#/book/${g.id}">${esc(t)}</a></h2></div>
      <ul class="rlist">${g.list.map((r) => `<li><div class="rn">${r.heading ? `<span class="rh">${esc(r.heading)} · </span>` : ""}${esc(r.name)}</div>
      ${whereHTML(r)}</li>`).join("")}</ul>`).join("") || `<div class="empty">Star recipes on a book's page to add them here.</div>`}`;
}
async function reviewView() {
  const [{ books }, { recipes: garbled, merged }, { books: unindexed }] = await Promise.all([
    api("books?review=1&sort=title"), api("recipes/garbled"), api("books?unindexed=1&sort=title")]);
  const why = (b) => [b.needs_review, !b.isbn && "no ISBN", !b.cover && "no cover"].filter(Boolean).join(" · ");
  const flagged = books.filter((b) => b.needs_review);
  const rest = books.filter((b) => !b.needs_review);
  const list = (arr) => `<ul class="rlist">${arr.map((b) => `<li><div class="rn"><a href="#/book/${b.id}/edit">${esc(b.title)}</a>
    <div class="bk">${esc(byline(b))}</div><div class="small review">${esc(why(b))}</div></div></li>`).join("")}</ul>`;
  view.innerHTML = `<h1 style="margin-top:18px">Needs review</h1>
    <p class="muted small">Open a book and use <em>Find matches on Open Library</em> to fill in the ISBN, year and cover in one step.</p>
    ${merged ? `<p class="small">Merged ${merged.n.toLocaleString()} garbled duplicate ${merged.n === 1 ? "recipe into its correctly spelled copy" : "recipes into their correctly spelled copies"} on ${esc(merged.at)}, keeping their ingredient tags, stars and notes.</p>` : ""}
    ${garbled.length ? `<div class="section-h"><h2>Recipes with garbled letters <span class="muted small">${garbled.length}</span></h2></div>
      <p class="muted small">These were imported with “�” where accented letters were lost, and there's no correctly spelled copy to merge them into. Open the book and use ✎ to type the name in again.</p>
      <ul class="rlist">${garbled.map((r) => `<li><div class="rn"><a href="#/book/${r.book_id}">${esc(r.name)}</a><div class="bk">${esc(r.book_title)}</div></div>
        <span class="pg">${r.page ? "p. " + esc(r.page) : ""}</span></li>`).join("")}</ul>` : ""}
    ${flagged.length ? `<div class="section-h"><h2>Flagged during import <span class="muted small">${flagged.length}</span></h2></div>${list(flagged)}` : ""}
    ${unindexed.length ? `<div class="section-h"><h2>Not indexed yet <span class="muted small">${unindexed.length}</span></h2></div>
      <p class="muted small">Books with no recipes in the catalog yet, so search can't find anything in them. Use <em>Import index</em> to scan or paste the index.</p>
      <ul class="rlist">${unindexed.map((b) => `<li><div class="rn"><a href="#/book/${b.id}">${esc(b.title)}</a><div class="bk">${esc(byline(b))}</div></div>
        <span class="acts"><a class="btn" href="#/book/${b.id}/index">Import index</a></span></li>`).join("")}</ul>` : ""}
    <div class="section-h"><h2>Missing ISBN or cover <span class="muted small">${rest.length}</span></h2></div>${list(rest)}`;
}
async function moreView() {
  let feat = {}; try { feat = await api("features"); } catch { /* ignore */ }
  const mcpUrl = `${location.origin}/mcp`;
  view.innerHTML = `<h1 style="margin-top:18px">More</h1>
    <div class="panel"><h3>Import from Eat Your Books</h3>
      <p class="small muted" style="margin-top:0">Bring in your EYB Bookshelf (ISBNs, formats) and indexed recipes, then fetch covers.</p>
      <a class="btn primary" href="#/eyb">Open importer</a></div>
    <div class="panel"><h3>House recipes</h3>
      <p class="small muted" style="margin-top:0">Our own recipes, with ingredients and steps, in one house book. Only signed-in members see them unless you make the book public.</p>
      <div class="actions" style="margin:0"><a class="btn primary" href="#/recipe/new">New recipe</a><a class="btn" href="#/house">Open the house book</a></div></div>
    <div class="panel"><h3>Recipes from websites</h3>
      <p class="small muted" style="margin-top:0">Save recipes from NYT Cooking and other sites by their links: your whole Recipe Box at once, or one at a time from your phone's share sheet.</p>
      <a class="btn primary" href="#/links">Add from links</a></div>
    <div class="panel"><h3>Shelf locations</h3>
      <p class="small muted" style="margin-top:0">Rooms, shelves, anything nested: add, rename, move, reorder or delete. Each book's location shows on the public catalog.</p>
      <button class="primary" id="loc-more">Manage locations</button></div>
    <div class="panel"><h3>Recent changes</h3>
      <p class="small muted" style="margin-top:0">Who changed which book, recipe or shelf, and when. Undo a change or a whole import from here.</p>
      <a class="btn primary" href="#/changes">Open recent changes</a></div>
    <div class="panel"><h3>Lists</h3><div class="actions" style="margin:0">
      <a class="btn" href="#/try">Want to try</a><a class="btn" href="#/review">Needs review</a>
      <a class="btn" href="#/stats">Dinner Log matching</a></div></div>
    <div class="panel"><h3>Use with Claude (MCP)</h3>
      <p class="small muted">Add as a custom connector in Claude: <code>${esc(mcpUrl)}</code> gives public search (books, recipes, pages).
      ${feat.mcp_private ? `For ratings, cooking history and suggestions, send <code>MCP_TOKEN</code> as a Bearer header${feat.mcp_url_token ? `, or use the read-only <code>${esc(mcpUrl)}/&lt;MCP_TOKEN&gt;</code> (keep that URL private)` : ""}. The token is in <code>/etc/conf.d/cookbooks</code>.
        ${feat.mcp_write ? "Starring and book fixes are on for the header token." : "Starring and book fixes are off: set <code>MCP_WRITE=1</code> to allow them for the header token."}`
        : `Set <code>MCP_TOKEN</code> in <code>/etc/conf.d/cookbooks</code> to unlock private tools (suggestions, stats; starring with <code>MCP_WRITE=1</code>).`}</p></div>
    <div class="panel"><h3>Export</h3><p class="small muted">Full backup including recipes, ratings and private notes.</p>
      <div class="actions"><a class="btn" href="/admin/api/export.json">Download JSON</a><a class="btn" href="/admin/api/export.csv">Books as CSV</a></div></div>
    <div class="panel"><h3>Public view</h3><p class="small muted">See what visitors see (no ratings, notes or loan details; shelf locations are shown).</p>
      <a class="btn" href="/">Open public catalog</a></div>`;
  $("#loc-more").onclick = () => locationsManager(() => { S.facets = null; });
}

/* ---------- recent changes (change history and undo) ---------- */
// Rows come newest first; rows of one batch (an import, a merge, an undo of several) arrive together and are shown
// as one group with "Undo all". The server says why an undo is refused (changed since, gone, would clash).
const OPS = { "edit": "Edit", "index import": "Index import", "Eat Your Books import": "Eat Your Books import",
  "ingredient import": "Ingredient import", "ingredient zip import": "Ingredient zip import", "ingredient tag delete": "Ingredient tag removed",
  "links from websites": "Links from websites", "fetch details": "Fetched details", "merge": "Merged garbled duplicates",
  "JSON import": "JSON import", "reorder shelves": "Reordered shelves", "undo": "Undo", "MCP": "Claude (MCP)" };
const whenUTC = (at) => { const t = new Date(String(at).replace(" ", "T") + "Z"); return isNaN(t) ? at : t.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", year: t.getFullYear() === new Date().getFullYear() ? undefined : "numeric" }); };
async function changesView(params) {
  const f = { book: params.get("book") || "", actor: params.get("actor") || "" };
  const qs = (extra = {}) => new URLSearchParams(Object.entries({ ...f, ...extra }).filter(([, v]) => v)).toString();
  let d = await api("changes?" + qs({ limit: 60 }));
  const rows = [...d.changes];
  view.innerHTML = `<a class="back" href="#/more">← More</a><h1>Recent changes</h1>
    <p class="small muted" style="margin-top:0">Edits, imports and merges from the last ${d.keep_days > 0 ? d.keep_days + " days" : "while"}, newest first. Undo puts back what a change replaced, or brings back what it deleted with its original ids, so Dinner Log's links match again. <strong>Photos and covers aren't part of it:</strong> a deleted photo or cover is gone for good. An undo is refused if the row was changed again since; undo the later change first.</p>
    <div class="controls"><select id="ch-book" aria-label="Book"><option value="">All books</option>${d.books.map((b) => `<option value="${esc(b.uid)}" ${b.uid === f.book ? "selected" : ""}>${esc(b.title)}</option>`).join("")}</select>
      <select id="ch-actor" aria-label="Who"><option value="">Everyone</option>${d.actors.map((a) => `<option ${a === f.actor ? "selected" : ""}>${esc(a)}</option>`).join("")}</select></div>
    <div id="ch-list"></div><div class="actions"><button id="ch-more" type="button" style="display:none">Show older</button></div>`;
  const go = () => { location.hash = "#/changes" + (qs() ? "?" + qs() : ""); };
  $("#ch-book").onchange = (e) => { f.book = e.target.value; go(); };
  $("#ch-actor").onchange = (e) => { f.actor = e.target.value; go(); };
  const item = (c) => `<li data-id="${c.id}"><div class="rn"><div>${c.book_id && c.entity !== "location" ? `<a href="#/book/${c.book_id}">${esc(c.summary)}</a>` : esc(c.summary)}</div>
      ${c.details.length ? `<div class="extra">${c.details.map(esc).join(" · ")}</div>` : ""}
      ${c.warning ? `<div class="small review">${esc(c.warning)}</div>` : ""}
      ${!c.batch_id ? `<div class="extra">${esc(whenUTC(c.at))} · ${esc(c.actor)}${c.op && c.op !== "edit" ? " · " + esc(OPS[c.op] || c.op) : ""}${c.undone_by != null ? " · <em>undone</em>" : ""}</div>` : ""}</div>
      ${c.undone_by == null && (!c.batch_id || c.batch_size > 1) ? `<span class="acts"><button class="btn ch-undo" type="button">Undo</button></span>` : ""}</li>`;
  const render = () => {
    const groups = [];
    for (const c of rows) {
      const g = groups[groups.length - 1];
      if (c.batch_id && g && g.batch === c.batch_id) g.items.push(c); else groups.push({ batch: c.batch_id, items: [c] });
    }
    $("#ch-list").innerHTML = groups.map((g) => {
      if (!g.batch) return `<ul class="rlist">${item(g.items[0])}</ul>`;
      const c = g.items[0], open = c.batch_open;
      const head = `${esc(OPS[c.op] || c.op || "Changes")} · ${esc(whenUTC(c.at))} · ${esc(c.actor)} · ${c.batch_size} change${c.batch_size > 1 ? "s" : ""}${g.items.length < c.batch_size ? ` (${g.items.length} shown)` : ""}${open === 0 ? " · <em>undone</em>" : ""}`;
      return `<div class="panel" data-batch="${esc(g.batch)}" style="padding:10px 14px"><div class="section-h" style="margin:0 0 6px"><h2 class="small" style="margin:0">${head}</h2>
        ${open > 0 ? `<button class="btn ch-undo-all" type="button">Undo all${open < c.batch_size ? " remaining" : ""}</button>` : ""}</div>
        <ul class="rlist">${g.items.map(item).join("")}</ul></div>`;
    }).join("") || `<div class="empty">No changes recorded${f.book || f.actor ? " for this filter" : " yet"}.</div>`;
    $$(".ch-undo", view).forEach((b) => b.onclick = () => busy(b, async () => {
      const c = rows.find((x) => x.id === +b.closest("li").dataset.id);
      if (!confirm(`Undo: ${c.summary}?`)) return;
      const r = await api(`changes/${c.id}/undo`, { method: "POST" });
      S.facets = null; toast(r.message); route();
    }));
    $$(".ch-undo-all", view).forEach((b) => b.onclick = () => busy(b, async () => {
      const box = b.closest("[data-batch]"), first = rows.find((x) => x.batch_id === box.dataset.batch);
      if (!confirm(`Undo all ${first.batch_open} change${first.batch_open > 1 ? "s" : ""} of this ${(OPS[first.op] || "batch").toLowerCase()}? It's all or nothing.`)) return;
      const r = await api(`changes/batch/${box.dataset.batch}/undo`, { method: "POST" });
      S.facets = null; toast(r.message); route();
    }));
    const more = $("#ch-more");
    more.style.display = d.next_before ? "" : "none";
    more.onclick = () => busy(more, async () => {
      d = await api("changes?" + qs({ limit: 60, before: d.next_before }));
      rows.push(...d.changes); render();
    });
  };
  render();
}

/* ---------- Dinner Log on a book page ---------- */
async function dinnersPanel(b) {
  let d;
  try { d = (await api(`books/${b.id}/dinners`)).dinners; } catch { return; }
  const box = $("#dinners");
  if (!box || !d.length) return;
  const opts = (sel) => `<option value="">${sel ? "Automatic match" : "Pick the recipe…"}</option>` + b.recipes.map((r) =>
    `<option value="${r.id}" ${r.id === sel ? "selected" : ""}>${r.page ? "p. " + esc(r.page) + " · " : ""}${r.heading ? esc(r.heading) + " · " : ""}${esc(r.name)}</option>`).join("");
  const item = (m) => `<li data-meal="${m.meal_id}"><div class="rn">
      <div>${esc(m.meal_name)} ${m.rating != null ? `<span class="rating">${num(m.rating)}/10</span>` : ""}</div>
      <div class="extra">${fmtDay(m.day)}${m.dl_detail ? " · logged as “" + esc(m.dl_detail) + "”" : ""}
        ${m.how === "book" ? ` · <span class="review">not matched to a recipe</span>` : m.how === "manual" ? " · linked by hand" : ""}</div>
      <div class="dlrow"><select aria-label="Recipe">${opts(m.recipe_id)}</select>
        ${m.how === "book" ? `<button class="mk">Add as recipe</button>` : ""}</div></div></li>`;
  const todo = d.filter((m) => !m.recipe_id), done = d.filter((m) => m.recipe_id);
  box.innerHTML = `<div class="section-h"><h2>From Dinner Log <span class="muted small">${d.length}</span></h2></div>
    ${todo.length ? `<p class="small muted">${todo.length} dinner${todo.length > 1 ? "s" : ""} from this book aren't matched to a recipe yet.</p><ul class="rlist dl">${todo.map(item).join("")}</ul>` : ""}
    ${done.length ? `<details><summary class="small">Matched dinners (${done.length})</summary><ul class="rlist dl">${done.map(item).join("")}</ul></details>` : ""}`;
  $$("li[data-meal]", box).forEach((li) => {
    const meal = d.find((x) => x.meal_id === +li.dataset.meal);
    $("select", li).onchange = (e) => busy(e.target, async () => {
      await api("dinnerlog/link", { method: "POST", body: { meal_id: meal.meal_id, recipe_id: e.target.value ? +e.target.value : null } });
      toast("Linked"); bookView(b.id);
    });
    const mk = $(".mk", li);
    if (mk) mk.onclick = () => busy(mk, async () => {
      const page = (meal.dl_detail || "").match(/\d+/);
      await api("dinnerlog/link", { method: "POST", body: { meal_id: meal.meal_id, create: { book_id: b.id, name: meal.meal_name, page: page ? page[0] : "" } } });
      toast("Recipe added and linked"); bookView(b.id);
    });
  });
}

/* ---------- family wish list ---------- */
async function wishDialog(b) {
  let people;
  try { people = (await api("wishlist/people")).people; } catch (e) { toast(e.message); return; }
  const d = document.createElement("dialog");
  d.innerHTML = `<form method="dialog"><h2>Add to a wish list</h2>
    <p class="small muted" style="margin:0">“${esc(b.title)}” goes on that person's list with a shopping link${b.isbn ? " by ISBN" : ""}.</p>
    <div class="people">${people.map((p) => `<label class="person"><input type="radio" name="pid" value="${p.id}" required>
      <span style="--pc:${esc(p.color || "#777")}">${esc(p.name)}</span></label>`).join("")}</div>
    <div class="row2"><label class="f">Price (optional)<input name="price" inputmode="decimal" placeholder="35.00"></label>
      <label class="f">Priority<select name="priority"><option value="1">High</option><option value="2" selected>Normal</option><option value="3">Low</option></select></label></div>
    <div class="actions" style="justify-content:flex-end"><button value="cancel" formnovalidate>Cancel</button><button class="primary" value="add">Add</button></div></form>`;
  document.body.append(d);
  d.addEventListener("close", async () => {
    if (d.returnValue === "add") {
      const fd = new FormData($("form", d));
      try {
        const r = await api(`books/${b.id}/wishlist`, { method: "POST", body: { person_id: +fd.get("pid"), price: fd.get("price"), priority: +fd.get("priority") } });
        toast(`Added to ${r.person}'s wish list`); bookView(b.id);
      } catch (e) { toast(e.message); }
    }
    d.remove();
  });
  d.showModal();
}

/* ---------- Eat Your Books import ---------- */
async function eybView() {
  view.innerHTML = `<a class="back" href="#/more">← More</a><h1>Import from Eat Your Books</h1>
    <div class="panel"><ol class="small steps">
      <li><strong>Bookshelf:</strong> on EYB, My Bookshelf → Export (CSV). Matches your books by ISBN and title, fills in ISBNs and formats, and adds books you don't have here yet.</li>
      <li><strong>Recipes:</strong> EYB's recipe CSV exports, one page at a time or combined into a single file. Recipes already here are skipped, so overlapping files are fine.</li>
      <li><strong>Covers:</strong> once books have ISBNs, fetch covers and details below.</li></ol>
      <label class="btn primary">Choose an EYB CSV file<input id="eybfile" type="file" accept=".csv,text/csv" class="vh"></label>
      <span id="eybname" class="small muted"></span></div>
    <div id="eybprev"></div>
    <div class="panel"><h3>Ingredient lists</h3>
      <p class="small muted" style="margin-top:0">Search an ingredient on EYB (e.g. Chicken), export the results, and import them here.
        Those recipes get tagged, so searching “chicken” here also finds recipes that use chicken without saying so in the title.
        A <strong>search hint</strong> (zucchini, kale…) works the same for search and Claude, but gets no chip on the catalog page.</p>
      <div class="isbnrow"><input id="ingname" placeholder="Ingredient, e.g. Chicken" aria-label="Ingredient" autocomplete="off" list="dl-ing">
        <label class="btn primary">Choose ingredient CSV<input id="ingfile" type="file" accept=".csv,text/csv" class="vh"></label></div>
      <label class="chk"><input type="checkbox" id="inghint" checked> Search hint only (no chip on the catalog page)</label>
      <datalist id="dl-ing"></datalist>
      <div id="ingprev"></div>
      <div class="zipimp"><label class="btn">Choose a zip of CSVs<input id="ingzip" type="file" accept=".zip,application/zip" class="vh"></label>
        <span class="small muted">Many lists at once, one CSV per ingredient. Files given the same tag name are combined into one tag
        (“chicken broth” and “white stock” → Chicken stock). Or add a <code>tags.csv</code> to the zip with columns <code>file,tag,hint</code>.</span></div>
      <div id="zipprev"></div><div id="inglist" style="margin-top:12px"></div></div>
    <div class="panel"><h3>Covers & details</h3><p class="small muted" id="enrichmsg" style="margin-top:0">Checking…</p>
      <div class="actions" style="margin:0"><button id="enrich">Fetch covers & details</button></div></div>`;
  const status = async () => {
    const r = await api("enrich");
    $("#enrichmsg").textContent = r.remaining ? `${r.remaining} books have an ISBN but are missing a cover, year or publisher.` : "Every book with an ISBN has been looked up.";
    $("#enrich").disabled = !r.remaining;
  };
  status().catch((e) => toast(e.message));
  $("#enrich").onclick = () => busy($("#enrich"), async () => {
    let covers = 0, done = 0, last = -1;
    for (;;) {
      const r = await api("enrich", { method: "POST", body: { limit: 5 } });
      done += r.results.length; covers += r.results.filter((x) => x.filled.includes("cover")).length;
      $("#enrichmsg").textContent = `Looked up ${done}… ${covers} covers so far, ${r.remaining} to go.`;
      if (!r.remaining || !r.results.length || r.remaining === last) break;
      last = r.remaining;
    }
    toast(`Done: ${covers} covers added`); S.facets = null; await status();
  });
  let ingNames = [];
  const ingList = async () => {
    const { ingredients } = await api("ingredients");
    ingNames = ingredients.map((i) => i.name);
    $("#dl-ing").innerHTML = ingredients.map((i) => `<option value="${esc(i.name)}">`).join("");
    $("#inglist").innerHTML = ingredients.length ? `<ul class="rlist">${ingredients.map((i) => `<li data-id="${i.id}" data-hint="${i.hint ? 1 : 0}"><div class="rn">
      <a href="#/?ingredient=${encodeURIComponent(i.name)}">${esc(i.name)}</a>${i.hint ? ` <span class="small muted">· search hint</span>` : ""}</div><span class="pg">${i.n.toLocaleString()} recipes</span>
      <span class="acts"><button class="kind" title="${i.hint ? "Show as a chip on the catalog page" : "Make it a search hint: no chip on the catalog page"}">${i.hint ? "Show chip" : "Hide chip"}</button>
      <button class="rm" title="Remove this ingredient tag (recipes stay)" aria-label="Remove the tag ${esc(i.name)} (recipes stay)">✕</button></span></li>`).join("")}</ul>` : "";
    $$("#inglist .kind").forEach((b) => b.onclick = async () => {
      const li = b.closest("li");
      try { await api("ingredients/" + li.dataset.id, { method: "PUT", body: { hint: li.dataset.hint !== "1" } }); } catch (e) { toast(e.message); return; }
      S.ingredients = null; ingList();
    });
    $$("#inglist .rm").forEach((b) => b.onclick = () => busy(b, async () => {
      const li = b.closest("li");
      if (!confirm(`Remove the “${li.querySelector("a").textContent}” tag? The recipes themselves stay.`)) return;
      await api("ingredients/" + li.dataset.id, { method: "DELETE" }); S.ingredients = null; ingList();
    }));
  };
  ingList();
  let ingfile = null;
  const ingPreview = async () => {
    const fd = new FormData(); fd.append("file", ingfile); fd.append("ingredient", $("#ingname").value);
    let r;
    try { r = await api("ingredients/preview", { method: "POST", body: fd }); } catch (err) { toast(err.message); return; }
    if (!$("#ingname").value && r.ingredient) $("#ingname").value = r.ingredient;
    if (r.exists) $("#inghint").checked = r.hint;  // an existing tag keeps its kind unless you change it here
    const box = $("#ingprev");
    const sum = (k) => r.groups.reduce((a, g) => a + (g.match ? g[k] : 0), 0);
    const opt = (sel) => `<option value="skip">Skip</option><option value="create">Add as a new book</option>` + r.books.map((b) => `<option value="${b.id}" ${sel && sel.id === b.id ? "selected" : ""}>${esc(b.title)}</option>`).join("");
    const un = r.groups.filter((g) => !g.match);
    const parts = [sum("existing") && `${sum("existing").toLocaleString()} already in your catalog get tagged`,
      sum("new") && `${sum("new").toLocaleString()} new ones get added`,
      sum("already") && `${sum("already").toLocaleString()} are already tagged${sum("existing") + sum("new") ? "" : " (nothing to do)"}`].filter(Boolean);
    box.innerHTML = `<p class="small" style="margin:12px 0 4px"><strong>${r.total.toLocaleString()} recipes</strong> in ${r.groups.length} books: ${parts.join(", ")}.
        ${un.length ? `${un.length} book${un.length > 1 ? "s aren't" : " isn't"} in your catalog (skipped unless you pick one).` : ""}</p>
      ${un.length ? `<div class="overflow"><table class="preview eyb"><tbody>${un.map((g) => `<tr data-t="${esc(g.eyb_title)}"><td><strong>${esc(g.eyb_title)}</strong>
        <div class="small muted">${esc(g.author)} · ${g.count} recipes</div></td><td><select class="dec" aria-label="Which book">${opt(null)}</select></td></tr>`).join("")}</tbody></table></div>` : ""}
      <div class="actions"><button class="primary" id="inggo">Tag recipes</button></div>`;
    $("#inggo").onclick = () => busy($("#inggo"), async () => {
      const name = $("#ingname").value.trim();
      if (!name) { toast("Name the ingredient first"); $("#ingname").focus(); return; }
      const decisions = {};
      r.groups.forEach((g) => { decisions[g.eyb_title] = g.match ? g.match.id : "skip"; });
      $$("#ingprev tr[data-t]").forEach((tr) => { decisions[tr.dataset.t] = $(".dec", tr).value; });
      const fd2 = new FormData(); fd2.append("file", ingfile); fd2.append("ingredient", name); fd2.append("decisions", JSON.stringify(decisions));
      fd2.append("hint", $("#inghint").checked ? "1" : "0");
      try {
        const res = await api("ingredients/commit", { method: "POST", body: fd2 });
        box.innerHTML = `<div class="panel" style="margin:12px 0 0"><strong>${esc(res.ingredient)}${res.hint ? " (search hint)" : ""}:</strong> ${res.tagged.toLocaleString()} recipes tagged${res.added_recipes ? ` (${res.added_recipes} new recipes added)` : ""}, ${res.total.toLocaleString()} in all.
          <a href="#/?ingredient=${encodeURIComponent(res.ingredient)}">Show them</a></div>`;
        S.ingredients = null; S.facets = null; ingList(); $("#ingname").value = ""; ingfile = null;
      } catch (err) { toast(err.message); }
    });
  };
  $("#ingfile").onchange = (e) => { ingfile = e.target.files[0]; e.target.value = ""; if (ingfile) ingPreview(); };
  // A zip of ingredient lists: preview every file with an editable tag name, then tag them all at once.
  const MINOR = /^(.+)\s(stock|broth|fat|paste|sauce|vinegar|wine|flour|starch)$/i;  // see MINOR_FORMS in ingredients.py
  const sing = (w) => w.toLowerCase().replace(/(oe|ie)?s$/, (m, x) => (x === "ie" ? "y" : x === "oe" ? "o" : ""));
  // "Tomato paste" -> "Tomatoes", when that tag exists (only then does it demote anything)
  const minorOf = (tag) => { const m = tag.trim().match(MINOR); return m ? ingNames.find((n) => sing(n) === sing(m[1])) : null; };
  let zipfile = null;
  const zipPreview = async () => {
    const box = $("#zipprev");
    box.innerHTML = `<p class="small muted">Reading the zip…</p>`;
    const fd = new FormData(); fd.append("file", zipfile);
    let r;
    try { r = await api("ingredients/zip/preview", { method: "POST", body: fd }); } catch (err) { box.innerHTML = ""; toast(err.message); return; }
    const short = (f) => f.replace(/\s*-\s*all pages combined/i, "").replace(/\.csv$/i, "");
    const key = (t) => t.trim().toLowerCase();
    box.innerHTML = `<p class="small" id="zipsum"></p>
      ${r.problems.length ? `<p class="small review">${r.problems.map(esc).join("<br>")}</p>` : ""}
      <div class="overflow"><table class="preview zip"><thead><tr><th></th><th>File</th><th>Tag</th><th>Search hint</th><th>Recipes</th></tr></thead><tbody>
      ${r.files.map((f, i) => `<tr data-i="${i}"><td><input type="checkbox" class="zin" checked aria-label="Import this file"></td>
        <td>${esc(short(f.file))}</td>
        <td><input class="ztag" list="dl-ing" value="${esc(f.tag)}" aria-label="Tag name"><div class="small muted zminor"></div></td>
        <td><input type="checkbox" class="zhint" ${f.hint ? "checked" : ""} aria-label="Search hint only"></td>
        <td class="small">${f.recipes.toLocaleString()}: ${(f.existing + f.new).toLocaleString()} to tag${f.new ? ` (${f.new.toLocaleString()} new)` : ""}${f.already ? `, ${f.already.toLocaleString()} already` : ""}${f.skipped ? `, <span title="${esc(f.skipped_books.join(", "))}">${f.skipped.toLocaleString()} skipped</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="small muted">“Skipped” recipes are from books that aren't in your catalog (hover to see which); a zip import never adds books.</p>
      <div class="actions"><button class="primary" id="zipgo">Tag recipes</button></div>`;
    const rows = $$("#zipprev tr[data-i]");
    const refresh = () => {
      const on = rows.filter((tr) => $(".zin", tr).checked && $(".ztag", tr).value.trim());
      const tags = new Set(on.map((tr) => key($(".ztag", tr).value)));
      $("#zipsum").innerHTML = `<strong>${on.length} of ${rows.length} files</strong> → ${tags.size} tag${tags.size === 1 ? "" : "s"}.`;
      rows.forEach((tr) => { const m = minorOf($(".ztag", tr).value); $(".zminor", tr).textContent = m ? `↓ minor form of ${m}` : ""; });
    };
    rows.forEach((tr) => {
      $(".zin", tr).onchange = refresh;
      $(".ztag", tr).oninput = refresh;
      $(".zhint", tr).onchange = () => {  // one tag is either a hint or a chip: keep its files in step
        const k = key($(".ztag", tr).value);
        rows.forEach((o) => { if (key($(".ztag", o).value) === k) $(".zhint", o).checked = $(".zhint", tr).checked; });
      };
    });
    refresh();
    $("#zipgo").onclick = async () => {
      const plan = rows.filter((tr) => $(".zin", tr).checked && $(".ztag", tr).value.trim())
        .map((tr) => ({ file: r.files[+tr.dataset.i].file, tag: $(".ztag", tr).value.trim(), hint: $(".zhint", tr).checked }));
      if (!plan.length) { toast("Nothing selected"); return; }
      const fd2 = new FormData(); fd2.append("file", zipfile); fd2.append("plan", JSON.stringify(plan));
      $("#zipgo").disabled = true; $("#zipgo").textContent = "Tagging…";
      try {
        const res = await api("ingredients/zip/commit", { method: "POST", body: fd2 });
        box.innerHTML = `<div class="panel" style="margin:12px 0 0"><strong>Done:</strong> ${res.tags.length} tag${res.tags.length === 1 ? "" : "s"} updated.
          <ul class="rlist">${res.tags.map((t) => `<li><div class="rn"><a href="#/?ingredient=${encodeURIComponent(t.tag)}">${esc(t.tag)}</a>${t.hint ? ` <span class="small muted">· search hint</span>` : ""}
            <div class="bk">${t.files.length} file${t.files.length === 1 ? "" : "s"} · ${t.tagged.toLocaleString()} tagged${t.added_recipes ? ` (${t.added_recipes.toLocaleString()} new recipes)` : ""}</div></div>
            <span class="pg">${t.total.toLocaleString()} in all</span></li>`).join("")}</ul></div>`;
        S.ingredients = null; S.facets = null; zipfile = null; ingList();
      } catch (err) { toast(err.message); $("#zipgo").disabled = false; $("#zipgo").textContent = "Tag recipes"; }
    };
  };
  $("#ingzip").onchange = (e) => { zipfile = e.target.files[0]; e.target.value = ""; if (zipfile) zipPreview(); };
  let file = null;
  $("#eybfile").onchange = async (e) => {
    file = e.target.files[0]; e.target.value = "";
    if (!file) return;
    $("#eybname").textContent = file.name;
    const fd = new FormData(); fd.append("file", file);
    try { renderEyb(await api("eyb/preview", { method: "POST", body: fd })); } catch (err) { toast(err.message); }
  };
  function bookRow(x, others) {
    const e = x.eyb, m = x.match;
    const opts = (m ? `<option value="${m.id}" selected>${esc(m.title)} (${esc(x.method)})</option>` : "") +
      `<option value="create" ${m ? "" : "selected"}>Add as a new book</option><option value="skip">Skip</option>` +
      (others.length ? `<optgroup label="Or link to">${others.map((b) => `<option value="${b.id}">${esc(b.title)}</option>`).join("")}</optgroup>` : "");
    return `<tr data-i="${x.i}"><td><strong>${esc(e.title)}</strong><div class="small muted">${esc(e.authors.join(", "))}${e.isbn ? " · " + esc(e.isbn) : ""}${e.format ? " · " + esc(e.format) : ""}</div></td>
      <td><select class="dec" aria-label="Which book">${opts}</select>
      ${x.retitle ? `<label class="chk small"><input type="checkbox" class="rt" ${x.retitle_default ? "checked" : ""}> Use EYB's title</label>` : ""}
      ${x.fills.length ? `<div class="small muted">adds ${esc(x.fills.join(", "))}</div>` : ""}</td></tr>`;
  }
  function renderEyb(r) {
    const box = $("#eybprev");
    if (r.kind === "books") {
      const attention = r.rows.filter((x) => !x.match || !["ISBN", "earlier import", "title + author"].includes(x.method) || x.retitle_default);
      const rest = r.rows.filter((x) => !attention.includes(x));
      const others = r.catalog_unmatched;
      box.innerHTML = `<div class="section-h"><h2>Bookshelf: ${r.total} books</h2><span class="small muted">${r.matched} matched · ${r.total - r.matched} new</span></div>
        <p class="small muted">Check the first list: new books, weaker matches and suggested title fixes. Pick “Or link to” if a new one is really a book you already have here.
          EYB may list books you no longer own; new ones land on Needs review either way.</p>
        ${r.total - r.matched ? `<div class="actions" style="margin:6px 0"><button id="allnew">Add all ${r.total - r.matched} new</button><button id="skipnew">Skip all new</button></div>` : ""}
        <div class="overflow"><table class="preview eyb"><tbody>${attention.map((x) => bookRow(x, others)).join("")}</tbody></table></div>
        <details><summary class="small">Matched by ISBN or title + author (${rest.length})</summary>
        <div class="overflow"><table class="preview eyb"><tbody>${rest.map((x) => bookRow(x, others)).join("")}</tbody></table></div></details>
        ${others.length ? `<p class="small muted">${others.length} books here aren't on your EYB Bookshelf (they stay as they are).</p>` : ""}
        <div class="actions"><button class="primary" id="eybgo">Import bookshelf</button></div>`;
      const setNew = (v) => $$("tr[data-i] select.dec", box).forEach((sel) => {
        if (["create", "skip"].includes(sel.value)) sel.value = v;
      });
      if ($("#allnew")) { $("#allnew").onclick = () => setNew("create"); $("#skipnew").onclick = () => setNew("skip"); }
      $("#eybgo").onclick = () => busy($("#eybgo"), async () => {
        const decisions = {}, retitle = [];
        $$("tr[data-i]", box).forEach((tr) => {
          decisions[tr.dataset.i] = $(".dec", tr).value;
          if ($(".rt", tr) && $(".rt", tr).checked) retitle.push(+tr.dataset.i);
        });
        const fd = new FormData(); fd.append("file", file); fd.append("decisions", JSON.stringify(decisions)); fd.append("retitle", JSON.stringify(retitle));
        const res = await api("eyb/commit", { method: "POST", body: fd });
        S.facets = null;
        box.innerHTML = `<div class="panel"><strong>Bookshelf imported.</strong> ${res.updated} books updated${res.retitled ? ` (${res.retitled} retitled)` : ""}, ${res.created} added${res.skipped ? `, ${res.skipped} skipped` : ""}. New books are on the <a href="#/review">Needs review</a> list for cuisine and tags.</div>`;
        status();
      });
    } else {
      const opt = (sel) => `<option value="skip">Skip</option><option value="create">Add as a new book</option>` + r.books.map((b) => `<option value="${b.id}" ${sel && sel.id === b.id ? "selected" : ""}>${esc(b.title)}</option>`).join("");
      box.innerHTML = `<div class="section-h"><h2>Recipes: ${r.total}</h2><span class="small muted">${r.groups.length} books</span></div>
        <div class="overflow"><table class="preview eyb"><tbody>${r.groups.map((g) => `<tr data-t="${esc(g.eyb_title)}">
          <td><strong>${esc(g.eyb_title)}</strong><div class="small muted">${g.count} recipe${g.count === 1 ? "" : "s"}${g.with_pages < g.count ? ` (${g.count - g.with_pages} without page numbers)` : ""} · ${g.new} new</div></td>
          <td><select class="dec" aria-label="Which book">${opt(g.match)}</select>${g.match ? (g.method === "eyb title" ? "" : `<div class="small muted">matched by ${esc(g.method)}</div>`) : `<div class="small review hint">Not in your catalog: pick the book, add it, or skip</div>`}</td></tr>`).join("")}</tbody></table></div>
        ${r.groups.some((g) => !g.match) ? `<div class="actions" style="margin:6px 0"><button id="unadd">Add all unmatched as new books</button><button id="unskip">Skip all unmatched</button></div>` : ""}
        <div class="actions"><button class="primary" id="eybgo">Import recipes</button></div>`;
      const setUn = (v) => r.groups.forEach((g) => { if (!g.match) { const sel = $(`tr[data-t="${CSS.escape(g.eyb_title)}"] select`, box); if (sel) { sel.value = v; sel.onchange && sel.onchange(); } } });
      if ($("#unadd")) { $("#unadd").onclick = () => setUn("create"); $("#unskip").onclick = () => setUn("skip"); }
      $$("tr[data-t] select", box).forEach((sel) => sel.onchange = () => { const h = $(".hint", sel.closest("td")); if (h) h.hidden = sel.value !== "skip"; });
      $$("tr[data-t] select", box).forEach((sel) => sel.onchange && sel.onchange());
      $("#eybgo").onclick = () => busy($("#eybgo"), async () => {
        const decisions = {};
        $$("tr[data-t]", box).forEach((tr) => { decisions[tr.dataset.t] = $(".dec", tr).value; });
        const fd = new FormData(); fd.append("file", file); fd.append("decisions", JSON.stringify(decisions));
        const res = await api("eyb/commit", { method: "POST", body: fd });
        S.facets = null;
        box.innerHTML = `<div class="panel"><strong>Recipes imported.</strong> ${res.added.toLocaleString()} added to ${res.books} books${res.created_books ? ` (${res.created_books} new books, on <a href="#/review">Needs review</a>)` : ""}${res.duplicates ? `, ${res.duplicates} already here` : ""}${res.skipped ? `, ${res.skipped} skipped` : ""}.</div>`;
      });
    }
  }
}

/* ---------- what should we cook ---------- */
const COOK_MODES = [["new", "Never made"], ["stale", "Not lately"], ["want", "Want to try"], ["neglected", "From a neglected book"], ["any", "Any"]];
const COOK_EMPTY = {
  new: "Everything that fits has been made.", stale: "Nothing you've made is overdue. Try Any or Never made.",
  want: "Nothing starred and unmade fits. Star recipes (☆) on a book's page to fill this list.", any: "Nothing fits.",
  neglected: "No cookbook is overdue: every one with an index has been cooked from in the past year.",
};
const newSeed = () => Math.random().toString(36).slice(2, 10);
S.cook = { mode: "any", cuisine: "", ing: ["", ""], seed: newSeed() };
async function cookView() {
  if (!S.facets) S.facets = await api("facets");
  if (!S.ingredients) { try { S.ingredients = (await api("ingredients")).ingredients.filter((i) => i.n && !i.hint); } catch { S.ingredients = []; } }
  const c = S.cook, f = S.facets;
  view.innerHTML = `<h1 style="margin-top:18px">What should we cook?</h1>
    <form class="cookbar" id="cook-form" autocomplete="off">
      <div class="cook-ings">
        <input id="ci0" type="search" list="dl-cook-ing" placeholder="An ingredient" aria-label="Ingredient" enterkeyhint="search" value="${esc(c.ing[0])}">
        <input id="ci1" type="search" list="dl-cook-ing" placeholder="…and another (optional)" aria-label="Second ingredient" enterkeyhint="search" value="${esc(c.ing[1])}">
      </div>
      <datalist id="dl-cook-ing">${S.ingredients.map((i) => `<option value="${esc(i.name)}">`).join("")}</datalist>
      <div class="seg" role="group" aria-label="Which recipes">${COOK_MODES.map(([k, t]) =>
        `<button type="button" data-m="${k}" aria-pressed="${c.mode === k}" class="${c.mode === k ? "on" : ""}">${t}</button>`).join("")}</div>
      <div class="cook-row">
        <select id="cc" aria-label="Cuisine"><option value="">Any cuisine</option>${f.cuisines.map((x) =>
          `<option value="${esc(x.name)}" ${x.name === c.cuisine ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
        <button type="button" class="primary" id="shuffle">↻ Shuffle</button>
      </div>
    </form>
    <p class="small muted" id="cook-note"></p>
    <ul class="cooklist" id="cook-list"></ul>
    <div class="actions hidden" id="cook-more"><button type="button" id="cook-more-btn">Show more</button></div>`;
  const card = (x) => `<li><a class="idea cook" href="${x.house ? `#/recipe/${esc(x.uid)}` : `#/book/${x.book_id}?recipe=${x.id}`}">
      ${x.photo ? `<img class="cthumb" loading="lazy" src="${esc(x.photo)}" alt="">` : ""}
      <div class="cbody">
        <div class="ib">${esc(x.book_title)}${x.page ? ` · p. ${esc(x.page)}` : x.url ? " · website" : ""}</div>
        <div class="in">${x.heading ? `<span class="rh">${esc(x.heading)} · </span>` : ""}${esc(x.name)}</div>
        <div class="extra">${[x.starred ? `<span class="star">★ want to try</span>` : "",
          x.times_made ? `made ${x.times_made}×, last ${fmtDay(x.last_made)}` : x.last_made ? `made ${fmtDay(x.last_made)}` : "never made",
          x.best_rating != null ? `<span class="rating">${num(x.best_rating)}/10</span>` : "",
          x.book_status === "out" || x.book_status === "loaned" ? "book is off the shelf" : ""].filter(Boolean).join(" · ")}</div>
      </div></a></li>`;
  let seq = 0, next = null;
  const load = async (more = false) => {
    const mine = ++seq;
    const p = new URLSearchParams({ mode: c.mode, limit: 20, offset: more ? next : 0, seed: c.seed });
    for (const x of c.ing) if (x.trim()) p.append("with", x.trim());
    if (c.cuisine) p.set("cuisine", c.cuisine);
    let r;
    try { r = await api("cook?" + p); } catch (e) { if (mine === seq && $("#cook-note")) $("#cook-note").textContent = e.message; return; }
    if (mine !== seq || !$("#cook-list")) return;  // a newer request, or the page changed
    next = r.next_offset;
    const list = $("#cook-list");
    if (!more) list.innerHTML = "";
    list.insertAdjacentHTML("beforeend", r.recipes.map(card).join(""));
    if (!more && !r.recipes.length) list.innerHTML = `<li class="empty">${COOK_EMPTY[c.mode]}${c.ing.some((x) => x.trim()) || c.cuisine ? " Try fewer ingredients or another cuisine." : ""}</li>`;
    $("#cook-more").classList.toggle("hidden", next == null);
    $("#cook-note").textContent = (r.pool ? `${r.pool.toLocaleString()} recipe${r.pool === 1 ? "" : "s"} fit` : "")
      + (r.title_only.length ? `${r.pool ? ". " : ""}No ingredient tag for ${r.title_only.join(", ")}: matched on recipe titles only.` : "");
  };
  $("#cook-form").onsubmit = (e) => { e.preventDefault(); load(); };
  $$("#cook-form .seg button").forEach((b) => b.onclick = () => {
    c.mode = b.dataset.m;
    $$("#cook-form .seg button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    load();
  });
  [0, 1].forEach((i) => $("#ci" + i).addEventListener("input", debounce((e) => { c.ing[i] = e.target.value; load(); }, 300)));
  $("#cc").onchange = (e) => { c.cuisine = e.target.value; load(); };
  $("#shuffle").onclick = () => { c.seed = newSeed(); load(); };
  $("#cook-more-btn").onclick = () => load(true);
  await load();
}

/* ---------- stats ---------- */
// Top 12 as bars, with every row in a list underneath when there are more.
function ranked(rows, href, empty) {
  if (!rows.length) return `<p class="small muted">${empty}</p>`;
  const books = (r) => (r.books === 1 ? " book" : " books");
  return hbars(rows.slice(0, 12), (r) => r.name, (r) => r.books, href, books) + (rows.length > 12 ? `
    <details style="margin-top:12px"><summary>All ${rows.length}</summary><ul class="rlist">${rows.map((r) =>
      `<li><div class="rn"><a href="${href(r)}">${esc(r.name)}</a></div><span class="pg">${r.books}</span></li>`).join("")}</ul></details>` : "");
}
function hbars(rows, label, value, href, note) {
  const max = Math.max(1, ...rows.map(value));
  return `<div class="hbars">${rows.map((r) => `<a class="hb" href="${href ? href(r) : "#/stats"}" data-tip="${esc(label(r))}: ${value(r)}${note ? esc(note(r)) : ""}">
    <span class="hl">${esc(label(r))}</span><span class="ht"><span class="hf" style="width:${(value(r) / max) * 100}%"></span></span>
    <span class="hv">${value(r)}</span></a>`).join("")}</div>`;
}
// A short list for Shelf check: the first few, the rest under "more". `right` is the note on the right of a row.
function shelfList(rows, right, empty) {
  if (!rows.length) return `<p class="small muted">${empty}</p>`;
  const li = (b) => `<li><div class="rn"><a href="#/book/${b.id}">${esc(b.title)}</a></div><span class="pg">${right(b)}</span></li>`;
  return `<ul class="rlist">${rows.slice(0, 8).map(li).join("")}</ul>` + (rows.length > 8 ? `
    <details style="margin-top:12px"><summary>${rows.length - 8} more</summary><ul class="rlist">${rows.slice(8).map(li).join("")}</ul></details>` : "");
}
async function statsView() {
  const [s, dl] = await Promise.all([api("stats"), api("dinnerlog?force=1")]);
  const months = [];
  const now = new Date(); now.setDate(1);
  for (let i = 11; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    const k = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    months.push({ k, label: d.toLocaleDateString(undefined, { month: "short" }), n: (s.months.find((m) => m.m === k) || {}).n || 0 });
  }
  const mmax = Math.max(1, ...months.map((m) => m.n));
  let allBooks = null;
  const missing = (k) => s.missing[k] ? `${s.missing[k]} book${s.missing[k] === 1 ? " has" : "s have"} no ${k === "year" ? "publication year" : k} recorded` : "";
  const note = (k) => s.missing[k] ? `<p class="small muted" style="margin-top:0">${missing(k)}.</p>` : "";
  view.innerHTML = `<h1 style="margin-top:18px">Stats</h1>
    <div class="tiles">
      <div class="tile"><div class="tv">${s.books}</div><div class="tk">cookbooks</div></div>
      <div class="tile"><div class="tv">${s.recipes.toLocaleString()}</div><div class="tk">recipes indexed${s.books_indexed ? ` from ${s.books_indexed} indexes` : ""}</div></div>
      <div class="tile"><div class="tv">${s.dinners_last_year}</div><div class="tk">cookbook dinners, last 12 months</div></div>
      <div class="tile"><div class="tv">${s.books_cooked_from}</div><div class="tk">books cooked from, ever</div></div>
      ${s.online.length ? `<div class="tile"><div class="tv">${s.online.reduce((t, o) => t + o.recipes, 0).toLocaleString()}</div><div class="tk">recipes saved from ${s.online.map((o) => `<a href="#/book/${o.id}">${esc(o.title)}</a>`).join(", ")}</div></div>` : ""}
    </div>
    ${dl.available ? `<div class="panel wide"><h3>Cookbook dinners per month</h3>
      <div class="vbars" role="img" aria-label="Cookbook dinners per month, last 12 months">${months.map((m) => `<div class="vb" tabindex="0" data-tip="${m.label}: ${m.n} dinner${m.n === 1 ? "" : "s"}">
        <span class="vv">${m.n || ""}</span><span class="vf" style="height:${(m.n / mmax) * 100}%"></span><span class="vl">${m.label}</span></div>`).join("")}</div></div>` : ""}
    <div class="cols">
      <div class="panel"><h3>Most cooked-from books</h3>${s.top_books.length ? hbars(s.top_books, (r) => r.title, (r) => r.n, (r) => `#/book/${r.id}`, (r) => `, last ${fmtDay(r.last)}`) : `<p class="small muted">Nothing from Dinner Log yet.</p>`}</div>
      <div class="panel"><h3>Best-rated recipes</h3>${s.top_recipes.length ? `<ul class="rlist">${s.top_recipes.map((r) => `<li><div class="rn"><a href="#/book/${r.book_id}">${esc(r.name)}</a><div class="bk">${esc(r.book_title)}${r.n ? ` · cooked ${r.n}×` : ""}</div></div><span class="rating">${num(r.avg)}</span></li>`).join("")}</ul>` : `<p class="small muted">No ratings yet.</p>`}</div>
      <div class="panel"><h3>Cuisines</h3><p class="small muted" style="margin-top:0">Books per cuisine${(s.cuisines.find((c) => c.name === "(none)") || {}).books ? ` · ${s.cuisines.find((c) => c.name === "(none)").books} books have no cuisine set` : ""}</p>${hbars(s.cuisines.filter((c) => c.name !== "(none)").slice(0, 14), (r) => r.name, (r) => r.books, (r) => r.name === "(none)" ? "#/" : `#/?cuisine=${encodeURIComponent(r.name)}`, (r) => ` books, ${r.cooked} dinners`)}</div>
      <div class="panel"><h3>Off the shelf <span class="muted small">${s.off_shelf.length}</span></h3>${s.off_shelf.length ? `<ul class="rlist">${s.off_shelf.map((b) => `<li><div class="rn"><a href="#/book/${b.id}">${esc(b.title)}</a></div><span class="pg">since ${fmtDay(b.out_since)}</span></li>`).join("")}</ul>` : `<p class="small muted">Everything's on the shelf. Use “Pulled out” on a book's page.</p>`}</div>
    </div>
    <h2 class="stats-h">Shelf check</h2>
    <p class="small muted" style="margin-top:0">What the collection is neglecting. <a href="#/cook">Cook → From a neglected book</a> draws recipes from the first two lists.</p>
    <div class="cols">
      <div class="panel"><h3>Never cooked from <span class="muted small">${s.never_cooked.length}</span></h3>
        ${shelfList(s.never_cooked, () => "", "Every cookbook has been cooked from at least once.")}</div>
      <div class="panel"><h3>Not cooked from in 12 months <span class="muted small">${s.shelf_check.lapsed.length}</span></h3>
        <p class="small muted" style="margin-top:0">Cooked from before, longest ago first.</p>
        ${shelfList(s.shelf_check.lapsed, (b) => (b.last ? `last ${fmtDay(b.last)}` : "no date"), "Nothing has gone quiet: every book cooked from before was used this year.")}</div>
      <div class="panel"><h3>Cooked a lot, never rated</h3>
        <p class="small muted" style="margin-top:0">The books we use most that have no stars yet.</p>
        ${shelfList(s.shelf_check.unrated_most_cooked, (b) => `${b.n} dinner${b.n === 1 ? "" : "s"}`, "Every book we cook from has a rating.")}</div>
      <div class="panel"><h3>Index coverage</h3>${(() => {
        const ix = s.shelf_check.index;
        return `<p class="small muted" style="margin-top:0">${ix.indexed} of ${ix.total} cookbooks have an index in the catalog.</p>
          <div class="prog"><span class="bar"><span style="width:${ix.total ? Math.round((ix.indexed / ix.total) * 100) : 0}%"></span></span></div>
          ${ix.not_indexed ? `<p><a href="#/review">Not indexed yet (${ix.not_indexed}) →</a></p>` : `<p class="small muted">Every cookbook is indexed.</p>`}`;
      })()}</div>
    </div>
    <h2 class="stats-h">The collection</h2>
    <div class="cols">
      <div class="panel"><h3>By author <span class="muted small">${s.by_author.length}</span></h3>
        <p class="small muted" style="margin-top:0">${s.by_author.filter((a) => a.books > 1).length} authors with more than one book. Books by several authors count for each.${s.missing.author ? ` ${missing("author")}.` : ""}</p>
        ${ranked(s.by_author, (r) => `#/?author=${encodeURIComponent(r.name)}`, "No authors yet.")}</div>
      <div class="panel"><h3>By publisher <span class="muted small">${s.by_publisher.length}</span></h3>
        ${note("publisher")}
        ${ranked(s.by_publisher, (r) => `#/?publisher=${encodeURIComponent(r.name)}`, "No publishers recorded yet.")}</div>
      <div class="panel"><h3>By tag <span class="muted small">${s.by_tag.length}</span></h3>
        ${ranked(s.by_tag, (r) => `#/?tag=${encodeURIComponent(r.name)}`, "No tags yet.")}</div>
      <div class="panel"><h3>Book ratings</h3>${s.ratings.length ? `
        <p class="small muted" style="margin-top:0">${s.ratings.reduce((t, r) => t + r.books, 0)} rated, averaging ${num(s.rating_avg)} stars</p>
        ${hbars(s.ratings, (r) => starLabel(r.stars), (r) => r.books, () => "#/?sort=rating", (r) => (r.books === 1 ? " book" : " books"))}`
        : `<p class="small muted">No books rated yet. Rate one with the stars on its page.</p>`}</div>
      ${s.by_decade.length ? `<div class="panel"><h3>By decade published</h3>
        ${note("year")}
        ${hbars(s.by_decade, (r) => r.name, (r) => r.books, null, (r) => (r.books === 1 ? " book" : " books"))}</div>` : ""}
      ${s.by_format.length ? `<div class="panel"><h3>By format</h3>
        ${note("format")}
        ${hbars(s.by_format, (r) => r.name, (r) => r.books, null, (r) => (r.books === 1 ? " book" : " books"))}</div>` : ""}
    </div>
    <div class="panel wide" id="dlpanel"><h3>Dinner Log</h3>${dl.available ? `
      <p class="small muted" style="margin-top:0">${dl.meals} cookbook dinners · ${dl.recipe_matched} matched to a recipe${dl.linked ? ` (${dl.linked} logged from here)` : ""} · ${dl.book_only} matched to a book only · fetched ${esc(dl.pulled || "")}</p>
      ${dl.unmatched_titles.length ? `<p class="small">These cookbook names from Dinner Log don't match a book here. Pick the book once and every dinner with that name follows.</p>
        <ul class="rlist">${dl.unmatched_titles.map((t) => `<li data-title="${esc(t.title)}"><div class="rn">${esc(t.title)} <span class="muted small">${t.n} dinner${t.n > 1 ? "s" : ""}</span></div>
        <select class="alias" aria-label="Which book" style="max-width:55%"><option value="">Pick a book…</option></select></li>`).join("")}</ul>` : `<p class="small">Every cookbook name in Dinner Log matches a book.</p>`}`
      : dl.source ? `<p class="small muted" style="margin-top:0">Couldn't get cooking history from Dinner Log at <code>${esc(dl.source)}</code>${dl.error ? ` (${esc(dl.error)})` : ""}.${dl.pulled ? ` Showing what it sent at ${esc(dl.pulled)}.` : ""}</p>`
      : `<p class="small muted" style="margin-top:0">Cooking history is off. Set <code>DINNERLOG_API_URL</code> and <code>DINNERLOG_TOKEN</code> to Dinner Log's private address and its <code>HISTORY_TOKEN</code>.</p>`}</div>`;
  const sels = $$("select.alias");
  if (sels.length) {
    allBooks = (await api("books?sort=title")).books;
    const o = allBooks.map((b) => `<option value="${b.id}">${esc(b.title)}</option>`).join("");
    sels.forEach((sel) => {
      sel.insertAdjacentHTML("beforeend", o);
      sel.onchange = () => busy(sel, async () => {
        await api("dinnerlog/alias", { method: "POST", body: { title: sel.closest("li").dataset.title, book_id: +sel.value } });
        toast("Matched"); statsView();
      });
    });
  }
}

/* ---------- recipes from websites ---------- */
// Bookmarklet: copies every NYT Cooking recipe link on the page (e.g. your Recipe Box) to the clipboard.
const BOOKMARKLET = String.raw`javascript:(()=>{const s=new Set();document.querySelectorAll('a[href]').forEach(a=>{const h=a.href.split(/[?#]/)[0];if(/cooking\.nytimes\.com\/recipes\/\d+/.test(h))s.add(h)});if(!s.size){alert('No NYT Cooking recipe links on this page.');return}const t=[...s].join('\n');navigator.clipboard.writeText(t).then(()=>alert(s.size+' recipe links copied. Paste them into Cookbook Collection: More, Recipes from websites.'),()=>prompt('Copy these '+s.size+' links:',t))})()`;
// "Spicy Chili With Beans Recipe - NYT Cooking" -> "Spicy Chili With Beans": the title a share sheet passes along.
const sharedTitle = (t) => (t || "").replace(/\s*[-–|]\s*(NYT Cooking|The New York Times|Serious Eats|Bon App[ée]tit|Epicurious|Food52)\b.*$/i, "")
  .replace(/\s+Recipe$/i, "").trim();
async function linksView() {
  const prefill = S.shared ? S.shared.text : "";
  const shareTitle = S.shared ? sharedTitle(S.shared.title) : "";
  S.shared = null;
  view.innerHTML = `<h1 style="margin-top:18px">Recipes from websites</h1>
    <p class="muted small">Paste links to recipes from NYT Cooking, Serious Eats or other sites: one or many, in any text. Each is saved with its name and link under its website, like a cookbook's index, and opens on the site where you're signed in. Nothing is downloaded from the site, so the name comes from the link: check it below.</p>
    <div class="panel"><textarea id="lt" rows="6" aria-label="Recipe links" placeholder="https://cooking.nytimes.com/recipes/1017256-french-onion-soup">${esc(prefill)}</textarea>
      <div class="actions"><button class="primary" id="lp">Preview</button></div><div id="lres"></div></div>
    <div class="panel"><h3>Your whole NYT Cooking Recipe Box</h3>
      <ol class="small"><li>On a computer, drag this button to the browser's bookmarks bar: <a class="btn" id="bml" href="${esc(BOOKMARKLET)}">Copy recipe links</a></li>
        <li>Open your Recipe Box on cooking.nytimes.com and scroll to the bottom, so every recipe has loaded.</li>
        <li>Click <b>Copy recipe links</b> in the bookmarks bar, paste here and preview.</li></ol>
      <p class="small muted" style="margin-bottom:0">It only reads the links already on the page, in your own signed-in browser. Saving again skips recipes that are already here, so you can repeat it whenever you've saved more.</p></div>
    <div class="panel"><h3>From your phone</h3>
      <p class="small muted" style="margin:0">Open Cookbook Collection's editor page in Chrome on Android and use <b>Add to Home screen</b> (install). Then <b>Share → Cookbook Collection</b> from the NYT Cooking app or any recipe page brings the link straight here.</p></div>`;
  $("#bml").addEventListener("click", (e) => e.preventDefault());   // it's for dragging to the bookmarks bar, not clicking
  const preview = async () => {
    let r;
    try { r = await api("online/preview", { method: "POST", body: { text: $("#lt").value } }); } catch (e) { toast(e.message); return; }
    const fresh = r.items.filter((x) => !x.recipe_id), saved = r.items.length - fresh.length;
    if (fresh.length === 1 && shareTitle) fresh[0].title = shareTitle;  // the site's own title beats one rebuilt from the link
    const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? "" : "s"}`;
    $("#lres").innerHTML = `<p class="small">${plural(r.items.length, "recipe link")}${saved ? ` · ${saved.toLocaleString()} already saved` : ""}${r.skipped.length ? ` · ${plural(r.skipped.length, "other link")} skipped (not a recipe page)` : ""}</p>
      ${fresh.length ? `<ul class="rlist links">${fresh.map((x, i) => `<li data-i="${i}"><input type="checkbox" class="inc" checked aria-label="Save this one">
        <div class="rn"><input class="ln" value="${esc(x.title)}" aria-label="Recipe name"><div class="bk">${esc(x.site_name)} · <a href="${esc(x.url)}" target="_blank" rel="noopener">open ↗</a></div></div></li>`).join("")}</ul>
        <label class="chk"><input type="checkbox" id="lw"> Star them as want to try</label>
        <div class="actions"><button class="primary" id="ls">Save ${plural(fresh.length, "recipe")}</button></div>` : ""}`;
    if ($("#ls")) $("#ls").onclick = () => busy($("#ls"), async () => {
      const items = $$("#lres li[data-i]").filter((li) => $(".inc", li).checked)
        .map((li) => ({ url: fresh[+li.dataset.i].url, title: $(".ln", li).value, want_to_try: $("#lw").checked }));
      if (!items.length) { toast("Nothing selected"); return; }
      try {
        const res = await api("online/commit", { method: "POST", body: { items } });
        S.facets = null; S.ingredients = null;
        $("#lres").innerHTML = `<p>Saved ${plural(res.added, "recipe")}${res.duplicates ? ` (${res.duplicates.toLocaleString()} already here)` : ""}.
          ${res.books.map((b) => `<a href="#/book/${b.id}">Open ${esc(b.title)}</a>`).join(" · ")}</p>`;
        $("#lt").value = "";
      } catch (e) { toast(e.message); }
    });
  };
  $("#lp").onclick = preview;
  if (prefill) preview();
}

/* ---------- boot ---------- */
// Opened from the phone's share sheet (manifest share_target): /admin?title=…&text=…&url=…
if (ADMIN) {
  const sp = new URLSearchParams(location.search);
  if (sp.get("url") || sp.get("text")) {
    S.shared = { title: sp.get("title") || "", text: [sp.get("text"), sp.get("url")].filter(Boolean).join("\n") };
    history.replaceState(null, "", location.pathname + "#/links");
  }
}
(async () => {
  if (location.pathname.replace(/\/$/, "") === "/admin/recipe/new") history.replaceState(null, "", "/admin#/recipe/new" + location.search);
  if (!ADMIN) {
    const hit = cachedEditor();
    if (hit) applyEditor(hit);
    else probeEditor().then((p) => { applyEditor(p); renderNav(currentRoute()); });   // after first paint: nobody waits on it
  } else {
    try { S.features = await api("features"); } catch { S.features = {}; }
    try { await api("me"); } catch (e) {
      const why = e.data && e.data.reason;
      view.innerHTML = `<div class="empty">You're not signed in as an editor.${why ? `<p class="small" style="max-width:560px;margin:12px auto">${esc(why)}</p>` : ""} <a href="/">Back to the catalog</a></div>`;
      renderNav(""); return;
    }
  }
  route();
})();
