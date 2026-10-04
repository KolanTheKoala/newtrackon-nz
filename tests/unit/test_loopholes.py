"""Closing the ways a broken tracker could stay listed: intermittent fake peers, timed recoveries, a peer test
that's never conclusive, repeat removals, and reinstatement with a clean history."""

from __future__ import annotations

from collections import deque
from time import time

import pytest

from newtrackon import ingest, ntextra
from newtrackon import tracker as T

URL = "udp://lh.example:6969/announce"
DAY = 86400


def test_fake_peers_every_other_check_is_caught() -> None:
    for fake in (True, False, True, False, True):
        T._fake_hist_add(URL, fake)
    assert T.FAKE_FAILS[URL] == 3 >= T.PEER_FAIL_LIMIT  # a streak counter would be at 1
    for _ in range(4):
        T._fake_hist_add(URL, False)
    assert T.FAKE_FAILS.get(URL, 0) < T.PEER_FAIL_LIMIT  # clean checks age it out


class TestBadShare:
    BAD = ["hands out no peers (3+ of its last 6 peer tests failed)"]

    def test_good_spells_are_logged_and_share_counted(self) -> None:
        now = int(time())
        s = {"st": "up_bad", "bad": self.BAD, "since": now - 3 * DAY}
        s.update(T._bad_track(None, "up_bad", now - 3 * DAY, self.BAD))
        s = {"st": "up_good", "bad": [], "since": now - DAY, **T._bad_track(s, "up_good", now - DAY, [])}  # 13 h+ good: resets the stretch
        assert s["bad_log"] == [[now - 3 * DAY, now - DAY]]
        T.LAST_STATE[URL] = {"st": "up_bad", "bad": self.BAD, "since": now - 0.4 * DAY, "bad_log": s["bad_log"]}
        assert abs(T._nt_bad_share(URL, now) - 2.4 / 7) < 0.01

    def test_removed_when_bad_most_of_the_week(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        now = int(time())
        t = sample_tracker
        # bad 4.5 days, good 13 h, bad again 1.5 days: never 5 days in a row, but 86% of the week
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.BAD, "since": now - int(1.5 * DAY), "bad_since": now - int(1.5 * DAY),
                               "bad_log": [[now - 7 * DAY + 3600, now - int(2.05 * DAY)]]}
        monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
        t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 48, maxlen=1440)
        assert T._nt_upbad_days(t.url, now) < T.UPBAD_DAYS and T._nt_bad_share(t.url, now) >= T.BAD_SHARE
        t.update_uptime()
        assert t.to_be_deleted is True and "% of the last 7 days (Up/Bad)" in T._NT_DEL_REASON[t.url]

    def test_not_for_a_tracker_listed_under_a_week(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        now = int(time())
        t = sample_tracker
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.BAD, "since": now - int(4 * DAY), "bad_since": now - int(4 * DAY)}
        monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
        t.added, t.last_uptime, t.status, t.historic = now - 4 * DAY, now, 1, deque([1] * 48, maxlen=1440)
        t.update_uptime()
        assert t.to_be_deleted is False  # 4 days bad of 4 listed: the 5-day clock decides


class TestInconclusive:
    def test_counts_as_failing_after_7_days(self) -> None:
        now = time()
        T._PEER_ANY[0] = now  # the test works for other trackers
        T.PEER_LAST[URL] = int(now - 8 * DAY)
        for _ in range(3):
            T._peer_conclusive(URL, None, now)
        assert T.PEER_FAILS.get(URL, 0) == 3

    def test_not_while_the_test_itself_is_broken(self) -> None:
        now = time()
        T._PEER_ANY[0] = now - 2 * 3600  # nothing conclusive anywhere for 2 hours: our probe, not them
        T.PEER_LAST[URL] = int(now - 8 * DAY)
        T._peer_conclusive(URL, None, now)
        assert T.PEER_FAILS.get(URL, 0) == 0

    def test_clock_starts_at_first_inconclusive_and_resets_on_a_result(self) -> None:
        now = time()
        T._peer_conclusive(URL, None, now)
        assert T.PEER_LAST[URL] == int(now)
        T._peer_conclusive(URL, True, now + 2 * 3600)
        assert T.PEER_LAST[URL] == int(now + 2 * 3600)


class TestBanSteps:
    def _ban(self, t, removals: int) -> str:
        if removals:
            T.REMOVED[t.host] = {"url": t.url, "t": 0, "reason": "x", "count": removals}
        open("data/denylist.txt", "w").close()
        t._nt_ban()
        return open("data/denylist.txt").read().split("\n")[0]

    def test_30_then_90_then_for_good(self, sample_tracker) -> None:
        h = sample_tracker.host
        assert self._ban(sample_tracker, 0).split()[::2] == [h]  # "host <epoch>": 30 days
        assert len(self._ban(sample_tracker, 0).split()) == 2
        assert self._ban(sample_tracker, 1).split()[2] == "90"
        assert self._ban(sample_tracker, 2) == h

    def test_entries_and_expiry(self) -> None:
        now = int(time())
        with open("data/denylist.txt", "w") as f:
            f.write(f"# c\na.example {now - 40 * DAY}\nb.example {now - 40 * DAY} 90\nc.example\n")
        assert T._nt_ban_entries() == [("a.example", now - 40 * DAY, 30), ("b.example", now - 40 * DAY, 90), ("c.example", None, None)]
        assert not T._nt_banned("a.example") and T._nt_banned("b.example") and T._nt_banned("c.example")
        assert ingest._denylist_hosts() == {"b.example", "c.example"}

    def test_removal_count_kept(self, sample_tracker) -> None:
        sample_tracker.historic = deque([1, 0.5, 0], maxlen=1440)
        T._removed_add(sample_tracker, "x")
        T._removed_add(sample_tracker, "y")
        r = T.REMOVED[sample_tracker.host]
        assert r["count"] == 2 and r["hist"] == "1h0"

    def test_ban_shown_with_its_length(self, sample_tracker) -> None:
        now = int(time())
        T.REMOVED[sample_tracker.host] = {"url": sample_tracker.url, "t": now, "reason": "x", "count": 2}
        with open("data/denylist.txt", "w") as f:
            f.write(f"{sample_tracker.host} {now} 90\n")
        assert ntextra._ban(sample_tracker.host, now)["until"] == now + 90 * DAY


def test_reinstated_tracker_gets_its_history_back(sample_tracker) -> None:
    T.REMOVED["back.example"] = {"url": "udp://back.example:1/announce", "t": 0, "reason": "x", "added": 1_700_000_000, "hist": "110h0"}
    ingest._restore_history(sample_tracker, "back.example")
    assert list(sample_tracker.historic) == [1, 1, 0, 0.5, 0] and sample_tracker.added == 1_700_000_000
