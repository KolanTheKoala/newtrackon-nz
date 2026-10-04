"""Diagnostics from the trackers' own replies: warning messages, error messages, announce intervals."""

from __future__ import annotations

import struct

import pytest
from flask.testing import FlaskClient

from newtrackon import ntextra, scraper
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

AKL = "udp://akl.example:1/announce"


def test_udp_error_reply_keeps_the_whole_message() -> None:
    buf = struct.pack("!II", 3, 1234) + b"torrent not registered\x00"
    assert scraper.udp_error_text(buf) == "torrent not registered"
    assert scraper.udp_error_text(struct.pack("!II", 3, 1)) == "(no message)"


@pytest.mark.parametrize(("raw", "label"), [
    ("Tracker error message: Invalid info_hash: '8e' length 2", "Rejected"),
    ("Error while announcing: torrent not found", "Rejected"),
    ("Tracker error message: timeout waiting for backend", "Rejected"),  # its own message, even if it says timeout
    ("UDP timeout", "Timeout"), ("HTTP connection failed", "Refused")])
def test_down_label(raw: str, label: str) -> None:
    assert T._nt_down_label(raw) == label
    assert T.FIX_DOWN[label] in ntextra.FIX_TITLES


def test_warning_kept_and_cleared() -> None:
    T._warn_set(AKL, "Require passkey or authkey")
    assert T.WARNINGS[AKL]["msg"] == "Require passkey or authkey" and T._jload("data/warnings.json") == T.WARNINGS
    t0 = T.WARNINGS[AKL]["t"]
    T._warn_set(AKL, "Require passkey or authkey")
    assert T.WARNINGS[AKL]["t"] == t0  # unchanged: not saved again
    T._warn_set(AKL, None)
    assert AKL not in T.WARNINGS and T._jload("data/warnings.json") == {}


@pytest.mark.parametrize(("msg", "fixable", "word"), [
    ("Require passkey or authkey", False, "private"),
    ("info hash is not authorized with this tracker", False, "whitelist"),
    ("Rate limited, slow down", True, "limiting"),
    ("Welcome to our tracker", True, None)])
def test_warning_meaning(msg: str, fixable: bool, word: str | None) -> None:
    T.WARNINGS[AKL] = {"msg": msg, "t": 0}
    w = ntextra._warning(AKL)
    assert w[0] == msg and w[2] is fixable and (word in w[1] if word else w[1] is None)


@pytest.mark.parametrize(("iv", "note"), [(120, "every 2 min"), (30, "every 30 s"), (86400, "every 24 h"), (1800, None), (None, None)])
def test_interval_note(iv: int | None, note: str | None) -> None:
    n = ntextra._interval_note(iv)
    assert (note in n) if note else n is None


@pytest.mark.usefixtures("region_db")
def test_tracker_page_shows_them(flask_client: FlaskClient) -> None:
    T.WARNINGS[AKL] = {"msg": "info hash is not authorized with this tracker", "t": 0}
    T.ANN_IV[AKL] = 86400
    html = flask_client.get("/tracker/akl.example").get_data(as_text=True)
    assert "Message from the tracker" in html and "&ldquo;info hash is not authorized with this tracker&rdquo;" in html
    assert "every 24 h, so they rarely get new peers" in html
    assert flask_client.get("/api/tracker/akl.example").get_json()["warning_message"] == "info hash is not authorized with this tracker"


@pytest.mark.parametrize(("msg", "why"), [
    ("Require passkey or authkey", "a private tracker: it asks for a passkey"),
    ("info hash is not authorized with this tracker", "it only serves its own torrents (a whitelist)"),
    ("Tracker error message: Unregistered torrent", "it only serves its own torrents (a whitelist)"),
    ("Tracker error message: Invalid info_hash", None), ("not found", None), ("Welcome!", None), (None, None)])
def test_closed_reason(msg: str | None, why: str | None) -> None:
    assert T._nt_closed_reason(msg) == why


def test_closed_streak_counts_and_resets() -> None:
    for _ in range(2):
        T._closed_seen(AKL, "Require passkey")
    assert T.CLOSED[AKL] == {"why": "a private tracker: it asks for a passkey", "n": 2}
    T._closed_seen(AKL, None)  # a normal reply
    assert AKL not in T.CLOSED and T._jload("data/closed.json") == {}


class TestClosedEviction:
    def _check(self, t, n: int, peer_fails: int, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections import deque
        from time import time
        T.CLOSED[t.url] = {"why": "it only serves its own torrents (a whitelist)", "n": n}
        monkeypatch.setitem(T.PEER_FAILS, t.url, peer_fails)
        t.added, t.last_uptime, t.status = int(time()) - 86400, int(time()), 1  # one day old, answering
        t.historic = deque([1] * 48, maxlen=1440)
        t.update_uptime()

    def test_removed_and_banned_after_3(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, 3, T.PEER_FAIL_LIMIT, monkeypatch)
        assert sample_tracker.to_be_deleted is True
        assert T._NT_DEL_REASON[sample_tracker.url] == "it only serves its own torrents (a whitelist)"
        assert open("data/denylist.txt").read().split()[0] == sample_tracker.host

    def test_not_before_3(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, 2, T.PEER_FAIL_LIMIT, monkeypatch)
        assert sample_tracker.to_be_deleted is False

    def test_not_while_peers_work(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, 5, 0, monkeypatch)  # says so, but still hands out peers: leave it
        assert sample_tracker.to_be_deleted is False


def test_submission_refused_when_its_first_answer_says_closed() -> None:
    from newtrackon import ingest, persistence
    url = "http://wl.example:80/announce"
    persistence.submitted_data.clear()
    try:
        persistence.submitted_data.appendleft({"url": url, "time": 0, "ip": "", "status": 1,
                                               "info": ["{'interval': 86400, 'warning message': 'info hash is not authorized with this tracker'}"]})
        assert ingest._closed_on_submit(url) == "saying it only serves its own torrents (a whitelist)"
        ingest.log_wrong_interval_denial(ingest._closed_on_submit(url))
        row = persistence.submitted_data[0]
        assert row["status"] == 0 and row["info"][1] == "Tracker rejected for saying it only serves its own torrents (a whitelist)"
        assert ingest._closed_on_submit("http://other.example:80/announce") is None
    finally:
        persistence.submitted_data.clear()


class TestUpBadClock:
    NOPEERS = ["hands out no peers (3+ of its last 6 peer tests failed)"]

    def _state(self, url: str, days: float, bad: list[str]) -> None:
        from time import time
        T.LAST_STATE[url] = {"st": "up_bad", "bad": bad, "dead": [], "since": int(time() - days * 86400)}

    def test_days_counts_no_or_fake_peers_only(self) -> None:
        self._state(AKL, 3, self.NOPEERS)
        assert 2.99 < T._nt_upbad_days(AKL) < 3.01
        self._state(AKL, 3, ["its published IPv4 address is dead"])
        assert T._nt_upbad_days(AKL) is None  # Up/Broken: not on the clock

    def test_grey_from_day_3(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        t = SimpleNamespace(url=AKL, last_uptime=0)
        with patch.object(ntextra, "_rowcls", return_value="orange"):
            self._state(AKL, 2.5, self.NOPEERS)
            assert ntextra._dying(t) is None
            self._state(AKL, 3.2, self.NOPEERS)
            assert ntextra._dying(t) == "Up/Bad for 3+ days: removed and banned after 5 unless fixed"

    def _check(self, t, days: float, peer_fails: int, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections import deque
        from time import time
        self._state(t.url, days, self.NOPEERS)
        monkeypatch.setitem(T.PEER_FAILS, t.url, peer_fails)
        t.added, t.last_uptime, t.status = int(time()) - 30 * 86400, int(time()), 1
        t.historic = deque([1] * 48, maxlen=1440)
        t.update_uptime()

    def test_removed_after_5_days_like_down(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, 5.1, T.PEER_FAIL_LIMIT, monkeypatch)
        assert sample_tracker.to_be_deleted is True
        assert T._NT_DEL_REASON[sample_tracker.url] == "handed out no peers for 5 days (Up/Bad)"
        assert open("data/denylist.txt").read().split()[0] == sample_tracker.host

    def test_kept_before_5_days_or_once_fixed(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, 4.5, T.PEER_FAIL_LIMIT, monkeypatch)
        assert sample_tracker.to_be_deleted is False
        self._check(sample_tracker, 8, 0, monkeypatch)  # passing the peer test again now
        assert sample_tracker.to_be_deleted is False


def test_one_clock_for_down_and_up_bad() -> None:
    assert T.UPBAD_DAYS == T.REMOVE_DAYS == 5 and ntextra._DYING_DAYS == 3


class TestBadStretch:
    H = 3600
    BAD = "is Up/Bad: hands out no peers (3+ of its last 6 peer tests failed)"

    def test_short_recovery_does_not_reset(self) -> None:
        s = {"st": "up_bad", "since": 0, **T._bad_track(None, "up_bad", 0)}
        s = {"st": "up_good", "since": 10 * self.H, **T._bad_track(s, "up_good", 10 * self.H)}  # passes for an hour
        s = {"st": "up_bad", "since": 11 * self.H, **T._bad_track(s, "up_bad", 11 * self.H)}
        assert s["bad_since"] == 0

    def test_long_recovery_resets(self) -> None:
        s = {"st": "up_bad", "since": 0, **T._bad_track(None, "up_bad", 0)}
        s = {"st": "up_good", "since": 10 * self.H, **T._bad_track(s, "up_good", 10 * self.H)}
        s = {"st": "up_good", "since": 10 * self.H, **T._bad_track(s, "up_good", 23 * self.H)}  # still good 13 h later
        assert "bad_since" not in s
        assert T._bad_track(s, "up_bad", 31 * self.H) == {"bad_since": 31 * self.H}

    def test_seeded_from_events_like_corpscorp(self) -> None:
        url, now = "udp://cc.example:80/announce", 200 * self.H
        evs = [(122.8, "good", "is Up/Good again"), (121.7, "bad", self.BAD), (120.2, "good", "is Up/Good again"), (119.3, "bad", self.BAD),
               (72.2, "good", "is Up/Good again"), (50.8, "bad", self.BAD), (47.1, "good", "is Up/Good again"), (46.2, "bad", self.BAD),
               (9.6, "good", "is Up/Good again"), (8.6, "bad", self.BAD), (6.1, "good", "is Up/Good again"), (5.2, "bad", self.BAD)]
        T.EVENTS[:] = [{"t": int(now - h * self.H), "url": url, "host": "cc.example", "type": k, "text": x} for h, k, x in evs]
        T.LAST_STATE[url] = {"st": "up_bad", "bad": ["hands out no peers (3+ of its last 6 peer tests failed)"], "dead": [], "since": int(now - 5.2 * self.H)}
        T._bad_seed(now)
        assert T.LAST_STATE[url]["bad_since"] == int(now - 50.8 * self.H)  # the 21 h recovery 72 h ago counts; the 1 h blips don't
        assert 2.1 < T._nt_upbad_days(url, now) < 2.2


class TestFamilyHysteresis:
    U = "udp://fam.example:1/announce"

    def test_one_good_check_does_not_revive_a_dead_family(self) -> None:
        for _ in range(3):
            T._fam_set(self.U, "v6", {"v4": True, "v6": False})
        assert T.FAM_FAILS[self.U]["n"] == 3
        T._fam_set(self.U, None, {"v4": True, "v6": True})
        assert T.FAM_FAILS[self.U]["n"] == 3 and T.FAM_FAILS[self.U]["ok"] == 1  # still counted dead
        T._fam_set(self.U, "v6", {"v4": True, "v6": False})
        assert T.FAM_FAILS[self.U] == {"n": 4, "fam": "v6"}  # the lucky answer is forgotten
        T._fam_set(self.U, None, {"v4": True, "v6": True})
        T._fam_set(self.U, None, {"v4": True, "v6": True})
        assert self.U not in T.FAM_FAILS  # two good checks in a row: working again

    def test_unconfirmed_failures_clear_at_once(self) -> None:
        T._fam_set(self.U, "v6", {"v4": True, "v6": False})
        T._fam_set(self.U, None, {"v4": True, "v6": True})
        assert self.U not in T.FAM_FAILS

    def test_answer_history_for_the_page(self) -> None:
        for v6 in (False, True, False, False):
            T._fam_set(self.U, None, {"v4": True, "v6": v6})
        assert T.FAM_HIST[self.U] == {"v4": [1, 1, 1, 1], "v6": [0, 1, 0, 0]}
        assert ntextra._fam_answers(self.U) == {"v6": (1, 4)}
