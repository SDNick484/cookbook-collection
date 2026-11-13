"""Cookbooks we want to buy: five columns on books, their validation, release dates, the Wanted list and Bought it.

A wanted book is an ordinary books row with status 'wanted'. It is a shopping list, not the collection:
app.IN_COLLECTION keeps it out of PUBLIC_BOOKS, COOKBOOKS and the editor catalog, so only the Wanted screen shows
it. Buying one flips its status to 'owned' in place (same id and uid), so nothing is entered twice.
"""
import datetime
import re
from decimal import Decimal, InvalidOperation

COLS = {
    "price_cents":    "INTEGER CHECK (price_cents IS NULL OR price_cents >= 0)",
    "price_currency": "TEXT",   # ISO 4217, upper-cased on write; NULL is shown as USD
    "release_date":   "TEXT",   # 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'
    "buy_url":        "TEXT",   # http(s) only
    "want_priority":  "INTEGER CHECK (want_priority IS NULL OR want_priority BETWEEN 1 AND 3)",
}

PRICE_MAX_CENTS = 100_000_000  # 1,000,000.00
URL_MAX = 2000
_PRICE = re.compile(r"\d+(\.\d{1,2})?")
_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_URL = re.compile(r"https?://\S", re.I)


def migrate(con):
    """Additive only: five nullable columns on books. Safe on every start."""
    have = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    for col, decl in COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE books ADD COLUMN {col} {decl}")


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _price_cents(v):
    s = str(v).strip()
    if s[:1] in ("$", "€", "£"):
        s = s[1:].strip()
    if "," in s and "." in s:  # 1,234.50 or 1.234,50: which is the decimal point is a guess, so refuse
        raise ValueError("Price is a number like 35 or 24.50")
    s = s.replace(",", ".")
    if not _PRICE.fullmatch(s):
        raise ValueError("Price is a number like 35 or 24.50")
    try:
        cents = int(Decimal(s) * 100)
    except InvalidOperation:
        raise ValueError("Price is a number like 35 or 24.50") from None
    if cents > PRICE_MAX_CENTS:
        raise ValueError("Price is a number like 35 or 24.50")
    return cents


def release_start(s):
    """The first day of a release date ('2027' -> Jan 1, '2027-03' -> Mar 1), or None if missing or unparseable."""
    m = _DATE.fullmatch((s or "").strip()) if isinstance(s, str) else None
    if not m:
        return None
    try:
        return datetime.date(int(m[1]), int(m[2] or 1), int(m[3] or 1))
    except ValueError:
        return None


def today():
    """Today in the server's TZ (the zone app.local_midnight uses). Tests patch it."""
    return datetime.date.today()


def is_released(s, today):
    """Out by `today`? A book with no (or an unparseable) release date counts as out."""
    start = release_start(s)
    return start is None or start <= today


def clean_fields(data):
    """The want fields present in `data`, as DB column -> value. Raises ValueError with a message for the user.
    Keys not sent are left out, so an edit that doesn't show them leaves them alone."""
    out = {}
    if "price" in data:
        out["price_cents"] = None if _blank(data["price"]) else _price_cents(data["price"])
    if "price_currency" in data:
        v = data["price_currency"]
        if _blank(v):
            out["price_currency"] = None
        elif isinstance(v, str) and re.fullmatch(r"[A-Za-z]{3}", v.strip()):
            out["price_currency"] = v.strip().upper()
        else:
            raise ValueError("Currency is a three-letter code, like USD or EUR")
    if "release_date" in data:
        v = data["release_date"]
        if _blank(v):
            out["release_date"] = None
        else:
            v = str(v).strip()
            start = release_start(v)
            if start is None or not 1900 <= start.year <= 2100:
                raise ValueError("Release date is a year, year-month or full date, like 2027-03")
            out["release_date"] = v
    if "buy_url" in data:
        v = data["buy_url"]
        if _blank(v):
            out["buy_url"] = None
        else:
            v = str(v).strip()
            if not _URL.match(v) or len(v) > URL_MAX:
                raise ValueError("The buy link must be an http(s) link")
            out["buy_url"] = v
    if "want_priority" in data:
        v = data["want_priority"]
        if _blank(v):
            out["want_priority"] = None
        else:
            try:
                p = int(v)
            except (TypeError, ValueError):
                raise ValueError("Priority is 1, 2 or 3") from None
            if isinstance(v, bool) or p not in (1, 2, 3) or str(v).strip() not in ("1", "2", "3"):
                raise ValueError("Priority is 1, 2 or 3")
            out["want_priority"] = p
    return out
