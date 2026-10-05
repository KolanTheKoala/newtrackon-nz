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
        assert "Otherwise not until" in html and "Check again now" in html and "Germany" in html and html.count('title="2026-09-2') == 2

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



class TestReinstate:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ingest, "REINSTATE", {})
        monkeypatch.setattr(ntextra, "_recheck_host", {})
        monkeypatch.setattr(ntextra, "_recheck_all", [])

    def test_button_queues_a_check_past_the_ban(self, flask_client: FlaskClient, removed: None, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading
        calls = []
        monkeypatch.setattr(ingest, "add_one_tracker_to_submitted_queue", lambda url: calls.append((url, ingest._reinstating(GONE))))

        class Now:  # run the thread's work straight away
            def __init__(self, target, args, daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)
        monkeypatch.setattr(threading, "Thread", Now)
        r = flask_client.post("/tracker/gone.example/recheck")
        assert r.status_code == 303 and r.headers["Location"].endswith("/tracker/gone.example?recheck=queued")
        assert calls == [(URL, True)]
        r = flask_client.post("/tracker/gone.example/recheck")  # once an hour
        assert "recheck=wait" in r.headers["Location"] and len(calls) == 1

    def test_pass_lets_it_past_the_denylist_for_a_while(self, removed: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="newtrackon")
        monkeypatch.setattr(T.Tracker, "from_url", staticmethod(lambda url: (_ for _ in ()).throw(ValueError("stop here"))))
        monkeypatch.setattr(ingest.db, "get_all_data", lambda: [])
        ingest.add_one_tracker_to_submitted_queue(URL)
        assert "host denylisted" in caplog.text
        caplog.clear()
        ingest.REINSTATE[GONE] = time()
        ingest.add_one_tracker_to_submitted_queue(URL)
        assert "host denylisted" not in caplog.text and "preprocessing failed" in caplog.text
        ingest.REINSTATE[GONE] = time() - ingest.REINSTATE_TTL - 100  # expired
        assert ingest._reinstating(GONE) is False

    def test_lifting_the_ban_keeps_permanent_entries(self, removed: None) -> None:
        _deny("%s %d" % (GONE, NOW), "%s" % "perm.example", "other.example %d" % NOW)
        ingest._lift_ban(GONE)
        lines = [ln for ln in open("data/denylist.txt").read().splitlines() if ln and not ln.startswith("#")]
        assert lines == ["perm.example", "other.example %d" % NOW]

    def test_up_bad_removal_keeps_its_clock(self) -> None:
        T.REMOVED["ub.example"] = {"url": "udp://ub.example:1/announce", "t": NOW, "reason": "handed out no peers for 5 days (Up/Bad)"}
        ingest._keep_upbad_clock("ub.example", "udp://ub.example:1/announce")
        assert T._nt_upbad_days("udp://ub.example:1/announce") >= T.REMOVE_DAYS
        T.REMOVED["dn.example"] = {"url": "udp://dn.example:1/announce", "t": NOW, "reason": "no answer for 5 days"}
        ingest._keep_upbad_clock("dn.example", "udp://dn.example:1/announce")
        assert "udp://dn.example:1/announce" not in T.LAST_STATE

    @pytest.mark.usefixtures("region_db")
    def test_no_button_or_check_for_a_permanent_ban(self, flask_client: FlaskClient, removed: None) -> None:
        _deny(GONE)
        assert "Check again now" not in flask_client.get("/tracker/gone.example").get_data(as_text=True)
        assert flask_client.post("/tracker/gone.example/recheck").status_code == 404


class TestSecondCheck:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ingest, "CONFIRM", {})
        monkeypatch.setattr(ingest, "CONFIRM_DELAY", 1800)

    def test_due_entries_are_queued_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading
        calls = []

        class Now:
            def __init__(self, target, args, daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)
        monkeypatch.setattr(threading, "Thread", Now)
        monkeypatch.setattr(ingest, "add_one_tracker_to_submitted_queue", lambda url: calls.append(url))
        ingest.CONFIRM.update({"udp://a.example:1/announce": {"t": NOW - 1900, "queued": False},
                               "udp://b.example:1/announce": {"t": NOW - 600, "queued": False}})
        ingest.confirm_due(NOW)
        ingest.confirm_due(NOW + 5)
        assert calls == ["udp://a.example:1/announce"] and ingest.CONFIRM["udp://a.example:1/announce"]["queued"] is True
        assert [c["url"] for c in ingest.confirming(NOW)] == ["udp://b.example:1/announce"]
        assert ingest.confirming(NOW)[0]["eta"] == 1200
        ingest.confirm_due(NOW + 6 * 3600)  # its second check never ran: dropped
        assert "udp://a.example:1/announce" not in ingest.CONFIRM

    def test_first_answer_waits_second_answer_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "udp://new.example:6969/announce"
        inserted = []
        monkeypatch.setattr(ingest.db, "get_all_data", lambda: [])
        monkeypatch.setattr(ingest.db, "insert_new_tracker", lambda t: inserted.append(t.url))
        monkeypatch.setattr(ingest, "attempt_submitted", lambda u: (1800, u, 50))
        persistence.submitted_data.clear()

        def cand():
            persistence.submitted_data.appendleft({"url": url, "time": NOW, "ip": "", "status": 1, "info": ["{'interval': 1800}"]})
            t = T.Tracker.from_url.__func__ if False else None  # noqa: F841
            return SimpleNamespace(url=url, host="new.example", ips=None, interval=0, latency=0, last_downtime=0, last_checked=0,
                                   update_ipapi_data=lambda: None, is_up=lambda: None, update_uptime=lambda: None)
        try:
            ingest.process_new_tracker(cand())
            assert inserted == [] and url in ingest.CONFIRM
            row = persistence.submitted_data[0]
            assert row["confirm"] and row["status"] == 0 and "checked again in 30 minutes" in row["info"][1]
            ingest.CONFIRM[url]["t"] -= 1800  # half an hour later
            ingest.CONFIRM[url]["queued"] = True
            ingest.process_new_tracker(cand())
            assert inserted == [url] and url not in ingest.CONFIRM
        finally:
            persistence.submitted_data.clear()

    def test_resubmitting_while_waiting_is_refused(self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        caplog.set_level(logging.INFO, logger="newtrackon")
        monkeypatch.setattr(ingest.db, "get_all_data", lambda: [])
        ingest.CONFIRM["udp://w.example:1/announce"] = {"t": NOW, "queued": False}
        ingest.add_one_tracker_to_submitted_queue("udp://w.example:1/announce")
        assert "already waiting for its second check" in caplog.text


@pytest.mark.usefixtures("region_db")
class TestBannedSubmission:
    def test_form_says_so_and_links_to_its_page(self, flask_client: FlaskClient, removed: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ingest, "enqueue_new_trackers", lambda text: None)
        html = flask_client.post("/", data={"new_trackers": "udp://gone.example:6969/announce\nudp://private.example:1/announce"}).get_data(as_text=True)
        assert "<strong>gone.example</strong> was removed from the list on" in html and 'href="/tracker/gone.example">See its page</a>' in html
        assert "private.example" not in html.split("Received, see")[0]  # manual denylist entries stay silent

    def test_refused_row_links_to_its_page(self, flask_client: FlaskClient, removed: None, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="newtrackon")
        persistence.submitted_data.clear()
        try:
            ingest.add_one_tracker_to_submitted_queue("udp://gone.example:6969/announce")
            html = flask_client.get("/submitted").get_data(as_text=True)
            assert '<a href="/tracker/gone.example" class="nt-tlink" title="Why it was removed' in html
        finally:
            persistence.submitted_data.clear()


@pytest.mark.parametrize("url", ["udp://gone.example:6969/announce", "http://gone.example:80/announce", "https://gone.example:443/announce",
                                 "udp://GONE.example:1337/announce", "http://gone.example.:6969/announce", "udp://gone.example.:1/announce",
                                 "wss://gone.example/announce", "http://user@gone.example.:80/announce"])
def test_a_ban_holds_whatever_the_protocol_port_or_spelling(url: str, removed: None, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.INFO, logger="newtrackon")
    monkeypatch.setattr(ingest.db, "get_all_data", lambda: [])
    monkeypatch.setattr(T.Tracker, "from_url", staticmethod(lambda u: (_ for _ in ()).throw(ValueError("got past the ban"))))
    ingest.enqueue_new_trackers(url)
    assert "host denylisted" in caplog.text and "got past the ban" not in caplog.text


class TestIpBan:
    @pytest.fixture(autouse=True)
    def _setup(self, removed: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="newtrackon")
        T.REMOVED[GONE]["ips"] = ["203.0.113.5", "2001:db8::5"]
        monkeypatch.setattr(ingest.db, "get_all_data", lambda: [])
        monkeypatch.setattr(ingest, "log_ip_conflicts", lambda t, ts: False)
        persistence.submitted_data.clear()
        self.queued: list[str] = []
        monkeypatch.setattr(ingest.submitted_queue, "put_nowait", lambda t: self.queued.append(t.url))
        monkeypatch.setattr(ingest, "save_queue", lambda: None)

    def _submit(self, monkeypatch: pytest.MonkeyPatch, url: str, ips: list[str]) -> None:
        monkeypatch.setattr(T.Tracker, "from_url", staticmethod(lambda u: SimpleNamespace(url=u, ips=ips, host=u.split("/")[2].split(":")[0])))
        ingest.add_one_tracker_to_submitted_queue(url)

    def test_a_new_name_on_the_banned_server_is_refused(self, monkeypatch: pytest.MonkeyPatch, flask_client: FlaskClient) -> None:
        self._submit(monkeypatch, "udp://alias.example:6969/announce", ["203.0.113.5"])
        assert self.queued == []
        row = persistence.submitted_data[0]
        assert row["info"][0].startswith("Same server as the banned tracker gone.example. Banned until") and row["ban_host"] == GONE

    def test_another_server_is_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._submit(monkeypatch, "udp://other.example:6969/announce", ["198.51.100.7"])
        assert self.queued == ["udp://other.example:6969/announce"]

    def test_cdn_addresses_are_never_banned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        T.REMOVED[GONE]["network"] = "Cloudflare, Inc."
        self._submit(monkeypatch, "https://behind-cf.example:443/announce", ["203.0.113.5"])
        assert self.queued == ["https://behind-cf.example:443/announce"]

    def test_expired_ban_frees_the_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _deny("%s %d" % (GONE, NOW - 40 * 86400))
        self._submit(monkeypatch, "udp://alias.example:6969/announce", ["203.0.113.5"])
        assert self.queued == ["udp://alias.example:6969/announce"]

    def test_removal_saves_the_addresses(self) -> None:
        t = SimpleNamespace(host="x.example", url="udp://x.example:1/announce", added=0, countries=[], networks=[],
                            ips=["192.0.2.1"], recent_ips={"192.0.2.9": 0}, historic=[])
        T._removed_add(t, "x")
        assert T.REMOVED["x.example"]["ips"] == ["192.0.2.1", "192.0.2.9"]


@pytest.mark.parametrize(("url", "want"), [
    ("http://bücher.example:80/announce", "http://xn--bcher-kva.example:80/announce"),
    ("http://BÜCHER.example.:80/announce", "http://xn--bcher-kva.example:80/announce"),
    ("udp://user@banned.example.:1/announce", "udp://user@banned.example:1/announce"),
    ("udp://plain.example:1/announce", "udp://plain.example:1/announce")])
def test_one_spelling_per_host(url: str, want: str) -> None:
    assert ingest.normalise_url(url.lower()) == want.lower()


def test_repeat_ban_counts_other_names_on_the_same_server(sample_tracker) -> None:
    sample_tracker.ips = ["203.0.113.9"]
    T.REMOVED["old-name.example"] = {"url": "udp://old-name.example:1/announce", "t": 0, "reason": "x", "count": 1, "ips": ["203.0.113.9"]}
    open("data/denylist.txt", "w").close()
    sample_tracker._nt_ban()
    assert open("data/denylist.txt").read().split()[2] == "90"  # its second removal on that server: 90 days, not 30
