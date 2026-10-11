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
