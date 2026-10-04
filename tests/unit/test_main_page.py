"""The main status page shows current status only; per-tracker details live on /tracker/<host>."""

from __future__ import annotations

import re
from time import time

import pytest
from flask.testing import FlaskClient


@pytest.mark.usefixtures("mock_db_connection")
def test_main_table_has_only_current_status_columns(flask_client: FlaskClient) -> None:
    html = flask_client.get("/").get_data(as_text=True)
    thead = re.search(r'<table[^>]*id="trackon_table".*?<thead>(.*?)</thead>', html, re.S)
    assert thead is not None
    headers = [re.sub(r"<[^>]+>|\s+", " ", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), re.S)]
    assert headers[:2] == ["Tracker URL", "Latency"] and "Status" in headers and len(headers) == 7
    for gone in ("Update interval", "IP address", "Country", "Network", "Added", "Stats", "Same operator"):
        assert not any(h.startswith(gone) for h in headers), gone


@pytest.mark.usefixtures("insert_sample_tracker")
def test_url_cell_links_to_tracker_page_with_chart_icon(flask_client: FlaskClient) -> None:
    html = flask_client.get("/").get_data(as_text=True)
    assert (
        'udp://tracker.example.com:6969/announce<a href="/tracker/tracker.example.com" class="nt-tlink"' in html
        and 'fa-chart-line' in html
    )


def test_events_box_keeps_the_last_7_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from newtrackon import tracker as T
    from newtrackon.views import app

    now = int(time())
    old = {"t": now - 8 * 86400, "url": "udp://a.example:1/announce", "type": "down", "text": "x"}
    new = {"t": now - 3600, "url": "udp://b.example:1/announce", "type": "up", "text": "y"}
    monkeypatch.setattr(T, "EVENTS", [old, new])
    nt_events = app.jinja_env.globals["nt_events"]
    assert nt_events(10, 7) == [new]
    assert nt_events(10) == [new, old]  # without a day limit: newest first, as before
