"""2026-10-12: a tracker that refuses this server (not only one that times out) but answers our test clients abroad is
Down from NZ and stays; the check abroad works for IPv6-only trackers; a reinstatement can be accepted from our test
clients abroad; and every announce sends numwant and a key, as real clients do."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from newtrackon import scraper
from newtrackon import tracker as T

from .test_sim import _REAL_LOCAL_FAULT


@pytest.mark.parametrize(("err", "abroad"), [
    ("UDP connection failed", True), ("HTTP connection failed", True), ("UDP timeout", True), ("HTTP timeout", True),
    ("UDP error: [Errno 113] No route to host", True),  # a firewall REJECT (icmp-host-prohibited)
    ("Tracker error message: info hash is not authorized with this tracker", False),  # its own message: recorded as it is
    ("Error while announcing: unregistered torrent", False),
    ("HTTP 403 status code returned", False)])
def test_refused_or_ignored_here_but_answering_abroad_is_half_down(monkeypatch: pytest.MonkeyPatch, err: str, abroad: bool) -> None:
    monkeypatch.setattr(T, "_nt_vps_retry_ok", lambda u: False)
    monkeypatch.setattr(T, "_nt_reachable_elsewhere", lambda u: True)
    monkeypatch.setattr(T, "_nt_signal", lambda kind, host: None)
    t = SimpleNamespace(url="udp://x.example:6969/announce", host="x.example")
    assert _REAL_LOCAL_FAULT(t, RuntimeError(err)) is False  # recorded either way, never skipped
    assert getattr(t, "_nt_partial", False) is abroad


def test_no_route_is_a_refusal() -> None:
    assert T._nt_down_label("UDP error: [Errno 113] No route to host") == "Refused"


def _only(families):
    real = socket.getaddrinfo

    def fake(host, port, family=0, *a, **kw):
        if family in (socket.AF_INET, socket.AF_INET6) and family not in families:
            raise socket.gaierror("no address in this family")
        return real("127.0.0.1" if family != socket.AF_INET6 else "::1", port, family, *a, **kw)
    return fake


@pytest.mark.parametrize(("families", "fam", "src"), [((socket.AF_INET6,), socket.AF_INET6, "fd00::2"),
                                                      ((socket.AF_INET, socket.AF_INET6), socket.AF_INET, "10.0.0.2")])
def test_tried_abroad_on_the_family_it_has(monkeypatch: pytest.MonkeyPatch, families, fam, src) -> None:
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", _only(families))
    monkeypatch.setattr(scraper, "_exits", lambda: {"a": {"src4": "10.0.0.2", "src6": "fd00::2"}})
    monkeypatch.setattr(scraper, "_rtt_via", lambda url, f, s: calls.append((f, s)) or 30.0)
    assert T._nt_reachable_elsewhere("udp://v6only.example:6969/announce") is True
    assert calls == [(fam, src)]
    calls.clear()
    assert T._nt_vps_retry_ok("udp://v6only.example:6969/announce") is True
    assert calls == [(fam, None)]


def test_one_exit_failing_doesnt_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
    monkeypatch.setattr(scraper, "_exits", lambda: {"a": {"src4": "10.0.0.2"}, "b": {"src4": "10.0.0.3"}})

    def rtt(url, f, s):
        if s == "10.0.0.2":
            raise OSError("this exit is down")
        return 40.0
    monkeypatch.setattr(scraper, "_rtt_via", rtt)
    assert T._nt_reachable_elsewhere("udp://x.example:6969/announce") is True


class TestAnnounceViaExits:
    EXITS = {"airvpn": {"src4": "10.0.0.2", "src6": "fd00::2", "region": "Europe", "location": "Frankfurt"},
             "airvpn2": {"src4": "10.0.0.3", "src6": "fd00::3", "region": "North America", "location": "Miami"}}

    def test_http_first_exit_that_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)
        seen = []

        def ann(url, src, thash, pid, left, port, event):
            seen.append((src, event))
            if src == "10.0.0.2":
                raise RuntimeError("probe: HTTP error")
            return {"interval": 1800, "complete": 0, "incomplete": 0, "peers": []}
        monkeypatch.setattr(scraper, "_http_ann", ann)
        resp, ms, where = scraper.announce_via_exits("http://x.example:6969/announce")
        assert resp["interval"] == 1800 and where == "North America: Miami" and ms >= 0
        assert seen == [("10.0.0.2", "started"), ("10.0.0.3", "started"), ("10.0.0.3", "stopped")]

    def test_its_own_error_message_isnt_an_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)

        def ann(*a):
            raise RuntimeError("probe: failure reason")  # _http_ann raises on a failure reason
        monkeypatch.setattr(scraper, "_http_ann", ann)
        assert scraper.announce_via_exits("http://x.example:6969/announce") is None

    def test_a_made_up_crowd_isnt_an_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)
        monkeypatch.setattr(scraper, "_http_ann", lambda *a: {"interval": 1800, "complete": 500, "incomplete": 900, "peers": []})
        assert scraper.announce_via_exits("http://x.example:6969/announce") is None

    def test_udp_ipv6_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET6,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)
        monkeypatch.setattr(scraper, "ordered_addrs", lambda h, p, f, t: [(f, t, 0, "", ("2001:db8::1", p, 0, 0))])
        used = []

        class Sock:
            def close(self):
                pass

        def session(fam, sa, src):
            used.append((fam, src))
            return Sock(), lambda pid, left, port, event, th: {"interval": 900, "leechers": 0, "seeds": 1, "peers": []}
        monkeypatch.setattr(scraper, "_udp_session", session)
        resp, _, where = scraper.announce_via_exits("udp://x.example:6969/announce")
        assert resp["interval"] == 900 and where == "Europe: Frankfurt" and used == [(socket.AF_INET6, "fd00::2")]


@pytest.mark.usefixtures("empty_queues")
class TestAcceptedFromAbroad:
    def test_logged_on_the_submitted_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from newtrackon import ingest
        monkeypatch.setattr(scraper, "get_bep_34", lambda h: (False, None))
        monkeypatch.setattr(scraper, "announce_via_exits", lambda u: (
            {"interval": 1800, "complete": 1, "incomplete": 0, "peers": [{"IP": "198.51.100.7", "port": 25000}]}, 120, "Europe: Frankfurt"))
        url = "udp://x.example:6969/announce"
        assert ingest._attempt_abroad(url) == (1800, url, 120)
        row = ingest.submitted_data[0]
        assert row["status"] == 1 and row["abroad"] is True and row["url"] == url
        assert "198.51.100.7" not in row["info"][0]  # the peers it handed out (our own test client abroad) aren't shown
        assert "our test client in Europe: Frankfurt" in row["info"][1] and "Down from NZ" in row["info"][1]
        ingest._mark_confirm_row(url)
        assert row["info"][1].startswith("Answered our test client abroad, not this site's server.")

    def test_not_past_its_bep34_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from newtrackon import ingest
        called = []
        monkeypatch.setattr(scraper, "get_bep_34", lambda h: (True, None))  # its DNS says: don't connect
        monkeypatch.setattr(scraper, "announce_via_exits", lambda u: called.append(u))
        assert ingest._attempt_abroad("udp://x.example:6969/announce") is None and called == []


class TestRealClientFields:
    def test_numwant_and_key(self) -> None:
        a = scraper.client_args(b"-qB5050-abcdefghijkl")
        assert a["numwant"] == 200 and len(a["key"]) == 8 and int(a["key"], 16) >= 0 and a["key"] == a["key"].upper()
        assert scraper.client_args(b"-qB5050-abcdefghijkl", "stopped") == {"numwant": 0, "key": a["key"]}  # same client
        assert scraper.client_args(b"-TR4060-abcdefghijkl")["numwant"] == 80
        assert scraper.client_args(b"-qB5050-zzzzzzzzzzzz")["key"] != a["key"]

    def test_main_announce_sends_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        got = []

        def get(url):
            got.append(url)
            return SimpleNamespace(status_code=200), b"d8:intervali1800e5:peers0:e"
        monkeypatch.setattr(scraper, "memory_limited_get", get)
        scraper._announce_http_once("http://x.example:6969/announce")
        q = parse_qs(urlparse(got[0]).query)
        assert q["numwant"] and q["key"] and q["peer_id"]

    def test_peer_test_clients_send_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import requests
        urls = []

        def get(self, url, **kw):
            urls.append(url)
            return SimpleNamespace(status_code=200, content=b"d8:intervali1800e5:peers0:e")
        monkeypatch.setattr(requests.Session, "get", get)
        monkeypatch.setattr(scraper, "_probe_src", lambda f: "10.0.0.2" if f == socket.AF_INET else None)
        monkeypatch.setattr(scraper, "_probe_srcs", lambda f, key=None: ["10.0.0.2"])
        monkeypatch.setattr(scraper, "_pref_set", lambda k, s: None)
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        scraper.rtt.probe = ("http", b"x" * 20, "http://x.example:6969/announce", None, b"-qB5050-000000000000")
        scraper.peer_probe()
        assert len(urls) >= 2  # client A from here, client B from the exit
        for u in urls:
            q = parse_qs(urlparse(u).query)
            assert q.get("numwant") and q.get("key"), u
        stops = [parse_qs(urlparse(u).query) for u in urls if "event=stopped" in u]
        assert all(q["numwant"] == ["0"] for q in stops)
