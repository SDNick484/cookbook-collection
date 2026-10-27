"use strict";
const ADMIN = location.pathname.startsWith("/admin");
const API = ADMIN ? "/admin/api" : "/api";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
const S = { facets: null, q: "", cuisine: "", tag: "", author: "", publisher: "", ingredient: "", sort: "author", editor: ADMIN, ingredients: null };

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
  if (b.kind === "online" && !b.cover) return `<div class="cover ph web"><span class="t">${esc(b.title)}</span><span class="a">🌐 ${esc(b.site || "")}</span></div>`;
  if (b.cover) return `<div class="cover"><img loading="lazy" src="/covers/${esc(b.cover)}" alt=""></div>`;
  return `<div class="cover ph" style="background:${spine(b.title)}"><span class="t">${esc(b.title)}</span><span class="a">${esc((b.authors || [])[0] || "")}</span></div>`;
}
const byline = (b) => (b.authors || []).join(", ");

/* ---------- nav / router ---------- */
function renderNav(route) {
  const items = ADMIN
    ? [["#/", "Catalog"], ["#/cook", "Cook"], ["#/add", "Add"], ["#/stats", "Stats"], ["#/more", "More"]]
    : [["#/", "Catalog"]].concat(S.editor ? [["/admin#/", "Edit"]] : [["/admin#/", "Sign in"]]);
  $("#nav").innerHTML = items.map(([h, t]) =>
    `<a href="${h}" class="${(h === "#/" ? route === "" : h.startsWith("#/") && route.startsWith(h.slice(2))) ? "on" : ""}">${t}</a>`).join("");
}
async function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [path, qs] = hash.split("?");
  renderNav(path);
  const m = path.match(/^book\/(\d+)(?:\/(edit|index))?$/);
  window.scrollTo(0, 0);
  try {
    if (m && m[2] === "edit" && ADMIN) return await editView(+m[1]);
    if (m && m[2] === "index" && ADMIN) return await indexView(+m[1]);
    if (m) return await bookView(+m[1]);
    if (path === "add" && ADMIN) return await addView();
    if (path === "try" && ADMIN) return await tryView();
    if (path === "review" && ADMIN) return await reviewView();
    if (path === "more" && ADMIN) return await moreView();
    if (path === "cook" && ADMIN) return await cookView();
    if (path === "stats" && ADMIN) return await statsView();
    if (path === "eyb" && ADMIN) return await eybView();
    if (path === "links" && ADMIN) return await linksView();
    if (qs) { const p = new URLSearchParams(qs); for (const k of ["q", "cuisine", "tag", "author", "publisher", "ingredient"]) S[k] = p.get(k) || ""; if (p.get("sort")) S.sort = p.get("sort"); }
    else { S.author = S.publisher = ""; }
    return await homeView();
  } catch (e) {
    view.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

/* ---------- home ---------- */
async function homeView() {
  if (!S.facets) S.facets = await api("facets");
  if (!S.ingredients) { try { S.ingredients = (await api("ingredients")).ingredients.filter((i) => i.n && !i.hint); } catch { S.ingredients = []; } }
  const f = S.facets;
  const opt = (arr, cur, label) => `<option value="">${label}</option>` +
    arr.map((x) => `<option value="${esc(x.name)}" ${x.name === cur ? "selected" : ""}>${esc(x.name)} (${x.n})</option>`).join("");
  view.innerHTML = `
    <div class="controls">
      <label class="search"><input id="q" type="search" placeholder="Search books, authors, recipes…" value="${esc(S.q)}" autocomplete="off" aria-label="Search"></label>
      <select id="cuisine" aria-label="Cuisine">${opt(f.cuisines, S.cuisine, "Cuisine")}</select>
      <select id="tag" aria-label="Tag">${opt(f.tags, S.tag, "Tag")}</select>
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
    for (const k of ["q", "cuisine", "tag", "author", "publisher", "sort"]) if (S[k]) p.set(k, S[k]);
    if (S.ingredient) p.set("ingredient", S.ingredient);
    const rp = new URLSearchParams({ limit: 60 });
    for (const k of ["q", "cuisine", "tag", "publisher", "ingredient"]) if (S[k]) rp.set(k, S[k]);
    const wantRecipes = S.q.trim().length >= 2 || S.ingredient;
    // With an ingredient picked, start from the books that have it and how many recipes each; open one to list them.
    const byBook = !!S.ingredient;
    const [bk, rc] = await Promise.all([
      api("books?" + p),
      byBook ? api("recipes/books?" + rp) : wantRecipes ? api("recipes?" + rp) : Promise.resolve({ recipes: [], count: 0 }),
    ]);
    if (seq !== loadSeq || !$("#summary")) return;
    const chips = [];
    if (S.author) chips.push(`<button class="chip x" data-clear="author">${esc(S.author)}</button>`);
    if (S.publisher) chips.push(`<button class="chip x" data-clear="publisher">${esc(S.publisher)}</button>`);
    if (S.cuisine) chips.push(`<button class="chip x" data-clear="cuisine">${esc(S.cuisine)}</button>`);
    if (S.tag) chips.push(`<button class="chip x" data-clear="tag">${esc(S.tag)}</button>`);
    if (S.ingredient) chips.push(`<button class="chip x" data-clear="ingredient">Uses ${esc(S.ingredient.toLowerCase())}</button>`);
    const nb = bk.books.length === bk.total ? `${bk.total} books` : bk.books.length ? `${bk.books.length} of ${bk.total} books` : "No matching books";
    $("#summary").innerHTML = `<span>${rc.count ? `${rc.count.toLocaleString()} recipe${rc.count > 1 ? "s" : ""} · ` : ""}${nb}</span>${chips.join("")}`;
    $$("[data-clear]").forEach((b) => b.onclick = () => {
      S[b.dataset.clear] = "";
      if (location.hash.includes("?")) history.replaceState(null, "", "#/");
      homeView();
    });
    const recipeLine = (r) => `<li><div class="rn"><a href="#/book/${r.book_id}">${r.heading ? `<span class="rh">${esc(r.heading)} · </span>` : ""}${esc(r.name)}</a></div>
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
        ${ADMIN && b.rating != null ? starsHTML(b.rating) : ""}</div></a>`).join("")
      : (rc.count ? "" : `<div class="empty">Nothing matches.</div>`);
  };
  $("#q").addEventListener("input", debounce((e) => { S.q = e.target.value; load(); }, 220));
  for (const k of ["cuisine", "tag", "sort"]) $("#" + k).onchange = (e) => { S[k] = e.target.value; load(); };
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
const starsHTML = (v, cls = "") => `<span class="stars ${cls}" title="${starLabel(v)}" aria-label="${starLabel(v)}">
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
// Where to find a recipe: the page in the book, or a link for one saved from a website.
const whereHTML = (r) => r.url ? `<a class="pg web" href="${esc(r.url)}" target="_blank" rel="noopener" title="Open the recipe on ${esc(r.book_title || S.bookTitle || "the site")}">Open ↗</a>`
  : `<span class="pg">${r.page ? "p. " + esc(r.page) : ""}</span>`;
function recipeRow(r) {
  const extra = [];
  if (r.cooked_count) extra.push(`<span class="cooked">Cooked ${r.cooked_count}×${r.cooked_avg != null ? ` · <span class="rating">${num(r.cooked_avg)}/10</span>` : ""} · ${fmtDay(r.cooked_last)}</span>`);
  if (r.rating != null) extra.push(`<span class="rating">${num(r.rating)}/10</span>${r.cooked_count ? " (journal)" : ""}`);
  if (r.last_made) extra.push(`made ${fmtDay(r.last_made)}`);
  if (r.notes) extra.push(esc(r.notes));
  const nph = (S.bookPhotos || []).filter((p) => p.recipe_id === r.id).length;
  return `<li data-id="${r.id}"><div class="rn">${r.heading ? `<span class="rh">${esc(r.heading)} · </span>` : ""}${esc(r.name)}
    ${ADMIN && extra.length ? `<div class="extra">${extra.join(" · ")}</div>` : ""}</div>
    ${whereHTML(r)}
    ${ADMIN ? `<span class="acts">${S.features && S.features.dinnerlog_url && S.bookTitle ? `<a class="log" title="Log this dinner in Dinner Log" target="_blank" rel="noopener"
        href="${esc(S.features.dinnerlog_url)}/new?source_type=cookbook&source_title=${encodeURIComponent(S.bookTitle)}&source_detail=${encodeURIComponent(r.page || r.url || "")}&name=${encodeURIComponent(r.heading && r.name[0] === r.name[0].toLowerCase() ? r.heading + ", " + r.name : r.name)}">🍽</a>` : ""}
      <button title="${nph ? `${nph} photo${nph > 1 ? "s" : ""}` : "Add photos"}" class="pho ${nph ? "on" : ""}">📷${nph ? `<span class="n">${nph}</span>` : ""}</button>
      <button title="Want to try" class="want ${r.want_to_try ? "on" : ""}">${r.want_to_try ? "★" : "☆"}</button>
      <button title="Edit" class="edit">✎</button></span>` : ""}</li>`;
}
async function bookView(id) {
  const b = await api("books/" + id);
  S.bookTitle = b.title;
  S.bookPhotos = b.photos || [];
  const rows = [];
  if (b.subtitle) rows.push(["Subtitle", esc(b.subtitle)]);
  if (b.publisher) rows.push(["Publisher", esc(b.publisher) + (b.year ? `, ${b.year}` : "")]);
  else if (b.year) rows.push(["Published", b.year]);
  if (b.cuisine) rows.push(["Cuisine", `<a href="#/?cuisine=${encodeURIComponent(b.cuisine)}">${esc(b.cuisine)}</a>`]);
  if (b.isbn) rows.push(["ISBN", esc(b.isbn)]);
  if (b.format) rows.push(["Format", esc(b.format)]);
  if (b.signed) rows.push(["", "Signed copy"]);
  if (b.kind === "online") rows.push(["Website", `<a href="https://${esc(b.site)}" target="_blank" rel="noopener">${esc(b.site)} ↗</a>`]);
  const priv = ADMIN ? [
    b.status && b.status !== "owned" ? `<div><strong>${STATUS[b.status]}</strong>${b.status === "out" && b.out_since ? " since " + fmtDay(b.out_since) : ""}${b.loaned_to ? " to " + esc(b.loaned_to) : ""}</div>` : "",
    b.cooked_count ? `<div>Cooked from ${b.cooked_count}× (Dinner Log), last ${fmtDay(b.cooked_last)}</div>` : "",
    (b.wishes || []).length ? `<div>On ${b.wishes.map((w) => esc(w.person_name) + "'s").join(", ")} wish list</div>` : "",
    b.location ? `<div>Shelf: ${esc(b.location)}</div>` : "",
    b.private_notes ? `<div>${esc(b.private_notes)}</div>` : "",
    b.eyb_status ? `<div>Eat Your Books: ${esc(b.eyb_status.toLowerCase())}${b.recipe_count ? "" : " (export its recipes from EYB to fill the index)"}</div>` : "",
    b.needs_review ? `<div class="review">Needs review: ${esc(b.needs_review)}</div>` : "",
  ].join("") : "";
  view.innerHTML = `
    <a class="back" href="#/">← All books</a>
    <div class="book">
      <div>${coverHTML(b)}</div>
      <div>
        <h1>${esc(b.title)}</h1>
        <div class="muted">${(b.authors || []).map((a) => `<a href="#/?author=${encodeURIComponent(a)}">${esc(a)}</a>`).join(", ")}</div>
        ${ADMIN && b.kind !== "online" ? `<div class="rate" id="rate">
          <span class="stars big" role="slider" tabindex="0" aria-label="Book rating" aria-valuemin="0" aria-valuemax="5"
            aria-valuenow="${b.rating ?? 0}" aria-valuetext="${starLabel(b.rating)}"><span class="on" style="width:${(b.rating || 0) * 20}%">★★★★★</span>☆☆☆☆☆</span>
          <span class="ratev small muted">${b.rating == null ? "Not rated" : num(b.rating)}</span>
          <button class="link clear" ${b.rating == null ? "hidden" : ""}>Clear</button></div>` : ""}
        <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
        ${b.tags.length ? `<div class="tags">${b.tags.map((t) => `<a class="chip" href="#/?tag=${encodeURIComponent(t)}">${esc(t)}</a>`).join("")}</div>` : ""}
        ${b.description ? `<p class="desc">${esc(b.description)}</p>` : ""}
        ${priv ? `<div class="private">${priv}</div>` : ""}
        ${ADMIN && b.kind === "online" ? `<p class="small muted">Recipes saved from this website: each one opens on the site, where you're signed in.</p>
          <div class="actions"><a class="btn primary" href="#/links">Add recipes from links</a><a class="btn" href="#/book/${id}/edit">Edit</a></div>` : ""}
        ${ADMIN && b.kind !== "online" ? `<div class="actions"><button id="shelf" class="${b.status === "out" ? "primary" : ""}">${b.status === "out" ? "Put back on shelf" : "Pulled out"}</button>
          ${b.status === "wanted" ? `<button id="wish" class="primary">Add to a wish list…</button>` : ""}
          <a class="btn" href="#/book/${id}/edit">Edit book</a>
          <a class="btn" href="#/book/${id}/index">Import index</a><button id="addr">Add recipe</button></div>` : ""}
      </div>
    </div>
    ${S.bookPhotos.length ? `<div class="section-h"><h2>What we've made <span class="muted small">${S.bookPhotos.length}</span></h2></div>
      <div class="photos">${S.bookPhotos.map((p, i) => `<button class="ph" data-i="${i}"><img loading="lazy" src="${esc(p.thumb)}" alt="${esc(p.recipe_name)}">
        <span class="pn">${esc(p.recipe_name)}</span><span class="pd">${fmtDay(p.taken_on)}${p.recipe_page ? " · p. " + esc(p.recipe_page) : ""}</span></button>`).join("")}</div>` : ""}
    <div class="section-h"><h2>Recipes <span class="muted small">${b.recipes.length || ""}</span></h2>
      ${b.recipes.length > 12 ? `<input id="rf" type="search" placeholder="Filter recipes" style="max-width:240px">` : ""}</div>
    ${b.recipes.length ? `<ul class="rlist" id="rl">${b.recipes.map(recipeRow).join("")}</ul>`
      : `<p class="muted">No recipes indexed for this book yet.${ADMIN ? " Use <em>Import index</em> to add the book's index." : ""}</p>`}
    ${ADMIN ? `<div id="dinners"></div>` : ""}`;
  const rf = $("#rf");
  if (rf) rf.oninput = () => {
    const q = fold(rf.value.trim());
    $$("#rl li").forEach((li) => li.classList.toggle("hidden", !!q && !fold(li.textContent).includes(q)));
  };
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
      b2.classList.toggle("on", !!r.want_to_try); b2.textContent = r.want_to_try ? "★" : "☆";
    });
    $(".edit", li).onclick = () => recipeDialog(r, id, () => bookView(id));
    $(".pho", li).onclick = () => photoDialog(r, () => bookView(id));
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
      <label class="btn primary">📷 Add photos<input type="file" accept="image/*" multiple hidden></label>
      <span class="small muted" id="pst"></span><button value="close">Done</button></div>
  </form>`;
  document.body.append(d);
  const grid = $(".pgrid", d), form = $("form", d);
  const show = (list) => {
    grid.innerHTML = list.length ? list.map((p) => `<div class="pt" data-id="${p.id}"><img src="${esc(p.thumb)}" alt="">
      <span class="small muted">${fmtDay(p.taken_on)}${p.caption ? " · " + esc(p.caption) : ""}</span>
      <button type="button" class="x" title="Delete photo" aria-label="Delete photo">×</button></div>`).join("")
      : `<p class="muted small" style="margin:0">No photos yet. Add some of the dish you made.</p>`;
    $$(".pt", grid).forEach((el) => {
      $("img", el).onclick = () => lightbox(list.map((p) => ({ ...p, recipe_name: r.name, recipe_page: r.page })), list.findIndex((p) => p.id === +el.dataset.id));
      $(".x", el).onclick = async () => {
        if (!confirm("Delete this photo?")) return;
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
    if (!confirm(`Delete “${r.name}”?`)) return;
    await api("recipes/" + r.id, { method: "DELETE" }); d.close(); toast("Deleted"); done();
  });
  d.showModal();
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
    <label class="f">Shelf / location<input name="location" value="${esc(b.location)}"></label>
    <label class="f">Rating<select name="rating"><option value="">Not rated</option>${[0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5].map((v) =>
      `<option value="${v}" ${b.rating === v ? "selected" : ""}>${starLabel(v)}</option>`).join("")}</select></label>
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
  if (el) el.innerHTML = `<div class="cover"><img src="${esc(url)}" alt="" onerror="this.closest('#newcover').innerHTML='<span class=muted>No cover image available</span>'"></div><span class="small muted">Cover will be saved with the book</span>`;
}

async function editView(id) {
  if (!S.facets) S.facets = await api("facets");
  const b = await api("books/" + id);
  view.innerHTML = `<a class="back" href="#/book/${id}">← ${esc(b.title)}</a><h1>Edit book</h1>
    <div class="panel"><div class="coverpick">${coverHTML(b)}
      <div style="display:grid;gap:8px"><label class="btn">Upload / photograph cover<input id="cf" type="file" accept="image/*" capture="environment" hidden></label>
      <button id="ol">Find matches on Open Library</button></div><div id="newcover" class="coverpick"></div></div>
      <div id="cands" class="cands"></div></div>
    ${bookForm(b)}
    <div class="actions"><button class="primary" id="save">Save</button><a class="btn" href="#/book/${id}">Cancel</a>
      <span style="flex:1"></span><button class="danger" id="delete">Delete book</button></div>`;
  const form = $("#bf");
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
    if (!confirm(`Delete “${b.title}” and its ${b.recipes.length} recipes? This can't be undone.`)) return;
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
        <input id="isbn" inputmode="numeric" placeholder="ISBN (10 or 13 digits)"><button id="look">Look up</button></div>
      ${!canScan ? `<p class="small muted">Barcode scanning needs Chrome on Android over HTTPS (the site's public address, not the LAN IP).</p>` : ""}
      <div id="lookres" class="small" style="margin-top:10px"></div>
      <div id="newcover" class="coverpick" style="margin-top:10px"></div>
    </div>
    ${bookForm({})}
    <div class="actions"><button class="primary" id="save">Save book</button><button id="ol">Find matches by title</button></div>
    <div id="cands" class="cands panel hidden"></div>`;
  const form = $("#bf");
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
      <p class="small muted" style="margin-top:0">Paste a page or two of the book's index, or use <strong>📷 Take photo</strong> on each
      index page in turn: the entries from every page collect in one preview. Without photo import, take a photo in Google Lens instead,
      choose <em>Text → Select all → Copy</em>, and paste here. Lines without a page number become headings
      (like “Chicken”), and the lowercase or indented lines under them become sub-entries.</p>
      <textarea id="txt" rows="12" placeholder="Chicken&#10;  braised with figs, honey, and vinegar, 350&#10;  roast, with bread salad, 342&#10;Zuni Caesar Salad, 154"></textarea>
      <div class="actions"><button class="primary" id="parse">Preview</button>
        <span id="photobtns"><label class="btn">📷 Take photo<input id="snap" type="file" accept="image/*" capture="environment" hidden></label>
        <label class="btn">Choose photos<input id="photos" type="file" accept="image/*" multiple hidden></label></span></div>
      <div id="photostatus" class="small muted"></div>
    </div>
    <div id="prev"></div>`;
  let feat = { photo_import: false };
  try { feat = await api("features"); } catch { /* ignore */ }
  if (!feat.photo_import) {
    $("#photobtns").classList.add("hidden");
    $("#photostatus").innerHTML = `Photo import is off. Add <code>ANTHROPIC_API_KEY</code> to <code>/etc/conf.d/cookbooks</code> to read index photos directly.`;
  }
  // Entries read so far. Photos add to them, one index page at a time ("Take photo" again for the next page),
  // keeping whatever was already corrected in the preview.
  let collected = [], photos = 0, photoChain = Promise.resolve();
  const keepEdits = () => {
    if (!$("#prev tbody")) return;
    collected = $$("#prev tbody tr").map((tr) => ({ ...collected[+tr.dataset.i], checked: $("input[type=checkbox]", tr).checked,
      heading: $("[name=heading]", tr).value, name: $("[name=name]", tr).value, page: $("[name=page]", tr).value }));
  };
  // Each photo is a job on the server (a dense page takes a minute or two); all photos start at once,
  // and their entries are added in the order they were chosen.
  const waitForJob = async (job, tick) => {
    const t0 = Date.now();
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 3000));
      if (!location.hash.startsWith(`#/book/${id}/index`)) throw new Error("Cancelled");
      let r;
      try { r = await api(`index/photo/${job}`); }
      catch (err) {
        if (err.status === 400 || err.status === 401 || err.status === 404 || Date.now() - t0 > 15 * 60000) throw err;
        continue;
      }  // a dropped connection: keep checking
      if (r.status === "done") return r;
      if (r.status === "error") throw new Error(r.error);
      tick(r.elapsed);
    }
  };
  const runBatch = async (files) => {
    if (!files.length || !location.hash.startsWith(`#/book/${id}/index`)) return;
    const st = $("#photostatus"), many = files.length > 1;
    const label = (i) => many ? `Photo ${i + 1}` : "The photo";
    const notes = [], waiting = new Set(files.keys());
    let read = 0, secs = 0;
    const show = () => {
      if (!$("#photostatus")) return;
      st.innerHTML = (waiting.size ? `<div>Reading ${many ? `${waiting.size} of ${files.length} photos` : "the photo"}… ${secs ? `${secs}s` : ""}
        (a full index page can take a minute or two)</div>` : "") + notes.map((x) => `<div class="${x.bad ? "err" : ""}">${esc(x.text)}</div>`).join("");
    };
    show();
    // The server reads a few photos at once; when it's full (429) wait and try again.
    const start = async (fd) => {
      for (let tries = 0; ; tries++) {
        try { return await api(`books/${id}/index/photo`, { method: "POST", body: fd }); }
        catch (err) { if (err.status !== 429 || tries >= 40) throw err; await new Promise((r) => setTimeout(r, 3000)); }
      }
    };
    const jobs = files.map(async (f, i) => {
      const fd = new FormData(); fd.append("file", f);
      try {
        const { job } = await start(fd);
        return await waitForJob(job, (s) => { secs = Math.max(secs, s); show(); });
      } catch (err) { return { error: err.message }; }
      finally { waiting.delete(i); show(); }
    });
    const results = [];
    for (const [i, p] of jobs.entries()) results.push([i, await p]);  // in the order chosen
    if (!location.hash.startsWith(`#/book/${id}/index`)) return;
    keepEdits();
    for (const [i, r] of results) {
      if (r.error) { notes.push({ bad: true, text: `${label(i)}: ${r.error}` }); continue; }
      collected.push(...r.rows.map((x) => ({ ...x, from: "photo" }))); read += r.rows.length; photos++;
      if (r.warning) notes.push({ bad: true, text: `${label(i)}: ${r.warning}` });
    }
    const n = (k, w) => `${k} ${w}${k === 1 ? "y" : "ies"}`;  // entry / entries
    if (collected.length) notes.unshift({ text: `${n(read, "entr")} from ${many ? "these photos" : "this photo"}; ${n(collected.length, "entr")} from ${photos} photo${photos === 1 ? "" : "s"} so far. `
      + "Take another photo for the next index page, or check them below (especially page numbers) and add them." });
    show();
    if (collected.length) renderPreview(collected);
  };
  const readPhotos = (e) => {
    const files = [...e.target.files]; e.target.value = "";
    if (!files.length) return;
    photoChain = photoChain.then(() => runBatch(files)).catch(() => {});
  };
  $("#snap").onchange = readPhotos;
  $("#photos").onchange = readPhotos;
  $("#parse").onclick = () => busy($("#parse"), async () => {
    const { rows } = await api(`books/${id}/index/parse`, { method: "POST", body: { text: $("#txt").value } });
    if (!rows.length) { $("#prev").innerHTML = `<p class="muted">Couldn't find any entries with page numbers in that text.</p>`; return; }
    keepEdits();  // the pasted text replaces what was pasted before; photo entries stay
    collected = collected.filter((r) => r.from !== "paste").concat(rows.map((r) => ({ ...r, from: "paste" })));
    renderPreview(collected);
  });
  function renderPreview(rows) {
    const fresh = rows.filter((r) => !r.duplicate).length;
    $("#prev").innerHTML = `<div class="section-h"><h2>${rows.length} entries</h2><span class="muted small">${rows.length - fresh} already in this book</span></div>
      <div class="overflow"><table class="preview"><thead><tr><th></th><th>Heading / recipe</th><th>Page</th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr class="${r.duplicate ? "dup" : ""}" data-i="${i}"><td><input type="checkbox" ${(r.checked ?? !r.duplicate) ? "checked" : ""}></td>
        <td class="hn"><input type="text" name="heading" placeholder="(no heading)" value="${esc(r.heading)}"><input type="text" name="name" value="${esc(r.name)}"></td>
        <td class="pg"><input type="text" name="page" value="${esc(r.page)}"></td></tr>`).join("")}</tbody></table></div>
      <div class="actions"><button class="primary" id="commit">Add checked entries</button><button id="again">Paste more</button></div>`;
    $("#again").onclick = () => {  // start over
      $("#txt").value = ""; $("#txt").focus(); $("#prev").innerHTML = ""; $("#photostatus").textContent = ""; collected = []; photos = 0;
      window.scrollTo(0, 0);
    };
    $("#commit").onclick = () => busy($("#commit"), async () => {
      const out = $$("#prev tbody tr").filter((tr) => $("input[type=checkbox]", tr).checked).map((tr) => ({
        heading: $("[name=heading]", tr).value, name: $("[name=name]", tr).value, page: $("[name=page]", tr).value }));
      const r = await api(`books/${id}/index/commit`, { method: "POST", body: { rows: out } });
      toast(`Added ${r.added} recipes${r.skipped ? `, skipped ${r.skipped} duplicates` : ""}`);
      $("#txt").value = ""; $("#prev").innerHTML = ""; $("#photostatus").textContent = ""; collected = []; photos = 0; S.facets = null; window.scrollTo(0, 0);
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
    <div class="panel"><h3>Recipes from websites</h3>
      <p class="small muted" style="margin-top:0">Save recipes from NYT Cooking and other sites by their links: your whole Recipe Box at once, or one at a time from your phone's share sheet.</p>
      <a class="btn primary" href="#/links">Add from links</a></div>
    <div class="panel"><h3>Lists</h3><div class="actions" style="margin:0">
      <a class="btn" href="#/try">Want to try</a><a class="btn" href="#/review">Needs review</a>
      <a class="btn" href="#/stats">Dinner Log matching</a></div></div>
    <div class="panel"><h3>Use with Claude (MCP)</h3>
      <p class="small muted">Add as a custom connector in Claude: <code>${esc(mcpUrl)}</code> gives public search (books, recipes, pages).
      ${feat.mcp_private ? `For ratings, cooking history, suggestions and starring, use <code>${esc(mcpUrl)}/&lt;MCP_TOKEN&gt;</code>, or send the token as a Bearer header. The token is in <code>/etc/conf.d/cookbooks</code>; keep that URL private.`
        : `Set <code>MCP_TOKEN</code> in <code>/etc/conf.d/cookbooks</code> to unlock private tools (suggestions, stats, starring).`}</p></div>
    <div class="panel"><h3>Export</h3><p class="small muted">Full backup including recipes, ratings and private notes.</p>
      <div class="actions"><a class="btn" href="/admin/api/export.json">Download JSON</a><a class="btn" href="/admin/api/export.csv">Books as CSV</a></div></div>
    <div class="panel"><h3>Public view</h3><p class="small muted">See what visitors see (no ratings, notes, shelf or loan details).</p>
      <a class="btn" href="/">Open public catalog</a></div>`;
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
      <label class="btn primary">Choose an EYB CSV file<input id="eybfile" type="file" accept=".csv,text/csv" hidden></label>
      <span id="eybname" class="small muted"></span></div>
    <div id="eybprev"></div>
    <div class="panel"><h3>Ingredient lists</h3>
      <p class="small muted" style="margin-top:0">Search an ingredient on EYB (e.g. Chicken), export the results, and import them here.
        Those recipes get tagged, so searching “chicken” here also finds recipes that use chicken without saying so in the title.
        A <strong>search hint</strong> (zucchini, kale…) works the same for search and Claude, but gets no chip on the catalog page.</p>
      <div class="isbnrow"><input id="ingname" placeholder="Ingredient, e.g. Chicken" autocomplete="off" list="dl-ing">
        <label class="btn primary">Choose ingredient CSV<input id="ingfile" type="file" accept=".csv,text/csv" hidden></label></div>
      <label class="chk"><input type="checkbox" id="inghint" checked> Search hint only (no chip on the catalog page)</label>
      <datalist id="dl-ing"></datalist>
      <div id="ingprev"></div>
      <div class="zipimp"><label class="btn">Choose a zip of CSVs<input id="ingzip" type="file" accept=".zip,application/zip" hidden></label>
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
      <button class="rm" title="Remove this ingredient tag (recipes stay)">✕</button></span></li>`).join("")}</ul>` : "";
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
        <div class="small muted">${esc(g.author)} · ${g.count} recipes</div></td><td><select class="dec">${opt(null)}</select></td></tr>`).join("")}</tbody></table></div>` : ""}
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
      <td><select class="dec">${opts}</select>
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
          <td><select class="dec">${opt(g.match)}</select>${g.match ? (g.method === "eyb title" ? "" : `<div class="small muted">matched by ${esc(g.method)}</div>`) : `<div class="small review hint">Not in your catalog: pick the book, add it, or skip</div>`}</td></tr>`).join("")}</tbody></table></div>
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
const MODES = [["want", "Starred, not yet made"], ["untried", "Never made"], ["neglected", "Forgotten books"], ["favorites", "Old favorites"]];
S.cook = { mode: "want", cuisine: "", tag: "" };
async function cookView() {
  if (!S.facets) S.facets = await api("facets");
  const f = S.facets, c = S.cook;
  const opt = (arr, cur, label) => `<option value="">${label}</option>` + arr.map((x) => `<option ${x.name === cur ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  view.innerHTML = `<h1 style="margin-top:18px">What should we cook?</h1>
    <div class="seg" role="tablist">${MODES.map(([k, t]) => `<button role="tab" data-m="${k}" class="${c.mode === k ? "on" : ""}">${t}</button>`).join("")}</div>
    <div class="controls" style="margin-top:10px"><select id="cc">${opt(f.cuisines, c.cuisine, "Cuisine")}</select>
      <select id="ct">${opt(f.tags, c.tag, "Tag")}</select><button class="primary" id="again">↻ More ideas</button></div>
    <p class="small muted" id="pool"></p><div id="ideas" class="ideas"></div>
    <p class="small"><a href="#/try">See the full want-to-try list</a></p>`;
  const load = async () => {
    const p = new URLSearchParams({ mode: c.mode, n: 5 }); if (c.cuisine) p.set("cuisine", c.cuisine); if (c.tag) p.set("tag", c.tag);
    const r = await api("suggest?" + p);
    $("#pool").textContent = r.pool ? `${r.label}: ${r.pool.toLocaleString()} recipes to choose from` : "";
    $("#ideas").innerHTML = r.recipes.length ? r.recipes.map((x) => `<a class="idea" ${x.url ? `href="${esc(x.url)}" target="_blank" rel="noopener"` : `href="#/book/${x.book_id}"`}>
        <div class="ib">${esc(x.book_title)}${x.page ? ` · p. ${esc(x.page)}` : x.url ? " ↗" : ""}</div>
        <div class="in">${x.heading ? `<span class="rh">${esc(x.heading)} · </span>` : ""}${esc(x.name)}</div>
        <div class="extra">${[x.cooked_count ? `cooked ${x.cooked_count}×, last ${fmtDay(x.cooked_last)}` : "",
          x.cooked_avg != null ? `<span class="rating">${num(x.cooked_avg)}/10</span>` : x.rating != null ? `<span class="rating">${num(x.rating)}/10</span>` : "",
          x.status === "out" ? "book is off the shelf" : "", x.notes ? esc(x.notes) : ""].filter(Boolean).join(" · ")}</div></a>`).join("")
      : `<div class="empty">Nothing fits.${c.mode === "want" ? " Star recipes (☆) on a book's page to fill this list." : c.mode === "favorites" ? " Ratings come from Dinner Log and the old recipe journal." : " Index more books to get ideas here."}</div>`;
  };
  $$(".seg button").forEach((b) => b.onclick = () => { c.mode = b.dataset.m; $$(".seg button").forEach((x) => x.classList.toggle("on", x === b)); load(); });
  $("#cc").onchange = (e) => { c.cuisine = e.target.value; load(); };
  $("#ct").onchange = (e) => { c.tag = e.target.value; load(); };
  $("#again").onclick = load;
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
async function statsView() {
  const [s, dl] = await Promise.all([api("stats"), api("dinnerlog")]);
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
      <div class="panel"><h3>Off the shelf <span class="muted small">${s.off_shelf.length}</span></h3>${s.off_shelf.length ? `<ul class="rlist">${s.off_shelf.map((b) => `<li><div class="rn"><a href="#/book/${b.id}">${esc(b.title)}</a></div><span class="pg">since ${fmtDay(b.out_since)}</span></li>`).join("")}</ul>` : `<p class="small muted">Everything's on the shelf. Use “Pulled out” on a book's page.</p>`}
        <details style="margin-top:14px"><summary>Never cooked from <span class="muted small">(${s.never_cooked.length})</span></summary>
        <ul class="rlist">${s.never_cooked.map((b) => `<li><div class="rn"><a href="#/book/${b.id}">${esc(b.title)}</a></div></li>`).join("")}</ul></details></div>
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
      <p class="small muted" style="margin-top:0">${dl.meals} cookbook dinners · ${dl.recipe_matched} matched to a recipe · ${dl.book_only} matched to a book only · checked ${esc(dl.synced || "")}</p>
      ${dl.unmatched_titles.length ? `<p class="small">These cookbook names from Dinner Log don't match a book here. Pick the book once and every dinner with that name follows.</p>
        <ul class="rlist">${dl.unmatched_titles.map((t) => `<li data-title="${esc(t.title)}"><div class="rn">${esc(t.title)} <span class="muted small">${t.n} dinner${t.n > 1 ? "s" : ""}</span></div>
        <select class="alias" style="max-width:55%"><option value="">Pick a book…</option></select></li>`).join("")}</ul>` : `<p class="small">Every cookbook name in Dinner Log matches a book.</p>`}`
      : `<p class="small muted" style="margin-top:0">Couldn't read Dinner Log at <code>${esc(dl.path)}</code>${dl.error ? ` (${esc(dl.error)})` : ""}. The installer gives this app read access when both run on the same server.</p>`}</div>`;
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
    <div class="panel"><textarea id="lt" rows="6" placeholder="https://cooking.nytimes.com/recipes/1017256-french-onion-soup">${esc(prefill)}</textarea>
      <div class="actions"><button class="primary" id="lp">Preview</button></div><div id="lres"></div></div>
    <div class="panel"><h3>Your whole NYT Cooking Recipe Box</h3>
      <ol class="small"><li>On a computer, drag this button to the browser's bookmarks bar: <a class="btn" href="${esc(BOOKMARKLET)}" onclick="event.preventDefault()">Copy recipe links</a></li>
        <li>Open your Recipe Box on cooking.nytimes.com and scroll to the bottom, so every recipe has loaded.</li>
        <li>Click <b>Copy recipe links</b> in the bookmarks bar, paste here and preview.</li></ol>
      <p class="small muted" style="margin-bottom:0">It only reads the links already on the page, in your own signed-in browser. Saving again skips recipes that are already here, so you can repeat it whenever you've saved more.</p></div>
    <div class="panel"><h3>From your phone</h3>
      <p class="small muted" style="margin:0">Open Cookbook Collection's editor page in Chrome on Android and use <b>Add to Home screen</b> (install). Then <b>Share → Cookbook Collection</b> from the NYT Cooking app or any recipe page brings the link straight here.</p></div>`;
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
  if (!ADMIN) {
    try { S.editor = (await api("/api/me")).editor; } catch { /* ignore */ }
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
