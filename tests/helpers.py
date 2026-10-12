"""Shared test data types."""

import sqlite3
from typing import TypedDict, override

from newtrackon.persistence import HistoryData


class TrackerDataDict(TypedDict):
    host: str
    url: str
    ips: list[str] | None
    latency: int | None
    last_checked: int
    interval: int
    status: int
    uptime: float
    countries: list[str] | None
    country_codes: list[str] | None
    networks: list[str] | None
    added: int
    historic: list[int]
    last_downtime: int
    last_uptime: int


class ReusableConnection(sqlite3.Connection):
    """Keep a fixture's database open across application calls."""

    @override
    def close(self) -> None:
        pass


def make_history(timestamp: int) -> HistoryData:
    return {"url": "udp://tracker.example.com:6969/announce", "time": timestamp, "status": 1, "ip": "1.2.3.4", "info": []}


def set_peer_fails(mp, url: str, n: int, fams: tuple[str, ...] = ("v4",)) -> None:
    """A peer-test history with n failures in the last 6 tests on each family, the newest last (the failure count is
    worked out from it: tracker.PEER_FAILS)."""
    from newtrackon import tracker as T
    h = [1] * (T.PEER_WINDOW - n) + [0] * n
    mp.setitem(T.PEER_HIST_FAM, url, {f: list(h) for f in fams})
    mp.setitem(T.PEER_HIST, url, list(h))


def set_fake_fails(mp, url: str, n: int) -> None:
    """A fake-peer history with fake peers in n of the last 6 tests, the newest last (tracker.FAKE_FAILS)."""
    from newtrackon import tracker as T
    mp.setitem(T.FAKE_HIST, url, [0] * (T.PEER_WINDOW - n) + [1] * n)
