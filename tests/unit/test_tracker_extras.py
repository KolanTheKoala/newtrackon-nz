"""Per-tracker JSON, "is it down?" page metadata, follow link and the monitor-health line."""

from __future__ import annotations

import json
import re
from collections import deque
from time import time

import pytest
from flask.testing import FlaskClient

from newtrackon import persistence
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)


@pytest.mark.usefixtures("region_db")
class TestTrackerJson:
    def test_known_host(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/api/tracker/AKL.example")
        assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == "*"
        d = r.get_json()
        assert d["host"] == "akl.example" and d["url"] == "udp://akl.example:1/announce" and "score" in d and "status" in d

    def test_same_fields_as_details(self, flask_client: FlaskClient) -> None:
        one = flask_client.get("/api/tracker/akl.example").get_json()
        all_ = {d["host"]: d for d in flask_client.get("/api/details").get_json()}
        assert set(one) == set(all_["akl.example"])

    def test_unknown_host(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/api/tracker/nope.example")
        assert r.status_code == 404 and r.get_json()["error"] == "not listed" and r.headers["Access-Control-Allow-Origin"] == "*"


@pytest.mark.usefixtures("region_db")
class TestIsItDownPage:
    def _html(self, client: FlaskClient) -> str:
        return client.get("/tracker/akl.example").get_data(as_text=True)

    def test_title_and_description(self, flask_client: FlaskClient) -> None:
        html = self._html(flask_client)
        assert "<title>Is akl.example down? Live tracker status" in html
        desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
        assert desc.startswith("akl.example is ") and "New Zealand" in desc

    def test_answer_line(self, flask_client: FlaskClient) -> None:
        assert re.search(r"Is it down\? <b>(No: up for|Yes: no answer for) [^<]+</b>", self._html(flask_client))

    def test_structured_data_is_safe_json(self, flask_client: FlaskClient) -> None:
        html = self._html(flask_client)
        raw = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1)
        ld = json.loads(raw)
        assert ld["@type"] == "WebPage" and ld["url"] == "https://newtrackon.co.nz/tracker/akl.example" and "dateModified" in ld
        assert "Rating" not in raw and "<" not in raw  # no rating schema; tojson escapes angle brackets

    def test_follow_links(self, flask_client: FlaskClient) -> None:
        html = self._html(flask_client)
        assert '<link rel="alternate" type="application/atom+xml" title="Status changes for akl.example" href="/feed.xml?tracker=akl.example">' in html
        assert 'href="/feed.xml?tracker=akl.example"' in html.split("<body", 1)[1]

    def test_other_pages_keep_the_default_description(self, flask_client: FlaskClient) -> None:
        assert "mirror of newTrackon" in re.search(r'<meta name="description" content="([^"]*)"', flask_client.get("/about").get_data(as_text=True)).group(1)


@pytest.mark.usefixtures("region_db")
class TestMonitorHealth:
    def test_counts_and_regions(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        now = int(time())
        monkeypatch.setattr(persistence, "raw_data", deque([{"time": now - 60}, {"time": now - 1200}, {"time": now - 7200}], maxlen=600))
        monkeypatch.setattr(T, "LAT_HIST", {"u": {"Oceania": [[now - 3600, 40]], "Europe": [[now - 86400, 300]], "Asia": [[now - 600, 200]]}})
        html = flask_client.get("/").get_data(as_text=True)
        line = re.search(r'<div class="nt-health"[^>]*>(.*?)</div>', html, re.S).group(1)
        assert "2 checks in the last hour" in line and "last 1m ago" in line and " ago ago" not in line
        assert "latency from Oceania, Asia" in line and "Europe" not in line  # Europe's last sample is a day old

    def test_offline_says_so(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(T, "_ONLINE", [time(), False])
        assert "Monitor offline" in flask_client.get("/").get_data(as_text=True)

    def test_on_the_about_page(self, flask_client: FlaskClient) -> None:
        assert 'class="nt-health"' in flask_client.get("/about").get_data(as_text=True)


@pytest.mark.usefixtures("region_db")
class TestBadge:
    def test_listed_tracker(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/badge/AKL.example.svg")
        svg = r.get_data(as_text=True)
        assert r.status_code == 200 and r.mimetype == "image/svg+xml" and "max-age=300" in r.headers["Cache-Control"]
        assert svg.startswith("<svg ") and "newTrackon NZ" in svg and "·" in svg
        import xml.dom.minidom
        xml.dom.minidom.parseString(svg)  # well-formed

    def test_down_tracker_says_down(self, flask_client: FlaskClient) -> None:
        assert "Down · " in flask_client.get("/badge/down.example.svg").get_data(as_text=True)

    def test_unknown_host_still_an_image(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/badge/nope.example.svg")
        assert r.status_code == 404 and r.mimetype == "image/svg+xml" and "not listed" in r.get_data(as_text=True)

    def test_escapes_and_no_script(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/badge/%3Cscript%3E.svg")
        svg = r.get_data(as_text=True)
        assert "<script" not in svg.lower() and r.headers["X-Content-Type-Options"] == "nosniff"

    def test_snippet_on_tracker_page(self, flask_client: FlaskClient) -> None:
        html = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert '<img src="/badge/akl.example.svg"' in html
        assert "[![newTrackon NZ status](https://newtrackon.co.nz/badge/akl.example.svg)](https://newtrackon.co.nz/tracker/akl.example)" in html
