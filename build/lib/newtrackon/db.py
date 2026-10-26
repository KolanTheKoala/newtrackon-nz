import json
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from os import path
from typing import TypedDict, cast

from newtrackon import tracker as _tracker
from newtrackon.regions import NO_FILTER, RegionFilter
from newtrackon.tracker import Tracker
from newtrackon.utils import TrackerEndpointInput, dict_factory, format_list, remove_ipvx_only_trackers


class TrackerRow(TypedDict):
    """Columns written by this module; collection fields contain JSON."""

    host: str
    url: str
    ip: str
    latency: int | None
    last_checked: int
    interval: int
    status: int
    uptime: float
    country: str
    country_code: str
    network: str
    added: int
    historic: str
    last_downtime: int
    last_uptime: int
    recent_ip: str | None


db_file = "data/trackon.db"


def ensure_db_existence() -> None:
    if not path.exists(db_file):
        create_db()


def create_db() -> None:
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    _ = c.execute(
        """CREATE TABLE `status` (
        `host`	TEXT NOT NULL,
        `url`	TEXT NOT NULL,
        `ip`	TEXT,
        `latency`	INTEGER,
        `last_checked`	INTEGER,
        `interval`	INTEGER,
        `status`	INTEGER,
        `uptime`	INTEGER,
        `country`	TEXT,
        `country_code`	TEXT,
        `network`	TEXT,
        `added`		INTEGER,
        `historic`	TEXT,
        `last_downtime` INTEGER,
        `last_uptime`	INTEGER,
        `recent_ip`	TEXT,
        PRIMARY KEY(`host`)
        );"""
    )
    conn.commit()
    conn.close()


def update_tracker(tracker: Tracker) -> None:
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute(
        """UPDATE status SET url=?, ip=?, latency=?, last_checked=?, status=?, interval=?, uptime=?,
           historic=?, country=?, country_code=?, network=?, last_downtime=?, last_uptime=?, recent_ip=? WHERE host=?""",
        (
            tracker.url,
            json.dumps(tracker.ips),
            tracker.latency,
            tracker.last_checked,
            tracker.status,
            tracker.interval,
            tracker.uptime,
            json.dumps(list(tracker.historic) if tracker.historic else []),
            json.dumps(tracker.countries),
            json.dumps(tracker.country_codes),
            json.dumps(tracker.networks),
            tracker.last_downtime,
            tracker.last_uptime,
            json.dumps(tracker.recent_ips),
            tracker.host,
        ),
    ).fetchone()
    conn.commit()
    conn.close()


def delete_tracker(tracker: Tracker) -> None:
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute(
        "DELETE FROM status WHERE host=?",
        (tracker.host,),
    ).fetchone()
    conn.commit()
    conn.close()


def get_all_data() -> list[Tracker]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = dict_factory
    c = conn.cursor()
    trackers_from_db: list[Tracker] = []
    for row in cast(Iterable[TrackerRow], c.execute("SELECT * FROM STATUS ORDER BY uptime DESC")):
        tracker_in_db = Tracker(
            host=row["host"],
            url=row["url"],
            ips=cast(list[str] | None, json.loads(row["ip"])),
            latency=row["latency"],
            last_checked=row["last_checked"],
            interval=row["interval"],
            status=row["status"],
            uptime=row["uptime"],
            countries=cast(list[str] | None, json.loads(row["country"])),
            country_codes=cast(list[str] | None, json.loads(row["country_code"])),
            historic=deque(cast(list[int], json.loads(row["historic"])), maxlen=_tracker.HISTORIC_SLOTS),
            added=row["added"],
            networks=cast(list[str] | None, json.loads(row["network"])),
            last_downtime=row["last_downtime"],
            last_uptime=row["last_uptime"],
            recent_ips=cast(dict[str, int], json.loads(row["recent_ip"] or "{}")),
        )
        trackers_from_db.append(tracker_in_db)
    conn.close()
    return trackers_from_db


def get_api_data(
    query: str,
    uptime: int = 0,
    include_ipv4_only: bool = True,
    include_ipv6_only: bool = True,
    added_before: int | None = None,
    *,
    region_filter: RegionFilter = NO_FILTER,
    url_filter: Callable[[str], bool] | None = None,
) -> str:
    conn = sqlite3.connect(db_file)
    conn.row_factory = None  # plain tuples (a reused connection may have a dict factory set)
    c = conn.cursor()
    sql = ""
    params: tuple[int, ...] = ()

    if query == "/api/http":
        sql = 'SELECT URL, IP, COUNTRY_CODE FROM STATUS WHERE URL LIKE "http%" AND STATUS = 1 AND ROUND(UPTIME) >= 90'
    elif query == "/api/udp":
        sql = 'SELECT URL, IP, COUNTRY_CODE FROM STATUS WHERE URL LIKE "udp://%" AND STATUS = 1 AND ROUND(UPTIME) >= 90'
    elif query == "/api/live":
        sql = "SELECT URL, IP, COUNTRY_CODE FROM STATUS WHERE STATUS = 1"
    elif query == "percentage":
        sql = "SELECT URL, IP, COUNTRY_CODE FROM STATUS WHERE ROUND(UPTIME) >= ?"
        params = (uptime,)
        if uptime > 0:  # live-only for score lists; /api/all (0) stays complete
            sql += " AND STATUS = 1"

    if added_before is not None:
        sql += " AND ADDED <= ?"
        params += (added_before,)

    sql += " ORDER BY (STATUS = 1) DESC, ROUND(UPTIME) DESC, COALESCE(LATENCY, 99999) ASC"
    _ = c.execute(sql, params)

    raw_rows = cast(list[tuple[str, str, str | None]], c.fetchall())
    conn.close()

    if url_filter is not None:
        raw_rows = [r for r in raw_rows if url_filter(r[0])]
    if region_filter.active:
        raw_rows = [
            r for r in raw_rows if region_filter.matches(r[0], json.loads(r[2]) if r[2] else None, _tracker.REGION_LAT)
        ]
    urls_and_ips: Sequence[TrackerEndpointInput] = [(url, cast(list[str] | None, json.loads(ips))) for url, ips, _ in raw_rows]

    if not include_ipv4_only:
        urls_and_ips = remove_ipvx_only_trackers(urls_and_ips, version=4)

    if not include_ipv6_only:
        urls_and_ips = remove_ipvx_only_trackers(urls_and_ips, version=6)

    return format_list(urls_and_ips)


def insert_new_tracker(tracker: Tracker) -> None:
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    _ = c.execute(
        "INSERT INTO status VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tracker.host,
            tracker.url,
            json.dumps(tracker.ips),
            tracker.latency,
            tracker.last_checked,
            tracker.interval,
            tracker.status,
            tracker.uptime,
            json.dumps(tracker.countries),
            json.dumps(tracker.country_codes),
            json.dumps(tracker.networks),
            tracker.added,
            json.dumps(list(tracker.historic) if tracker.historic else []),
            tracker.last_downtime,
            tracker.last_uptime,
            json.dumps(tracker.recent_ips),
        ),
    )
    conn.commit()
    conn.close()
