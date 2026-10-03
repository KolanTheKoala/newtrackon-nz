"""Region filters: 'located in' (IP country -> region) and 'fast from' (measured latency from the region's probe points)."""

from __future__ import annotations

import json
import re
import sqlite3
from time import time

import pytest
from flask.testing import FlaskClient

from newtrackon import regions as R

# Country codes stored on the live site when this was written: all must map to a region.
LIVE_CODES = "nl no fr nz de us ru tw ca fi lu ch lv sg cn hk kr za se cl br tr ua".split()


class TestCountryTable:
    def test_live_codes_all_map(self) -> None:
        assert [c for c in LIVE_CODES if c not in R.COUNTRY_REGION] == []

    def test_codes_are_well_formed_and_unique(self) -> None:
        assert all(re.fullmatch(r"[a-z]{2}", c) for c in R.COUNTRY_REGION)
        assert not set(R.COUNTRY_REGION) & R.NO_REGION
        assert set(R.COUNTRY_REGION.values()) == set(R.REGIONS)

    @pytest.mark.parametrize(
        ("cc", "region"),
        [
            ("us", "americas"), ("br", "americas"), ("cl", "americas"),  # South America routes via North America
            ("de", "europe"), ("ru", "europe"), ("tr", "europe"), ("ae", "europe"), ("za", "europe"), ("kz", "europe"),
            ("sg", "asia-pacific"), ("in", "asia-pacific"), ("nz", "asia-pacific"), ("au", "asia-pacific"),
        ],
    )
    def test_routing_regions(self, cc: str, region: str) -> None:
        assert R.COUNTRY_REGION[cc] == region

    def test_regions_of(self) -> None:
        assert R.regions_of(["NZ", "nz"]) == {"asia-pacific"}
        assert R.regions_of(["us", "de"]) == {"americas", "europe"}  # multi-homed: in both
        assert R.regions_of(["aq", "", "zz"]) == set()  # no region / unknown: never an error
        assert R.regions_of(None) == set()


class TestParse:
    def test_no_args_is_no_filter(self) -> None:
        f = R.parse_region_filter({})
        assert f == R.NO_FILTER and not f.active

    def test_region_list_case_insensitive(self) -> None:
        f = R.parse_region_filter({"region": "Europe, asia-pacific"})
        assert f.located_in == {"europe", "asia-pacific"} and f.active

    @pytest.mark.parametrize("args", [{"region": "oceania"}, {"region": "europe,mars"}, {"region": "asia"}])
    def test_unknown_region_rejected_with_valid_names(self, args: dict[str, str]) -> None:
        with pytest.raises(ValueError, match="Valid regions: americas, europe, asia-pacific"):
            R.parse_region_filter(args)

    @pytest.mark.parametrize("value", ["mars", "north-america", "apac"])
    def test_unknown_fast_from_rejected_with_valid_names(self, value: str) -> None:
        with pytest.raises(ValueError, match="Valid: americas, europe, asia, oceania"):
            R.parse_region_filter({"fast_from": value})

    @pytest.mark.parametrize("value", ["americas", "europe", "asia", "oceania", "asia-pacific"])
    def test_fast_from_values(self, value: str) -> None:
        assert R.parse_region_filter({"fast_from": value}).fast_from == value

    @pytest.mark.parametrize("ms", ["abc", "0", "2001", "-5"])
    def test_bad_fast_from_ms(self, ms: str) -> None:
        with pytest.raises(ValueError, match="fast_from_ms"):
            R.parse_region_filter({"fast_from": "europe", "fast_from_ms": ms})


LAT = {
    "udp://ams.example:1/announce": {"Europe": 10, "North America": 90, "Asia": 160, "Oceania": 280},
    "udp://sgp.example:1/announce": {"Europe": 160, "North America": 180, "Asia": 5, "Oceania": 140},
    "udp://akl.example:1/announce": {"Europe": 280, "North America": 130, "Asia": 120, "Oceania": 3},
    "udp://half.example:1/announce": {"Europe": 20},  # only measured from Europe so far
}


class TestMatches:
    def test_located_in(self) -> None:
        f = R.parse_region_filter({"region": "asia-pacific"})
        assert f.matches("udp://akl.example:1/announce", ["nz"], LAT)
        assert not f.matches("udp://ams.example:1/announce", ["nl"], LAT)
        assert not f.matches("udp://x.example:1/announce", None, LAT)

    def test_fast_from_default_150_for_europe(self) -> None:
        f = R.parse_region_filter({"fast_from": "europe"})
        assert f.matches("udp://ams.example:1/announce", [], LAT)
        assert not f.matches("udp://sgp.example:1/announce", [], LAT)  # 160 ms

    def test_fast_from_asia_and_oceania_are_separate(self) -> None:
        asia, oce = R.parse_region_filter({"fast_from": "asia"}), R.parse_region_filter({"fast_from": "oceania"})
        assert asia.matches("udp://sgp.example:1/announce", [], LAT) and oce.matches("udp://sgp.example:1/announce", [], LAT)
        assert asia.matches("udp://akl.example:1/announce", [], LAT)  # 120 ms from Asia: under 150
        assert not asia.matches("udp://ams.example:1/announce", [], LAT)  # 160 ms from Asia
        assert oce.matches("udp://akl.example:1/announce", [], LAT)  # 3 ms from Oceania
        assert not oce.matches("udp://ams.example:1/announce", [], LAT)  # 280 ms from Oceania

    def test_legacy_asia_pacific_needs_both_points_250_default(self) -> None:
        f = R.parse_region_filter({"fast_from": "asia-pacific"})
        assert f.matches("udp://sgp.example:1/announce", [], LAT)  # Asia 5, Oceania 140
        assert f.matches("udp://akl.example:1/announce", [], LAT)  # Asia 120, Oceania 3
        assert not f.matches("udp://ams.example:1/announce", [], LAT)  # Oceania 280

    def test_fast_from_ms_overrides_default(self) -> None:
        f = R.parse_region_filter({"fast_from": "asia-pacific", "fast_from_ms": "130"})
        assert f.matches("udp://akl.example:1/announce", [], LAT)
        assert not f.matches("udp://sgp.example:1/announce", [], LAT)  # Oceania 140

    def test_unmeasured_point_is_not_fast(self) -> None:
        assert not R.parse_region_filter({"fast_from": "americas"}).matches("udp://half.example:1/announce", [], LAT)
        assert not R.parse_region_filter({"fast_from": "europe"}).matches("udp://new.example:1/announce", [], LAT)

    def test_both_filters_combine(self) -> None:
        f = R.parse_region_filter({"region": "europe", "fast_from": "americas"})
        assert f.matches("udp://ams.example:1/announce", ["nl"], LAT)  # in Europe, 90 ms from North America
        assert not f.matches("udp://akl.example:1/announce", ["nz"], LAT)


# ---- through the API ----
TRACKERS = [  # url, country codes, score, status
    ("udp://ams.example:1/announce", ["nl"], 95, 1),
    ("udp://sgp.example:1/announce", ["sg"], 96, 1),
    ("udp://akl.example:1/announce", ["nz"], 97, 1),
    ("http://nyc.example:80/announce", ["us"], 98, 1),
    ("udp://down.example:1/announce", ["de"], 99, 0),
]


@pytest.fixture
def region_db(mock_db_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    from newtrackon import tracker as T

    old = int(time()) - 30 * 86400
    for i, (url, cc, score, status) in enumerate(TRACKERS):
        host = url.split("/")[2].split(":")[0]
        mock_db_connection.execute(
            "INSERT INTO status VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (host, url, json.dumps([f"93.184.216.{i + 1}"]), 50 + i, int(time()), 1800, status, score,
             json.dumps(["x"]), json.dumps(cc), json.dumps(["isp"]), old, json.dumps([1] * 400), 0, int(time()), json.dumps({})),
        )
    mock_db_connection.commit()
    lat = dict(LAT)
    lat["http://nyc.example:80/announce"] = {"Europe": 80, "North America": 5, "Asia": 200, "Oceania": 150}
    monkeypatch.setattr(T, "REGION_LAT", lat)
    return mock_db_connection


def _urls(resp) -> list[str]:  # type: ignore[no-untyped-def]
    return [l for l in resp.get_data(as_text=True).split() if l]


@pytest.mark.usefixtures("region_db")
class TestApi:
    def test_no_new_params_unchanged(self, flask_client: FlaskClient) -> None:
        for path in ("/api/all", "/api/stable?min_age_days=0", "/api/90", "/api/live", "/api/udp", "/api/http"):
            plain = flask_client.get(path).get_data()
            sep = "&" if "?" in path else "?"
            assert flask_client.get(f"{path}{sep}region=&fast_from=").get_data() == plain, path

    def test_all_located_in(self, flask_client: FlaskClient) -> None:
        assert _urls(flask_client.get("/api/all?region=asia-pacific")) == [
            "udp://akl.example:1/announce", "udp://sgp.example:1/announce"]
        assert _urls(flask_client.get("/api/all?region=americas,europe")) == [
            "http://nyc.example:80/announce", "udp://ams.example:1/announce", "udp://down.example:1/announce"]

    def test_stable_fast_from(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/api/stable?min_age_days=0&fast_from=asia-pacific")
        assert set(_urls(r)) == {"udp://akl.example:1/announce", "udp://sgp.example:1/announce", "http://nyc.example:80/announce"}

    def test_filtered_list_is_subset(self, flask_client: FlaskClient) -> None:
        full = set(_urls(flask_client.get("/api/all")))
        for q in ("region=europe", "fast_from=europe", "region=americas&fast_from=americas&fast_from_ms=20"):
            assert set(_urls(flask_client.get(f"/api/all?{q}"))) <= full

    def test_udp_and_http_lists(self, flask_client: FlaskClient) -> None:
        assert _urls(flask_client.get("/api/udp?region=europe")) == ["udp://ams.example:1/announce"]
        assert _urls(flask_client.get("/api/http?fast_from=americas")) == ["http://nyc.example:80/announce"]

    @pytest.mark.parametrize("q", ["region=oceania", "fast_from=mars", "fast_from=europe&fast_from_ms=fast"])
    @pytest.mark.parametrize("path", ["/api/all", "/api/stable", "/api/udp", "/api/clean", "/api/details"])
    def test_invalid_values_400(self, flask_client: FlaskClient, path: str, q: str) -> None:
        r = flask_client.get(f"{path}?{q}")
        assert r.status_code == 400
        assert r.headers["Access-Control-Allow-Origin"] == "*"

    def test_details_has_regions_and_filters(self, flask_client: FlaskClient) -> None:
        d = flask_client.get("/api/details").get_json()
        assert {t["url"]: t["regions"] for t in d}["udp://akl.example:1/announce"] == ["asia-pacific"]
        f = flask_client.get("/api/details?region=americas").get_json()
        assert [t["url"] for t in f] == ["http://nyc.example:80/announce"]

    def test_clean_region(self, flask_client: FlaskClient) -> None:
        r = flask_client.get("/api/clean?min_age_days=0&region=asia-pacific")
        assert set(_urls(r)) == {"udp://akl.example:1/announce", "udp://sgp.example:1/announce"}
