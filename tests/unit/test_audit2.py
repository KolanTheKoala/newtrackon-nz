"""Audit part 2 (2026-10-12): protocol switches, the private/whitelist rule, restarts, and the feed after a reinstatement."""

from __future__ import annotations

from collections import deque
from time import time

import pytest

from newtrackon import ingest
from newtrackon import tracker as T

DAY = 86400
HTTP, UDP = "http://tracker.example.com:6969/announce", "udp://tracker.example.com:6969/announce"


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
def test_udp_upgrade_of_an_up_bad_http_tracker_is_judged_on_udp(sample_tracker) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch
    now = int(time())
    old = SimpleNamespace(url=HTTP, host="tracker.example.com", historic=deque([1] * 336), added=now - 30 * DAY,
                          last_downtime=0, last_uptime=now, recent_ips={})
    T.LAST_STATE[HTTP] = {"st": "up_bad", "bad": ["hands out no peers (3+ of its last 6 peer tests failed)"], "dead": [],
                          "since": now - int(4.9 * DAY), "bad_since": now - int(4.9 * DAY), "pb_since": now - int(4.9 * DAY),
                          "useless_since": now - int(4.9 * DAY)}
    T.PEER_HIST[HTTP], T.PEER_HIST_FAM[HTTP] = [0] * 6, {"v4": [0] * 6}
    t = sample_tracker
    t.url = UDP
    with patch("newtrackon.ingest.db.get_all_data", return_value=[old]), \
            patch("newtrackon.ingest.attempt_submitted", return_value=(1800, UDP, 50)), \
            patch("newtrackon.ingest.db.insert_new_tracker"), patch("newtrackon.ingest.db.delete_tracker"), \
            patch.object(t, "update_ipapi_data"):
        ingest.process_new_tracker(t)
    t.historic, t.status, t.last_uptime = deque([1] * 336, maxlen=1440), 1, now
    T.LAST_STATE[UDP]["since"] = T.LAST_STATE[UDP]["bad_since"] = now - int(5.1 * DAY)  # hours later, past 5 days
    T._peer_hist_add(UDP, True, "v4")  # UDP shares peers fine
    t.update_uptime()
    assert t.to_be_deleted is False


@pytest.mark.parametrize(("msg", "why"), [
    ("info hash is not authorized with this tracker", "whitelist"), ("Unregistered torrent", "whitelist"),
    ("Torrent not registered with this tracker", "whitelist"), ("torrent not in whitelist", "whitelist"),
    ("Requested download is not authorized for use with this tracker", "whitelist"),
    ("passkey not found", "passkey"), ("Invalid passkey", "passkey"),
    ("You are not authorized to announce more often than every 30 minutes", None), ("Rate limited: not authorized for 60 s", None),
    ("IP not authorized", None), ("Your client is not on the whitelist", None), ("client not whitelisted", None),
    ("not authorised: slow down", None), ("Tracker is in maintenance", None)])
def test_private_or_whitelist_only_from_what_it_says_about_the_torrent(msg, why) -> None:
    r = T._nt_closed_reason(msg)
    assert (r is None) if why is None else (why in r)


@pytest.mark.parametrize(("msg", "words"), [
    ("You are not authorized to announce more often than every 30 minutes", "limiting how often"),
    ("Your client is not on the whitelist", "certain torrent clients"),
    ("info hash is not authorized with this tracker", "whitelist"), ("Invalid passkey", "private tracker")])
def test_tracker_page_reads_its_message_the_same_way(msg, words) -> None:
    from newtrackon import ntextra
    T.WARNINGS["udp://w.example:1/announce"] = {"msg": msg, "t": 1}
    assert words in ntextra._warning("udp://w.example:1/announce")[1]


def test_a_restart_doesnt_bring_back_spells_from_before_a_removal() -> None:
    now = int(time())
    url = "udp://back.example:1/announce"
    T.EVENTS[:] = [{"t": now - 6 * DAY, "url": url, "host": "back.example", "type": "bad", "text": "is Up/Bad: hands out no peers"},
                   {"t": now - 2 * DAY, "url": url, "host": "back.example", "type": "removed", "text": "removed from the list (x)"},
                   {"t": now - 1 * DAY, "url": url, "host": "back.example", "type": "added", "text": "is back on the list at its operator's request"},
                   {"t": now - 3600, "url": url, "host": "back.example", "type": "good", "text": "is Up/Good again"}]
    T.LAST_STATE[url] = {"st": "up_good", "bad": [], "dead": [], "since": now - 3600}  # its state now: no Up/Bad spells
    import importlib.util
    src = open(importlib.util.find_spec("newtrackon.tracker").origin).read()
    assert "\n_bad_seed()" not in src and "\n_bad_log_seed()" not in src  # not run at start any more
    assert T._nt_bad_share(url, now) == 0.0 and "bad_since" not in T.LAST_STATE[url]


class TestOurOwnFaultCountsForNothing:
    """A removal marked as our fault (nt-admin reinstate) is no removal: not for its own next ban, nor its sisters'."""

    def _rec(self, host, **kw):
        T.REMOVED[host] = {"url": "udp://%s:1/announce" % host, "t": int(time()) - 40 * DAY, "count": 1,
                           "reason": "removed in error: our checks misjudged it (being reinstated)", "ips": ["93.184.216.34"],
                           "network": "Example ISP", "fault": True, **kw}

    def test_its_own_next_removal_is_a_first(self, sample_tracker) -> None:
        self._rec(sample_tracker.host)
        sample_tracker._nt_ban()
        T._removed_add(sample_tracker, "no answer for 5 days")
        assert len(open("data/denylist.txt").read().split()) == 2 and T.REMOVED[sample_tracker.host]["count"] == 1

    def test_a_sisters_doesnt_count(self, sample_tracker) -> None:
        self._rec("sister.example")
        sample_tracker.added = int(time()) - 10 * DAY  # listed after the sister's removal
        sample_tracker._nt_ban()
        assert len(open("data/denylist.txt").read().split()) == 2  # 30 days, not 90

    def test_no_same_server_refusal(self) -> None:
        self._rec("sister.example", t=int(time()) - DAY)
        with open("data/denylist.txt", "w") as f:
            f.write("sister.example %d\n" % (int(time()) - DAY))
        assert T._nt_ban_ips(["93.184.216.34"]) is None

    def test_not_on_the_ban_list(self) -> None:
        from newtrackon import ntextra
        self._rec("sister.example", t=int(time()) - DAY)
        with open("data/denylist.txt", "w") as f:
            f.write("sister.example %d\n" % (int(time()) - DAY))
        assert ntextra._bans() == []


def test_http_tracker_refusing_our_quick_second_announce_is_our_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its main announce has just worked; the peer test's first client then announces again at once, which a tracker
    enforcing its minimum interval refuses. That's our test, not a fault real clients meet: never 'inconclusive'."""
    import socket
    import requests
    from newtrackon import scraper

    class R:
        status_code, content = 200, b"d14:failure reason26:announce interval too shorte"

    monkeypatch.setattr(scraper, "_probe_src", lambda f: "10.9.0.2")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))])
    monkeypatch.setattr(requests.Session, "get", lambda self, *a, **k: R())
    scraper.rtt.probe = ("http", b"x" * 20, "http://strict.example:80/announce", None, b"-qB5000-000000000000")
    assert scraper.peer_probe() is None
    assert scraper.rtt.probe_extra.get("exit_blocked") is True


@pytest.mark.parametrize(("ev", "nat", "fix"), [
    ({"type": "bad", "text": "is now Up/Broken: runs separate servers that don't share swarms"}, False, "split-swarm"),
    ({"type": "bad", "text": "is Up/Bad: hands out no peers (3+ of its last 6 peer tests failed)"}, True, "hidden-address"),
    ({"type": "bad", "text": "is Up/Bad: hands out no peers (3+ of its last 6 peer tests failed)"}, False, "no-peers"),
    ({"type": "removed", "text": "removed from the list (no answer for 5 days)"}, False, "removed")])
def test_telegram_fix_links(ev, nat, fix) -> None:
    url = "udp://f.example:1/announce"
    if nat:
        T.NAT_SEEN[url] = {"v6": {"ip": "172.17.0.1", "t": 1}}
    assert T._nt_fix_for_event(dict(ev, url=url)) == fix


def test_bep34_move_keeps_its_records(sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
    from newtrackon import scraper
    old, new = sample_tracker.url, "udp://tracker.example.com:7000/announce"
    T.LAST_STATE[old] = {"st": "up_bad", "bad": ["hands out no peers (3+ of its last 6 peer tests failed)"], "dead": [], "since": 5, "bad_since": 5}
    T.DAILY[old] = [["2026-10-01", 100.0, 48, {}]]
    T.PEER_HIST_FAM[old] = {"v4": [0] * 6}
    monkeypatch.setattr(scraper, "get_bep_34", lambda host: (True, [("udp", 7000)]))
    monkeypatch.setattr(sample_tracker, "update_ips", lambda: (_ for _ in ()).throw(RuntimeError("stop here")))
    monkeypatch.setattr(T, "_monitor_online", lambda: True)
    monkeypatch.setattr(T, "_nt_local_fault", lambda t, e: True)  # end the check right after the move
    sample_tracker.last_uptime = int(time())
    sample_tracker.update_status()
    assert sample_tracker.url == new and old not in T.LAST_STATE and T.LAST_STATE[new]["bad_since"] == 5
    assert T.DAILY[new] == [["2026-10-01", 100.0, 48, {}]] and new not in T.PEER_FAILS  # history kept, peer tests afresh


def test_bep34_refusal_says_so(sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
    from newtrackon import scraper
    monkeypatch.setattr(scraper, "get_bep_34", lambda host: (True, []))
    with pytest.raises(RuntimeError):
        sample_tracker.update_scheme_from_bep_34()
    assert T._NT_DEL_REASON[sample_tracker.url] == "its own DNS records (BEP 34) say it doesn't accept connections"


def test_our_resolver_giving_a_private_address_isnt_counted(sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
    from newtrackon import scraper
    monkeypatch.setattr(scraper, "get_bep_34", lambda host: (False, None))
    monkeypatch.setattr(T.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    monkeypatch.setattr(T, "_nt_public_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(T, "_monitor_online", lambda: True)
    sample_tracker.last_uptime = int(time())
    before = list(sample_tracker.historic)
    for i in range(6):
        sample_tracker.update_status()
        assert list(sample_tracker.historic) == before and sample_tracker.status == 1, i  # not recorded as down
    sample_tracker.update_status()  # the 7th in a row is recorded: a lasting fault shows in the end
    assert sample_tracker.status == 0


def test_closed_on_submit_reads_a_message_with_an_apostrophe() -> None:
    from newtrackon import persistence
    persistence.submitted_data.appendleft({"url": "http://w.example:80/announce", "time": 0, "ip": "", "status": 1,
                                           "info": [str({"interval": 1800, "warning message": "Torrent isn't registered, torrent not registered with this tracker"})]})
    assert ingest._closed_on_submit("http://w.example:80/announce") == "saying it only serves its own torrents (a whitelist)"


def test_a_bug_in_our_bookkeeping_never_marks_the_tracker_down(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from .test_sim import Sim
    s = Sim(monkeypatch, lambda d: {})
    monkeypatch.setattr(T, "_fam_set", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("a bug of ours")))
    s.run(0.1)
    assert s.log and all(st != "down" for _, st, _ in s.log) and s.tr.status == 1
    assert "a bug of ours" in caplog.text


def test_the_check_loop_survives_one_trackers_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from newtrackon import trackon

    class Stop(Exception):
        pass
    done = []
    a = SimpleNamespace(url="udp://a.example:1/announce", interval=1800, last_checked=0, to_be_deleted=False,
                        update_status=lambda: (_ for _ in ()).throw(TypeError("boom")))
    b = SimpleNamespace(url="udp://b.example:1/announce", interval=1800, last_checked=0, to_be_deleted=False,
                        update_status=lambda: done.append("b"))
    monkeypatch.setattr(trackon.db, "get_all_data", lambda: [a, b])
    monkeypatch.setattr(trackon.db, "update_tracker", lambda t: None)
    monkeypatch.setattr(trackon, "save_deque_to_disk", lambda *x: None)
    monkeypatch.setattr(trackon._T, "daily_update", lambda *x: None)
    monkeypatch.setattr(trackon._T, "removed_retest_due", lambda *x: None)
    monkeypatch.setattr(trackon, "sleep", lambda n: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        trackon.update_outdated_trackers()
    assert done == ["b"]


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
def test_sister_names_banned_together_can_each_be_reinstated(monkeypatch: pytest.MonkeyPatch) -> None:
    """tracker.dler.org and tracker2.dler.org were removed together and share a server: pressing "Check again now" on
    either was refused as "same server as the banned" other one, so neither could ever come back that way."""
    from unittest.mock import patch
    from newtrackon.tracker import Tracker
    now = int(time())
    for h in ("one.example", "two.example"):
        T.REMOVED[h] = {"url": "udp://%s:6969/announce" % h, "t": now - DAY, "count": 1, "reason": "x",
                        "ips": ["93.184.216.34"], "network": "Example ISP"}
    with open("data/denylist.txt", "w") as f:
        f.write("one.example %d\ntwo.example %d\n" % (now - DAY, now - DAY))
    monkeypatch.setattr(Tracker, "update_ips", lambda tr: setattr(tr, "ips", ["93.184.216.34"]))
    ingest.REINSTATE["one.example"] = now
    with patch("newtrackon.ingest.db.get_all_data", return_value=[]):
        ingest.add_one_tracker_to_submitted_queue("udp://one.example:6969/announce")
    assert [t.url for t in ingest.submitted_queue.queue] == ["udp://one.example:6969/announce"]
    with patch("newtrackon.ingest.db.get_all_data", return_value=[]):
        ingest.add_one_tracker_to_submitted_queue("udp://new-name.example:6969/announce")  # a new name: still refused
    assert len(ingest.submitted_queue.queue) == 1


def test_removal_needs_its_latest_peer_test_to_fail(sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
    t, now = sample_tracker, int(time())
    T.LAST_STATE[t.url] = {"st": "up_bad", "bad": ["hands out no peers (3+ of its last 6 peer tests failed)"], "dead": [],
                           "since": now - int(5.1 * DAY), "bad_since": now - int(5.1 * DAY), "pb_since": now - int(5.1 * DAY),
                           "useless_since": now - int(5.1 * DAY)}
    T.PEER_HIST_FAM[t.url] = {"v4": [0, 0, 0, 0, 0, 1]}  # 5 failures, but fixed: its latest test passed
    T.PEER_OK[t.url] = True
    t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 336, maxlen=1440)
    t.update_uptime()
    assert t.to_be_deleted is False
    T.PEER_OK[t.url] = False  # a lucky pass, then failing again: removed
    t.update_uptime()
    assert t.to_be_deleted is True


def test_no_fake_peers_removal_once_it_stopped_returning_them(sample_tracker) -> None:
    t, now = sample_tracker, int(time())
    T.LAST_STATE[t.url] = {"st": "up_bad", "bad": ["returns fake peers (3+ of its last 6 checks)"], "dead": [],
                           "since": now - int(5.1 * DAY), "bad_since": now - int(5.1 * DAY), "pb_since": now - int(5.1 * DAY),
                           "useless_since": now - int(5.1 * DAY)}
    T.FAKE_HIST[t.url] = [1, 1, 1, 1, 0, 0]  # fakes earlier, none in its latest two checks
    T.FAKE_N[t.url], T.PEER_OK[t.url] = 0, False  # its latest test failed, but not with fake peers
    t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 336, maxlen=1440)
    t.update_uptime()
    assert t.to_be_deleted is False


def test_inconclusive_for_7_days_still_leads_to_removal(sample_tracker) -> None:
    t, now = sample_tracker, int(time())
    T.LAST_STATE[t.url] = {"st": "up_bad", "bad": ["hands out no peers (3+ of its last 6 peer tests failed)"], "dead": [],
                           "since": now - int(5.1 * DAY), "bad_since": now - int(5.1 * DAY), "pb_since": now - int(5.1 * DAY),
                           "useless_since": now - int(5.1 * DAY)}
    T.PEER_HIST_FAM[t.url] = {"v4": [0] * 6}  # the 7-day rule's failures
    T.PEER_OK[t.url] = None  # its tests still come back with no result
    T.PEER_LAST[t.url] = now - 13 * DAY
    T._PEER_ANY[0] = now  # the test works for other trackers
    t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 336, maxlen=1440)
    t.update_uptime()
    assert t.to_be_deleted is True



class TestExitsOnlyPeerTest:
    """scraper.peer_probe_exits: both test clients on VPN exits; B must be handed A at A's exit's public address."""

    EXITS = [("10.8.0.2", "203.0.113.10"), ("10.8.1.2", "198.51.100.20")]

    def _run(self, monkeypatch, handed, exits=None, b_fails=False):
        import socket
        from newtrackon import scraper
        monkeypatch.setattr(scraper, "_exit_pairs", lambda fam: (exits if exits is not None else self.EXITS) if fam == socket.AF_INET else [])
        monkeypatch.setattr(scraper, "ordered_addrs", lambda h, p, fam=0, st=None: [(fam, 2, 17, "", ("93.184.216.34", 6969))] if fam == socket.AF_INET else [])

        class S:
            def close(self):
                pass

        def session(fam, sa, src):
            def ann(pid, left, port, event, th):
                if src == "10.8.1.2" and b_fails:
                    raise TimeoutError("UDP timeout")
                if left == 1 and event == 2:  # client B's announce
                    return {"seeds": 1, "leechers": 1, "peers": [dict(p, port=(port - 1 if p.pop("a") else port)) for p in handed()]}
                return {"seeds": 1, "leechers": 0, "peers": []}
            return S(), ann
        monkeypatch.setattr(scraper, "_udp_session", session)
        ok = scraper.peer_probe_exits("udp://abroad.example:6969/announce")
        return ok, scraper.rtt.probe_extra

    def test_pass(self, monkeypatch) -> None:
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "203.0.113.10", "a": True}])
        assert ok is True and ex["fam"] == "v4" and ex["exits_only"] and ex["seen"]["a"] == "ok"

    def test_private_address_fails(self, monkeypatch) -> None:
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "172.17.0.1", "a": True}])
        assert ok is False and ex["nat_ip"] == "172.17.0.1"

    def test_another_public_address_is_no_verdict(self, monkeypatch) -> None:
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "9.9.9.9", "a": True}])  # a proxy, or our exit list is out of date
        assert ok is None and ex["exit_blocked"] is True

    def test_an_exit_that_moved_since_is_looked_up_again(self, monkeypatch) -> None:
        from newtrackon import scraper
        looked = []
        monkeypatch.setattr(scraper, "_exit_public", lambda src, fam, fresh=False: looked.append((src, fresh)) or "208.77.22.27")
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "208.77.22.27", "a": True}])  # the VPS moved the exit to another server
        assert ok is True and ex["seen"]["a"] == "ok" and looked == [("10.8.0.2", True)]

    def test_still_another_address_after_looking_again_is_no_verdict(self, monkeypatch) -> None:
        from newtrackon import scraper
        monkeypatch.setattr(scraper, "_exit_public", lambda src, fam, fresh=False: None)  # the lookup fails
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "9.9.9.9", "a": True}])
        assert ok is None and ex["exit_blocked"] is True

    def test_only_itself_is_a_split(self, monkeypatch) -> None:
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "198.51.100.20", "a": False}])
        assert ok is False and ex["split"] is True

    def test_needs_two_exits(self, monkeypatch) -> None:
        ok, ex = self._run(monkeypatch, lambda: [], exits=self.EXITS[:1])
        assert ok is None and ex["exit_blocked"] is True

    def test_an_exit_with_no_answer_is_our_limit(self, monkeypatch) -> None:
        ok, ex = self._run(monkeypatch, lambda: [{"IP": "203.0.113.10", "a": True}], b_fails=True)
        assert ok is None and ex["exit_blocked"] is True


def test_exit_public_address_is_looked_up_through_the_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import requests
    from newtrackon import scraper
    calls = []

    class R:
        text = "146.70.123.114\n"

    def get(self, url, **k):
        calls.append(url)
        if len(calls) > 1:
            raise requests.ConnectionError("no route")
        return R()
    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(scraper, "_EXIT_PUB", {})
    assert scraper._exit_public("10.8.0.2", socket.AF_INET) == "146.70.123.114"
    assert scraper._exit_public("10.8.0.2", socket.AF_INET) == "146.70.123.114" and len(calls) == 1  # kept
    assert scraper._exit_public("10.8.1.2", socket.AF_INET) is None  # lookup failed: no address (no verdict abroad)
    assert calls[0] == "https://ipv4.icanhazip.com/"


def test_exit_public_address_is_looked_up_again_when_the_exit_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import requests
    from newtrackon import scraper
    answers = iter(["146.70.123.114\n", "208.77.22.27\n", "184.75.208.10\n"])

    class R:
        def __init__(self):
            self.text = next(answers)
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **k: R())
    monkeypatch.setattr(scraper, "_EXIT_PUB", {})
    exits = {"airvpn2": {"src4": "10.8.0.2", "endpoint": "184.75.208.162"}}
    monkeypatch.setattr(scraper, "_exits", lambda: exits)
    assert scraper._exit_public("10.8.0.2", socket.AF_INET) == "146.70.123.114"
    assert scraper._exit_public("10.8.0.2", socket.AF_INET) == "146.70.123.114"  # kept
    exits["airvpn2"]["endpoint"] = "208.77.22.214"  # airvpn-rotate.sh moved it to another server
    assert scraper._exit_public("10.8.0.2", socket.AF_INET) == "208.77.22.27"
    assert scraper._exit_public("10.8.0.2", socket.AF_INET, fresh=True) == "184.75.208.10"  # asked to look again
