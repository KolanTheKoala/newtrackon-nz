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
    T.PEER_HIST[HTTP], T.PEER_HIST_FAM[HTTP], T.PEER_FAILS[HTTP] = [0] * 6, {"v4": [0] * 6}, 6
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
