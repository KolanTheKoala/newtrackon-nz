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


@pytest.mark.parametrize(("err", "kind"), [
    ("UDP connection failed", "path"), ("HTTP connection failed", "path"), ("UDP timeout", "path"), ("HTTP timeout", "path"),
    ("UDP error: [Errno 113] No route to host", "path"),  # a firewall REJECT (icmp-host-prohibited)
    ("HTTP 403 status code returned", "blocked"),  # a CDN in front blocks this server
    ("Tracker error message: your IP address is banned", "blocked"),  # it bans this server
    ("Failed bdecoding HTTP response: html", "blocked"),  # a block page
    ("Can't resolve IP", None)])  # DNS has its own check
def test_failing_here_but_answering_abroad_is_half_down(monkeypatch: pytest.MonkeyPatch, err: str, kind) -> None:
    """Whatever failed here, if our test clients abroad get a genuine answer it's Down from NZ. Only a timeout or a refusal
    is a 'path' signal (nt-watch: 5 in 30 min means our own network): a reply saying no is about this server."""
    signals = []
    monkeypatch.setattr(T, "_nt_vps_retry_ok", lambda u: False)
    monkeypatch.setattr(T, "_nt_reachable_elsewhere", lambda u: True)
    monkeypatch.setattr(T, "_nt_public_dns_has", lambda h: False)
    monkeypatch.setattr(T, "_nt_signal", lambda k, host: signals.append(k))
    t = SimpleNamespace(url="udp://x.example:6969/announce", host="x.example")
    assert _REAL_LOCAL_FAULT(t, RuntimeError(err)) is False  # recorded either way, never skipped
    assert getattr(t, "_nt_partial", False) is (kind is not None) and signals == ([kind] if kind else [])


def test_not_when_the_check_is_removing_it_anyway(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = []
    monkeypatch.setattr(T, "_nt_vps_retry_ok", lambda u: asked.append(u) or False)
    monkeypatch.setattr(T, "_nt_reachable_elsewhere", lambda u: asked.append(u) or True)
    t = SimpleNamespace(url="udp://x.example:6969/announce", host="x.example", to_be_deleted=True)
    assert _REAL_LOCAL_FAULT(t, RuntimeError("its own DNS records (BEP 34) say it doesn't accept connections")) is False
    assert asked == [] and not getattr(t, "_nt_partial", False)


def test_no_route_is_a_refusal() -> None:
    assert T._nt_down_label("UDP error: [Errno 113] No route to host") == "Refused"


def _only(families):
    real = socket.getaddrinfo

    def fake(host, port, family=0, *a, **kw):
        if family in (socket.AF_INET, socket.AF_INET6) and family not in families:
            raise socket.gaierror("no address in this family")
        return real("127.0.0.1" if family != socket.AF_INET6 else "::1", port, family, *a, **kw)
    return fake


EXITS2 = {"a": {"src4": "10.0.0.2", "src6": "fd00::2", "region": "Europe", "location": "Frankfurt"}}


@pytest.mark.parametrize(("families", "tried"), [((socket.AF_INET6,), {(socket.AF_INET6, "fd00::2")}),
                                                 ((socket.AF_INET, socket.AF_INET6), {(socket.AF_INET, "10.0.0.2"), (socket.AF_INET6, "fd00::2")})])
def test_tried_abroad_on_every_family_it_has(monkeypatch: pytest.MonkeyPatch, families, tried) -> None:
    """IPv6-only, or IPv4 dead everywhere while IPv6 answers abroad: both found."""
    calls = set()

    def genuine(url, fam, src):
        calls.add((fam, src))
        if fam == socket.AF_INET:
            raise OSError("its IPv4 address is dead")
        return {"interval": 1800, "peers": []}, 50
    monkeypatch.setattr(socket, "getaddrinfo", _only(families))
    monkeypatch.setattr(scraper, "_exits", lambda: EXITS2)
    monkeypatch.setattr(scraper, "_genuine", genuine)
    assert T._nt_reachable_elsewhere("udp://x.example:6969/announce") is True
    assert calls == tried
    calls.clear()
    assert T._nt_vps_retry_ok("udp://x.example:6969/announce") is True  # the retry from here, on each family too
    assert calls == {(f, None) for f, _ in tried}


def test_one_exit_failing_doesnt_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
    monkeypatch.setattr(scraper, "_exits", lambda: {"a": {"src4": "10.0.0.2"}, "b": {"src4": "10.0.0.3"}})

    def genuine(url, fam, src):
        if src == "10.0.0.2":
            raise OSError("this exit is down")
        return {"interval": 1800, "peers": []}, 40
    monkeypatch.setattr(scraper, "_genuine", genuine)
    assert T._nt_reachable_elsewhere("udp://x.example:6969/announce") is True


def test_the_retry_from_here_is_pinned_to_each_family(monkeypatch: pytest.MonkeyPatch) -> None:
    srcs = set()

    def ann(url, src, thash, pid, left, port, event):
        srcs.add(src)
        return {"interval": 1800, "complete": 0, "incomplete": 0, "peers": []}
    monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET, socket.AF_INET6)))
    monkeypatch.setattr(scraper, "_http_ann", ann)
    assert scraper.answers_here("http://x.example:6969/announce") is True
    assert srcs <= {"0.0.0.0", "::"} and srcs  # this server's own address, on that family only


class TestAnnounceViaExits:
    EXITS = {"airvpn": {"src4": "10.0.0.2", "src6": "fd00::2", "region": "Europe", "location": "Frankfurt"},
             "airvpn2": {"src4": "10.0.0.3", "src6": "fd00::3", "region": "North America", "location": "Miami"}}

    def test_http_the_exit_that_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)
        seen = []

        def ann(url, src, thash, pid, left, port, event):
            seen.append((src, event))
            if src == "10.0.0.2":
                raise RuntimeError("probe: HTTP error")  # a 403 from a CDN, say
            return {"interval": 1800, "complete": 0, "incomplete": 0, "peers": []}
        monkeypatch.setattr(scraper, "_http_ann", ann)
        resp, ms, where = scraper.announce_via_exits("http://x.example:6969/announce")
        assert resp["interval"] == 1800 and where == "North America: Miami" and ms >= 0
        time.sleep(0.2)  # the client that answered leaves again
        assert {("10.0.0.2", "started"), ("10.0.0.3", "started"), ("10.0.0.3", "stopped")} <= set(seen)

    def test_its_own_error_message_isnt_an_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)

        def ann(*a):
            raise RuntimeError("probe: failure reason")  # _http_ann raises on a failure reason
        monkeypatch.setattr(scraper, "_http_ann", ann)
        assert scraper.announce_via_exits("http://x.example:6969/announce") is None

    def test_bencoded_but_not_a_tracker_answer_isnt_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)
        monkeypatch.setattr(scraper, "_http_ann", lambda *a: {"interval": 1800, "complete": 0})  # no peer list at all
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
        assert resp["interval"] == 900 and where in ("Europe: Frankfurt", "North America: Miami")
        assert used and {f for f, _ in used} == {socket.AF_INET6}  # IPv6 only: it has no IPv4 address

    def test_a_whitelist_isnt_reachable_elsewhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _only((socket.AF_INET,)))
        monkeypatch.setattr(scraper, "_exits", lambda: self.EXITS)
        monkeypatch.setattr(scraper, "_udp_session", lambda fam, sa, src: (SimpleNamespace(close=lambda: None), self._refuse))
        monkeypatch.setattr(scraper, "ordered_addrs", lambda h, p, f, t: [(f, t, 0, "", ("192.0.2.1", p))])
        assert T._nt_reachable_elsewhere("udp://x.example:6969/announce") is False

    @staticmethod
    def _refuse(pid, left, port, event, th):
        raise RuntimeError("Error while announcing: info hash is not authorized")  # what an error reply (action 3) raises


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
