"""local_midnight(): the moment the daily limits (Ask, photo import) reset. It follows TZ, so the installer's TZ
setting moves the reset from UTC midnight to the owner's midnight. POSIX TZ strings are used so the test needs no
zoneinfo files (Alpine has none without the tzdata package)."""
import calendar
import os
import time
from contextlib import contextmanager

import pytest

from conftest import A

PACIFIC = "PST8PDT,M3.2.0,M11.1.0"  # UTC-8 in November, after DST ends on the 1st
NOW = calendar.timegm((2026, 11, 11, 5, 0, 0))  # 05:00 UTC = 21:00 on Nov 10 in Pacific time


@contextmanager
def tz(value):
    old = os.environ.get("TZ")
    os.environ["TZ"] = value
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.fixture()
def clock(monkeypatch):
    """Freeze "now" for time.localtime() only (mktime must keep using the real zone rules)."""
    real = time.localtime
    monkeypatch.setattr(time, "localtime", lambda *a: real(*a) if a else real(NOW))


def test_resets_at_utc_midnight_in_utc(clock):
    with tz("UTC0"):
        assert A.local_midnight() == calendar.timegm((2026, 11, 11, 0, 0, 0))


def test_resets_at_local_midnight_with_a_time_zone_set(clock):
    with tz(PACIFIC):
        # still Nov 10 in Pacific time, so the reset was 00:00 PST on the 10th = 08:00 UTC
        assert A.local_midnight() == calendar.timegm((2026, 11, 10, 8, 0, 0))


def test_the_two_zones_really_differ(clock):
    with tz("UTC0"):
        utc = A.local_midnight()
    with tz(PACIFIC):
        local = A.local_midnight()
    assert utc - local == 16 * 3600  # one calendar day minus the 8 h offset


def test_local_midnight_is_never_in_the_future_or_a_day_old():
    for zone in ("UTC0", PACIFIC, "JST-9"):
        with tz(zone):
            m = A.local_midnight()
            assert 0 <= time.time() - m < 25 * 3600  # 25: a DST change makes a day 25 h long
