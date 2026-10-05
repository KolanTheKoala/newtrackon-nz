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


class TestJunkClock:
    def _state(self, url: str, days: float, **extra) -> None:
        T.LAST_STATE[url] = {"st": "up_junk", "bad": [], "dead": [], "since": int(time() - days * DAY), **extra}

    def test_short_spells_above_junk_do_not_reset(self) -> None:
        s = {"st": "up_junk", "since": 0, **T._bad_track(None, "up_junk", 0)}
        s = {"st": "up_unreliable", "since": 20 * DAY, **T._bad_track(s, "up_unreliable", 20 * DAY)}  # scrapes over 50
        s = {"st": "up_junk", "since": 20 * DAY + 6 * 3600, **T._bad_track(s, "up_junk", 20 * DAY + 6 * 3600)}  # 6 h later
        assert s["junk_since"] == 0

    def test_a_day_or_more_above_resets(self) -> None:
        s = {"st": "up_junk", "since": 0, **T._bad_track(None, "up_junk", 0)}
        s = {"st": "up_good", "since": 20 * DAY, **T._bad_track(s, "up_good", 20 * DAY)}
        s = {"st": "up_good", "since": 20 * DAY, **T._bad_track(s, "up_good", 21 * DAY + 60)}
        assert "junk_since" not in s

    def _check(self, t, days: float) -> None:
        self._state(t.url, days)
        t.added, t.last_uptime, t.status = int(time()) - 60 * DAY, int(time()), 1
        t.historic = deque([1, 1, 1, 0] * 360, maxlen=1440)  # up 3 checks in 4, flipping: Junk (about 19), above the 15% line
        t.update_uptime()

    def test_removed_after_30_days_of_junk(self, sample_tracker) -> None:
        self._check(sample_tracker, 30.2)
        assert sample_tracker.to_be_deleted is True
        assert T._NT_DEL_REASON[sample_tracker.url] == "too unreliable: Up/Junk (score under 50) for 30 days"

    def test_kept_before_30_days(self, sample_tracker) -> None:
        self._check(sample_tracker, 29)
        assert 15 < sample_tracker.uptime < 50 and sample_tracker.to_be_deleted is False

    def test_grey_from_day_28(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        t = SimpleNamespace(url=URL, last_uptime=0)
        with patch.object(ntextra, "_rowcls", return_value="orange"):
            self._state(URL, 27)
            assert ntextra._dying(t) is None
            self._state(URL, 28.5)
            assert ntextra._dying(t) == "Up/Junk or Up/Broken for 28+ days: removed and banned after 30 unless fixed"


class TestUselessClock:
    BAD = ["hands out no peers (3+ of its last 6 peer tests failed)"]

    def test_down_then_up_bad_is_one_stretch(self) -> None:
        s = {"st": "down", "bad": [], "since": 0, **T._bad_track(None, "down", 0, [])}
        s = {"st": "up_bad", "bad": self.BAD, "since": 4 * DAY, **T._bad_track(s, "up_bad", 4 * DAY, self.BAD)}
        assert s["useless_since"] == 0
        s = {"st": "up_good", "bad": [], "since": 5 * DAY, **T._bad_track(s, "up_good", 5 * DAY, [])}
        s = {"st": "up_good", "bad": [], "since": 5 * DAY, **T._bad_track(s, "up_good", 5 * DAY + 13 * 3600, [])}
        assert "useless_since" not in s  # working for 13 h: the stretch is over

    def test_seeded_like_farted(self) -> None:
        from types import SimpleNamespace
        now = 1_800_000_000 - 1_800_000_000 % 1800
        url = "udp://f.example:1/announce"
        h = [1] * 60 + [0] * 211 + [1] * 21  # up, 4.4 days down, back 10.5 h: 2.5 h on probation, then 8 h Up/Bad
        T.LAST_REC[url] = now
        T.LAST_STATE[url] = {"st": "up_bad", "bad": self.BAD, "dead": [], "since": now - 16 * 1800}
        T._useless_seed([SimpleNamespace(url=url, historic=h, last_checked=now)], now)
        assert T.LAST_STATE[url]["useless_since"] == now - (20 + 211) * 1800  # the first down slot (the newest slot is "now")
        assert 4.8 < T._nt_useless_days(url, now) < 4.9

    def test_removed_after_5_days_mixed(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        t = sample_tracker
        now = int(time())
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.BAD, "dead": [], "since": now - int(0.5 * DAY),
                               "bad_since": now - int(0.5 * DAY), "useless_since": now - int(5.1 * DAY)}
        monkeypatch.setitem(T.PEER_FAILS, t.url, T.PEER_FAIL_LIMIT)
        t.added, t.last_uptime, t.status, t.historic = now - 30 * DAY, now, 1, deque([1] * 48, maxlen=1440)
        t.update_uptime()
        assert t.to_be_deleted is True and T._NT_DEL_REASON[t.url].startswith("not working for 5 days")



class TestBrokenOnTheJunkClock:
    DEAD = ["its published IPv6 address is dead"]

    def test_flipping_between_junk_and_broken_is_one_stretch(self) -> None:
        s = {"st": "up_junk", "bad": [], "since": 0, **T._bad_track(None, "up_junk", 0, [])}
        s = {"st": "up_bad", "bad": self.DEAD, "since": 5 * DAY, **T._bad_track(s, "up_bad", 5 * DAY, self.DEAD)}
        s = {"st": "up_junk", "bad": [], "since": 9 * DAY, **T._bad_track(s, "up_junk", 9 * DAY, [])}
        assert s["junk_since"] == 0

    def test_no_peers_is_not_broken(self) -> None:
        assert T._poor("up_bad", self.DEAD) and not T._poor("up_bad", ["hands out no peers (3+ of its last 6 peer tests failed)"])

    def test_broken_for_30_days_is_removed(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        t = sample_tracker
        now = int(time())
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.DEAD, "dead": ["v6"], "since": now - 31 * DAY, "junk_since": now - 31 * DAY}
        monkeypatch.setitem(T.FAM_FAILS, t.url, {"n": 40, "fam": "v6"})
        t.added, t.last_uptime, t.status, t.historic = now - 60 * DAY, now, 1, deque([1] * 1440, maxlen=1440)
        t.update_uptime()
        assert t.to_be_deleted is True and "IPv6 address dead" in T._NT_DEL_REASON[t.url]

    def test_fixed_address_is_kept(self, sample_tracker) -> None:
        t = sample_tracker
        now = int(time())
        T.LAST_STATE[t.url] = {"st": "up_bad", "bad": self.DEAD, "dead": ["v6"], "since": now - 31 * DAY, "junk_since": now - 31 * DAY}
        t.added, t.last_uptime, t.status, t.historic = now - 60 * DAY, now, 1, deque([1] * 1440, maxlen=1440)
        t.update_uptime()  # no dead family any more and a good score: not removed
        assert t.to_be_deleted is False
