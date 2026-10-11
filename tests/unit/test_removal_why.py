"""A removed tracker's page says exactly what the checks saw and which part of How to fix applies, so its operator can fix it."""

from __future__ import annotations

from collections import deque
from time import time

import pytest
from flask.testing import FlaskClient

from newtrackon import ingest, ntextra
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

DAY = 86400
NOPEERS = ["hands out no peers (3+ of its last 6 peer tests failed)"]


def _seen(url: str, fam: str, **seen) -> None:
    T._peer_seen_set(url, fam, {"a": None, "a_ip": None, "self": False, "foreign": 0, "seeds": None, "leech": None, **seen}, False)


def _remove_upbad(t, monkeypatch: pytest.MonkeyPatch) -> str:
    now = int(time())
    T.LAST_STATE[t.url] = {"st": "up_bad", "bad": NOPEERS, "dead": [], "since": now - int(5.1 * DAY)}
    monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
    monkeypatch.setitem(T.PEER_OK, t.url, not T.PEER_FAIL_LIMIT)  # its latest test, as a check records it
    t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 48, maxlen=1440)
    t.update_uptime()
    assert t.to_be_deleted is True
    return T._NT_DEL_REASON[t.url]


class TestWhatTheAddressSays:
    @pytest.mark.parametrize(("ip", "what", "words"), [
        ("127.0.0.1", "a loopback address", "the tracker's own machine"),
        ("::1", "a loopback address", "reverse proxy"),
        ("::ffff:127.0.0.1", "a loopback address", "port forward"),
        ("172.17.0.1", "a private address", "Docker"),
        ("172.20.0.1", "a private address", "Docker"),
        ("10.0.0.5", "a private address", "NAT"),
        ("192.168.0.195", "a private address", "NAT"),
        ("104.21.83.32", "a proxy or CDN's address", "Cloudflare"),
        ("255.255.255.255", "an invalid address", "placeholder"),
        ("0.0.0.0", "an invalid address", "IPv6 clients"),
        ("100.64.0.1", "a private address", "NAT")])
    def test_cause(self, ip: str, what: str, words: str) -> None:
        w, cause = T._addr_cause(ip)
        assert w == what and words in cause
        assert ("Docker" in cause) == ip.startswith("172.")  # 127.0.0.1 and 10.x aren't Docker

    def test_seen_sentence_names_the_cause(self) -> None:
        s = T._seen_sentence({"a": "private", "a_ip": "127.0.0.1", "foreign": 1})
        assert s.startswith("our second test client was handed our first one as 127.0.0.1, a loopback address nobody can connect to, plus 1 peer")
        assert s.endswith("without the client's real address") and "Docker" not in s


class TestRemovalReason:
    def test_unusable_address(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        _seen(sample_tracker.url, "v6", a="private", a_ip="127.0.0.1", seeds=1, leech=1)
        why = _remove_upbad(sample_tracker, monkeypatch)
        assert why == "handed out unusable peers for 5 days (Up/Bad): over IPv6 it gave out every client's address as 127.0.0.1"

    def test_not_tracking(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        T._peer_seen_set(sample_tracker.url, "v4", {"swarm_empty": True}, False)
        assert _remove_upbad(sample_tracker, monkeypatch).endswith("(Up/Bad): it wasn't tracking the torrents announced to it (it counted nobody)")

    def test_split(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        _seen(sample_tracker.url, "v4", self=True, seeds=0, leech=1)
        assert _remove_upbad(sample_tracker, monkeypatch).endswith("(Up/Bad): its servers don't share swarms, so clients rarely met")

    def test_counted_but_not_handed_out(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        _seen(sample_tracker.url, "v4", seeds=1, leech=1)
        assert _remove_upbad(sample_tracker, monkeypatch).endswith("(Up/Bad): it counted our test client but never handed it to a second one")

    def test_no_evidence_no_guess(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _remove_upbad(sample_tracker, monkeypatch) == "handed out no peers for 5 days (Up/Bad)"

    def test_banned_comeback_still_carries_its_clock(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        _seen(sample_tracker.url, "v6", a="private", a_ip="127.0.0.1")
        why = _remove_upbad(sample_tracker, monkeypatch)
        T.REMOVED[sample_tracker.host] = {"url": sample_tracker.url, "t": int(time()), "reason": why}
        ingest._keep_upbad_clock(sample_tracker.host, sample_tracker.url)
        assert T.LAST_STATE[sample_tracker.url]["bad_since"] <= time() - T.REMOVE_DAYS * 86400  # ingest reads "(Up/Bad)" in the reason: still there

    def test_down_says_down(self, sample_tracker) -> None:
        t = sample_tracker
        t.status, t.historic = 0, deque([0] * 200 + [1] * 10 + [0] * 30, maxlen=1440)
        T._nt_down_why_set(t.url, "UDP connection failed")
        why = T._useless_reason(t, 5.0)
        assert why == "not working for 5 days: no answer in 96% of checks, and never back for 12 hours in a row (connection refused)"

    def test_up_bad_then_down_says_both(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        t = sample_tracker  # Up/Bad for 4 days, then down for 1, removed while down
        t.status, t.historic = 0, deque([1] * 192 + [0] * 48, maxlen=1440)
        monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
        monkeypatch.setitem(T.PEER_OK, t.url, not T.PEER_FAIL_LIMIT)  # its latest test, as a check records it
        _seen(t.url, "v6", a="private", a_ip="172.17.0.1")
        T._nt_down_why_set(t.url, "UDP timeout")
        why = T._useless_reason(t, 5.0)
        assert why == ("not working for 5 days: no answer in 20% of checks, the rest answering without usable peers "
                       "(over IPv6 it gave out every client's address as 172.17.0.1); down now (timed out)")
        detail, fix = T._removal_detail({"url": t.url, "reason": why})
        assert fix == "hidden-address" and "Last error: UDP timeout" in detail and "as 172.17.0.1" in detail[0]

    def test_up_bad_says_peers(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        t = sample_tracker
        t.status, t.historic = 1, deque([0] * 120 + [1] * 120, maxlen=1440)
        monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
        monkeypatch.setitem(T.PEER_OK, t.url, not T.PEER_FAIL_LIMIT)  # its latest test, as a check records it
        _seen(t.url, "v4")
        assert T._useless_reason(t, 5.0) == ("not working for 5 days: no answer in 50% of checks, the rest answering without "
                                             "usable peers (a second client was never told about the first)")


class TestRemovedPage:
    def test_kept_with_the_record(self, sample_tracker) -> None:
        url = sample_tracker.url
        _seen(url, "v6", a="private", a_ip="127.0.0.1", seeds=1, leech=1)
        T.PEER_HIST_FAM[url] = {"v6": [0] * 6}
        T._removed_add(sample_tracker, "handed out unusable peers for 5 days (Up/Bad): over IPv6 it gave out every client's address as 127.0.0.1")
        rec = T.REMOVED[sample_tracker.host]
        assert rec["fix"] == "hidden-address"
        assert rec["detail"][0].startswith("Last IPv6 peer test: our second test client was handed our first one as 127.0.0.1, a loopback address")
        assert rec["detail"][1] == "Passed 0 of its last 6 IPv6 peer tests."
        T.PEER_SEEN.clear()  # nt-admin clear-peers, or a later check: the page still says what it saw
        assert ntextra._removed_detail(rec) == (rec["detail"], "hidden-address")

    @pytest.mark.usefixtures("region_db")
    def test_page_and_api(self, flask_client: FlaskClient, sample_tracker) -> None:
        _seen(sample_tracker.url, "v6", a="private", a_ip="172.17.0.1")
        T._removed_add(sample_tracker, "handed out unusable peers for 5 days (Up/Bad): over IPv6 it gave out every client's address as 172.17.0.1")
        html = flask_client.get("/tracker/%s" % sample_tracker.host).get_data(as_text=True)
        assert "What our checks saw" in html and "as 172.17.0.1, a private address nobody can connect to" in html and "Docker" in html
        assert 'href="/fix#hidden-address">Hands out an address nobody can reach</a>' in html
        d = flask_client.get("/api/tracker/%s" % sample_tracker.host).get_json()["removed"]
        assert d["fix"] == "/fix#hidden-address" and "172.17.0.1" in d["details"][0]

    def test_older_record_ignores_later_checks(self) -> None:
        url, t0 = "udp://old.example:1/announce", int(time()) - 3 * DAY
        rec = {"url": url, "t": t0, "reason": "handed out no peers for 5 days (Up/Bad)"}
        T.PEER_SEEN[url] = {"v4": {"a": "private", "a_ip": "10.0.0.1", "ok": False, "t": t0 + 3600}}  # a check after its removal
        assert ntextra._removed_detail(rec) == ([], "no-peers")
        T.PEER_SEEN[url]["v4"]["t"] = t0 - 3600
        detail, fix = ntextra._removed_detail(rec)
        assert fix == "hidden-address" and "as 10.0.0.1" in detail[0]

    def test_older_mixed_record_that_was_down(self) -> None:
        url, t0 = "udp://down.example:1/announce", int(time()) - DAY
        rec = {"url": url, "t": t0, "reason": "not working for 5 days: down, or answering without handing out peers", "hist": "0" * 300 + "1" * 36}
        T.PEER_SEEN[url] = {"v4": {"a": "ok", "ok": True, "t": t0 - 600}}  # its peer tests passed: it was down
        T.DOWN_WHY[url] = "UDP connection failed"
        detail, fix = ntextra._removed_detail(rec)
        assert fix == "down-refused" and detail == ["Last error: UDP connection failed",
                                                    "Over its last 7 days listed it answered in 11% of checks and dropped out 0 times."]

    def test_our_own_fault_shows_nothing_to_fix(self) -> None:
        rec = {"url": "udp://x.example:1/announce", "t": 1, "reason": "removed in error: our checks misjudged it (being reinstated)", "fault": True}
        assert ntextra._removed_detail(rec) == ([], None)


@pytest.mark.usefixtures("region_db")
class TestListedPage:
    def test_up_bad_with_a_hidden_address_links_its_fix(self, sample_tracker) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        T._nat_seen_set(sample_tracker.url, "v6", "127.0.0.1")
        t = SimpleNamespace(url=sample_tracker.url)
        with patch.object(ntextra, "_fix_base", return_value="no-peers"):
            assert ntextra._fix_anchor(t) == "hidden-address"
        T._nat_seen_set(sample_tracker.url, "v6", None)
        with patch.object(ntextra, "_fix_base", return_value="no-peers"):
            assert ntextra._fix_anchor(t) == "no-peers"
