"""Rankings page: most reliable over 14 days, longest unbroken uptime, fastest from each region."""

from __future__ import annotations

import json
import sqlite3
from time import time

import pytest
from flask.testing import FlaskClient

from newtrackon import ntextra

from .test_regions import region_db  # noqa: F401  (fixture)


def _set(db: sqlite3.Connection, host: str, **cols: object) -> None:
    for k, v in cols.items():
        db.execute(f"UPDATE status SET {k} = ? WHERE host = ?", (json.dumps(v) if isinstance(v, list) else v, host))
    db.commit()


@pytest.mark.usefixtures("region_db")
class TestRankings:
    def test_reliability_needs_7_days_and_outages_rank_lower(self, region_db: sqlite3.Connection) -> None:
        _set(region_db, "akl.example", historic=[1] * 100)  # only ~2 days of history: not ranked
        _set(region_db, "ams.example", historic=[1] * 380 + [0] * 4 + [1] * 16)  # one 2-hour outage
        r = ntextra._rankings()
        hosts = [x["host"] for x in r["reliable"]]
        assert "akl.example" not in hosts
        assert hosts.index("ams.example") > max(hosts.index(h) for h in hosts if h != "ams.example" and h != "down.example")
        ams = next(x for x in r["reliable"] if x["host"] == "ams.example")
        assert ams["outages"] == 1 and ams["avail"] == pytest.approx(99.0)
        assert r["days"] == 14 and all(x["days"] <= 14 for x in r["reliable"])

    def test_streaks_only_up_trackers_longest_first(self, region_db: sqlite3.Connection) -> None:
        now = int(time())
        _set(region_db, "akl.example", last_downtime=now - 3600)
        _set(region_db, "ams.example", last_downtime=now - 5 * 86400)
        hosts = [x["host"] for x in ntextra._rankings()["streaks"]]
        assert "down.example" not in hosts
        assert all(x["status"] in ("up_good", "up_new", "up_slow") for x in ntextra._rankings()["streaks"])
        assert hosts.index("ams.example") < hosts.index("akl.example")

    def test_fastest_per_region_healthy_only(self) -> None:
        f = ntextra._rankings()["fastest"]
        assert list(f) == ["Oceania", "Asia", "Europe", "North America"]  # region names only, never cities
        assert f["North America"][0]["host"] == "nyc.example" and f["North America"][0]["ms"] == 5
        assert all(row["host"] != "down.example" for rows in f.values() for row in rows)
        assert all([r["ms"] for r in rows] == sorted(r["ms"] for r in rows) for rows in f.values())

    def test_page(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/rankings")
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        for h in ("Most reliable, last 14 days", "Longest unbroken uptime", "Fastest from each region", "From Oceania"):
            assert h in html, h
        assert 'href="/tracker/akl.example"' in html and "fa-chart-line" in html
        assert '<meta name="description" content="The most reliable public BitTorrent trackers' in html

    def test_in_the_menu(self, flask_client: FlaskClient) -> None:
        assert 'href="/rankings"' in flask_client.get("/").get_data(as_text=True)


def test_tools_page(flask_client: FlaskClient) -> None:
    r = flask_client.get("/tools")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '<script src="/static/js/tools.js"></script>' in html and 'id="nt-mag-in"' in html and 'id="nt-tor-in"' in html
    assert "never leaves your device" in html and 'href="/tools"' in html
    assert flask_client.get("/static/js/tools.js").status_code == 200


def test_menu_bar_is_sticky_not_fixed(flask_client: FlaskClient) -> None:
    html = flask_client.get("/about").get_data(as_text=True)
    assert 'navbar-dark sticky-top"' in html and "fixed-top" not in html


def test_clients_page(flask_client: FlaskClient) -> None:
    r = flask_client.get("/clients")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    for s in ("qBittorrent", "Automatically append trackers from URL to new downloads", "default-trackers", "Default Trackers", 'href="/tools"'):
        assert s in html, s
    assert 'href="/clients"' in flask_client.get("/").get_data(as_text=True)
    assert 'href="/clients"' in flask_client.get("/faq").get_data(as_text=True)


def test_clients_page_screenshots(flask_client: FlaskClient) -> None:
    import re
    from pathlib import Path

    html = flask_client.get("/clients").get_data(as_text=True)
    srcs = re.findall(r'<img src="(/static/img/clients/[a-z0-9-]+\.png)"[^>]*loading="lazy" alt="([^"]+)"', html)
    assert len(srcs) == 6
    static = Path(__file__).resolve().parents[2] / "newtrackon"
    for src, alt in srcs:
        f = static / src.lstrip("/")
        assert f.exists() and f.stat().st_size < 60_000, src
        assert len(alt) > 10, src
    assert "Tools &rarr; Preferences" in html and "Default Public Trackers" in html and "tracker list URL" in html


def test_submitted_page_shows_the_queue_as_pending(flask_client: FlaskClient) -> None:
    from collections import deque as _dq
    from types import SimpleNamespace
    from unittest.mock import patch

    from newtrackon import ingest

    q = _dq([SimpleNamespace(url="udp://wait.example:1/announce", added=1_800_000_000),
             SimpleNamespace(url="udp://now.example:1/announce", added=1_800_000_000)])
    with patch.object(ingest.submitted_queue, "queue", q), patch.object(ingest, "_in_flight", ["udp://now.example:1/announce"]):
        assert [(p["url"], p["checking"]) for p in ingest.pending()] == [
            ("udp://now.example:1/announce", True), ("udp://wait.example:1/announce", False)]
        html = flask_client.get("/submitted").get_data(as_text=True)
    assert html.count('<td class="pending"><b>Pending</b></td>') == 2 and "Being checked now" in html and "Queue position 2 &middot; ETA ~15 s" in html  # 10 s default: one ahead plus half of the current one
    assert "table tbody tr.nt-pending:not(#_nt) > *:not(#_nt)" in html  # grey must outrank the white row rule
    assert html.index("wait.example") < html.index("now.example")  # newest at the top, next to be checked at the bottom


def test_submission_rate_from_recent_results() -> None:
    from time import time as _now
    from unittest.mock import patch

    from newtrackon import ingest

    n = int(_now())
    with patch.object(ingest, "submitted_data", [{"time": n - 12 * k} for k in range(30)]):
        assert ingest.seconds_per_url() == 12.0
    with patch.object(ingest, "submitted_data", [{"time": n - 7200 - k} for k in range(30)]):
        assert ingest.seconds_per_url() == 10.0  # nothing recent: the default
    with patch.object(ingest, "submitted_data", [{"time": n}] * 30):
        assert ingest.seconds_per_url() == 10.0  # all at once: no rate to measure
