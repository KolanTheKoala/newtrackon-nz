"""Banned trackers are tested again once a day with the site's own checks, so their Removed page says what's wrong now
(also for removals from before their evidence was kept). The retest never touches the ban, the record or any check data."""

from __future__ import annotations

import copy
import socket
from time import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from newtrackon import scraper
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

NOW = int(time())
HOST, URL = "gone.example", "udp://gone.example:6969/announce"
V4 = [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("93.184.216.34", 6969))]


def _deny(*lines: str) -> None:
    with open("data/denylist.txt", "w", encoding="utf-8") as f:
        f.write("".join(ln + "\n" for ln in lines))


@pytest.fixture
def banned() -> None:
    T.REMOVED[HOST] = {"url": URL, "t": NOW - 2 * 86400, "reason": "handed out no peers for 5 days (Up/Bad)", "count": 1}
    _deny("%s %d" % (HOST, NOW - 2 * 86400))


def _addrs(v4=V4, v6=None):
    def gai(host, port, fam=0, *a, **k):
        r = v4 if fam == socket.AF_INET else v6
        if not r:
            raise socket.gaierror("no address")
        return r
    return gai


def _probe(*results):
    """peer_probe_family replies in turn: (ok, family_extra)."""
    it = iter(results)

    def f(url, fam):
        ok, ex = next(it)
        scraper.rtt.family_extra = ex
        return ok
    return f


OK = (True, {"fam": "v4", "seen": {"a": "ok", "self": False, "foreign": 0}})
LOOP = (False, {"fam": "v4", "seen": {"a": "private", "a_ip": "127.0.0.1", "self": False, "foreign": 0}})
SELF = (False, {"fam": "v4", "seen": {"a": None, "self": True, "foreign": 0, "seeds": 0, "leech": 1}})
DEAD = (None, None)


def _retest(*probes, announce_err=None, online=True, elsewhere=False, v4=V4, dns_elsewhere=False, reply=None):
    def ann(url):
        if announce_err:
            raise RuntimeError(announce_err)
        return (reply or {}), "93.184.216.34"
    with patch.object(socket, "getaddrinfo", _addrs(v4)), patch.object(scraper, "peer_probe_family", _probe(*probes)), \
            patch.object(scraper, "announce_udp", ann), patch.object(T, "_monitor_online", return_value=online), \
            patch.object(T, "_nt_reachable_elsewhere", return_value=elsewhere), \
            patch.object(T, "_nt_public_dns_has", return_value=dns_elsewhere):
        return T.removed_retest(HOST, now=NOW)


@pytest.mark.usefixtures("banned")
class TestWhatItSays:
    def test_works_now(self) -> None:
        r = _retest(OK, OK)
        assert r["works"] is True and r["fix"] is None and r["t"] == NOW
        assert r["detail"][0].startswith("IPv4, peer test passed 2 of 2: our second test client was handed our first one, with its real address")
        assert r["detail"][-1] == "It works now: press Check again now on this page to get it listed again."

    def test_unusable_address(self) -> None:
        r = _retest(LOOP, LOOP)
        assert r["works"] is False and r["fix"] == "hidden-address"
        assert "IPv4, peer test failed 2 of 2: our second test client was handed our first one as 127.0.0.1, a loopback address" in r["detail"][0]

    def test_mixed_split(self) -> None:
        r = _retest(OK, SELF)
        assert r["fix"] == "split-swarm" and r["detail"][0].startswith("IPv4, peer test passed only 1 of 2: our second test client was handed back only itself")

    def test_no_answer(self) -> None:
        r = _retest(DEAD, DEAD, announce_err="UDP timeout")
        assert r == {"detail": ["It didn't answer: UDP timeout"], "fix": "down-timeout", "works": False, "t": NOW}

    def test_answers_only_abroad(self) -> None:
        r = _retest(DEAD, DEAD, announce_err="UDP timeout", elsewhere=True)
        assert r["detail"][0].startswith("It doesn't answer this site's server, but it does answer our test clients in other countries")

    def test_our_outage_says_nothing(self) -> None:
        assert _retest(DEAD, DEAD, announce_err="UDP timeout", online=False) is None

    def test_our_dns_says_nothing(self) -> None:
        assert _retest(v4=None, dns_elsewhere=True) is None
        assert _retest(v4=None)["fix"] == "down-dns"

    def test_whitelist(self) -> None:
        r = _retest(DEAD, DEAD, announce_err="tracker error message: info hash is not authorized with this tracker")
        assert r["fix"] is None and r["detail"][1].startswith("That means it only serves its own torrents (a whitelist).")

    def test_its_own_message(self) -> None:
        none = (False, {"fam": "v4", "seen": {"a": None, "self": False, "foreign": 0, "seeds": 0, "leech": 0}})
        r = _retest(none, none, reply={"warning message": "info hash is not authorized with this tracker"})
        assert r["detail"][0] == "Its reply says: \u201cinfo hash is not authorized with this tracker\u201d"
        assert r["detail"][-1].startswith("That means it only serves its own torrents (a whitelist).") and r["fix"] is None

    def test_one_pass_doesnt_prove_reliable(self) -> None:
        T.REMOVED[HOST]["reason"] = "too unreliable: 15% once dropouts are counted (under 15%)"
        r = _retest(OK, OK)
        assert "one check can't show that's fixed" in r["detail"][-2] and r["works"] is True

    def test_fake_peers_dont_work(self) -> None:
        r = _retest(OK, (True, {"fam": "v4", "seen": {"a": "ok", "foreign": 2}}))
        assert r["works"] is False and r["fix"] == "fake-peers"


@pytest.mark.usefixtures("banned")
class TestTouchesNothingElse:
    def test_only_its_own_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        names = ("PEER_HIST", "PEER_HIST_FAM", "PEER_SEEN", "NAT_SEEN", "SPLIT_HIST", "FAKE_HIST", "DOWN_WHY", "CLOSED",
                 "PEER_LAST", "_PEER_ANY", "_FAILSTREAK", "LAST_STATE", "PEER_FAILS", "FAKE_FAILS", "FAM_FAILS")
        before = {n: copy.deepcopy(getattr(T, n)) for n in names}
        rec = copy.deepcopy(T.REMOVED[HOST])
        deny = open("data/denylist.txt").read()
        scraper.rtt.probe, scraper.rtt.profile = ("udp", b"x", 2, None, b"p"), ("-qB5000-", "qBittorrent/5.0.0")
        monkeypatch.setattr(T, "_RETEST_NEXT", [0])
        with patch.object(socket, "getaddrinfo", _addrs()), patch.object(scraper, "peer_probe_family", _probe(LOOP, LOOP)):
            T.removed_retest_due([], NOW)
        assert {n: getattr(T, n) for n in names} == before
        after = T.REMOVED[HOST]
        assert after.pop("retest")["fix"] == "hidden-address" and after == rec and open("data/denylist.txt").read() == deny
        assert scraper.rtt.probe == ("udp", b"x", 2, None, b"p") and scraper.rtt.profile == ("-qB5000-", "qBittorrent/5.0.0")
        assert T._jload("data/removed.json")[HOST]["retest"]["fix"] == "hidden-address"


class TestSchedule:
    def _rec(self, host: str, **kw) -> None:
        T.REMOVED[host] = {"url": "udp://%s:1/announce" % host, "t": NOW - 86400, "reason": "x", **kw}

    def test_who_is_due(self) -> None:
        self._rec("a.example", detail=["seen"])
        self._rec("b.example")  # no evidence from its removal: first
        self._rec("c.example", retest={"t": NOW - 3600})  # tested an hour ago
        self._rec("d.example", fault=True)  # our own fault
        self._rec("e.example")  # listed again
        self._rec("f.example")  # banned for good
        self._rec("g.example")  # ban over
        _deny(*("%s.example %d" % (h, NOW - 86400) for h in "abcde"), "f.example", "g.example %d" % (NOW - 40 * 86400))
        assert T._retest_candidates({"e.example"}, NOW) == ["b.example", "a.example"]

    def test_not_after_a_start_and_one_at_a_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._rec("a.example")
        self._rec("b.example")
        _deny("a.example %d" % (NOW - 86400), "b.example %d" % (NOW - 86400))
        calls = []
        monkeypatch.setattr(T, "removed_retest", lambda h, now=None: calls.append(h) or {"detail": [], "fix": None, "works": False, "t": NOW})
        monkeypatch.setattr(T, "_RETEST_NEXT", [NOW + 600])
        T.removed_retest_due([], NOW)
        assert calls == []  # just started
        T.removed_retest_due([], NOW + 600)
        T.removed_retest_due([], NOW + 700)
        assert len(calls) == 1  # one per RETEST_GAP
        T.removed_retest_due([], NOW + 600 + T.RETEST_GAP)
        assert sorted(calls) == ["a.example", "b.example"]


@pytest.mark.usefixtures("region_db", "banned")
def test_page_and_api(flask_client: FlaskClient) -> None:
    T.REMOVED[HOST]["retest"] = {"t": NOW - 3600, "detail": ["IPv4, peer test failed 2 of 2: as 172.17.0.1"], "fix": "hidden-address", "works": False}
    html = flask_client.get("/tracker/" + HOST).get_data(as_text=True)
    assert "Tested again" in html and "after its removal" in html and "failed 2 of 2" in html
    assert 'How to fix what this test found: <a href="/fix#hidden-address">' in html
    d = flask_client.get("/api/tracker/" + HOST).get_json()["removed"]["retest"]
    assert d == {"time": NOW - 3600, "details": ["IPv4, peer test failed 2 of 2: as 172.17.0.1"], "fix": "/fix#hidden-address", "works": False}
    T.REMOVED[HOST]["fault"] = True
    assert "Tested again" not in flask_client.get("/tracker/" + HOST).get_data(as_text=True)
