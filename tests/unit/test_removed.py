"""Removed trackers: their page, the ban list, and refused submissions on /submitted."""

from __future__ import annotations

import logging
from time import time
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient

from newtrackon import ingest, ntextra, persistence
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

NOW = int(time())
GONE = "gone.example"
URL = "udp://gone.example:6969/announce"


def _deny(*lines: str) -> None:
    with open("data/denylist.txt", "w", encoding="utf-8") as f:
        f.write("# host per line\n" + "".join(ln + "\n" for ln in lines))


@pytest.fixture
def removed() -> None:
    t = SimpleNamespace(host=GONE, url=URL, added=NOW - 20 * 86400, countries=["Germany"], networks=["Example GmbH"])
    T._removed_add(t, "no answer for 5+ days", now=NOW - 2 * 86400)
    _deny("%s %d" % (GONE, NOW - 2 * 86400), "private.example %d" % (NOW - 86400), "manual.example")


class TestRecords:
    def test_removal_is_recorded_and_saved(self, removed: None) -> None:
        assert T.REMOVED[GONE]["url"] == URL and T.REMOVED[GONE]["country"] == "Germany"
        assert T._jload("data/removed.json") == T.REMOVED

    def test_seeded_from_the_event_history(self) -> None:
        T.EVENTS[:] = [{"t": NOW - 500, "url": "udp://old.example:1/announce", "host": "old.example", "type": "removed",
                        "text": "removed from the list (its hostname no longer points to a public IP address)"},
                       {"t": NOW - 400, "url": "udp://x.example:1/announce", "host": "x.example", "type": "down", "text": "went Down"}]
        T._removed_seed()
        assert list(T.REMOVED) == ["old.example"]
        assert T.REMOVED["old.example"]["reason"] == "its hostname no longer points to a public IP address"


class TestBans:
    def test_only_hosts_this_site_removed(self, removed: None) -> None:
        b = ntextra._ban(GONE, NOW)
        assert b == {"since": NOW - 2 * 86400, "until": NOW + 28 * 86400, "active": True}
        assert ntextra._ban("private.example", NOW) is None and ntextra._ban("manual.example", NOW) is None  # never shown
        assert [x["host"] for x in ntextra._bans(NOW)] == [GONE]

    def test_expired_ban(self, removed: None) -> None:
        assert ntextra._ban(GONE, NOW + 29 * 86400)["active"] is False
        assert ntextra._bans(NOW + 29 * 86400) == []


@pytest.mark.usefixtures("region_db")
class TestPages:
    def test_removed_tracker_page(self, flask_client: FlaskClient, removed: None) -> None:
        T.DAILY[URL] = [["2026-09-20", 100.0, 48, {}], ["2026-09-21", 25.0, 48, {}]]
        r = flask_client.get("/tracker/gone.example")
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and "Removed from the list" in html and "no answer for 5+ days" in html
        assert "Not until" in html and "Germany" in html and html.count('title="2026-09-2') == 2

    def test_never_listed_is_still_404(self, flask_client: FlaskClient, removed: None) -> None:
        assert flask_client.get("/tracker/private.example").status_code == 404
        assert flask_client.get("/tracker/nope.example").status_code == 404

    def test_api_404_says_why(self, flask_client: FlaskClient, removed: None) -> None:
        r = flask_client.get("/api/tracker/gone.example")
        d = r.get_json()
        assert r.status_code == 404 and d["removed"]["reason"] == "no answer for 5+ days"
        assert d["removed"]["banned_until"] == NOW + 28 * 86400 and d["removed"]["banned_permanently"] is False
        assert "removed" not in flask_client.get("/api/tracker/private.example").get_json()

    def test_ban_list_on_submitted(self, flask_client: FlaskClient, removed: None) -> None:
        html = flask_client.get("/submitted").get_data(as_text=True)
        banned = html.split('id="banned"')[1]
        assert 'href="/tracker/gone.example"' in banned and "private.example" not in html and "manual.example" not in html


class TestRefusedRows:
    @pytest.fixture(autouse=True)
    def _info_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="newtrackon")  # the rows come from INFO log records, as in production

    def setup_method(self) -> None:
        persistence.submitted_data.clear()

    def teardown_method(self) -> None:
        persistence.submitted_data.clear()

    def test_banned_removed_host_is_listed_as_refused(self, removed: None) -> None:
        ingest.add_one_tracker_to_submitted_queue("udp://gone.example:6969/announce")
        row = persistence.submitted_data[0]
        assert row["refused"] and row["status"] == 0 and row["info"][0].startswith("Banned until ")

    def test_private_denylist_entries_leave_no_row(self, removed: None) -> None:
        ingest.add_one_tracker_to_submitted_queue("udp://private.example:6969/announce")
        ingest.add_one_tracker_to_submitted_queue("udp://manual.example:6969/announce")
        assert len(persistence.submitted_data) == 0

    def test_duplicates_are_refused_once_a_day(self) -> None:
        log = logging.getLogger("newtrackon")
        for _ in range(2):
            log.info("Tracker %s denied, already being tracked as %s", "http://a.example:80/announce", "udp://a.example:6969/announce")
        assert len(persistence.submitted_data) == 1
        assert persistence.submitted_data[0]["info"] == ["Already listed as udp://a.example:6969/announce"]

    def test_not_while_restoring_the_saved_queue(self) -> None:
        ingest._restoring[0] = True
        try:
            logging.getLogger("newtrackon").info("Tracker %s denied, already in the queue", "udp://b.example:1/announce")
        finally:
            ingest._restoring[0] = False
        assert len(persistence.submitted_data) == 0

    @pytest.mark.usefixtures("region_db")
    def test_shown_grey_as_refused(self, flask_client: FlaskClient) -> None:
        logging.getLogger("newtrackon").info("Tracker %s denied, already in the queue", "udp://c.example:1/announce")
        html = flask_client.get("/submitted").get_data(as_text=True)
        assert '<tr class="nt-pending">' in html and "<b>Refused</b>" in html and "Already waiting in the queue" in html


@pytest.mark.usefixtures("region_db")
def test_result_colours_outrank_the_row_rule(flask_client: FlaskClient) -> None:
    html = flask_client.get("/submitted").get_data(as_text=True)
    assert "table tbody tr:not(#_nt) > td.rejected:not(#_nt), table tbody tr:not(#_nt) > td.rejected:not(#_nt) * { color: #ff3b30 !important; }" in html
    assert "table tbody tr:not(#_nt) > td.up:not(#_nt), table tbody tr:not(#_nt) > td.up:not(#_nt) * { color: #00e676 !important; }" in html


@pytest.mark.usefixtures("region_db")
def test_accepted_submissions_link_to_the_tracker_page(flask_client: FlaskClient) -> None:
    persistence.submitted_data.clear()
    try:
        persistence.submitted_data.appendleft({"url": "udp://ok.example:6969/announce", "time": NOW, "ip": "", "info": ["{}"], "status": 1})
        persistence.submitted_data.appendleft({"url": "udp://no.example:6969/announce", "time": NOW, "ip": "", "info": ["UDP timeout"], "status": 0})
        html = flask_client.get("/submitted").get_data(as_text=True)
        assert '<a href="/tracker/ok.example" class="nt-tlink"' in html and '/tracker/no.example"' not in html
    finally:
        persistence.submitted_data.clear()


def test_api_removed_lists_active_bans_only(flask_client: FlaskClient, removed: None) -> None:
    d = flask_client.get("/api/removed").get_json()
    assert [x["host"] for x in d] == [GONE] and d[0]["banned_until"] == NOW + 28 * 86400 and d[0]["url"] == URL
