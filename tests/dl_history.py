"""Test helper: stand in for Dinner Log by storing cooking history the way a pull does."""
import app as A
import extras as X


class History:
    """Meals as Dinner Log's /api/history would send them. Each add() stores a new snapshot, as if a pull
    had just brought it in, so the next request rebuilds `cooked`."""

    def __init__(self, monkeypatch):
        monkeypatch.setattr(X, "DINNERLOG_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(X, "DINNERLOG_TOKEN", "test-token")
        monkeypatch.setattr(X, "start_background", lambda fn: None)   # no real pulls from tests
        self.meals = []
        self._store()

    def add(self, day, name, source_type="cookbook", title="", detail="", rating=None, **ids):
        mid = len(self.meals) + 1
        self.meals.append({"id": mid, "day": day, "name": name, "source_type": source_type, "source_title": title,
                           "source_detail": detail, "rating": rating, **ids})
        self._store()
        return mid

    def _store(self):
        con = A.connect()
        try:
            X.store_snapshot(con, self.meals, f'"{len(self.meals)}"')
        finally:
            con.close()
