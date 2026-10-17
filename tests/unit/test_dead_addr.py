"""A published address (DNS record) that doesn't answer makes a tracker Up/Broken, out of the clean lists; slow trackers
lose more points (2026-10-17)."""

from __future__ import annotations

import copy
import socket
from collections import deque
from time import time
from types import SimpleNamespace

import pytest

from newtrackon import ntextra, scraper
from newtrackon import tracker as T

_ADDR_SWEEP = scraper.addr_sweep  # the real one (conftest stubs it out for every other test)
H, P = "multi.example", 6969
U = f"udp://{H}:{P}/announce"


def _tracker(sample_tracker, ips):
    t = sample_tracker
    t.url, t.ips = U, ips
    t.added, t.last_uptime, t.status, t.historic = int(time()) - 30 * 86400, int(time()), 1, deque([1] * 1440, maxlen=1440)
    return t


def _health(**ips):
    now = int(time())
    scraper.ADDR_HEALTH[f"{H}:{P}"] = {ip.replace("_", "."): ({"ok": now, "fails": 0} if ok else {"ok": 0, "fails": 3, "t": now})
                                       for ip, ok in ips.items()}


class TestDeadAddress:
    def test_one_dead_record_of_the_same_family_is_up_broken(self, sample_tracker) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1", "192.0.2.2"])
        _health(**{"192_0_2_1": True, "192_0_2_2": False})
        t.update_uptime()
        st, bad = T.status_rule(t)
        assert st == "up_broken" and bad == ["dead_addr"] and t.uptime <= 50
        assert T.feed_state(t)[1] == ["its published address 192.0.2.2 doesn't answer (it answers on its others)"]
        assert T._nt_bad_lbl(T.feed_state(t)[1]) == "Up/Broken" and T._broken("up_bad", T.feed_state(t)[1])
        assert T._junk_broken_now(t) == "one of its addresses doesn't answer"

    def test_a_dead_record_of_the_other_family_counts_too(self, sample_tracker) -> None:
        # ibksturm.synology.me: its only IPv4 address never answered, its IPv6 one does
        t = _tracker(sample_tracker, ["2001:db8::1", "192.0.2.2"])
        scraper.ADDR_HEALTH["other.example:1"] = {"192.0.2.50": {"ok": int(time()), "fails": 0}}  # IPv4 works here
        scraper.ADDR_HEALTH[f"{H}:{P}"] = {"2001:db8::1": {"ok": int(time()), "fails": 0}, "192.0.2.2": {"ok": 0, "fails": 4}}
        assert T.status_rule(t) == ("up_broken", ["dead_addr"])

    def test_not_while_this_server_reaches_no_one_on_that_family(self, sample_tracker) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1", "2001:db8::2"])
        _health(**{"192_0_2_1": True})
        scraper.ADDR_HEALTH[f"{H}:{P}"]["2001:db8::2"] = {"ok": 0, "fails": 5}
        assert T._dead_addrs(t) == []  # no IPv6 answer anywhere: our own IPv6 is down, not the tracker's

    def test_a_confirmed_dead_family_is_not_counted_twice(self, sample_tracker) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1", "2001:db8::2"])
        scraper.ADDR_HEALTH["v6.example:1"] = {"2001:db8::99": {"ok": int(time()), "fails": 0}}
        scraper.ADDR_HEALTH[f"{H}:{P}"] = {"192.0.2.1": {"ok": int(time()), "fails": 0}, "2001:db8::2": {"ok": 0, "fails": 5}}
        T.FAM_FAILS[U] = {"n": 3, "fam": "v6"}
        assert T.status_rule(t) == ("up_broken", ["dead_ipv6"])

    def test_two_fails_or_a_recent_answer_is_not_dead(self, sample_tracker) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1", "192.0.2.2"])
        _health(**{"192_0_2_1": True})
        scraper.ADDR_HEALTH[f"{H}:{P}"]["192.0.2.2"] = {"ok": 0, "fails": 2}
        assert T._dead_addrs(t) == []
        scraper.ADDR_HEALTH[f"{H}:{P}"]["192.0.2.2"] = {"ok": int(time()) - 600, "fails": 3}
        assert T._dead_addrs(t) == []

    def test_none_answering_is_not_this_rule(self, sample_tracker) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1", "192.0.2.2"])
        _health(**{"192_0_2_1": False, "192_0_2_2": False})
        assert T._dead_addrs(t) == []

    def test_left_out_of_the_clean_lists(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1", "192.0.2.2"])
        _health(**{"192_0_2_1": True, "192_0_2_2": False})
        t.update_uptime()
        monkeypatch.setattr(ntextra, "_trackers", lambda: [t])
        assert not ntextra.quality_filter({"good": "true"})(U)


class TestSweep:
    @pytest.fixture(autouse=True)
    def _real_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper, "addr_sweep", _ADDR_SWEEP)

    def _gai(self, monkeypatch: pytest.MonkeyPatch, ips: list[str]) -> None:
        rows = [((socket.AF_INET6 if ":" in ip else socket.AF_INET), socket.SOCK_DGRAM, 17, "", (ip, P)) for ip in ips]
        monkeypatch.setattr(scraper.socket, "getaddrinfo", lambda host, port, fam=0, st=0, *a: [r for r in rows if fam in (0, r[0])])
        monkeypatch.setattr(scraper, "ip_is_public", lambda ip: True)

    def test_asks_every_address_even_the_failing_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._gai(monkeypatch, ["192.0.2.1", "192.0.2.2", "2001:db8::3"])
        asked: list[str] = []

        def session(fam, sa, src):
            asked.append(sa[0])
            if sa[0] == "192.0.2.2":
                raise TimeoutError
            return SimpleNamespace(close=lambda: None), (lambda *a: {"interval": 1800})
        monkeypatch.setattr(scraper, "_udp_session", session)
        scraper.ADDR_HEALTH[f"{H}:{P}"] = {"192.0.2.2": {"ok": 0, "fails": 2}}
        scraper.addr_sweep(U)
        h = scraper.ADDR_HEALTH[f"{H}:{P}"]
        assert sorted(asked) == ["192.0.2.1", "192.0.2.2", "2001:db8::3"]
        assert h["192.0.2.2"]["fails"] == 3 and h["192.0.2.1"]["fails"] == 0 and h["2001:db8::3"]["ok"]

    def test_one_address_or_a_fresh_answer_is_not_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[str] = []
        monkeypatch.setattr(scraper, "_udp_session", lambda fam, sa, src: asked.append(sa[0]) or (_ for _ in ()).throw(TimeoutError))
        self._gai(monkeypatch, ["192.0.2.1"])
        scraper.addr_sweep(U)
        self._gai(monkeypatch, ["192.0.2.1", "192.0.2.2"])
        scraper.ADDR_HEALTH[f"{H}:{P}"] = {"192.0.2.1": {"ok": int(time()), "fails": 0}}
        scraper.addr_sweep(U)
        assert asked == ["192.0.2.2"]


class TestLatency:
    def test_steeper_penalty(self) -> None:
        assert T._nt_lat_penalty(150) == 0
        assert T._nt_lat_penalty(300) == pytest.approx(6)
        assert T._nt_lat_penalty(500) == pytest.approx(18)
        assert T._nt_lat_penalty(2000) == T.LAT_PEN_MAX == 45

    def test_a_reliable_400_ms_tracker_drops_under_90(self) -> None:
        assert 100 - T._nt_lat_penalty(400) < 90

    def test_penalty_uses_the_shown_latency_when_regions_are_missing(self, sample_tracker) -> None:
        # ibksturm: 1002 ms, only Europe measured in the last 48 h: it was 'Latency penalty: none', score 98
        t = _tracker(sample_tracker, ["192.0.2.1"])
        t.latency = 1002
        T.REGION_LAT[U] = {"Europe": 331}
        t.update_uptime()
        assert T.SCORE_PEN[U][0] == T.LAT_PEN_MAX and t.uptime <= 100 - T.LAT_PEN_MAX

    def test_slow_fault_box_shows_the_average(self, sample_tracker, monkeypatch: pytest.MonkeyPatch) -> None:
        t = _tracker(sample_tracker, ["192.0.2.1"])
        T.REGION_LAT[U] = {"Oceania": 737, "Asia": 670, "Europe": 331, "North America": 500}
        monkeypatch.setattr(ntextra, "_fix_base", lambda t: "slow")
        out = ntextra._evidence(t, {})
        assert "Average latency 560 ms across 4 regions." in out
        T.REGION_LAT[U] = {"Europe": 331}
        t.latency = 1002
        assert ntextra._evidence(t, {})[0].startswith("Average latency 1002 ms from this site's server")

    def test_region_latency_falls_back_to_the_other_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper, "_exits", lambda: {"ch": {"region": "Europe", "location": "Zurich", "src4": "10.0.0.4", "src6": "fd00::4"},
                                                         "us": {"region": "North America", "location": "Dallas", "src4": "10.0.0.5", "src6": "fd00::5"}})
        monkeypatch.setattr(scraper.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 2, 17, "", ("203.0.113.9", 6969))])

        def via(url, fam, src):
            if fam == socket.AF_INET and src != "10.0.0.4":  # IPv4 answers only from Europe
                raise TimeoutError
            return 300.0
        monkeypatch.setattr(scraper, "_rtt_via", via)
        monkeypatch.setattr(scraper, "_tunnel_rtt", lambda src, fam: 20.0)
        out = scraper.region_latency(U, {"v4": True, "v6": True})
        assert out == {"Oceania: Wellington": 300, "Europe: Zurich": 280, "North America: Dallas": 280}


class TestRescoreAtStart:
    def test_caps_and_penalties_apply_without_waiting_for_a_check(self, sample_tracker) -> None:
        # after the deploy: Up/Broken trackers still showed 100, ibksturm (1002 ms) had no latency penalty
        t = _tracker(sample_tracker, ["192.0.2.1", "192.0.2.2"])
        t.uptime = 100.0
        _health(**{"192_0_2_1": True, "192_0_2_2": False})
        slow = copy.copy(t)
        slow.url, slow.ips, slow.latency, slow.uptime = "https://slow.example:6969/announce", [], 1002, 98.0
        steady = copy.copy(t)
        steady.url, steady.ips, steady.latency = "udp://steady.example:1/announce", [], 50
        steady.update_uptime()
        before, last = steady.uptime, steady.last_checked
        changed = T.rescore_all([t, slow, steady])
        assert t.uptime <= 50 and T.status_rule(t)[0] == "up_broken"
        assert slow.uptime == 100 - T.LAT_PEN_MAX and T.SCORE_PEN[slow.url][0] == T.LAT_PEN_MAX
        assert changed == [t, slow] and steady.uptime == before and steady.last_checked == last
        assert not t.to_be_deleted and not getattr(t, "_nt_no_removal")
