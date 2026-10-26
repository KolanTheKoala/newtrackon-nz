"""The daily '&' hash test (scraper.amp_hash_test, tracker.AMP_OK): some HTTP(S) trackers decode the announce query
before splitting it on '&', so they refuse every info hash holding byte 0x26 (about 1 in 13 torrents). Such a
tracker stays listed and Up, gets a warning on its page and a flag in the table, and is left out of the clean lists
(/api/clean and good=true): user's call 2026-10-14."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote_to_bytes, urlparse

import pytest
from flask.testing import FlaskClient

from newtrackon import scraper
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)
from .test_sim import DAY, HOUR, Sim

REAL_AMP = scraper.amp_hash_test  # conftest replaces it with "no verdict" for every other test
NYC = "http://nyc.example:80/announce"


def _urls(r) -> list[str]:
    return [u for u in r.get_data(as_text=True).split("\n") if u.strip()]


class TestTheProbe:
    def _run(self, monkeypatch: pytest.MonkeyPatch, reply=None, status=200, exc=None):
        sent = []

        def get(url):
            sent.append(url)
            if exc:
                raise exc
            return SimpleNamespace(status_code=status), reply
        monkeypatch.setattr(scraper, "memory_limited_get", get)
        return REAL_AMP("http://t.example:6969/announce"), sent

    def test_the_hash_holds_0x26_and_it_says_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ok, sent = self._run(monkeypatch, b"d8:intervali1800e5:peers0:e")
        q = dict(p.split("=", 1) for p in urlparse(sent[0]).query.split("&"))
        h = unquote_to_bytes(q["info_hash"].replace("+", " "))
        assert len(h) == 20 and h.count(0x26) == 1 and "%26" in q["info_hash"]
        assert q["event"] == "stopped" and q["numwant"] == "0" and ok is True

    def test_refused_as_an_invalid_info_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for msg in (b"Invalid info_hash: 'be93b4f9' length 8", b"invalid infohash", b"bad info hash"):
            reply = b"d14:failure reason%d:%se" % (len(msg), msg)
            assert self._run(monkeypatch, reply)[0] is False, msg

    def test_other_refusals_and_trouble_give_no_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = b"Requested download is not authorized"
        assert self._run(monkeypatch, b"d14:failure reason%d:%se" % (len(msg), msg))[0] is None
        assert self._run(monkeypatch, b"x", status=500)[0] is None
        assert self._run(monkeypatch, b"")[0] is None
        assert self._run(monkeypatch, exc=OSError("timed out"))[0] is None
        assert self._run(monkeypatch, b"not bencoded")[0] is None


class TestDaily:
    URL = "http://sim.example:6969/announce"

    def _sim(self, monkeypatch, verdicts, url=URL, world=lambda d: {}):
        sim = Sim(monkeypatch, world, url=url)
        calls = []

        def amp(u):
            calls.append(sim.now)
            return verdicts(len(calls)) if callable(verdicts) else verdicts
        monkeypatch.setattr(scraper, "amp_hash_test", amp)
        return sim, calls

    def test_once_a_day_and_the_result_stands(self, monkeypatch) -> None:
        sim, calls = self._sim(monkeypatch, False)
        sim.run(3)
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        assert 2 <= len(calls) <= 4 and all(DAY <= g < DAY + 4 * HOUR + 600 for g in gaps), [g / HOUR for g in gaps]
        assert T.AMP_OK[self.URL] is False and len(sim.log) > 10
        from newtrackon import persistence
        assert persistence.raw_data[0]["flags"]["amp_ok"] is False  # every check's /raw row shows the standing result

    def test_no_verdict_asks_again_next_check(self, monkeypatch) -> None:
        sim, calls = self._sim(monkeypatch, lambda n: None if n < 3 else True)
        sim.run(0.5)
        assert len(calls) == 3 and T.AMP_OK[self.URL] is True  # two inconclusive checks, then a verdict that stands

    def test_udp_trackers_are_never_asked(self, monkeypatch) -> None:
        sim, calls = self._sim(monkeypatch, False, url="udp://sim.example:6969/announce")
        sim.run(2)
        assert calls == [] and T.AMP_OK.get("udp://sim.example:6969/announce") is None

    def test_a_restart_keeps_the_result_and_the_day(self, monkeypatch) -> None:
        sim, calls = self._sim(monkeypatch, False)
        sim.run(0.5)
        sim.restart()
        sim.run(0.4)
        assert len(calls) == 1 and T.AMP_OK[self.URL] is False

    def test_a_fixed_tracker_clears_at_the_next_daily_test(self, monkeypatch) -> None:
        sim, calls = self._sim(monkeypatch, lambda n: n > 1)
        sim.run(0.5)
        assert T.AMP_OK[self.URL] is False
        sim.run(1)
        assert len(calls) == 2 and T.AMP_OK[self.URL] is True


@pytest.mark.usefixtures("region_db")
class TestPenaltyAndWarning:
    def test_left_out_of_the_clean_list(self, flask_client: FlaskClient) -> None:
        assert NYC in _urls(flask_client.get("/api/clean?min_age_days=0"))
        T.AMP_OK[NYC] = True
        assert NYC in _urls(flask_client.get("/api/clean?min_age_days=0"))
        T.AMP_OK[NYC] = False
        assert NYC not in _urls(flask_client.get("/api/clean?min_age_days=0"))

    def test_left_out_of_good_true_but_not_the_plain_lists(self, flask_client: FlaskClient) -> None:
        good = set(_urls(flask_client.get("/api/all?good=true")))
        T.AMP_OK[NYC] = False
        assert set(_urls(flask_client.get("/api/all?good=true"))) == good - {NYC}
        assert NYC in _urls(flask_client.get("/api/all")) and NYC in _urls(flask_client.get("/api/http"))

    def test_api_record(self, flask_client: FlaskClient) -> None:
        rec = {t["url"]: t for t in flask_client.get("/api/details").get_json()}
        assert rec[NYC]["ampersand_hash_ok"] is None and rec["udp://akl.example:1/announce"]["ampersand_hash_ok"] is None
        T.AMP_OK[NYC] = False
        T.AMP_OK["udp://akl.example:1/announce"] = False  # UDP isn't affected: never reported
        rec = {t["url"]: t for t in flask_client.get("/api/details").get_json()}
        assert rec[NYC]["ampersand_hash_ok"] is False and rec["udp://akl.example:1/announce"]["ampersand_hash_ok"] is None

    def test_tracker_page_warning_and_table_flag(self, flask_client: FlaskClient) -> None:
        page = lambda: flask_client.get("/tracker/nyc.example").get_data(as_text=True)
        assert "Refuses about 1 in 13 torrents" not in page()
        assert 'href="/fix#ampersand-hash"' not in flask_client.get("/").get_data(as_text=True)
        T.AMP_OK[NYC] = False
        assert "Refuses about 1 in 13 torrents" in page() and 'href="/fix#ampersand-hash"' in page()
        assert 'class="nt-peerwarn nt-amp" href="/fix#ampersand-hash"' in flask_client.get("/").get_data(as_text=True)

    def test_fix_page_section(self, flask_client: FlaskClient) -> None:
        html = flask_client.get("/fix").get_data(as_text=True)
        assert 'id="ampersand-hash"' in html and 'href="#ampersand-hash"' in html


@pytest.mark.usefixtures("region_db")
def test_the_status_page_key_explains_the_flag(flask_client: FlaskClient) -> None:
    html = flask_client.get("/").get_data(as_text=True)
    key = html[html.index('<h6>Other</h6>'):]
    assert "refuses 1 in 13 torrents" in key[:key.index("</div></div>")]
