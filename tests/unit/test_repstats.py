"""The tracker's own stats (scraper.fetch_stats, tracker.stats_due, ntextra._repstats): once a day each listed, up
tracker's public stats page is read and shown on its page and in its API record, as reported. A tracker that publishes
none is asked again only after 30 days (user's call 2026-10-15: "dont repeatedy retry trackers that dont report stats"),
and every tracker gets the same rows ('–' for what it doesn't report)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient

from newtrackon import ntextra, scraper
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)

REAL_FETCH = scraper.fetch_stats  # conftest replaces it with "none published" for every other test
DAY = 86400
NYC = "http://nyc.example:80/announce"

OT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<stats>
  <tracker_id>1801921171</tracker_id>
  <version>
https://erdgeist.org/gitweb/opentracker/commit/?id=7ee8a8e
  </version>
  <uptime>3203</uptime>
  <torrents>
    <count_mutex>780590</count_mutex>
    <count_iterator>780592</count_iterator>
  </torrents>
  <peers>
    <count>1313781</count>
  </peers>
  <seeds>
    <count>179685</count>
  </seeds>
  <completed>
    <count>4614</count>
  </completed>
  <connections>
    <tcp>
      <accept>39</accept>
      <announce>2</announce>
      <scrape>1</scrape>
    </tcp>
    <udp>
      <overall>4609321</overall>
      <connect>1199936</connect>
      <announce>2350042</announce>
      <scrape>26127</scrape>
      <missmatch>1029968</missmatch>
    </udp>
  </connections>
</stats>"""
DUCKS = {"torrents": 13295490, "active_torrents": 2842507, "retained_torrents": 10452983, "peers": 5587381,
         "seeders": 1455770, "t": 3769201, "connections": 717598539674, "announces": 355876343768, "scrapes": 234524726,
         "errors": 18547713}
RETRACKER = {"ann_req": 134.99, "annonces": 4619911274, "leechers": 59932, "scrapes": 1283180549, "seeders": 38941,
             "torrents": 62344, "users": 67201}


class TestParse:
    def test_opentracker(self) -> None:
        kind, f = scraper.parse_stats(OT_XML)
        assert kind == "opentracker"
        assert f == {"uptime_s": 3203, "torrents": 780590, "peers": 1313781, "seeds": 179685, "leechers": 1313781 - 179685,
                     "completed": 4614, "requests": 4609321 + 39, "announces": 2350042 + 2, "scrapes": 26127 + 1,
                     "refused": 1029968, "tracker_id": 1801921171}

    def test_the_kind_with_active_and_retained_torrents(self) -> None:
        kind, f = scraper.parse_stats(json.dumps(DUCKS).encode())
        assert kind == "other"
        assert f == {"torrents": 13295490, "peers": 5587381, "seeds": 1455770, "leechers": 5587381 - 1455770,
                     "uptime_s": 3769201, "requests": 717598539674, "announces": 355876343768, "scrapes": 234524726,
                     "refused": 18547713}

    def test_the_retracker_kind(self) -> None:
        kind, f = scraper.parse_stats(json.dumps(RETRACKER).encode())
        assert kind == "other"
        assert f == {"torrents": 62344, "seeds": 38941, "leechers": 59932, "peers": 38941 + 59932,
                     "announces": 4619911274, "scrapes": 1283180549}
        assert "uptime_s" not in f  # it has no uptime, and a bare 't' elsewhere isn't read as one

    def test_not_a_stats_page(self) -> None:
        for body in (b"<h1>MyWaifu</h1> a public tracker", b"d14:failure reason4:nopee", b"[1, 2]", b"{not json",
                     b'{"users": 5}', b"", b"<stats></stats>"):
            assert scraper.parse_stats(body) is None, body

    def test_nonsense_counts_are_dropped(self) -> None:
        kind, f = scraper.parse_stats(json.dumps({"torrents": 5, "peers": -3, "seeders": 10**15, "scrapes": True}).encode())
        assert f == {"torrents": 5}

    def test_stats_url(self) -> None:
        assert scraper.stats_url("udp://t.example:6969/announce") == "http://t.example:6969/stats?mode=everything"
        assert scraper.stats_url("https://t.example/announce") == "https://t.example:443/stats?mode=everything"
        assert scraper.stats_url("udp://[2001:db8::1]:1337/announce") == "http://[2001:db8::1]:1337/stats?mode=everything"
        assert scraper.stats_url("udp://ours.example:1337/announce", local=True) == "http://127.0.0.1:1337/stats?mode=everything"


class TestFetch:
    def test_public_read_uses_an_honest_user_agent_and_no_redirects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}

        def get(url, **kw):
            seen.update(kw, url=url)
            return SimpleNamespace(status_code=200, raw=SimpleNamespace(read=lambda n, decode_content: OT_XML))
        monkeypatch.setattr(scraper.requests, "get", get)
        assert REAL_FETCH("udp://t.example:6969/announce")[0] == "opentracker"
        assert seen["url"] == "http://t.example:6969/stats?mode=everything" and seen["allow_redirects"] is False
        assert seen["headers"]["User-Agent"] == scraper.STATS_UA and seen["timeout"] == 10

    def test_not_200_or_too_big_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for status, body in ((404, OT_XML), (200, b"x" * (scraper.MAX_RESPONSE_SIZE + 1))):
            monkeypatch.setattr(scraper.requests, "get", lambda url, s=status, b=body, **kw: SimpleNamespace(
                status_code=s, raw=SimpleNamespace(read=lambda n, decode_content: b)))
            assert REAL_FETCH("udp://t.example:6969/announce") is None


def _t(url=NYC, status=1, ips=("93.184.216.34",)):
    return SimpleNamespace(url=url, status=status, ips=list(ips))


class TestSchedule:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch: pytest.MonkeyPatch):
        self.calls, self.paths, self.answer = [], [], ("opentracker", {"torrents": 10, "peers": 20}, "/stats?mode=everything")
        monkeypatch.setattr(scraper, "fetch_stats", lambda url, local=False, path=None: self.calls.append((url, local))
                            or self.paths.append(path) or self.answer)
        monkeypatch.setattr(T, "_STATS_NEXT", [0])

    def test_one_per_gap_and_daily(self) -> None:
        a, b = _t("udp://a.example:1/announce"), _t("udp://b.example:1/announce")
        T.stats_due([a, b], now=1000)
        T.stats_due([a, b], now=1001)  # inside the gap
        T.stats_due([a, b], now=1000 + T.STATS_GAP)
        assert [u for u, _ in self.calls] == [a.url, b.url]
        T.stats_due([a, b], now=1000 + 2 * T.STATS_GAP)
        assert len(self.calls) == 2  # both read: next due a day later
        assert T.REPSTATS[a.url]["next"] == 1000 + DAY and T.REPSTATS[a.url]["hist"][-1] == {"t": 1000, "torrents": 10, "peers": 20}

    def test_none_published_is_asked_again_only_after_30_days(self) -> None:
        self.answer = None
        t = _t()
        T.stats_due([t], now=1000)
        assert T.REPSTATS[t.url]["next"] == 1000 + T.STATS_RETRY and not T.REPSTATS[t.url].get("ok")
        for d in range(1, 30):
            T.stats_due([t], now=1000 + d * DAY)
        assert len(self.calls) == 1

    def test_one_that_published_gets_3_days_then_30(self) -> None:
        t, b = _t(), 10 * DAY
        T.stats_due([t], now=b)
        self.answer = None
        for n in (1, 2):
            T.stats_due([t], now=b + n * DAY)
            assert T.REPSTATS[t.url]["next"] == b + (n + 1) * DAY
        T.stats_due([t], now=b + 3 * DAY)
        assert T.REPSTATS[t.url]["next"] == b + 3 * DAY + T.STATS_RETRY and len(self.calls) == 4

    def test_down_trackers_wait_and_our_outage_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        T.stats_due([_t(status=0)], now=1000)
        assert self.calls == []
        self.answer = None
        monkeypatch.setattr(T, "_monitor_online", lambda: False)
        T.stats_due([_t()], now=1000)
        assert len(self.calls) == 1 and NYC not in T.REPSTATS

    def test_nothing_in_the_first_10_minutes(self) -> None:
        assert T._STATS_NEXT is not None  # module start sets now + 600; here the test controls it
        T._STATS_NEXT[0] = 5000
        T.stats_due([_t()], now=4999)
        assert self.calls == []

    def test_this_servers_own_tracker_is_read_on_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper, "my_ipv4", "160.30.240.158")
        monkeypatch.setattr(scraper, "my_ipv6", "2401:c060:1010:4007::")
        T.stats_due([_t(ips=("160.30.240.158", "2401:c060:1010:4007::"))], now=1000)
        T._STATS_NEXT[0] = 0
        T.stats_due([_t("udp://other.example:1/announce", ips=("160.30.240.158", "93.184.216.34"))], now=2000)
        assert [loc for _, loc in self.calls] == [True, False]

    def test_kept_31_days(self) -> None:
        t = _t()
        for d in range(1, 41):
            T._STATS_NEXT[0] = 0
            T.stats_due([t], now=d * DAY)
        assert [h["t"] for h in T.REPSTATS[t.url]["hist"]][0] == 40 * DAY - T.STATS_KEEP

    def test_survives_a_restart(self) -> None:
        T.stats_due([_t()], now=1000)
        assert T._store_load("REPSTATS")[NYC]["next"] == 1000 + DAY


def _hist(*readings):
    T.REPSTATS[NYC] = {"kind": "opentracker", "ok": True, "hist": [dict(r) for r in readings]}


class TestShown:
    NOW = 10 * DAY

    def test_change_over_a_day(self) -> None:
        _hist({"t": self.NOW - 2 * DAY, "torrents": 1}, {"t": self.NOW - DAY - 3600, "torrents": 100, "announces": 1000,
              "requests": 2000, "refused": 100, "uptime_s": 5 * DAY}, {"t": self.NOW - 3600, "torrents": 90,
              "announces": 87400, "requests": 2000 + 86400 * 2, "refused": 100 + 8640, "uptime_s": 6 * DAY})
        r = ntextra._repstats(NYC, self.NOW)
        assert r["change_24h"] == {"torrents": -10, "announces": 86400, "requests": 172800, "refused": 8640}
        assert r["restarted"] is False and r["change_hours"] == 24.0 and r["requests_per_s"] == 2
        assert r["refused_pct_24h"] == 5.0 and r["refused_pct"] == round(100 * 8740 / 174800, 3)

    def test_a_restart_shows_no_change_in_running_totals(self) -> None:
        _hist({"t": self.NOW - DAY, "torrents": 100, "announces": 5000, "uptime_s": 9 * DAY},
              {"t": self.NOW, "torrents": 80, "announces": 300, "uptime_s": 3600})
        r = ntextra._repstats(NYC, self.NOW)
        assert r["restarted"] is True and r["change_24h"] == {"torrents": -20}

    def test_no_reading_a_day_before_no_change(self) -> None:
        _hist({"t": self.NOW - 3 * 3600, "torrents": 5}, {"t": self.NOW, "torrents": 9})
        r = ntextra._repstats(NYC, self.NOW)
        assert r["change_24h"] == {} and r["change_hours"] is None

    def test_old_or_missing_is_not_shown(self) -> None:
        assert ntextra._repstats(NYC, self.NOW) is None
        _hist({"t": self.NOW - 3 * DAY - 1, "torrents": 5})
        assert ntextra._repstats(NYC, self.NOW) is None

    def test_every_tracker_gets_the_same_rows(self) -> None:
        _hist({"t": self.NOW - DAY, "torrents": 722_000, "peers": 1_100_000}, {"t": self.NOW, "torrents": 722_700,
              "peers": 1_164_200, "uptime_s": 2445})
        full = ntextra._repstats_lines(ntextra._repstats(NYC, self.NOW))
        _hist({"t": self.NOW, "seeds": 5, "torrents": 1})
        bare = ntextra._repstats_lines(ntextra._repstats(NYC, self.NOW))
        label = lambda ls: [line.split()[0] for line in ls]
        assert label(full) == label(bare) == ["uptime", "-" * 32, "torrents", "peers", "seeds", "leechers", "completed",
                                              "-" * 32, "requests", "announces", "scrapes", "refused", "refused", "requests/s"]
        assert "torrents      722.7k  ▲700" in full and "peers           1.2M  ▲64.2k" in full
        assert "uptime         0.7 h" in full and "leechers           –" in bare
        _hist({"t": self.NOW, "torrents": 1, "refused": 3, "requests": 100_000})
        assert "refused %      <0.1%  last 24 h –" in ntextra._repstats_lines(ntextra._repstats(NYC, self.NOW))


@pytest.mark.usefixtures("region_db")
class TestPageAndApi:
    def test_page_block_and_api_record(self, flask_client: FlaskClient) -> None:
        import time
        now = int(time.time())
        page = lambda: flask_client.get("/tracker/nyc.example").get_data(as_text=True)
        assert "<pre class=\"nt-rep\">" not in page()
        assert flask_client.get("/api/tracker/nyc.example").get_json()["reported_stats"] is None
        _hist({"t": now - DAY, "torrents": 100}, {"t": now - 60, "torrents": 150, "peers": 3})
        html = page()
        assert "The tracker's own stats" in html and "nyc.example  read" in html and "&middot; opentracker" in html
        assert "torrents         150  ▲50" in html and "reported by the tracker, not measured by this site" in html
        rec = flask_client.get("/api/tracker/nyc.example").get_json()["reported_stats"]
        assert rec["values"] == {"torrents": 150, "peers": 3} and rec["change_24h"] == {"torrents": 50}
        assert rec["software"] == "opentracker"

    def test_tracker_supplied_text_never_reaches_the_page(self, flask_client: FlaskClient) -> None:
        import time
        _hist({"t": int(time.time()), "torrents": 1})
        T.REPSTATS[NYC]["kind"] = "<script>x</script>"
        assert "<script>x" not in flask_client.get("/tracker/nyc.example").get_data(as_text=True)


class TestSameInstance:
    """Two names served by one running opentracker are grouped as the same operator (user's call 2026-10-15), by its
    tracker_id and start time; that feeds the page's 'Same operator as', the rankings and /api/clean's one per operator."""
    NOW = 10 * DAY

    def _read(self, url, tid, start, t=None):
        T.REPSTATS[url] = {"kind": "opentracker", "id": tid, "start": start, "hist": [{"t": t or self.NOW - 3600, "torrents": 1}]}

    def _trk(self, url):
        return SimpleNamespace(url=url)

    def test_same_id_and_start_is_one_instance(self) -> None:
        a, b, c = "udp://a.example:1/announce", "https://b.example:443/announce", "udp://c.example:1/announce"
        self._read(a, 7, 1000)
        self._read(b, 7, 1030)  # read later, uptime a few seconds out: still the same start
        self._read(c, 7, 1000 + 3600)  # same id by chance, started an hour apart: not the same
        assert T._same_instance([self._trk(x) for x in (a, b, c)], now=self.NOW) == [(a, b)]

    def test_stale_or_without_uptime_isnt_used(self) -> None:
        a, b = "udp://a.example:1/announce", "udp://b.example:1/announce"
        self._read(a, 7, 1000, t=self.NOW - 4 * DAY)
        self._read(b, 7, 1000)
        assert T._same_instance([self._trk(a), self._trk(b)], now=self.NOW) == []
        self._read(a, 7, None)
        assert T._same_instance([self._trk(a), self._trk(b)], now=self.NOW) == []

    def test_read_keeps_id_and_start_out_of_the_figures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper, "fetch_stats", lambda url, local=False, path=None: scraper.parse_stats(OT_XML) + ("/stats?mode=everything",))
        T.stats_read(_t(), now=self.NOW)
        rec = T.REPSTATS[NYC]
        assert rec["id"] == 1801921171 and rec["start"] == self.NOW - 3203 and "tracker_id" not in rec["hist"][-1]


@pytest.mark.usefixtures("region_db")
def test_grouped_on_the_page_as_the_same_operator(flask_client: FlaskClient) -> None:
    import time
    now = int(time.time())
    page = lambda: flask_client.get("/api/tracker/nyc.example").get_json()["same_operator_as"]
    assert "udp://akl.example:1/announce" not in page()
    for u in (NYC, "udp://akl.example:1/announce"):
        T.REPSTATS[u] = {"kind": "opentracker", "id": 42, "start": now - 5000, "hist": [{"t": now - 60, "torrents": 1}]}
    assert "udp://akl.example:1/announce" in page()


class TestNote:
    NOW = 100 * DAY

    def test_each_case(self) -> None:
        note = lambda status=1: ntextra._repstats_note(NYC, status, self.NOW)
        assert note()["code"] == "not_read"
        T.REPSTATS[NYC] = {"t": self.NOW - 3 * DAY, "next": self.NOW + 27 * DAY, "fails": 1}
        n = note()
        assert n["code"] == "none" and "asked http://nyc.example:80 for /stats, /stats.json, /metrics" in n["text"]
        assert "asked again in 27 days" in n["text"]
        T.REPSTATS[NYC] = {"t": self.NOW - 4 * DAY, "next": self.NOW + 26 * DAY, "fails": 3, "ok": True,
                           "hist": [{"t": self.NOW - 6 * DAY, "torrents": 1}]}
        assert note()["code"] == "stopped" and "stopped answering" in note()["text"]
        T.REPSTATS[NYC] = {"t": self.NOW - 5 * DAY, "next": self.NOW - 4 * DAY, "fails": 0, "ok": True,
                           "hist": [{"t": self.NOW - 5 * DAY, "torrents": 1}]}
        assert note(status=0)["code"] == "down" and note()["code"] == "stale"

    def test_read_records_when_it_was_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper, "fetch_stats", lambda url, local=False, path=None: None)
        T.stats_read(_t(), now=self.NOW)
        assert T.REPSTATS[NYC]["t"] == self.NOW


@pytest.mark.usefixtures("region_db")
def test_page_and_api_say_why_there_are_none(flask_client: FlaskClient) -> None:
    html = flask_client.get("/tracker/nyc.example").get_data(as_text=True)
    assert "The tracker's own stats" in html and "Not read yet" in html and 'href="/faq#stats"' in html
    rec = flask_client.get("/api/tracker/nyc.example").get_json()
    assert rec["reported_stats"] is None and rec["reported_stats_note"]["code"] == "not_read"
    assert 'id="stats"' in flask_client.get("/faq").get_data(as_text=True)


XBT_METRICS = """xbt_uptime 86400
xbt_infohashes_count 1200
xbt_peers_count{type="seeders"} 300
xbt_peers_count{type="leechers"} 700
xbt_connections_count{proto="tcp",type="accepted"} 5000
xbt_connections_count{proto="tcp",type="slow"} 3
xbt_connections_count{proto="udp",type="received"} 9000
xbt_requests_count{proto="http",method="announce"} 4000
xbt_requests_count{proto="udp",method="announce"} 8000
xbt_requests_count{proto="http",method="scrape"} 10
xbt_requests_count{proto="udp",method="scrape"} 20
"""
XBT_HTML = ("<!DOCTYPE HTML><meta http-equiv=refresh content=60><title>XBT Tracker</title><table><tr><td>peers<td class=ar>1000"
            "<tr><td>seeders<td class=ar>300<td class=ar>30 %<tr><td>leechers<td class=ar>700<td class=ar>70 %"
            "<tr><td>torrents<td class=ar>1,200<tr><td><tr><td>announced<td class=ar>12000<td class=ar>99 %</table>")
CHIHAYA = """# HELP chihaya_storage_infohashes_count The number of Infohashes tracked
# TYPE chihaya_storage_infohashes_count gauge
chihaya_storage_infohashes_count 500
chihaya_storage_seeders_count 120
chihaya_storage_leechers_count 380
chihaya_http_response_duration_milliseconds_count{action="announce",address_family="IPv4",error=""} 1000
chihaya_http_response_duration_milliseconds_count{action="scrape",address_family="IPv4",error=""} 40
chihaya_udp_response_duration_milliseconds_count{action="announce",address_family="IPv6",error=""} 2000
chihaya_udp_response_duration_milliseconds_count{action="announce",address_family="IPv4",error="bad request"} 7
chihaya_udp_response_duration_milliseconds_bucket{action="announce",address_family="IPv4",error="",le="1"} 9999
"""
AQUATIC = """aquatic_torrents{ip_version="4",worker_index="0"} 600
aquatic_torrents{ip_version="6",worker_index="0"} 50
aquatic_peers{ip_version="4",worker_index="0"} 2000
aquatic_requests_total{ip_version="4",worker_index="0"} 30000
aquatic_responses_total{type="announce",ip_version="4",worker_index="0"} 25000
aquatic_responses_total{type="scrape",ip_version="4",worker_index="0"} 100
aquatic_responses_total{type="connect",ip_version="4",worker_index="0"} 4800
"""
WEBTORRENT = {"torrents": 30, "activeTorrents": 20, "peersAll": 90, "peersSeederOnly": 40, "peersLeecherOnly": 45,
              "peersSeederAndLeecher": 5, "peersIPv4": 80, "peersIPv6": 10, "clients": {}}


class TestOtherSoftware:
    def test_xbt_metrics(self) -> None:
        assert scraper.parse_stats(XBT_METRICS.encode()) == ("XBT", {
            "uptime_s": 86400, "torrents": 1200, "seeds": 300, "leechers": 700, "peers": 1000, "announces": 12000,
            "scrapes": 30, "requests": 14000})

    def test_xbt_html_page(self) -> None:
        assert scraper.parse_stats(XBT_HTML.encode()) == ("XBT", {
            "torrents": 1200, "peers": 1000, "seeds": 300, "leechers": 700, "announces": 12000})

    def test_chihaya(self) -> None:
        assert scraper.parse_stats(CHIHAYA.encode()) == ("chihaya", {
            "torrents": 500, "seeds": 120, "leechers": 380, "peers": 500, "requests": 3047, "announces": 3007,
            "scrapes": 40, "refused": 7})

    def test_aquatic(self) -> None:
        assert scraper.parse_stats(AQUATIC.encode()) == ("aquatic", {
            "torrents": 650, "peers": 2000, "requests": 30000, "announces": 25000, "scrapes": 100})

    def test_webtorrent(self) -> None:
        assert scraper.parse_stats(json.dumps(WEBTORRENT).encode()) == ("bittorrent-tracker", {
            "torrents": 30, "peers": 90, "seeds": 45, "leechers": 45})

    def test_unknown_metrics_arent_read(self) -> None:
        assert scraper.parse_stats(b"go_goroutines 12\nprocess_cpu_seconds_total 3.5\n") is None


class TestProbe:
    def _get(self, monkeypatch, answers):
        asked = []

        def get(url, **kw):
            asked.append(url)
            a = answers.get("/" + url.split("/", 3)[3], (404, b""))
            if isinstance(a, Exception):
                raise a
            return SimpleNamespace(status_code=a[0], raw=SimpleNamespace(read=lambda n, decode_content, b=a[1]: b))
        monkeypatch.setattr(scraper.requests, "get", get)
        return asked

    def test_paths_in_turn_until_one_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = self._get(monkeypatch, {"/metrics": (200, XBT_METRICS.encode())})
        got = REAL_FETCH("udp://t.example:6969/announce")
        assert got[0] == "XBT" and got[2] == "/metrics"
        assert asked == ["http://t.example:6969/stats?mode=everything", "http://t.example:6969/stats.json",
                         "http://t.example:6969/metrics"]

    def test_stops_at_the_first_that_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = self._get(monkeypatch, {"/stats?mode=everything": (200, OT_XML)})
        assert REAL_FETCH("udp://t.example:6969/announce")[2] == "/stats?mode=everything" and len(asked) == 1

    def test_nothing_listening_means_one_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = self._get(monkeypatch, {"/stats?mode=everything": scraper.requests.ConnectionError("refused")})
        assert REAL_FETCH("udp://t.example:6969/announce") is None and len(asked) == 1

    def test_a_known_path_is_one_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = self._get(monkeypatch, {"/stats.json": (200, json.dumps(WEBTORRENT).encode())})
        assert REAL_FETCH("https://t.example/announce", path="/stats.json")[0] == "bittorrent-tracker"
        assert asked == ["https://t.example:443/stats.json"]


class TestPathKept:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch: pytest.MonkeyPatch):
        self.paths, self.answer = [], ("XBT", {"torrents": 1}, "/metrics")
        monkeypatch.setattr(scraper, "fetch_stats", lambda url, local=False, path=None: self.paths.append(path) or self.answer)

    def test_daily_reads_use_the_path_that_worked_and_a_retry_looks_again(self) -> None:
        t, b = _t(), 10 * DAY
        T.stats_read(t, now=b)
        assert T.REPSTATS[NYC]["path"] == "/metrics" and T.REPSTATS[NYC]["kind"] == "XBT"
        self.answer = None
        for n in (1, 2, 3):
            T.stats_read(t, now=b + n * DAY)
        T.stats_read(t, now=b + 33 * DAY)  # the 30-day retry
        assert self.paths == [None, "/metrics", "/metrics", "/metrics", None]


def test_a_none_from_the_old_one_path_probe_is_asked_again_now(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(scraper, "fetch_stats", lambda url, local=False, path=None: calls.append(url))
    monkeypatch.setattr(T, "_STATS_NEXT", [0])
    T.REPSTATS[NYC] = {"t": 1000, "next": 1000 + T.STATS_RETRY, "fails": 1}  # before the version mark
    T.stats_due([_t()], now=2000)
    assert calls == [NYC] and T.REPSTATS[NYC]["v"] == T.STATS_PROBE_V and T.REPSTATS[NYC]["next"] == 2000 + T.STATS_RETRY
    T._STATS_NEXT[0] = 0
    T.stats_due([_t()], now=3000)
    assert calls == [NYC]  # this time it waits the 30 days
