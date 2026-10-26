"""Which rule removed a tracker is kept as data in its record (REMOVED[host]["kind"]), not read back from the words of
its reason: a reinstated tracker removed for Up/Bad carries its Up/Bad clock over (ingest._keep_upbad_clock)."""

from __future__ import annotations

from time import time

import pytest

from newtrackon import ingest
from newtrackon import tracker as T


def test_the_kind_goes_into_the_record(sample_tracker) -> None:
    t = sample_tracker
    T._nt_removal_why(t.url, "handed out no peers for 5 days (Up/Bad)", "upbad")
    t.to_be_deleted = True
    t._emit_events()
    assert T.REMOVED[t.host]["kind"] == "upbad" and T.REMOVED[t.host]["reason"] == "handed out no peers for 5 days (Up/Bad)"
    assert t.url not in T._NT_DEL_KIND and T._jload("data/removed.json")[t.host]["kind"] == "upbad"


@pytest.mark.parametrize("rec, carried", [
    ({"reason": "handed out no peers 85% of the last 7 days", "kind": "upbad"}, True),  # the kind decides, not the words
    ({"reason": "not working for 5 days: answering without usable peers", "kind": "not_working"}, False),
    ({"reason": "handed out no peers for 5 days (Up/Bad)"}, True),  # a record from before the kind was kept
    ({"reason": "no answer for 6 days"}, False),
])
def test_the_upbad_clock_carries_over_by_kind(rec, carried) -> None:
    url, host = "udp://back.example:1/announce", "back.example"
    T.REMOVED[host] = dict(rec, url=url, t=int(time()) - 3 * 86400, count=1)
    ingest._keep_upbad_clock(host, url)
    assert ("bad_since" in (T.LAST_STATE.get(url) or {})) is carried
