"""Ask: plain-language search over the catalog for editors (a tool-use loop over the read-only MCP tools).

This file holds the settings, the `ask_log` table, thread validation, the daily cap and the route. The thread
comes from the browser and is untrusted; only plain question/answer text is ever turned into model messages.
"""
import os, time

from flask import g, jsonify, request

import app as core
import extras
from app import app, db, editor_required, make_err, clean

SEARCH_KEY = os.environ.get("ANTHROPIC_SEARCH_KEY", "").strip()
SEARCH_MODEL = os.environ.get("ANTHROPIC_SEARCH_MODEL", "").strip() or "claude-haiku-5-5"
DAILY_LIMIT = int(os.environ.get("ASK_DAILY_LIMIT") or 200)
MAX_ROUNDS = 5       # model calls per question; the last one is forced to finish
MAX_QUESTIONS = 5    # questions per thread
BUDGET_S = 60        # total time per question
CALL_TIMEOUT_S = 25  # time per API call
MAX_TOKENS = 2048
MAX_Q = 500
MAX_A = 1000


def search_key():
    return SEARCH_KEY or extras.ANTHROPIC_API_KEY


def enabled():
    return bool(search_key())


def migrate(con):
    con.execute("""CREATE TABLE IF NOT EXISTS ask_log (
      id INTEGER PRIMARY KEY,
      ts REAL NOT NULL,            -- unix time
      editor TEXT,                 -- identity from @editor_required (g.editor)
      rounds INTEGER NOT NULL,
      input_tokens INTEGER NOT NULL DEFAULT 0,
      output_tokens INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL         -- ok | capped | api_error | timeout
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS ask_log_ts ON ask_log(ts)")


class BadThread(ValueError):
    pass


def validate_thread(thread):
    """The browser's thread -> Claude messages. Raises BadThread with a short message."""
    if not isinstance(thread, list) or not thread:
        raise BadThread("Ask a question first.")
    out, questions = [], 0
    for i, entry in enumerate(thread):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise BadThread("That conversation isn't valid; start a new question.")
        (key, val), = entry.items()
        if key not in ("q", "a") or not isinstance(val, str):
            raise BadThread("That conversation isn't valid; start a new question.")
        if key != ("q" if i % 2 == 0 else "a"):
            raise BadThread("That conversation isn't valid; start a new question.")
        if key == "q":
            questions += 1
            if questions > MAX_QUESTIONS:
                raise BadThread("Start a new question to keep going.")
            text = clean(val)
            if not text:
                raise BadThread("Ask a question first.")
            if len(text) > MAX_Q:
                raise BadThread(f"Keep questions under {MAX_Q} characters.")
            out.append({"role": "user", "content": text})
        else:
            if len(val) > MAX_A:
                raise BadThread("That conversation isn't valid; start a new question.")
            out.append({"role": "assistant", "content": val})
    if thread[-1].keys() != {"q"}:
        raise BadThread("That conversation isn't valid; start a new question.")
    return out


def asked_today(con):
    """Questions that reached the model since local midnight (capped requests don't count)."""
    t = time.localtime()
    start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return con.execute("SELECT COUNT(*) FROM ask_log WHERE status != 'capped' AND ts >= ?", (start,)).fetchone()[0]


def log(editor, rounds, in_tok, out_tok, status):
    con = db()
    con.execute("INSERT INTO ask_log(ts, editor, rounds, input_tokens, output_tokens, status) VALUES (?,?,?,?,?,?)",
                (time.time(), editor, rounds, in_tok, out_tok, status))
    con.commit()


def answer(messages):
    """(status, body) for a validated thread. The tool loop arrives in the next change."""
    raise NotImplementedError


@app.post("/admin/api/ask")
@editor_required
def ask_route():
    if not enabled():
        return make_err(503, "AI search isn't set up (no API key).")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "thread" not in body:
        return make_err(400, "Ask a question first.")
    try:
        messages = validate_thread(body["thread"])
    except BadThread as e:
        return make_err(400, str(e))
    if asked_today(db()) >= DAILY_LIMIT:
        log(g.editor, 0, 0, 0, "capped")
        return make_err(429, "Daily AI search limit reached; try again tomorrow.")
    status, payload = answer(messages)
    return jsonify(payload), status
