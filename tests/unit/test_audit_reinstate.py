"""Audit 2026-10-11: a removed tracker whose operator fixed it and pressed "Check again now"."""

from __future__ import annotations

from collections import deque
from time import time

import pytest

from newtrackon import ingest
from newtrackon import tracker as T

DAY = 86400


@pytest.fixture
def reinstated(sample_tracker):
    t, now = sample_tracker, int(time())
    T.PEER_HIST[t.url] = [0] * 6  # its last 6 peer tests before removal: all failed
    T.PEER_HIST_FAM[t.url] = {"v4": [0] * 6}
    T.PEER_FAILS[t.url] = 6
    T.REMOVED[t.host] = {"url": t.url, "t": now - 3 * DAY, "reason": "handed out no peers for 5 days (Up/Bad)", "count": 1, "added": now - 30 * DAY}
    T._peer_reset(t.url)  # what accepting the reinstatement does
    ingest._keep_upbad_clock(t.host, t.url)
    t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 48, maxlen=1440)
    return t


def test_fixed_tracker_is_not_removed_on_its_first_passing_check(reinstated) -> None:
    T._peer_hist_add(reinstated.url, True, "v4")  # its first check back: the peer test passes
    reinstated.update_uptime()
    assert reinstated.to_be_deleted is False


def test_still_broken_goes_again_soon(reinstated) -> None:
    for _ in range(3):
        T._peer_hist_add(reinstated.url, False, "v4")
    reinstated.update_uptime()
    assert reinstated.to_be_deleted is True


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
def test_through_the_real_acceptance_path(sample_tracker) -> None:
    from unittest.mock import patch
    t, now = sample_tracker, int(time())
    T.PEER_HIST[t.url], T.PEER_HIST_FAM[t.url], T.PEER_FAILS[t.url] = [0] * 6, {"v4": [0] * 6}, 6
    T.REMOVED[t.host] = {"url": t.url, "t": now - 3 * DAY, "reason": "handed out no peers for 5 days (Up/Bad)", "count": 1,
                         "added": now - 30 * DAY, "hist": "1" * 48}
    ingest.REINSTATE[t.host] = now
    with patch("newtrackon.ingest.db.get_all_data", return_value=[]), \
            patch("newtrackon.ingest.attempt_submitted", return_value=(1800, t.url, 50)), \
            patch("newtrackon.ingest.db.insert_new_tracker"), patch.object(t, "update_ipapi_data"):
        ingest.process_new_tracker(t)
    assert t.url not in T.PEER_HIST and t.url not in T.PEER_FAILS and T.LAST_STATE[t.url]["st"] == "up_bad"
    T._peer_hist_add(t.url, True, "v4")  # first check back: fixed
    t.update_uptime()
    assert t.to_be_deleted is False


def test_inconclusive_for_7_days_counts_on_the_families_that_count() -> None:
    url, now = "udp://quiet.example:1/announce", time()
    T.PEER_HIST_FAM[url] = {"v4": [1] * 6, "v6": [1] * 6}
    T.PEER_HIST[url] = [1] * 6
    T.PEER_LAST[url] = int(now - 8 * DAY)
    T._PEER_ANY[0] = now  # the test works for other trackers
    for _ in range(3):
        T._peer_conclusive(url, None, now)
    assert T.PEER_FAILS.get(url, 0) >= T.PEER_FAIL_LIMIT


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
def test_fixed_flaky_tracker_survives_its_first_checks(sample_tracker) -> None:
    from unittest.mock import patch
    t, now = sample_tracker, int(time())
    hist = ("1" + "0" * 7) * 42  # its last week before removal: up 12% of checks, dropping out every 4 hours
    T.REMOVED[t.host] = {"url": t.url, "t": now - 3 * DAY, "reason": "too unreliable: 12% once dropouts are counted (under 15%)",
                         "count": 1, "added": now - 30 * DAY, "hist": hist}
    ingest.REINSTATE[t.host] = now
    with patch("newtrackon.ingest.db.get_all_data", return_value=[]), \
            patch("newtrackon.ingest.attempt_submitted", return_value=(1800, t.url, 50)), \
            patch("newtrackon.ingest.db.insert_new_tracker"), patch.object(t, "update_ipapi_data"):
        ingest.process_new_tracker(t)
    assert t.to_be_deleted is False
    for _ in range(4):  # fixed: answering every check from now on
        t._prev_checked, t.last_checked = t.last_checked, t.last_checked + 1800
        t.is_up()
        t.update_uptime()
        assert t.to_be_deleted is False


def test_still_flaky_after_3_days_goes(sample_tracker) -> None:
    t, now = sample_tracker, int(time())
    T.REINSTATED[t.url] = now - 3 * DAY - 60  # reinstated over 3 days ago and still dropping out
    t.added, t.last_uptime, t.status = now - 30 * DAY, now, 1
    t.historic = deque([1, 0, 0, 0, 0, 0, 0, 0] * 60, maxlen=1440)
    t.update_uptime()
    assert t.to_be_deleted is True


def test_no_exit_for_its_family_is_our_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    from newtrackon import scraper
    monkeypatch.setattr(scraper, "_probe_src", lambda f: None if f == socket.AF_INET6 else "10.9.0.2")
    scraper.rtt.probe = ("http", b"x" * 20, "http://v6only.example:6969/announce", None, b"-qB5000-000000000000")
    assert scraper.peer_probe(only_family=socket.AF_INET6) is None
    assert scraper.rtt.probe_extra.get("exit_blocked") is True  # so the 7-day inconclusive rule never fails it


def test_fixed_flaky_tracker_is_clear_of_15_percent_after_3_days(sample_tracker) -> None:
    t, now = sample_tracker, int(time())
    hist = ("1" + "0" * 7) * 42  # the restored week: up 12%, dropping out every 4 hours
    T.REINSTATED[t.url] = now - 3 * DAY - 60  # past its 3 days
    t.added, t.last_uptime, t.status = now - 30 * DAY, now, 1
    t.historic = deque([{"1": 1, "h": 0.5}.get(c, 0) for c in hist] + [1] * (3 * 48 + 1), maxlen=1440)  # then 3 days all up
    t.update_uptime()
    assert t.to_be_deleted is False


class TestOneThirtyDayRule:
    """2026-10-12: Up/Junk or any kind of Up/Broken for 30 days in a row is removed, saying which it was at the end."""

    def _run(self, t, kind):
        from unittest.mock import patch
        now = int(time())
        bad = {"split": ["runs separate servers that don't share swarms"], "fam": ["its IPv6 side doesn't share peers"],
               "dead": ["its published IPv6 address is dead"]}[kind]
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": bad, "dead": [], "since": now - 31 * DAY, "junk_since": now - 31 * DAY}
        if kind == "dead":
            T.FAM_FAILS[t.url] = {"n": 10, "fam": "v6"}
        t.added, t.last_uptime, t.status, t.historic = now - 60 * DAY, now, 1, deque([1] * 336, maxlen=1440)
        with patch.object(T, "_split", return_value={"split": 3, "of": 6, "passed": 3, "tests": 6} if kind == "split" else None), \
                patch.object(T, "_peer_fam_bad", return_value="v6" if kind == "fam" else None), \
                patch.object(T, "_nt_lat_penalty", return_value=0.0):
            t.update_uptime()
        return t.to_be_deleted, T._NT_DEL_REASON.get(t.url)

    @pytest.mark.parametrize(("kind", "now"), [("dead", "its IPv6 address is dead"), ("split", "its servers don't share swarms"),
                                               ("fam", "its IPv6 side doesn't share peers")])
    def test_every_kind_alike(self, sample_tracker, kind, now) -> None:
        assert self._run(sample_tracker, kind) == (True, "Up/Junk or Up/Broken for 31 days in a row; now " + now)

    def test_fix_link(self) -> None:
        for why, fix in (("Up/Junk or Up/Broken for 31 days in a row; now its servers don't share swarms", "split-swarm"),
                         ("Up/Junk or Up/Broken for 31 days in a row; now its IPv6 address is dead", "dead-address"),
                         ("Up/Junk or Up/Broken for 31 days in a row; now its score is under 50", "unreliable")):
            assert T._removal_detail({"url": "udp://x.example:1/announce", "reason": why})[1] == fix


def test_comeback_after_a_ban_for_good_counts(sample_tracker) -> None:
    now = int(time())
    with open("data/denylist.txt", "w") as f:
        f.write("old.example\n")  # banned for good
    T.REMOVED["old.example"] = {"url": "udp://old.example:1/announce", "t": now - 60 * DAY, "count": 3, "ips": list(sample_tracker.ips), "network": "Example ISP"}
    sample_tracker.added = now - 10 * DAY  # listed after that ban
    sample_tracker._nt_ban()
    assert open("data/denylist.txt").read().splitlines()[-1] == sample_tracker.host  # 3 earlier removals on its server: for good


def test_sister_banned_for_good_after_this_was_listed_doesnt_count(sample_tracker) -> None:
    now = int(time())
    with open("data/denylist.txt", "w") as f:
        f.write("old.example\n")
    T.REMOVED["old.example"] = {"url": "udp://old.example:1/announce", "t": now - 5 * DAY, "count": 3, "ips": list(sample_tracker.ips), "network": "Example ISP"}
    sample_tracker.added = now - 10 * DAY  # listed alongside it, before it was banned
    sample_tracker._nt_ban()
    assert open("data/denylist.txt").read().splitlines()[-1].split()[0] == sample_tracker.host
    assert len(open("data/denylist.txt").read().splitlines()[-1].split()) == 2  # its own first removal: 30 days
