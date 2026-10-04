"""Per-tracker page (/tracker/<host>) and the latency history behind it."""

from __future__ import annotations

from time import time

import pytest
from flask.testing import FlaskClient

from newtrackon import ntextra
from newtrackon import tracker as T

from .test_regions import region_db  # noqa: F401  (fixture)


@pytest.mark.usefixtures("region_db")
class TestTrackerPage:
    def test_page_for_a_listed_tracker(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/tracker/akl.example")
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        # the heading carries <wbr> break hints so long URLs wrap on phones
        assert "udp://akl.example:1/announce" in html.replace("<wbr>", "") and "Uptime, last 8 days" in html and "Last 48 hours" in html
        assert 'class="fi fi-nz"' in html and 'href="/#q=akl.example"' in html

    def test_host_is_case_insensitive(self, flask_client: FlaskClient) -> None:
        assert flask_client.get("/tracker/AKL.example").status_code == 200

    def test_unknown_host_is_404(self, flask_client: FlaskClient) -> None:
        assert flask_client.get("/tracker/nope.example").status_code == 404

    def test_latency_chart_and_events(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        now = int(time())
        url = "udp://akl.example:1/announce"
        monkeypatch.setattr(T, "LAT_HIST", {url: {"Oceania": [[now - 86400 * 3, 40], [now - 21600, 42], [now - 14400, 43], [now - 7200, 45]],
                                                  "Europe": [[now - 3600, 280]]}})
        monkeypatch.setattr(T, "EVENTS", [{"t": now - 600, "url": url, "host": "akl.example", "type": "down", "text": "went Down (<b>timeout</b>)"},
                                          {"t": now - 60, "url": "udp://other.example:1/announce", "host": "other.example", "type": "up", "text": "other"}])
        html = flask_client.get("/tracker/akl.example").get_data(as_text=True)
        assert html.count("<polyline") == 1 and html.count("<circle") == 2  # Oceania: lone old sample (dot) + a line; Europe: one dot
        assert "From Oceania: 45 ms now" in html and "From Europe: 280 ms now" in html
        assert "went Down (&lt;b&gt;timeout&lt;/b&gt;)" in html and "other" not in html.split("Recent events")[1]  # escaped; only its own events

    def test_table_and_map_link_to_it(self, flask_client: FlaskClient) -> None:
        assert 'href="/tracker/akl.example"' in flask_client.get("/").get_data(as_text=True)
        assert "'/tracker/' + encodeURIComponent" in flask_client.get("/static/js/map.js").get_data(as_text=True)


class TestUptimeDays:
    def test_exact_values(self) -> None:
        h = [0] * 48 + [1] * 24 + [0] * 24 + [1] * 48
        assert ntextra._uptime_days(h) == [{"ago": 2, "pct": 0}, {"ago": 1, "pct": 50}, {"ago": 0, "pct": 100}]

    def test_limit_and_short_history(self) -> None:
        assert len(ntextra._uptime_days([1] * 48 * 40)) == 30
        assert ntextra._uptime_days([1] * 47) == []


class TestLatencyHistory:
    URL = "udp://a.example:1/announce"

    def test_one_sample_per_step_per_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(T, "REGION_LAT", {self.URL: {"Oceania": 40, "Asia": 120}})
        base = 1_800_000_000 - 1_800_000_000 % T.LAT_HIST_STEP
        T._lat_hist_add(self.URL, base + 10)
        T._lat_hist_add(self.URL, base + 3000)  # same 2 h slot: ignored
        T.REGION_LAT[self.URL] = {"Oceania": 50, "Asia": 130}
        T._lat_hist_add(self.URL, base + T.LAT_HIST_STEP + 5)
        assert T.LAT_HIST[self.URL] == {"Oceania": [[base, 40], [base + T.LAT_HIST_STEP, 50]],
                                        "Asia": [[base, 120], [base + T.LAT_HIST_STEP, 130]]}
        assert T._jload("data/lat_hist.json") == T.LAT_HIST

    def test_seeded_from_recent_samples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = 1_800_000_000
        monkeypatch.setattr(T, "REGION_SAMPLES", {self.URL: {"Oceania": [[now - 20000, 30], [now - 19000, 31], [now - 9000, 35]]}})
        monkeypatch.setattr(T, "REGION_LAT", {self.URL: {"Oceania": 33}})
        T._lat_hist_add(self.URL, now)
        ts = [x[0] for x in T.LAT_HIST[self.URL]["Oceania"]]
        assert ts == sorted(set(ts)) and len(ts) >= 3 and all(t % T.LAT_HIST_STEP == 0 for t in ts)

    def test_old_samples_and_removed_trackers_pruned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = 1_800_000_000
        old = now - (T.LAT_HIST_DAYS + 1) * 86400
        monkeypatch.setattr(T, "LAT_HIST", {"udp://gone.example:1/announce": {"Asia": [[old, 99]]},
                                            self.URL: {"Oceania": [[old, 10]]}})
        monkeypatch.setattr(T, "REGION_LAT", {self.URL: {"Oceania": 40}})
        T._lat_hist_add(self.URL, now)
        assert T.LAT_HIST == {self.URL: {"Oceania": [[now - now % T.LAT_HIST_STEP, 40]]}}

    def test_region_set_records_history(self) -> None:
        T._region_set(self.URL, {"Oceania: Sydney": 44})
        assert T.LAT_HIST[self.URL]["Oceania"][-1][1] == 44
