"""How-to-fix links (/fix), the evidence on the tracker page, "check again now", and the fix link in Telegram messages."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient

from newtrackon import ntextra
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

AKL = "udp://akl.example:1/announce"
_REAL_NOTIFY = T._notify  # taken at import, before the isolation fixture swaps it out
DOWN = "udp://down.example:1/announce"


@pytest.fixture(autouse=True)
def fresh_recheck_limits() -> None:
    ntextra._recheck_host.clear()
    ntextra._recheck_all.clear()
    T.FORCE_CHECK.clear()


class TestFixPage:
    def test_every_section_exists(self, flask_client: FlaskClient) -> None:
        html = flask_client.get("/fix").get_data(as_text=True)
        ids = set(re.findall(r'<h4 id="([a-z0-9-]+)"', html))
        assert ids == set(ntextra.FIX_TITLES)
        assert set(T.FIX_DOWN.values()) <= ids

    def test_linked_from_faq(self, flask_client: FlaskClient) -> None:
        assert 'href="/fix"' in flask_client.get("/faq").get_data(as_text=True)


@pytest.mark.usefixtures("region_db")
class TestFixLinks:
    def test_down_tracker_links_to_its_cause(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(T, "DOWN_WHY", {DOWN: "UDP timeout"})
        html = flask_client.get("/").get_data(as_text=True)
        assert html.count('class="nt-fixlink"') == 1 and 'href="/fix#down-timeout"' in html
        cell = html[html.index('class="nt-fixlink"') - 3000:html.index('class="nt-fixlink"')]
        assert "</b>" not in cell.split("<b>")[-1]  # inside the status line, not after it

    def test_bad_trackers(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(T, "PEER_FAILS", {AKL: 3})
        monkeypatch.setattr(T, "PEER_HIST", {AKL: [1, 1, 1, 0, 0, 0]})
        html = flask_client.get("/").get_data(as_text=True)
        assert 'href="/fix#no-peers"' in html
        page = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert "What's wrong: Hands out no peers" in page and "Peer test passed 3 of the last 6 times" in page
        assert 'href="/fix#no-peers"' in page

    def test_fake_peers_beat_no_peers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        t = MagicMock(url=AKL, status=1, uptime=99)
        monkeypatch.setattr(T, "PEER_FAILS", {AKL: 3})
        monkeypatch.setattr(T, "FAKE_FAILS", {AKL: 3})
        assert ntextra._fix_anchor(t) == "fake-peers"

    def test_dead_address(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(T, "FAM_FAILS", {AKL: {"fam": "v6", "n": 4}})
        page = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert 'href="/fix#dead-address"' in page and "Its IPv6 address" in page and "4 checks in a row, while IPv4 did" in page

    def test_healthy_tracker_has_no_fix(self, flask_client: FlaskClient) -> None:
        page = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert "What's wrong" not in page and "/fix#" not in page


class TestEventFix:
    @pytest.mark.parametrize(("ev", "want"), [
        ({"type": "down", "text": "went Down (UDP timeout)"}, "down-timeout"),
        ({"type": "down", "text": "went Down (Connection refused)"}, "down-refused"),
        ({"type": "down", "text": "went Down (something odd)"}, "down"),
        ({"type": "bad", "text": "is Up/Bad: hands out no peers (3+ of its last 6 peer tests failed)"}, "no-peers"),
        ({"type": "bad", "text": "is Up/Bad: returns fake peers (3+ checks in a row)"}, "fake-peers"),
        ({"type": "bad", "text": "is Up/Broken: its published IPv6 address is dead"}, "dead-address"),
        ({"type": "family", "text": "IPv6 address confirmed dead"}, "dead-address"),
        ({"type": "family", "text": "IPv6 address answering again"}, None),
        ({"type": "bad", "text": "is Up/Unreliable: score 80, under 90"}, "unreliable"),
        ({"type": "bad", "text": "is Up/Slow: averages 320 ms across regions"}, "slow"),
        ({"type": "up", "text": "is back Up after 2h down"}, None),
        ({"type": "up", "text": "is back Up after 2h down, but Up/Unreliable: score 70, under 90"}, "unreliable"),
        ({"type": "good", "text": "is Up/Good again"}, None),
        ({"type": "added", "text": "added to the list"}, None),
    ])
    def test_event_sections(self, ev: dict, want: str | None) -> None:
        assert T._nt_fix_for_event(ev) == want

    def test_telegram_message_has_the_link(self) -> None:
        import json

        with open("data/notify.json", "w") as f:
            json.dump({"telegram": {"token": "x", "chat_id": "1"}}, f)
        sent: list[bytes] = []
        ev = {"t": 1, "url": DOWN, "host": "down.example", "type": "down", "text": "went Down (UDP timeout)"}
        with patch("urllib.request.urlopen", side_effect=lambda url, data, timeout: sent.append(data) or MagicMock()), \
             patch("threading.Thread", side_effect=lambda target, daemon: MagicMock(start=target)):
            _REAL_NOTIFY(ev)
        assert b"fix%23down-timeout" in sent[0] or b"fix#down-timeout" in sent[0]


@pytest.mark.usefixtures("region_db")
class TestRecheck:
    def test_queues_a_check_once_per_hour(self, flask_client: FlaskClient) -> None:
        r = flask_client.post("/tracker/akl.example/recheck")
        assert r.status_code == 303 and r.headers["Location"].endswith("/tracker/akl.example?recheck=queued")
        assert T.FORCE_CHECK == {AKL}
        r = flask_client.post("/tracker/akl.example/recheck")
        assert "recheck=wait" in r.headers["Location"] and "m=60" in r.headers["Location"]
        page = flask_client.get(r.headers["Location"]).get_data(as_text=True)
        assert "try again in 60 min" in page

    def test_global_limit(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ntextra, "RECHECK_PER_HOUR", 1)
        assert "queued" in flask_client.post("/tracker/akl.example/recheck").headers["Location"]
        assert "busy" in flask_client.post("/tracker/ams.example/recheck").headers["Location"]
        assert T.FORCE_CHECK == {AKL}

    def test_unknown_and_get(self, flask_client: FlaskClient) -> None:
        assert flask_client.post("/tracker/nope.example/recheck").status_code == 404
        assert flask_client.get("/tracker/akl.example/recheck").status_code == 405


class TestLoopTakesRechecksFirst:
    def test_forced_tracker_checked_first_even_if_not_due(self) -> None:
        from newtrackon import trackon

        order: list[str] = []

        def mk(url: str, last: int) -> MagicMock:
            m = MagicMock(url=url, last_checked=last, interval=1800, to_be_deleted=False)
            m.update_status.side_effect = lambda: order.append(url)
            return m

        due, fresh = mk("udp://due.example:1/announce", 0), mk("udp://fresh.example:1/announce", 99_990)
        T.FORCE_CHECK.add(fresh.url)
        with (
            patch("newtrackon.trackon.time", return_value=100_000),
            patch("newtrackon.trackon.db.get_all_data", return_value=[due, fresh]),
            patch("newtrackon.trackon.db.update_tracker"),
            patch("newtrackon.trackon.save_deque_to_disk"),
            patch("newtrackon.trackon.sleep", side_effect=StopIteration),
        ):
            with pytest.raises(StopIteration):
                trackon.update_outdated_trackers()
        assert order == [fresh.url, due.url] and not T.FORCE_CHECK


@pytest.mark.usefixtures("region_db")
class TestMainPageLayout:
    def test_submit_on_one_line_and_heading(self, flask_client: FlaskClient) -> None:
        html = flask_client.get("/").get_data(as_text=True)
        assert '<h1 class="h3 text-center mt-3">Tracker Status</h1>' in html
        form = html[html.index('<form method="post" action="/"'):html.index("</form>")]
        assert '<div class="d-flex gap-2 align-items-start">' in form and "<p>" not in form


@pytest.mark.usefixtures("region_db")
class TestOfflineLatency:
    def test_down_tracker_shows_offline(self, flask_client: FlaskClient) -> None:
        html = flask_client.get("/").get_data(as_text=True)
        assert html.count('<span class="nt-offl">Offline</span>') == 1  # down.example only
