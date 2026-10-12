"""Simulation: the real per-check code (Tracker.update_status and everything it calls) driven through scripted timelines,
with the network replaced by a scripted tracker and the clock by a variable. The outcomes are checked against what the
rules say should happen, not against the code's own clocks."""

from __future__ import annotations

from collections import deque

import pytest

from newtrackon import scraper
from newtrackon import tracker as T
from newtrackon.tracker import Tracker

from .test_regions import region_db  # noqa: F401  (fixture)

DAY, HOUR = 86400, 3600
START = 1_800_000_000 - 1_800_000_000 % 1800
SIM_IP = "93.184.216.34"
SEEN_OK = {"a": "ok", "a_ip": None, "self": False, "foreign": 0, "seeds": 1, "leech": 1}


def _probe_result(mode, fam, n):
    """What the peer test sees on one family: (ok, probe_extra)."""
    if mode == "ok":
        return True, {"fam": fam, "foreign": 0, "seen": dict(SEEN_OK), "split": False}
    if mode == "none":
        return False, {"fam": fam, "foreign": 0, "split": False,
                       "seen": {"a": None, "a_ip": None, "self": False, "foreign": 0, "seeds": 0, "leech": 1}}
    if mode == "split":  # separate servers: every other test reaches the one that saw client A
        if n % 2 == 0:
            return True, {"fam": fam, "foreign": 0, "seen": dict(SEEN_OK), "split": False}
        return False, {"fam": fam, "foreign": 0, "split": True,
                       "seen": {"a": None, "a_ip": None, "self": True, "foreign": 0, "seeds": 0, "leech": 1}}
    if mode == "rare":  # separate servers that rarely meet: 1 test in 4 passes
        if n % 4 == 0:
            return True, {"fam": fam, "foreign": 0, "seen": dict(SEEN_OK), "split": False}
        return False, {"fam": fam, "foreign": 0, "split": True,
                       "seen": {"a": None, "a_ip": None, "self": True, "foreign": 0, "seeds": 0, "leech": 1}}
    if mode == "nat":
        return False, {"fam": fam, "foreign": 0, "split": False, "nat_ip": "172.17.0.1",
                       "seen": {"a": "private", "a_ip": "172.17.0.1", "self": False, "foreign": 0, "seeds": 1, "leech": 1}}
    if mode == "fake":
        return True, {"fam": fam, "foreign": 2, "split": False, "seen": dict(SEEN_OK, foreign=2)}
    if mode == "na":  # no VPN exit got an answer: a limit of the test
        return None, {"fam": fam, "exit_blocked": True}
    if mode == "inconclusive":  # only our first client is ever answered: the test can't show it shares peers
        return None, {"fam": fam}
    raise ValueError(mode)


_REAL_LOCAL_FAULT = T._nt_local_fault


class Sim:
    """One simulated tracker. world(day) -> {"answers": bool, "peers": mode, "peers_v6": mode, "dead": {"v6"}}."""

    def __init__(self, monkeypatch, world, fams=("v4",), url="udp://sim.example:6969/announce", added_days=30):
        self.world, self.fams, self.now, self.n = world, fams, START, {}
        self.mp = monkeypatch
        self.tr = self._tracker(url, added_days)
        self.log: list = []  # (time, state, codes) at every check
        self.cid: list = []  # when the made-up connection ID was sent
        self.removed = None  # (time, reason)
        mp = monkeypatch
        mp.setattr(T, "time", lambda: self.now)
        from newtrackon import ingest
        mp.setattr(ingest, "time", lambda: self.now)
        mp.setattr(scraper, "get_bep_34", lambda host: (False, None))
        mp.setattr(Tracker, "update_ips", lambda tr: setattr(tr, "ips", [SIM_IP]))
        mp.setattr(Tracker, "update_ipapi_data", lambda tr: None)
        mp.setattr(scraper, "announce_udp", lambda u: (self._announce(), SIM_IP))
        mp.setattr(scraper, "announce_http", lambda u: self._announce())
        mp.setattr(scraper, "peer_probe", self._peer_probe)
        mp.setattr(scraper, "peer_probe_family", self._peer_probe_family)
        mp.setattr(scraper, "peer_probe_exits", self._peer_probe_exits)
        mp.setattr(scraper, "family_probe", lambda u: {f: f not in (self.state().get("dead") or ()) for f in self.fams})
        mp.setattr(scraper, "region_latency", lambda u, fams=None: {"Oceania": 30, "Asia": 60, "Europe": 90, "North America": 80})
        mp.setattr(T, "_delayed_maybe_start", lambda u, f: False)
        mp.setattr(T, "_monitor_online", lambda: True)
        # the real local-fault logic: only what it asks the network is scripted (world "elsewhere": our test clients
        # abroad get an answer while this server doesn't)
        mp.setattr(T, "_nt_local_fault", _REAL_LOCAL_FAULT)  # conftest replaces it for every test
        mp.setattr(T, "_nt_vps_retry_ok", lambda u: False)
        mp.setattr(T, "_nt_reachable_elsewhere", lambda u: bool(self.state().get("elsewhere")))
        mp.setattr(T, "_nt_public_dns_has", lambda h: False)
        mp.setattr(T, "_nt_signal", lambda kind, host: None)

    def _tracker(self, url, added_days):
        from urllib.parse import urlparse
        return Tracker(url=url, host=urlparse(url).hostname, ips=[SIM_IP], latency=30, last_checked=self.now - HOUR,
                       interval=1800, status=1, uptime=100.0, countries=["Sim"], country_codes=["xx"], networks=["Sim ISP"],
                       historic=deque([1] * 1440, maxlen=1440), added=self.now - added_days * DAY,
                       last_downtime=self.now - 40 * DAY, last_uptime=self.now - HOUR)

    def day(self):
        return (self.now - START) / DAY

    def state(self):
        s = {"answers": True, "peers": "ok"}
        s.update(self.world(self.day()))
        return s

    def _main_fam(self):
        dead = self.state().get("dead") or ()
        return next((f for f in self.fams if f not in dead), self.fams[0])

    def _announce(self):
        s = self.state()
        proto = "UDP" if self.tr.url.startswith("udp") else "HTTP"
        if s.get("rejects"):  # its own error message to every client (a whitelist: "info hash is not authorized")
            raise RuntimeError(("Error while announcing: %s" if proto == "UDP" else "Tracker error message: %s") % s["rejects"])
        if not s["answers"] or all(f in (s.get("dead") or ()) for f in self.fams):
            # world "refused": it refuses this server (a firewall REJECT) rather than ignoring it; "err": what this server
            # gets instead (a CDN's "HTTP 403", its own "you're banned")
            raise RuntimeError(s.get("err") or ("%s connection failed" % proto if s.get("refused") else "UDP timeout"))
        return {"interval": 1800, "leechers": 0, "seeds": 1, "peers": []}

    def _fam_mode(self, fam):
        s = self.state()
        return s.get("peers_" + fam, s["peers"])

    def _result(self, fam):
        k = self.n[fam] = self.n.get(fam, -1) + 1
        return _probe_result(self._fam_mode(fam), fam, k)

    def _peer_probe(self, only_family=None, cid=False):
        ok, extra = self._result(self._main_fam())
        if cid:  # the once-a-day made-up connection ID: refused, as BEP 15 asks
            self.cid.append(self.now)
            extra = dict(extra, cid_ok=True)
        scraper.rtt.probe_extra = extra
        self.latest = (ok, extra)
        return ok

    def _peer_probe_exits(self, url):
        """Both test clients abroad (it answers them, not this server): the same peer behaviour as from here."""
        if not self.state().get("elsewhere"):
            scraper.rtt.probe_extra = {"exit_blocked": True}
            self.latest = (None, {"exit_blocked": True})
            return None
        ok, extra = self._result(self._main_fam())
        extra = dict(extra, exits_only=True)
        scraper.rtt.probe_extra = extra
        self.latest = (ok, extra)
        return ok

    def _peer_probe_family(self, url, fam):
        if fam in (self.state().get("dead") or ()):
            scraper.rtt.family_extra = {}
            return None
        ok, extra = self._result(fam)
        scraper.rtt.family_extra = extra
        return ok

    def check(self):
        self.latest = (None, {})  # this check's main peer test, if it runs (not while it's down)
        self.tr.update_status()
        st, codes = T.status_rule(self.tr)
        self.log.append((self.now, st, codes))
        if self.tr.to_be_deleted:
            self.removed = (self.now, T.REMOVED[self.tr.host]["reason"])

    def run(self, days, step=300):
        end = self.now + int(days * DAY)
        while self.now < end and not self.removed:
            if self.now - self.tr.last_checked > self.tr.interval:
                self.check()
            self.now += step
        return self

    def resubmit(self, reinstating, url=None, listed=(), expect=True):
        """Its URL is checked again through the real ingest.process_new_tracker: 'Check again now' or a plain submission.
        The submission check from here gets an answer if the world gives this server one; our test clients abroad get
        one in world 'elsewhere'. expect: whether it should be accepted."""
        from unittest.mock import patch
        from newtrackon import ingest
        url = url or self.tr.url
        self.mp.setattr(ingest, "time", lambda: self.now)
        cand = Tracker.from_url(url)
        if reinstating:
            ingest.REINSTATE[cand.host] = self.now
        s = self.state()
        here = {"return_value": (1800, url, 30)} if s["answers"] and not s.get("rejects") else {"side_effect": RuntimeError}
        abroad = ({"interval": 1800, "leechers": 0, "seeds": 1, "peers": []}, 120, "Europe: Frankfurt") if s.get("elsewhere") else None
        with patch("newtrackon.ingest.db.get_all_data", return_value=list(listed)), \
                patch("newtrackon.ingest.attempt_submitted", **here), \
                patch("newtrackon.scraper.announce_via_exits", return_value=abroad), \
                patch("newtrackon.ingest.db.insert_new_tracker") as ins, patch("newtrackon.ingest.db.delete_tracker"):
            ingest.process_new_tracker(cand)
        if not expect:
            assert not ins.called, "accepted"
            return None
        assert ins.called, "not accepted"
        self.tr, self.removed, self.n = cand, None, {}
        return cand

    def restart(self):
        """The app restarts: every store is read back from the files it wrote; what's only in memory is gone."""
        import copy
        url = self.tr.url
        files = [n for n, s in T._STORES.items() if s["file"]]
        before = {n: copy.deepcopy(getattr(T, n).get(url)) for n in files}
        for name in T._STORES:
            self.mp.setattr(T, name, T._store_load(name))  # its file's contents, or empty (memory, probe state)
        T._probe_load()
        self.mp.setattr(T, "REMOVED", T._jload(T._REMOVED_FILE))
        self.mp.setattr(T, "_PEER_ANY", [0.0])
        after = {n: getattr(T, n).get(url) for n in files}
        assert after == before, {n: (before[n], after[n]) for n in before if before[n] != after[n]}

    def first(self, state):
        """When it was first in that state."""
        return next((t for t, st, _ in self.log if st == state), None)


def _days(t):
    return (t - START) / DAY


class TestTimelines:
    def test_healthy_for_40_days(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {}).run(40)
        assert s.removed is None
        assert {st for _, st, _ in s.log[3:]} == {"up_good"}
        assert not [e for e in T.EVENTS if e["type"] in ("bad", "down", "removed")]
        assert T._nt_bad_share(s.tr.url, s.now) == 0.0

    def test_no_peers_from_day_2_removed_5_days_after_up_bad(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "none" if d >= 2 else "ok"}).run(15)
        bad = s.first("up_bad")
        third = [t for t, _, _ in s.log if _days(t) >= 2][2]
        assert bad == third  # Up/Bad at its 3rd failed test
        assert s.removed is not None
        assert 5 * DAY <= s.removed[0] - bad <= 5 * DAY + 2 * HOUR, (_days(bad), _days(s.removed[0]))
        assert s.removed[1].startswith("handed out no peers for 5 days (Up/Bad)")

    def test_unusable_address_says_so(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "nat" if d >= 1 else "ok"}).run(10)
        assert s.removed and s.removed[1].startswith("handed out unusable peers for 5 days (Up/Bad): over IPv4 it gave out every client's address as 172.17.0.1")

    def test_down_from_day_2_removed_about_5_days_later(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 2}).run(15)
        last_up = max(t for t, st, _ in s.log if st != "down" and _days(t) < 2)
        assert s.removed is not None
        assert 5 * DAY <= s.removed[0] - last_up <= 5 * DAY + 5 * HOUR, (_days(last_up), _days(s.removed[0]))  # 5 days from its last answer

    def test_fixed_after_3_days_is_kept(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "none" if 1 <= d < 4 else "ok"}).run(20)
        assert s.removed is None and s.log[-1][1] == "up_good"

    def test_good_spells_under_12_hours_dont_reset_the_clock(self, monkeypatch) -> None:
        # Up/Bad for 20 h, then passing for 4 h, over and over: one stretch, removed after 5 days
        s = Sim(monkeypatch, lambda d: {"peers": "none" if d >= 1 and (d - 1) % 1.0 < 20 / 24 else "ok"}).run(15)
        assert s.removed is not None and s.removed[0] - START <= 7.5 * DAY, _days(s.removed[0])

    def test_good_spells_over_12_hours_reset_it(self, monkeypatch) -> None:
        # Up/Bad for 2.5 days, then passing for a day, over and over: never 5 days in a row (but 70% of the week: kept)
        s = Sim(monkeypatch, lambda d: {"peers": "none" if (d % 3.5) < 2.5 else "ok"}).run(20)
        assert s.removed is None, s.removed

    def test_up_bad_80_percent_of_the_week_is_removed(self, monkeypatch) -> None:
        # Up/Bad 4 days, then a day passing, over and over: 80% of a week, never 5 days in a row
        s = Sim(monkeypatch, lambda d: {"peers": "none" if (d % 5) < 4.2 else "ok"}).run(30)
        assert s.removed is not None and "of the last 7 days (Up/Bad)" in s.removed[1], s.removed

    @pytest.mark.parametrize(("world", "fams", "now"), [
        (lambda d: {"dead": {"v6"}}, ("v4", "v6"), "its IPv6 address is dead"),
        (lambda d: {"peers_v6": "none"}, ("v4", "v6"), "its IPv6 side doesn't share peers"),
        (lambda d: {"peers": "split"}, ("v4",), "its servers don't share swarms")])
    def test_up_broken_only_after_30_days(self, monkeypatch, world, fams, now) -> None:
        s = Sim(monkeypatch, world, fams=fams).run(33)
        broken = s.first("up_broken")
        assert broken is not None and s.removed is not None
        assert 30 * DAY <= s.removed[0] - broken <= 30 * DAY + 2 * HOUR, (_days(broken), _days(s.removed[0]))
        assert s.removed[1] == "Up/Junk or Up/Broken for 30 days in a row; now " + now
        assert not [st for _, st, _ in s.log if st == "up_bad"]

    def test_rare_meeting_split_is_up_bad(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "rare" if d >= 1 else "ok"}).run(10)
        assert s.removed is not None and "(Up/Bad)" in s.removed[1] and "its servers don't share swarms" in s.removed[1]

    def test_down_then_up_bad_one_clock(self, monkeypatch) -> None:
        # 3 days down, then 3 days answering without peers: not working for 6 days in one stretch
        s = Sim(monkeypatch, lambda d: {"answers": not (1 <= d < 4), "peers": "none" if d >= 4 else "ok"}).run(12)
        assert s.removed is not None and s.removed[0] - START <= 6.5 * DAY, (_days(s.removed[0]), s.removed[1])
        assert s.removed[1].startswith("not working for")

    def test_peer_test_blocked_by_our_exits_is_never_held_against_it(self, monkeypatch) -> None:
        monkeypatch.setattr(T, "_PEER_ANY", [START])
        s = Sim(monkeypatch, lambda d: {"peers": "na" if d >= 1 else "ok"})
        s.run(20)
        assert s.removed is None and s.log[-1][1] != "up_bad"


def _ban_line(host):
    return [ln for ln in open("data/denylist.txt").read().splitlines() if ln.split()[:1] == [host]][-1]


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
class TestLifecycle:
    def _no_peers(self, mp, fixed_at, **kw):
        s = Sim(mp, lambda d: {"peers": "none" if 1 <= d < fixed_at else "ok"}, **kw).run(10)
        assert s.removed and "(Up/Bad)" in s.removed[1], s.removed
        return s

    def test_fixed_then_check_again_now(self, monkeypatch) -> None:
        s = self._no_peers(monkeypatch, fixed_at=7)
        s.now = START + 8 * DAY
        s.resubmit(reinstating=True)
        n = len(T.EVENTS)
        s.run(12)
        assert s.removed is None and s.log[-1][1] == "up_good" and not T._nt_banned(s.tr.host)
        assert [e["type"] for e in T.EVENTS[n:]] in ([], ["good"]), T.EVENTS[n:]

    def test_still_broken_after_check_again_now(self, monkeypatch) -> None:
        s = self._no_peers(monkeypatch, fixed_at=999)
        back = s.now = s.removed[0] + DAY
        s.resubmit(reinstating=True)
        s.run(3)
        assert s.removed is not None and s.removed[0] - back <= 8 * HOUR, (s.removed[0] - back) / HOUR
        assert _ban_line(s.tr.host).split()[2] == "90"  # its second removal
        assert not [e for e in T.EVENTS if "Up/Good" in e["text"]]

    def test_ban_runs_out_then_resubmitted_fixed(self, monkeypatch) -> None:
        from newtrackon import ingest
        s = self._no_peers(monkeypatch, fixed_at=20)
        s.now = s.removed[0] + DAY
        assert s.tr.host in ingest._denylist_hosts()  # refused while banned
        s.now = s.removed[0] + 31 * DAY
        assert s.tr.host not in ingest._denylist_hosts()
        s.resubmit(reinstating=False)
        assert T.status_rule(s.tr)[0] != "up_bad"  # listed clean, not on its old failures
        n = len(T.EVENTS)
        s.run(10)
        assert s.removed is None and not [e for e in T.EVENTS[n:] if e["type"] in ("bad", "down", "removed")], T.EVENTS[n:]

    def test_ban_runs_out_resubmitted_still_broken(self, monkeypatch) -> None:
        s = self._no_peers(monkeypatch, fixed_at=999)
        back = s.now = s.removed[0] + 31 * DAY
        s.resubmit(reinstating=False)
        s.run(10)
        assert s.removed is not None and s.removed[0] - back >= 5 * DAY  # a fresh 5 days: it wasn't reinstated
        assert _ban_line(s.tr.host).split()[2] == "90"

    def test_udp_version_replaces_an_up_bad_http_tracker(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "none" if 1 <= d < 5.2 else "ok"}, url="http://sim.example:6969/announce").run(5.2)
        assert s.removed is None and s.log[-1][1] == "up_bad"
        old = s.tr
        s.resubmit(reinstating=False, url="udp://sim.example:6969/announce", listed=[old])
        assert s.tr.url.startswith("udp://") and T.LAST_STATE[s.tr.url]["st"] == "up_bad"  # its clock carried over
        s.run(10)
        assert s.removed is None and s.log[-1][1] == "up_good"

    def test_udp_version_with_the_same_fault_goes_soon(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "none" if d >= 1 else "ok"}, url="http://sim.example:6969/announce").run(5.2)
        old, bad = s.tr, s.first("up_bad")
        s.resubmit(reinstating=False, url="udp://sim.example:6969/announce", listed=[old])
        s.run(5)
        # its clock carried over: removed when the stretch that began on HTTP reaches 5 days
        assert s.removed is not None and 5 * DAY <= s.removed[0] - bad <= 5 * DAY + 4 * HOUR, (_days(bad), _days(s.removed[0]))

    def test_restart_mid_stretch_changes_nothing(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"peers": "none" if d >= 1 else "ok"}).run(4)
        bad = s.first("up_bad")
        s.restart()
        s.run(10)
        assert s.removed is not None and 5 * DAY <= s.removed[0] - bad <= 5 * DAY + 2 * HOUR, (_days(bad), _days(s.removed[0]))

    def test_restart_in_up_broken_changes_nothing(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"dead": {"v6"}}, fams=("v4", "v6")).run(12)
        broken = s.first("up_broken")
        s.restart()
        s.run(25)
        assert s.removed is not None and 30 * DAY <= s.removed[0] - broken <= 30 * DAY + 2 * HOUR

    def test_flaky_removed_then_fixed_and_reinstated(self, monkeypatch) -> None:
        # answers 1 check in 8 (dropping out), removed by the 15% rule, then fixed
        s = Sim(monkeypatch, lambda d: {"answers": d >= 9 or int(d * 48) % 8 == 0}).run(9)
        assert s.removed is not None and s.removed[1].startswith(("too unreliable", "no answer", "not working")), s.removed
        s.now = START + 9.5 * DAY
        s.resubmit(reinstating=True)
        s.run(10)
        assert s.removed is None, s.removed


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
def test_accepting_a_protocol_upgrade_never_writes_a_ban(monkeypatch) -> None:
    # a flaky HTTP tracker (answers 1 check in 8) whose UDP version is submitted while it's still listed
    s = Sim(monkeypatch, lambda d: {"answers": int(d * 48) % 8 == 0}, url="http://sim.example:6969/announce", added_days=40)
    s.tr.historic = deque(([1] + [0] * 7) * 180, maxlen=1440)
    s.tr.last_uptime = s.now - 600
    old = s.tr
    s.resubmit(reinstating=False, url="udp://sim.example:6969/announce", listed=[old])
    assert not [ln for ln in open("data/denylist.txt").read().splitlines() if ln.split()[:1] == ["sim.example"]] if __import__("os").path.exists("data/denylist.txt") else True


# ---- randomized timelines: whatever the tracker does, it's never removed earlier than the rules allow, and the reason
# matches what it was doing. The bounds come from the per-check record of what the world was doing, not from the code.
MODES = ["ok", "ok", "ok", "none", "nat", "split", "rare", "fake", "down", "flap", "dead6", "nopeers6", "inconclusive", "na",
         "abroad", "abroad_none", "abroad_refused", "down_refused", "abroad_403", "abroad_banned"]


def _random_world(rng):
    segs, t = [], 0.0
    while t < 40:
        t += rng.choice([0.05, 0.2, 0.4, 0.6, 1, 2, 3, 5, 8])
        segs.append((t, rng.choice(MODES)))

    def world(d):
        mode = next((m for end, m in segs if d < end), "ok")
        if mode == "down":
            return {"answers": False}
        if mode == "abroad":  # blocks this server, answers our test clients in other countries
            return {"answers": False, "elsewhere": True}
        if mode == "abroad_none":  # the same, and hands out no peers (its peer test runs from abroad)
            return {"answers": False, "elsewhere": True, "peers": "none"}
        if mode == "abroad_refused":  # refuses this server (a firewall REJECT, not silence), answers abroad
            return {"answers": False, "elsewhere": True, "refused": True}
        if mode == "down_refused":  # refuses everyone: its port is closed
            return {"answers": False, "refused": True}
        if mode == "abroad_403":  # a CDN in front blocks this server; our test clients abroad get answers
            return {"answers": False, "elsewhere": True, "err": "HTTP 403 status code returned"}
        if mode == "abroad_banned":  # it tells this server it's banned, and answers our test clients abroad
            return {"answers": False, "elsewhere": True, "err": "Tracker error message: your IP address is banned"}
        if mode == "flap":
            return {"answers": int(d * 48) % 3 != 0}
        if mode == "dead6":
            return {"dead": {"v6"}}
        if mode == "nopeers6":
            return {"peers_v6": "none"}
        return {"peers": mode}
    return world


def _peer_bad_world(s):
    return (s["answers"] or s.get("elsewhere")) and s.get("peers") in ("none", "nat", "fake", "rare", "inconclusive")


def _stretches(times, flags, bridge):
    """[(start, end)] of times flagged True, a gap shorter than `bridge` (from leaving to coming back) not breaking one."""
    out, start, left = [], None, None
    for t, f in zip(times, flags):
        if f:
            if start is None or (left is not None and t - left >= bridge):
                if start is not None:
                    out.append((start, left))
                start = t
            left = None
        elif start is not None and left is None:
            left = t
    if start is not None:
        out.append((start, left))
    return out


def _peer_bad_codes(codes):
    return any(c in ("no_peers", "fake_peers") for c in codes)


@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("NT_SIM_SEEDS", "10"))))
def test_random_timelines_never_remove_early(monkeypatch, seed) -> None:
    """The rules run on each check's status (3 failed peer tests of the last 6 make Up/Bad, so it lags the tracker by a
    few checks on purpose): the bounds are worked out here from that record, independently of the code's clocks, and the
    reason is checked against what the tracker was really doing."""
    import random
    rng = random.Random(seed)
    world = _random_world(rng)
    fams = ("v4", "v6") if seed % 2 else ("v4",)
    s = Sim(monkeypatch, world, fams=fams)
    seen = []  # world state at every check

    orig = s.check

    def check():
        seen.append(s.state())
        T._PEER_ANY[0] = s.now  # other trackers' peer tests keep working meanwhile
        orig()
    s.check = check
    s.run(40)
    times = [t for t, _, _ in s.log]
    # the status catches up with a fixed tracker within a few checks
    for k in range(len(s.log) - 6):
        if all(st["answers"] and st.get("peers", "ok") == "ok" and not st.get("dead") and "peers_v6" not in st for st in seen[k:k + 6]):
            assert s.log[k + 5][1] in ("up_good", "up_new", "up_slow", "up_unreliable", "up_junk"), (seed, _days(times[k + 5]), s.log[k + 5])
    if not s.removed:
        return
    t_rm, why = s.removed
    down = [st == "down" and not w.get("elsewhere") for (_, st, _), w in zip(s.log, seen)]  # answering abroad isn't down
    pbad = [st in ("up_bad", "down") and _peer_bad_codes(c) for _, st, c in s.log]  # down + answers abroad can be peer-bad
    world_now = seen[-1]
    if why.startswith("no answer for"):
        assert not world_now.get("elsewhere"), (seed, why, "removed while answering our test clients abroad")
    if why.startswith("not working for") and world_now.get("elsewhere"):
        assert s.latest[0] is False or s.latest[1].get("foreign"), (seed, why, "answering abroad and its peer test there passed")
    if why.startswith("no answer for"):
        last_answer = max((t for (t, st, _), w in zip(s.log, seen) if st != "down" or w.get("elsewhere")), default=None)
        assert not world_now["answers"] and (last_answer is None or t_rm - last_answer >= 5 * DAY), (seed, why)
    elif "(Up/Bad)" in why:
        ok, extra = s.latest  # its latest test showed the fault it's removed for
        if "no conclusive peer test" in why:
            assert ok is None and not extra.get("exit_blocked"), (seed, why, s.latest)  # never for a limit of ours
        else:
            assert (extra.get("foreign") if "fake peers" in why else ok is False), (seed, why, s.latest)
        # the decision uses the state stored at the check before: the stretch ends at the previous check
        runs = _stretches(times[:-1], pbad[:-1], 12 * HOUR)
        last = runs[-1] if runs else None
        if "of the last 7 days" in why:
            week = [(t, f) for t, f in zip(times[:-1], pbad[:-1]) if t_rm - t <= 7 * DAY]
            span = sum(t2 - t1 for (t1, f), (t2, _) in zip(week, week[1:] + [(t_rm, False)]) if f)
            assert span >= 0.8 * 7 * DAY - 2 * HOUR, (seed, why, span / DAY)
        else:
            assert last and last[1] is None and t_rm - last[0] >= 5 * DAY, (seed, why, [(_days(b), _days(e or t_rm)) for b, e in runs])
    elif why.startswith("not working for"):
        ok, extra = s.latest
        assert not (world_now["answers"] or world_now.get("elsewhere")) or (extra.get("foreign") if "can't exist" in why else
                                            ok is False or (ok is None and not extra.get("exit_blocked"))), (seed, why, world_now, s.latest)
        runs = _stretches(times[:-1], [d or p for d, p in zip(down[:-1], pbad[:-1])], 12 * HOUR)
        last = runs[-1] if runs else None
        assert last and last[1] is None and t_rm - last[0] >= 5 * DAY, (seed, why, [(_days(b), _days(e or t_rm)) for b, e in runs])
    elif why.startswith("too unreliable"):
        recent = [d for t, d in zip(times, down) if t_rm - t <= 7 * DAY]
        assert sum(recent) / len(recent) > 0.3, (seed, why, sum(recent) / len(recent))
    else:
        assert False, (seed, why)  # nothing here is Up/Junk or Up/Broken for 30 days, private or moved


def _fresh_state(monkeypatch) -> None:
    """Forget everything between two runs in one test: every store, in memory and on disk."""
    import glob
    import os
    for name in (*T._STORES, "REMOVED"):
        monkeypatch.setattr(T, name, {})
    monkeypatch.setattr(T, "EVENTS", [])
    monkeypatch.setattr(T, "_PEER_ANY", [0.0])
    for f in glob.glob("data/*"):
        if os.path.isfile(f):
            os.remove(f)


@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("NT_SIM_SEEDS", "10"))))
def test_random_restart_changes_nothing(monkeypatch, seed) -> None:
    import random
    rng = random.Random(1000 + seed)
    world = _random_world(rng)
    fams = ("v4", "v6") if seed % 2 else ("v4",)
    at = rng.uniform(1, 25)
    _fresh_state(monkeypatch)
    a = Sim(monkeypatch, world, fams=fams).run(30)
    ref = (a.removed, [(t, st) for t, st, _ in a.log])
    _fresh_state(monkeypatch)
    b = Sim(monkeypatch, world, fams=fams).run(at)
    if not b.removed:
        b.restart()
        b.run(30 - (b.now - START) / DAY)
    assert (b.removed, [(t, st) for t, st, _ in b.log]) == ref, (seed, at, a.removed, b.removed)


def test_answering_only_abroad_stays(monkeypatch) -> None:
    """No answer here, but our test clients in other countries get one (it blocks this server): it stays listed, shown as
    Down from here (user's call). Once it answers nobody, the usual 5 days run from its last answer anywhere."""
    s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": 1 <= d < 12})
    s.run(12)
    assert s.removed is None and s.log[-1][1] == "down"
    assert ntextra_dying(s.tr, s.now) is None  # not greyed as about to go
    last_abroad = T.ELSEWHERE[s.tr.url]
    assert 11.8 <= _days(last_abroad) < 12
    s.run(10)
    assert s.removed is not None and s.removed[1].startswith("no answer for 5 days"), s.removed
    assert 5 * DAY <= s.removed[0] - last_abroad <= 5 * DAY + 5 * HOUR, (_days(last_abroad), _days(s.removed[0]))


def ntextra_dying(t, now):
    from newtrackon import ntextra
    return ntextra._dying(t, now)


def test_inconclusive_for_7_days_end_to_end(monkeypatch) -> None:
    s = Sim(monkeypatch, lambda d: {"peers": "inconclusive" if d >= 1 else "ok"})
    orig = s.check

    def check():  # other trackers' tests keep working meanwhile
        T._PEER_ANY[0] = s.now
        orig()
    s.check = check
    s.run(25)
    bad = s.first("up_bad")
    assert bad is not None and 8 <= _days(bad) <= 8.6, _days(bad) if bad else None  # 7 days after its last conclusive test
    assert s.removed is not None and 5 * DAY <= s.removed[0] - bad <= 5 * DAY + 2 * HOUR, (_days(bad), _days(s.removed[0]))
    assert "no conclusive peer test" in s.removed[1] and "(Up/Bad)" in s.removed[1], s.removed[1]
    detail = T.REMOVED[s.tr.host]["detail"]
    assert detail[0].startswith("No conclusive peer test for 7 days") and not [x for x in detail if x.startswith("Passed 0")]


def test_a_failing_peer_test_brings_the_next_check_forward(monkeypatch) -> None:
    s = Sim(monkeypatch, lambda d: {"peers": "none" if 2 <= d < 6 else "ok"}).run(1.9)
    assert s.tr.interval >= 7200  # healthy: long intervals
    s.run(4.2)
    first_fail = next(t for t, st, _ in s.log if _days(t) >= 2)
    bad = s.first("up_bad")
    assert bad - first_fail <= HOUR, (bad - first_fail) / 60  # Up/Bad within the hour, not after 3 long intervals
    assert s.tr.interval > 900  # settled Up/Bad: not checked every 15 minutes
    s.run(2)
    first_pass = next(t for t, st, _ in s.log if _days(t) >= 6)
    good = next(t for t, st, _ in s.log if t > first_pass and st != "up_bad")
    assert good - first_pass <= 1.5 * HOUR, (good - first_pass) / 60  # the fix shows soon too


def test_split_servers_arent_checked_every_15_minutes(monkeypatch) -> None:
    s = Sim(monkeypatch, lambda d: {"peers": "split"}).run(3)
    ivs = [t2 - t1 for (t1, _, _), (t2, _, _) in zip(s.log[-20:], s.log[-19:])]
    assert min(ivs) > 1000, ivs


def test_up_bad_then_answering_only_abroad_stays(monkeypatch) -> None:
    # Up/Bad for 6 days (its clock past 5 days), then it blocks this server but answers our test clients abroad
    s = Sim(monkeypatch, lambda d: {"peers": "none"} if 1 <= d < 6.3 else ({"answers": False, "elsewhere": True} if d >= 6.3 else {}))
    s.run(6.3)
    if s.removed:  # removed for its 5 days of Up/Bad before it started blocking: fine, that's the rule
        assert "(Up/Bad)" in s.removed[1]
        return
    s.run(10)
    assert s.removed is None, s.removed


def test_answering_abroad_counts_in_full_for_the_15_percent_rule(sample_tracker) -> None:
    from time import time as _t
    t, now = sample_tracker, int(_t())
    t.added, t.last_uptime, t.status = now - 30 * DAY, now - HOUR, 0
    t.last_downtime = now
    T.ELSEWHERE[t.url] = now
    t.historic = deque([1, 0.5, 0.5, 0.5, 0, 0.5, 0.5, 1] * 60, maxlen=1440)  # mostly answering only abroad, flipping often
    t.update_uptime()
    assert t.to_be_deleted is False



class TestAnsweringOnlyAbroad:
    """It blocks this server but answers our test clients abroad: it stays, shown as Down from NZ, and its peer test runs
    from those test clients, so the Up/Bad rules apply to it like to every tracker."""

    def test_working_stays_and_says_so(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1}).run(20)
        assert s.removed is None
        assert T.status_rule(s.tr) == ("down", ["answers_elsewhere"]) and T.PEER_OK[s.tr.url] is True

    @pytest.mark.parametrize(("mode", "words"), [("none", "handed out no peers for 5 days (Up/Bad)"),
                                                 ("nat", "handed out unusable peers for 5 days (Up/Bad)"),
                                                 ("fake", "returned fake peers for 5 days (Up/Bad)")])
    def test_failing_its_peer_test_from_abroad_goes(self, monkeypatch, mode, words) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1, "peers": mode if d >= 1 else "ok"}).run(12)
        assert s.removed is not None and s.removed[1].startswith(words), s.removed
        assert 5.5 * DAY <= s.removed[0] - START <= 8 * DAY, _days(s.removed[0])

    def test_its_failing_test_brings_the_next_check_forward(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1, "peers": "none" if d >= 3 else "ok"}).run(4)
        first_fail = next(t for t, _, _ in s.log if _days(t) >= 3)
        bad = next(t for t, st, c in s.log if "no_peers" in c)
        assert bad - first_fail <= HOUR, (bad - first_fail) / 60

    def test_blocked_exits_are_never_held_against_it(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1, "peers": "na" if d >= 1 else "ok"})
        orig = s.check

        def check():
            T._PEER_ANY[0] = s.now
            orig()
        s.check = check
        s.run(25)
        assert s.removed is None


@pytest.mark.usefixtures("region_db")
def test_down_from_nz_on_the_pages(monkeypatch, flask_client) -> None:
    from unittest.mock import patch
    from newtrackon import db, ntextra
    s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1}).run(2)
    T._attach_extras([s.tr])
    with patch.object(db, "get_all_data", return_value=[s.tr]), patch.object(ntextra, "_trackers", return_value=[s.tr]):
        main = flask_client.get("/").get_data(as_text=True)
        page = flask_client.get("/tracker/sim.example").get_data(as_text=True)
        api = flask_client.get("/api/tracker/sim.example").get_json()
        badge = flask_client.get("/badge/sim.example.svg").get_data(as_text=True)
    assert ">Down from NZ<" in main and "our test clients in other countries do" in main
    assert "Down from NZ (answers elsewhere)" in page and "it works for users elsewhere, so it stays listed" in page
    assert 'href="/fix#answers-elsewhere"' in page
    assert api["status"] == "down" and api["problems"] == ["answers_elsewhere"]
    assert "Down from NZ" in badge


@pytest.mark.usefixtures("empty_queues", "mock_db_connection")
class TestRefusingThisServer:
    """It refuses this server ("connection failed": a firewall REJECT, not silence) but answers our test clients abroad:
    the same as one that ignores this server. Before 2026-10-12 only a timeout counted, so a tracker refusing this
    server (t.overflow.biz) was removed for no answer, and couldn't come back."""

    @pytest.mark.parametrize("url", ["udp://sim.example:6969/announce", "http://sim.example:6969/announce"])
    def test_stays_down_from_nz(self, monkeypatch, url) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1, "refused": True}, url=url).run(20)
        assert s.removed is None, s.removed
        assert T.status_rule(s.tr) == ("down", ["answers_elsewhere"])

    def test_refusing_everyone_goes_after_5_days(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "refused": True}).run(10)
        last_up = max(t for t, st, _ in s.log if st != "down")
        assert s.removed is not None and s.removed[1].startswith("no answer for 5 days"), s.removed
        assert 5 * DAY <= s.removed[0] - last_up <= 5 * DAY + 5 * HOUR, (_days(last_up), _days(s.removed[0]))

    def test_a_whitelist_is_still_judged_as_one(self, monkeypatch) -> None:
        # it refuses our test clients abroad too: that's no answer there (scraper._genuine), so it's recorded as it is
        # (down, then removed as whitelist-only), never as Down from NZ
        s = Sim(monkeypatch, lambda d: {"rejects": "info hash is not authorized with this tracker" if d >= 1 else None}).run(5)
        assert s.tr.url not in T.ELSEWHERE
        assert s.removed is not None and "whitelist" in s.removed[1], s.removed

    @pytest.mark.parametrize("err", ["HTTP 403 status code returned", "Tracker error message: your IP address is banned",
                                     "Failed bdecoding HTTP response: not a bencoded dict", "HTTP 429 status code returned"])
    def test_saying_no_to_this_server_alone_stays(self, monkeypatch, err) -> None:
        # a CDN blocking this server, or the tracker banning it, while it answers our test clients abroad: Down from NZ
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "elsewhere": d >= 1, "err": err}, url="http://sim.example:6969/announce").run(20)
        assert s.removed is None, s.removed
        assert T.status_rule(s.tr) == ("down", ["answers_elsewhere"])

    @pytest.mark.parametrize("record", ["as kept now", "like t.overflow.biz's"])
    def test_reinstated_through_our_test_clients_abroad_stays(self, monkeypatch, record) -> None:
        # removed for no answer (down everywhere for 5 days); fixed, but it still refuses this server; then its operator
        # presses Check again now. Through its 3-day grace with the restored week (all down from here) and 5 days past it.
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "refused": True, "elsewhere": d >= 8}).run(8)
        assert s.removed is not None and s.removed[1].startswith("no answer for"), s.removed
        if record != "as kept now":  # from before records kept the listing date; nt-admin reinstate --clean-history
            T.REMOVED[s.tr.host].update({"added": 0, "fault": True})
            T.REMOVED[s.tr.host].pop("hist", None)
        n = len(T.EVENTS)
        s.now = START + 9 * DAY
        s.resubmit(reinstating=True)
        assert s.tr.status == 0 and s.tr.last_uptime == 0 and T._answers_elsewhere_now(s.tr) and not T._nt_banned(s.tr.host)
        assert T.status_rule(s.tr) == ("down", ["answers_elsewhere"])  # Down from NZ from the start, never shown Up
        s.run(10)
        assert s.removed is None, s.removed
        assert T.status_rule(s.tr) == ("down", ["answers_elsewhere"]) and T.PEER_OK[s.tr.url] is True  # its test ran abroad
        new = [(e["type"], e["text"]) for e in T.EVENTS[n:]]
        assert new == [("added", "is back on the list at its operator's request (Down from NZ: it answers our test clients "
                                 "abroad, not this site's server)")], new

    def test_reinstated_through_abroad_still_failing_its_peer_test_goes(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": d < 1, "refused": True, "elsewhere": d >= 8,
                                        "peers": "none" if d >= 8 else "ok"}).run(8)
        s.now = START + 9 * DAY
        s.resubmit(reinstating=True)
        s.run(10)
        assert s.removed is not None and s.removed[1].startswith("handed out no peers for 5 days (Up/Bad)"), s.removed

    def test_a_new_submission_still_needs_this_server(self, monkeypatch) -> None:
        s = Sim(monkeypatch, lambda d: {"answers": False, "refused": True, "elsewhere": True})
        assert s.resubmit(reinstating=False, url="udp://other.example:6969/announce", expect=False) is None


class TestMadeUpConnectionId:
    """The BEP 15 check (an announce with a made-up connection ID) is the one request no real client sends: once a day per
    tracker, not at every check. The result stands in between, down or not: it's the tracker software's."""

    def test_once_a_day(self, monkeypatch) -> None:
        sim = Sim(monkeypatch, lambda d: {}).run(5)
        gaps = [b - a for a, b in zip(sim.cid, sim.cid[1:])]
        assert 4 <= len(sim.cid) <= 6 and all(DAY <= g < DAY + 4 * HOUR + 600 for g in gaps), [g / HOUR for g in gaps]
        assert len(sim.log) > 25 and T.CID_OK[sim.tr.url] is True  # every other check keeps the result

    def test_the_result_stands_between_tests(self, monkeypatch) -> None:
        sim = Sim(monkeypatch, lambda d: {}).run(0.2)
        assert len(sim.cid) == 1 and len(sim.log) > 1
        sim.run(0.5)
        assert len(sim.cid) == 1 and T.CID_OK[sim.tr.url] is True

    def test_a_spell_down_keeps_the_result(self, monkeypatch) -> None:
        sim = Sim(monkeypatch, lambda d: {"answers": not 0.5 <= d < 0.7}).run(0.6)
        assert sim.log[-1][1] == "down" and T.CID_OK[sim.tr.url] is True  # still shown by the API while it's down
        sim.run(0.3)
        back = next(t for t, st, _ in sim.log if t >= START + 0.7 * DAY and st != "down")
        assert back not in sim.cid and len(sim.cid) == 1 and T.CID_OK[sim.tr.url] is True  # back: shown at once, no retest

    def test_every_check_shows_the_standing_result(self, monkeypatch) -> None:
        from newtrackon import persistence
        sim = Sim(monkeypatch, lambda d: {}).run(0.3)
        assert len(sim.cid) == 1 and len(sim.log) > 1
        assert persistence.raw_data[0]["flags"]["cid_ok"] is True  # the /raw page's row for a check that didn't test it

    def test_a_restart_keeps_the_day(self, monkeypatch) -> None:
        sim = Sim(monkeypatch, lambda d: {}).run(1.5)
        sim.restart()
        sim.run(1.2)
        gaps = [b - a for a, b in zip(sim.cid, sim.cid[1:])]
        assert len(sim.cid) == 3 and all(g >= DAY for g in gaps) and T.CID_OK[sim.tr.url] is True
