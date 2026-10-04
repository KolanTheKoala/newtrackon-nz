"""The permanent daily summary (data/daily.json) and the long-term section built from it."""

from __future__ import annotations

import glob
import json
import os
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient

from newtrackon import ntextra
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

DAY = 86400
D = 1_800_000_000 - 1_800_000_000 % DAY  # a UTC midnight
URL = "udp://daily.example:1/announce"


def _day(n):
    return T._day(D + n * DAY)


def _tr(h, newest):
    T.LAST_REC[URL] = newest
    return SimpleNamespace(url=URL, historic=list(h), last_checked=0)


class TestDailyUpdate:
    # 10 slots at the end of day -1 (down), day 0 all up, day 1 half up, day 2 under way (00:00, 00:30, 01:00)
    H = [0] * 10 + [1] * 48 + [1] * 24 + [0] * 24 + [1] * 3
    NEWEST = D + 2 * DAY + 3600

    def test_backfills_completed_days_only(self) -> None:
        T.LAT_HIST[URL] = {"Oceania": [[D, 40], [D + 7200, 50], [D + 14400, 60]], "Asia": [[D + DAY, 200]]}
        T.daily_update([_tr(self.H, self.NEWEST)], now=self.NEWEST)
        assert T.DAILY[URL] == [[_day(-1), 0.0, 10, {}], [_day(0), 100.0, 48, {"Oceania": 50}], [_day(1), 50.0, 48, {"Asia": 200}]]
        assert T._jload("data/daily.json") == T.DAILY  # first save straight away

    def test_rerun_changes_nothing_and_next_day_is_added_when_over(self) -> None:
        T.daily_update([_tr(self.H, self.NEWEST)], now=self.NEWEST)
        before = [list(r) for r in T.DAILY[URL]]
        T.daily_update([_tr(self.H, self.NEWEST + 600)], now=self.NEWEST + 600)  # same slot, day 2 not over
        assert T.DAILY[URL] == before
        h = self.H + [1] * 45 + [0] * 2  # day 2 completes (3 + 45 slots up), day 3 has 2 slots
        T.daily_update([_tr(h, D + 3 * DAY + 1800)], now=D + 3 * DAY + 1800)
        assert T.DAILY[URL] == before + [[_day(2), 100.0, 48, {}]]

    def test_catches_up_after_downtime_from_last_checked(self) -> None:
        h = [1] * 48 * 5 + [1]
        T.DAILY[URL] = [[_day(0), 100.0, 48, {}]]
        t = SimpleNamespace(url=URL, historic=h, last_checked=D + 5 * DAY + 60)  # no LAST_REC: just restarted
        T.daily_update([t], now=D + 5 * DAY + 60)
        assert [r[0] for r in T.DAILY[URL]] == [_day(i) for i in range(5)]

    def test_saves_at_most_every_15_minutes(self) -> None:
        T.daily_update([_tr(self.H, self.NEWEST)], now=self.NEWEST)
        T.DAILY.clear()  # a new day found within 15 min of the last save stays in memory
        T.daily_update([_tr(self.H, self.NEWEST)], now=self.NEWEST + 60)
        assert T._DAILY_SAVE["dirty"] and len(T._jload("data/daily.json")[URL]) == 3
        T.daily_update([], now=self.NEWEST + 960)
        assert not T._DAILY_SAVE["dirty"]

    def test_unreadable_file_is_kept_aside_not_overwritten(self) -> None:
        with open("data/daily.json", "w") as f:
            f.write('{"udp://x:1/announce": [["2026-01-01", 100')
        assert T._daily_load("data/daily.json") == {}
        assert not os.path.exists("data/daily.json")
        bad = glob.glob("data/daily.json.bad-*")
        assert len(bad) == 1 and open(bad[0]).read().startswith('{"udp://x:1')
        assert T._daily_load("data/missing.json") == {}  # no file yet: nothing to keep


def _rows(n, start="2026-08-20"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [[(d0 + timedelta(days=i)).isoformat(), 100.0 if i % 10 else 50.0, 48, {"Europe": 300, "Oceania": 40 + i}] for i in range(n)]


class TestLongTerm:
    def test_needs_more_than_30_days(self) -> None:
        T.DAILY[URL] = _rows(30)
        assert ntextra._long_term(URL) is None

    def test_months_newest_first(self) -> None:
        T.DAILY[URL] = _rows(42)  # 20 Aug .. 30 Sep
        lt = ntextra._long_term(URL)
        assert lt["since"] == "2026-08-20" and lt["days"] == 42 and len(lt["strip"]) == 42
        assert [m["month"] for m in lt["months"]] == ["Sep 2026", "Aug 2026"]
        aug = lt["months"][1]
        assert aug["days"] == 12 and aug["pct"] == round(100 * (10 * 48 + 2 * 24) / (12 * 48), 1)  # 20th and 30th half up
        assert aug["lat"] == [("Oceania", 46), ("Europe", 300)]  # Oceania first

    def test_strip_is_the_last_365_days(self) -> None:
        T.DAILY[URL] = _rows(400, start="2025-01-01")
        assert len(ntextra._long_term(URL)["strip"]) == 365


@pytest.mark.usefixtures("region_db")
class TestPageAndApi:
    AKL = "udp://akl.example:1/announce"

    def test_page_shows_long_term_once_past_30_days(self, flask_client: FlaskClient) -> None:
        T.DAILY[self.AKL] = _rows(10)
        assert "Long-term uptime" not in flask_client.get("/tracker/akl.example").get_data(as_text=True)
        T.DAILY[self.AKL] = _rows(42)
        html = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert "Long-term uptime" in html and html.count("<rect") == 42 and "Sep 2026" in html and "Oceania" in html

    def test_api_has_daily(self, flask_client: FlaskClient) -> None:
        T.DAILY[self.AKL] = _rows(2)
        d = json.loads(flask_client.get("/api/tracker/akl.example").get_data(as_text=True))
        assert d["daily"][0] == {"day": "2026-08-20", "uptime": 50.0, "slots": 48, "latency_ms": {"Europe": 300, "Oceania": 40}}
        assert "daily" not in json.loads(flask_client.get("/api/details").get_data(as_text=True))[0]  # only per tracker


def test_history_keeps_30_days() -> None:
    assert T.HISTORIC_SLOTS == 30 * 48
