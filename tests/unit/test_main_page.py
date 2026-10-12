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


@pytest.mark.usefixtures("insert_sample_tracker")
@pytest.mark.parametrize("state, label, emo", [("up_good", "Up/Good", "&#11014;"), ("up_new", "Up/New", "\U0001F423"),
                                               ("up_slow", "Up/Slow", "\U0001F422"), ("up_unreliable", "Up/Unreliable", "\U0001F3B2")])
def test_the_table_names_every_up_state_itself(flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch, state, label, emo) -> None:
    """The label comes with the page, from the status rule: before, a script in the browser relabelled Up/Good rows (so
    curl, search engines and anyone without the script saw every working tracker as Up/Good)."""
    from newtrackon import tracker as T
    monkeypatch.setattr(T, "status_rule", lambda t, pst=None: (state, []))
    html = flask_client.get("/").get_data(as_text=True)
    cell = html[html.index('<span class="nt-st">'):][:1500]
    assert re.search(r">%s<span class=\"nt-emo\">%s" % (label, emo), cell), cell[:300]
    assert "ns(r,td)" not in html and "dataset.ntnew" not in html


@pytest.mark.usefixtures("insert_sample_tracker")
@pytest.mark.parametrize("uptime, shown", [(88.5, 88), (89.5, 90)])
def test_the_tooltip_score_is_the_score_shown(flask_client: FlaskClient, mock_db_connection, monkeypatch: pytest.MonkeyPatch,
                                              uptime, shown) -> None:
    """Both round halves the same way (Python's round, which Jinja's round filter uses): 88.5 reads 88 in the cell and
    in "score 88 is under 90"."""
    from newtrackon import tracker as T
    mock_db_connection.execute("UPDATE status SET uptime = ?", (uptime,))
    monkeypatch.setattr(T, "status_rule", lambda t, pst=None: ("up_unreliable", []))
    html = flask_client.get("/").get_data(as_text=True)
    assert "score %d is under 90" % shown in html and "</span> %d</td>" % shown in html
