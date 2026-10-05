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


class TestProbation:
    def _new(self, t, hist):
        from collections import deque
        from time import time
        t.added, t.last_uptime, t.status = int(time()) - 86400, int(time()), 1
        t.historic = deque(hist, maxlen=1440)
        t.update_uptime()
        return t

    def test_capped_but_perfect_is_new_not_unreliable(self, sample_tracker) -> None:
        t = self._new(sample_tracker, [1] * 48)  # one perfect day: held at the ~83 ceiling
        assert t.uptime < 90 and T._nt_is_new(t) is True
        assert ntextra._state(t)[0] == "up_new"

    def test_real_misses_are_still_unreliable(self, sample_tracker) -> None:
        t = self._new(sample_tracker, [1] * 30 + [0] * 4 + [1] * 14)  # a 2-hour outage on its first day
        assert T._nt_is_new(t) is False
        assert ntextra._state(t)[0] == "up_unreliable"

    def test_probation_ends_at_7_days(self, sample_tracker) -> None:
        from time import time
        t = self._new(sample_tracker, [1] * 48)
        t.added = int(time()) - 8 * 86400
        assert T._nt_is_new(t) is False


class TestIntervalPenalty:
    @pytest.mark.parametrize(("iv", "pen"), [(120, 10), (299, 10), (300, 5), (899, 5), (900, 0), (1800, 0), (7200, 0), (7201, 5), (21600, 5), (21601, 10), (86400, 10), (None, 0)])
    def test_bands(self, iv: int | None, pen: float) -> None:
        if iv is None:
            T.ANN_IV.pop(AKL, None)
        else:
            T.ANN_IV[AKL] = iv
        assert T._nt_iv_penalty(AKL) == pen

    def test_lowers_score_not_status(self, sample_tracker) -> None:
        from collections import deque
        from time import time
        t = sample_tracker
        t.added, t.last_uptime, t.status = int(time()) - 30 * 86400, int(time()), 1
        t.historic = deque([1] * 1440, maxlen=1440)
        T.ANN_IV[t.url] = 1800
        t.update_uptime()
        good = t.uptime
        T.ANN_IV[t.url] = 120
        t.update_uptime()
        assert round(good - t.uptime) == 10 and t.uptime < 91
        assert ntextra._state(t)[0] == "up_good" and ntextra._rowcls(t) == "green"  # still Up/Good: points are for ranking

    @pytest.mark.usefixtures("region_db")
    def test_page_shows_the_penalty_and_advice(self, flask_client: FlaskClient) -> None:
        T.ANN_IV[AKL] = 120
        html = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert "Around 30 minutes" in html and "&minus;10 (every 2 min" in html
        assert html.count('href="/fix#interval"') == 2


def test_udp_tries_every_published_address(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import struct as st

    addrs = [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("2001:db8::dead", 6969, 0, 0)),
             (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("2001:db8::bad", 6969, 0, 0)),
             (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("192.0.2.66", 6969)),
             (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("192.0.2.7", 6969))]  # only this one answers
    tried: list[str] = []

    class Sock:
        def __init__(self, af, *a):
            self.family, self.addr, self.req = af, None, b""

        def settimeout(self, t): pass
        def close(self): pass

        def connect(self, sa):
            self.addr = sa[0]

        def sendall(self, req):
            self.req = req
            tried.append(self.addr)

        def recv(self, n):
            if self.addr != "192.0.2.7":
                raise TimeoutError
            tid = st.unpack("!i", self.req[12:16])[0]
            if st.unpack("!i", self.req[8:12])[0] == 0:
                return st.pack("!iiq", 0, tid, 77)
            return st.pack("!iiiii", 1, tid, 1800, 0, 1) + bytes([192, 0, 2, 9, 0x1a, 0xe1])

    monkeypatch.setattr(scraper.socket, "getaddrinfo", lambda *a, **k: addrs)
    monkeypatch.setattr(scraper.socket, "socket", Sock)
    monkeypatch.setattr(scraper, "require_public", lambda sa: None)
    monkeypatch.setattr(scraper, "check_peer_count", lambda r: None)
    resp, _ = scraper.announce_udp("udp://multi.example:6969/announce")
    assert resp["interval"] == 1800
    assert [a for a in dict.fromkeys(tried)] == ["2001:db8::dead", "192.0.2.66", "2001:db8::bad", "192.0.2.7"]  # alternating families


class TestAddressHealth:
    H, P = "multi.example", 6969

    def _gai(self, monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
        import socket
        rows = [((socket.AF_INET6 if ":" in ip else socket.AF_INET), socket.SOCK_DGRAM, 17, "", (ip, self.P)) for ip in ips]
        monkeypatch.setattr(scraper.socket, "getaddrinfo", lambda host, port, fam=0, st=0, *a: [r for r in rows if fam in (0, r[0])])

    def test_known_good_first_failing_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._gai(monkeypatch, ["192.0.2.1", "192.0.2.2", "192.0.2.3"])
        scraper.addr_record(self.H, self.P, "192.0.2.1", False)
        scraper.addr_record(self.H, self.P, "192.0.2.3", True)
        assert [r[4][0] for r in scraper.ordered_addrs(self.H, self.P)] == ["192.0.2.3", "192.0.2.2", "192.0.2.1"]

    def test_forgets_addresses_no_longer_published(self) -> None:
        scraper.addr_record(self.H, self.P, "192.0.2.1", True)
        scraper.addr_record(self.H, self.P, "192.0.2.9", True, published={"192.0.2.9"})
        assert list(scraper.ADDR_HEALTH["multi.example:6969"]) == ["192.0.2.9"]

    def test_family_is_dead_only_if_no_address_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._gai(monkeypatch, ["192.0.2.66", "192.0.2.7", "2001:db8::bad"])
        good = {"192.0.2.7"}

        def session(fam, sa, src):
            if sa[0] not in good:
                raise TimeoutError
            return SimpleNamespace(close=lambda: None), (lambda *a: {"interval": 1800})
        from types import SimpleNamespace
        monkeypatch.setattr(scraper, "_udp_session", session)
        monkeypatch.setattr(scraper, "_probe_srcs", lambda fam, key=None: [])
        monkeypatch.setattr(scraper, "require_public", lambda sa: None)
        res = scraper.family_probe("udp://multi.example:6969/announce")
        assert res == {"v4": True, "v6": False}  # IPv4 alive through its second address; IPv6's only address dead
        h = scraper.ADDR_HEALTH["multi.example:6969"]
        assert h["192.0.2.7"]["fails"] == 0 and h["192.0.2.66"]["fails"] >= 1

    @pytest.mark.usefixtures("region_db")
    def test_page_lists_the_dead_address(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        from time import time
        url = "udp://akl.example:1/announce"
        import copy
        t = copy.copy(next(x for x in ntextra._trackers() if x.url == url))
        t.ips = ["192.0.2.1", "192.0.2.2"]
        monkeypatch.setattr(ntextra, "_trackers", lambda: [t])
        scraper.ADDR_HEALTH["akl.example:1"] = {"192.0.2.1": {"ok": int(time()), "fails": 0}, "192.0.2.2": {"ok": 0, "fails": 5}}
        html = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert "1 of its 2 published addresses doesn&#39;t answer" in html and "<b>192.0.2.2</b> (IPv4, last answer never seen answering)" in html


class TestHttpAddresses:
    H, P = "web.example", 8080

    def _setup(self, monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, object]) -> list[str]:
        import socket
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, self.P)) for ip in outcomes]
        monkeypatch.setattr(scraper.socket, "getaddrinfo", lambda *a, **k: rows)
        monkeypatch.setattr(scraper, "ip_is_public", lambda ip: True)
        used: list[str] = []

        def once(url):
            ip = scraper._conn.pin or next(iter(outcomes))  # unpinned: DNS order, the first
            scraper._conn.last = (self.H, self.P, ip)
            used.append(ip)
            out = outcomes[ip]
            if isinstance(out, Exception):
                raise out
            return out
        monkeypatch.setattr(scraper, "_announce_http_once", once)
        return used

    def test_a_wrong_server_answer_is_retried_on_the_next_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        used = self._setup(monkeypatch, {"192.0.2.10": RuntimeError("HTTP 404 status code returned"),
                                         "192.0.2.11": {"interval": 1800, "peers": []}})
        assert scraper.announce_http("http://web.example:8080/announce")["interval"] == 1800
        assert used == ["192.0.2.10", "192.0.2.11"]
        h = scraper.ADDR_HEALTH["web.example:8080"]
        assert h["192.0.2.10"]["fails"] == 1 and h["192.0.2.11"]["fails"] == 0 and h["192.0.2.11"]["ok"]
        assert scraper._conn.pin is None and scraper._conn.track is False  # cleaned up

    def test_the_trackers_own_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        used = self._setup(monkeypatch, {"192.0.2.10": RuntimeError("Tracker error message: torrent not registered"),
                                         "192.0.2.11": {"interval": 1800, "peers": []}})
        with pytest.raises(RuntimeError, match="torrent not registered"):
            scraper.announce_http("http://web.example:8080/announce")
        assert used == ["192.0.2.10"]

    def test_all_addresses_wrong_raises_the_first_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._setup(monkeypatch, {"192.0.2.10": RuntimeError("HTTP 502 status code returned"),
                                  "192.0.2.11": RuntimeError("HTTP 502 status code returned")})
        with pytest.raises(RuntimeError, match="HTTP 502"):
            scraper.announce_http("http://web.example:8080/announce")

    def test_connect_tries_known_good_first_and_records_only_tracker_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 80)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.21", 80))]
        monkeypatch.setattr(scraper.socket, "getaddrinfo", lambda *a, **k: rows)
        monkeypatch.setattr(scraper, "ip_is_public", lambda ip: True)
        tried: list[str] = []

        def connect(addr, *a, **k):
            tried.append(addr[0])
            if addr[0] == "192.0.2.21":
                raise OSError("refused")
            return "sock"
        monkeypatch.setattr(scraper, "_u3c_create_connection", connect)
        scraper.addr_record("x.example", 80, "192.0.2.20", True)
        scraper._conn.track = False
        assert scraper._public_create_connection(("x.example", 80)) == "sock" and tried == ["192.0.2.20"]
        scraper.ADDR_HEALTH.clear()
        scraper.addr_record("x.example", 80, "192.0.2.21", True)  # now .21 looks best, but it refuses
        scraper._conn.track = True
        tried.clear()
        assert scraper._public_create_connection(("x.example", 80)) == "sock" and tried == ["192.0.2.21", "192.0.2.20"]
        assert scraper.ADDR_HEALTH["x.example:80"]["192.0.2.21"]["fails"] == 1
        scraper._conn.track = False


@pytest.fixture
def http_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "HTTP_PEER_TEST_TRUSTED", False)


@pytest.mark.usefixtures("http_paused")
class TestHttpPeerRulePaused:
    BAD = ["hands out no peers (3+ of its last 6 peer tests failed)"]

    def _check(self, t, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections import deque
        from time import time
        t.url = url
        T.LAST_STATE[url] = {"st": "up_bad", "bad": self.BAD, "dead": [], "since": int(time() - 6 * 86400),
                             "bad_since": int(time() - 6 * 86400), "useless_since": int(time() - 6 * 86400)}
        monkeypatch.setitem(T.PEER_FAILS, url, T.PEER_FAIL_LIMIT)
        t.added, t.last_uptime, t.status, t.historic = int(time()) - 30 * 86400, int(time()), 1, deque([1] * 48, maxlen=1440)
        t.update_uptime()

    def test_http_tracker_is_not_removed_for_the_peer_test(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, "http://tracker.example.com:80/announce", monkeypatch)
        assert sample_tracker.to_be_deleted is False

    def test_udp_tracker_still_is(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, "udp://tracker.example.com:6969/announce", monkeypatch)
        assert sample_tracker.to_be_deleted is True

    def test_no_grey_row_for_http(self) -> None:
        from time import time
        from types import SimpleNamespace
        from unittest.mock import patch
        url = "https://h.example:443/announce"
        T.LAST_STATE[url] = {"st": "up_bad", "bad": self.BAD, "dead": [], "since": int(time() - 4 * 86400), "useless_since": int(time() - 4 * 86400)}
        with patch.object(ntextra, "_rowcls", return_value="orange"):
            assert ntextra._dying(SimpleNamespace(url=url, status=1, last_uptime=0)) is None


@pytest.fixture
def per_family_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "PEER_TEST_PER_FAMILY", False)


@pytest.mark.usefixtures("per_family_paused")
class TestDualStackPeerRulePaused:
    BAD = ["hands out no peers (3+ of its last 6 peer tests failed)"]

    def _check(self, t, fams: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections import deque
        from time import time
        t.url = "udp://tracker.example.com:6969/announce"
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.BAD, "dead": [], "since": int(time() - 6 * 86400),
                               "bad_since": int(time() - 6 * 86400), "useless_since": int(time() - 6 * 86400)}
        monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
        monkeypatch.setitem(T.FAMS, t.url, fams)
        t.added, t.last_uptime, t.status, t.historic = int(time()) - 30 * 86400, int(time()), 1, deque([1] * 48, maxlen=1440)
        t.update_uptime()

    def test_dual_stack_is_not_removed(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, {"v4": True, "v6": True}, monkeypatch)
        assert sample_tracker.to_be_deleted is False

    def test_single_family_still_is(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, {"v4": True}, monkeypatch)
        assert sample_tracker.to_be_deleted is True


class TestPeerTestPerFamily:
    U = "udp://pf.example:6969/announce"

    def _feed(self, v4: list[int], v6: list[int]) -> None:
        for a, b in zip(v4, v6):
            T._peer_hist_add(self.U, bool(a), "v4")
            T._peer_hist_add(self.U, bool(b), "v6")

    def test_one_family_failing_is_partial_not_bad(self) -> None:
        self._feed([1] * 6, [0] * 6)
        assert T.PEER_FAILS.get(self.U, 0) == 0 and T._peer_fam_bad(self.U) == "v6"

    def test_every_family_failing_is_bad(self) -> None:
        self._feed([0] * 6, [0] * 6)
        assert T.PEER_FAILS[self.U] == 6 and T._peer_fam_bad(self.U) is None

    def test_old_combined_history_counts_until_families_have_results(self) -> None:
        T.PEER_HIST[self.U] = [0] * 6  # from before families were told apart: failing
        T._peer_hist_add(self.U, True, "v4")
        assert T.PEER_HIST_FAM[self.U]["?"] == [0] * 6 and T.PEER_FAILS[self.U] == 6  # one IPv4 pass isn't evidence yet
        T._peer_hist_add(self.U, True, "v4")
        T._peer_hist_add(self.U, True, "v4")
        assert "?" not in T._peer_fams(self.U) and T.PEER_FAILS.get(self.U, 0) == 0  # three are

    def test_both_status_rules_agree_on_up_broken(self, sample_tracker) -> None:
        from collections import deque
        from time import time
        t = sample_tracker
        t.url = self.U
        t.added, t.last_uptime, t.status, t.historic = int(time()) - 30 * 86400, int(time()), 1, deque([1] * 1440, maxlen=1440)
        self._feed([1] * 6, [0] * 6)
        t.update_uptime()
        st, bad = ntextra._state(t)
        assert st == "up_broken" and bad == ["nopeers_ipv6"]
        T.LAST_STATE.pop(self.U, None)
        t._emit_events()
        s = T.LAST_STATE[self.U]
        assert s["st"] == "up_bad" and s["bad"] == ["its IPv6 side doesn't share peers (3+ of its last 6 IPv6 peer tests failed)"]
        assert T._nt_bad_lbl(s["bad"]) == "Up/Broken" and T._broken(s["st"], s["bad"]) and not T._peer_bad(s["st"], s["bad"])
        assert ntextra._fix_anchor(t) == "dead-address"
        assert t.to_be_deleted is False

    def test_probe_family_restores_the_main_probe_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket
        monkeypatch.setattr(scraper, "ordered_addrs", lambda *a, **k: [(socket.AF_INET6, 2, 17, "", ("2001:db8::1", 6969, 0, 0))])
        calls = []
        monkeypatch.setattr(scraper, "_udp_session", lambda fam, sa, src: (type("S", (), {"close": lambda self: None})(), lambda *a: calls.append(a) or {}))
        monkeypatch.setattr(scraper, "peer_probe", lambda only_family=None: (scraper.rtt.probe[2], only_family))
        scraper.rtt.probe, scraper.rtt.probe_extra = ("udp", b"main", socket.AF_INET, ("192.0.2.1", 6969), b"pid"), {"fam": "v4"}
        assert scraper.peer_probe_family(self.U, "v6") == (socket.AF_INET6, None)
        assert calls and calls[0][2] == 0x76FD  # client A registered on IPv6 with the probe's port
        assert scraper.rtt.probe[1] == b"main" and scraper.rtt.probe_extra == {"fam": "v4"}


class TestDualStackNeedsEvidence:
    BAD = ["hands out no peers (3+ of its last 6 peer tests failed)"]

    def _check(self, t, monkeypatch: pytest.MonkeyPatch, fam_hist: dict) -> None:
        from collections import deque
        from time import time
        t.url = "udp://tracker.example.com:6969/announce"
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.BAD, "dead": [], "since": int(time() - 6 * 86400),
                               "bad_since": int(time() - 6 * 86400), "useless_since": int(time() - 6 * 86400)}
        monkeypatch.setitem(T.PEER_FAILS, t.url, 6)
        monkeypatch.setitem(T.FAMS, t.url, {"v4": True, "v6": True})
        T.PEER_HIST_FAM[t.url] = fam_hist
        t.added, t.last_uptime, t.status, t.historic = int(time()) - 30 * 86400, int(time()), 1, deque([1] * 48, maxlen=1440)
        t.update_uptime()

    def test_like_farted_not_removed_on_pre_split_history(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, monkeypatch, {"?": [0] * 6, "v6": [0], "v4": [1]})
        assert sample_tracker.to_be_deleted is False

    def test_removed_once_both_families_fail_with_evidence(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        self._check(sample_tracker, monkeypatch, {"v4": [0, 0, 0], "v6": [0, 0, 0]})
        assert sample_tracker.to_be_deleted is True
