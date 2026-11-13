"""The family wish list link, registered onto the core app.

Adds a book to a person's list in the companion wish list app, over localhost, and remembers what was added.
"""
import json, os, urllib.error, urllib.parse, urllib.request

from flask import abort, jsonify, request

import app as core
from app import app, db, editor_required, make_err


WISHLIST_URL = os.environ.get("WISHLIST_URL", "http://127.0.0.1:8766").rstrip("/")
WISHLIST_BOOK_URL = os.environ.get("WISHLIST_BOOK_URL", "https://www.amazon.com/s?k={q}")
# The wish list app's address in a browser, shown as a link after Bought it. Display only: never called.
WISHLIST_PUBLIC_URL = os.environ.get("WISHLIST_PUBLIC_URL", "").rstrip("/")


WISH_SCHEMA = """CREATE TABLE IF NOT EXISTS wish_items (
  id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  person_id INTEGER, person_name TEXT, item_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def migrate(con):
    con.execute(WISH_SCHEMA)


def wishlist_call(method, path, body=None, timeout=5):
    """Talk to the wish list app over localhost (it treats loopback requests as LAN editors)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(WISHLIST_URL + path, data=data, method=method, headers={
        "X-Wishlist": "1", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        raise RuntimeError(msg or f"wish list answered {e.code}")
    except OSError as e:
        raise RuntimeError(f"wish list isn't reachable at {WISHLIST_URL} ({e})")


def wishlist_available():
    """True only when the wish list app answers on WISHLIST_URL. The URL is set by default even when the app
    isn't installed, so checking the URL alone would show a button that always fails. A refused localhost
    connection returns at once, so checking on each features request is cheap."""
    if not WISHLIST_URL:
        return False
    try:
        wishlist_call("GET", "/api/people", timeout=1)
        return True
    except RuntimeError:
        return False


@app.get("/admin/api/wishlist/people")
@editor_required
def wishlist_people():
    try:
        people = wishlist_call("GET", "/api/people")
    except RuntimeError as e:
        return make_err(502, str(e))
    return jsonify(people=[{"id": p["id"], "name": p["name"], "color": p.get("color")} for p in people])


@app.post("/admin/api/books/<int:book_id>/wishlist")
@editor_required
def add_to_wishlist(book_id):
    d = request.get_json(force=True)
    con = db()
    row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        abort(404)
    b = core.book_dicts([row], True)[0]
    pid = int(d.get("person_id") or 0)
    by = f" by {', '.join(b['authors'][:2])}" if b["authors"] else ""
    q = b["isbn"] or f"{b['title']} {' '.join(b['authors'][:1])}"
    notes = ["Cookbook"]
    if b["isbn"]:
        notes.append(f"ISBN {b['isbn']}")
    # The dialog starts from the book's own price and priority (wanted.py) and the editor may change them, so what
    # it sends wins; the book's values fill in for a caller that sends none. The buy page beats the search link.
    if d.get("price") not in (None, ""):
        price = str(d["price"]).strip()
    else:
        price = f"{b['price_cents'] / 100:.2f}" if b["price_cents"] is not None else None
    if price and b["price_currency"] and b["price_currency"] != "USD":
        notes.append(f"{b['price_currency']} {price}")
    if d.get("notes"):
        notes.append(str(d["notes"])[:300])
    buy = b["buy_url"] if b["buy_url"] and b["buy_url"].lower().startswith(("http://", "https://")) else None
    item = {"name": f"{b['title']}{by}"[:200], "kind": "online",
            "url": buy or WISHLIST_BOOK_URL.replace("{q}", urllib.parse.quote_plus(q)),
            "notes": " · ".join(notes), "priority": int(d.get("priority") or b["want_priority"] or 2)}
    if price:
        item["price"] = price
    try:
        people = {p["id"]: p["name"] for p in wishlist_call("GET", "/api/people")}
        if pid not in people:
            return make_err(400, "Pick whose wish list")
        created = wishlist_call("POST", f"/api/people/{pid}/items", item)
    except RuntimeError as e:
        return make_err(502, str(e))
    con.execute("INSERT INTO wish_items(book_id, person_id, person_name, item_id) VALUES (?,?,?,?)",
                (book_id, pid, people[pid], created.get("id")))
    con.commit()
    return jsonify(ok=True, person=people[pid], item_id=created.get("id"))


def wishes_for_book(book_id):
    return [dict(r) for r in db().execute(
        "SELECT person_name, created_at FROM wish_items WHERE book_id=? ORDER BY id", (book_id,))]
