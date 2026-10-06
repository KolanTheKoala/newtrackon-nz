import random
import pprint
import re
import socket
import sys
from collections import deque
from http.client import HTTPResponse
from ipaddress import IPv4Address, IPv6Address, ip_address
from logging import getLogger
from time import sleep, time
from typing import cast
from urllib import parse, request

from newtrackon import persistence, scraper
from newtrackon.persistence import HistoryData
from newtrackon.utils import format_time

logger = getLogger("newtrackon")

HISTORIC_SLOTS = 1440  # historic keeps one up/down value per 30-minute slot: 30 days

max_downtime: int = 47304000  # 1.5 years
IP_HISTORY_WINDOW: int = 48 * 3600  # 48 hours in seconds


class Tracker:
    url: str
    host: str
    ips: list[str] | None
    latency: int | None
    last_checked: int
    interval: int
    status: int
    uptime: float
    countries: list[str] | None
    country_codes: list[str] | None
    networks: list[str] | None
    historic: deque[int]
    recent_ips: dict[str, int]
    added: int
    last_downtime: int
    last_uptime: int
    to_be_deleted: bool
    status_epoch: int | None
    status_readable: str | None

    def __init__(
        self,
        url: str,
        host: str,
        ips: list[str] | None,
        latency: int | None,
        last_checked: int,
        interval: int,
        status: int,
        uptime: float,
        countries: list[str] | None,
        country_codes: list[str] | None,
        networks: list[str] | None,
        historic: deque[int],
        added: int,
        last_downtime: int,
        last_uptime: int,
        recent_ips: dict[str, int] | None = None,
    ) -> None:
        self.url = url
        self.host = host
        self.ips = ips
        self.latency = latency
        self.last_checked = last_checked
        self.interval = interval if (interval == 900 or (interval % 1800 == 0 and 1800 <= interval <= 14400)) else 1800
        self.status = status
        self.uptime = uptime
        self.countries = countries
        self.country_codes = country_codes
        self.networks = networks
        self.historic = historic
        self.recent_ips = recent_ips if recent_ips is not None else {}
        self.added = added
        self.last_downtime = last_downtime
        self.last_uptime = last_uptime
        self.to_be_deleted = False
        self.status_epoch = None
        self.status_readable = None
        self.peer_ok = None

    @classmethod
    def from_url(cls, url: str) -> Tracker:
        # Parse the URL to get hostname first (validate_url will normalize it)
        parsed = parse.urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise RuntimeError("Invalid URL: cannot extract hostname")

        tracker = cls(
            url=url,
            host=hostname,
            ips=None,
            latency=None,
            last_checked=0,
            interval=10800,  # Default interval (3 hours)
            status=0,
            uptime=0.0,
            countries=[],
            country_codes=[],
            networks=[],
            historic=deque(maxlen=HISTORIC_SLOTS),
            added=int(time()),
            last_downtime=0,
            last_uptime=0,
        )
        tracker.validate_url()
        logger.info("Preprocessing %s", url)
        # Update host from the validated/normalized URL
        tracker.host = parse.urlparse(tracker.url).hostname or hostname
        tracker.update_ips()
        tracker.refresh_recent_ips()
        return tracker

    def update_status(self) -> None:
        try:
            now = int(time())
            if self.last_uptime < (now - max_downtime):
                self.to_be_deleted = True
                raise RuntimeError("Tracker unresponsive for too long, removed")

            self.update_scheme_from_bep_34()
            try:
                self.update_ips()
            finally:
                # Expire IP history even when DNS resolution fails.
                self.refresh_recent_ips()
        except RuntimeError as reason:
            if not _monitor_online():  # our own outage, not the tracker's: record nothing, retry soon
                self.to_be_deleted = False
                self._offline_skip()
                return
            if _nt_local_fault(self, reason):  # works from elsewhere: our fault, not the tracker's
                self.to_be_deleted = False
                self._offline_skip()
                return
            self._last_err = str(reason)
            self.clear_tracker(reason=str(reason))
            self._emit_events()
            return

        self.update_ipapi_data()
        self._prev_checked = self.last_checked
        self.last_checked = int(time())
        pp = pprint.PrettyPrinter(width=999999, compact=True)
        t1 = time()
        scraper.rtt.ms = None
        scraper.rtt.probe = None
        scraper.rtt.probe_extra = None
        try:
            if parse.urlparse(self.url).scheme == "udp":
                response, _ = scraper.announce_udp(self.url)
            else:
                response = scraper.announce_http(self.url)

            interval = response.get("interval")
            _ann_iv_set(self.url, interval if isinstance(interval, int) else None)
            _warn_set(self.url, response.get("warning message"))
            _closed_seen(self.url, response.get("warning message"))
            if isinstance(interval, int):
                pass  # interval is set adaptively in is_up()/is_down()
            pretty_data = scraper.redact_origin(pp.pformat(response))
            debug: HistoryData = {
                "url": self.url,
                "ip": next(iter(self.ips)) if self.ips else "",
                "time": int(t1),
                "info": pretty_data,
                "status": 1,
            }
            persistence.raw_data.appendleft(debug)
            self.latency = scraper.rtt.ms if getattr(scraper.rtt, "ms", None) is not None else int((time() - t1) * 1000)
            self.latency = _nt_region_avg(self.url) or self.latency
            self.peer_ok = scraper.peer_probe()
            PEER_OK[self.url] = self.peer_ok
            _ex = getattr(scraper.rtt, "probe_extra", None) or {}
            FAKE_N[self.url] = _ex.get("foreign")
            INFLATED[self.url] = _ex.get("inflated")
            STALE[self.url] = _ex.get("stale")
            CID_OK[self.url] = _ex.get("cid_ok")
            _fr = scraper.family_probe(self.url)
            _df = next((k for k, v in _fr.items() if not v), None) if len(_fr) == 2 and sum(_fr.values()) == 1 else None
            _fam_set(self.url, _df, _fr)
            try:
                _region_set(self.url, scraper.region_latency(self.url, _fr))
            except Exception:
                pass
            if _df:
                logger.info("%s DEAD FAMILY: published %s address does not answer", self.url, _df)
            _probe_save()
            debug["flags"] = {"peer_ok": self.peer_ok, "fake": _ex.get("foreign"), "fake3": FAKE_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT, "peer3": PEER_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT, "inflated": _ex.get("inflated"), "stale": _ex.get("stale"), "cid_ok": _ex.get("cid_ok")}
            if _ex.get("stale"):
                logger.info("%s STALE PEERS: still hands out a peer after it sent stopped", self.url)
            if _ex.get("cid_ok") is False:
                logger.info("%s CONNID NOT ENFORCED: accepted an announce with a made-up connection ID", self.url)
            if _ex.get("foreign") is not None:
                _fake_hist_add(self.url, _ex["foreign"] > 0)
                if _ex["foreign"]:
                    logger.info("%s FAKE PEERS: %d fake peer(s) on a random hash", self.url, _ex["foreign"])
            if _ex.get("inflated"):
                logger.info("%s INFLATED COUNTS: %s seeders / %s leechers on a random hash", self.url, *_ex["inflated"])
            if self.peer_ok is True:
                _peer_hist_add(self.url, True, _ex.get("fam"))
            elif self.peer_ok is False:
                _peer_hist_add(self.url, False, _ex.get("fam"))
            # a test that couldn't run because the VPN exits got no answer is a limit of the test, not the tracker's fault:
            # it doesn't count towards the 7-day "never conclusive" rule
            _peer_conclusive(self.url, "exit_blocked" if (self.peer_ok is None and _ex.get("exit_blocked")) else self.peer_ok)
            # the other published family gets its own peer test, so one family's result can't stand for both
            _main = _ex.get("fam")
            _other = {"v4": "v6", "v6": "v4"}.get(_main)
            if _other and len(_fr) == 2 and _fr.get(_other):
                _ok2 = scraper.peer_probe_family(self.url, _other)
                if _ok2 is not None:
                    _peer_hist_add(self.url, _ok2, _other)
            logger.info("%s peer test: %s%s", self.url, {True: "PASS", False: "FAIL", None: "n/a"}[self.peer_ok], " (inconclusive: only a same-IP client was answered, and the peer outlives stopped)" if _ex.get("inconclusive") else "")
            self.is_up()
            _FAILSTREAK[0] = 0  # a success: not an outage
            logger.info("%s status is UP", self.url)
        except RuntimeError as e:
            if not _monitor_online():  # our own outage, not the tracker's: record nothing, retry soon
                self._offline_skip()
                return
            if _nt_local_fault(self, e):  # works from elsewhere: our fault, not the tracker's
                self._offline_skip()
                return
            self._last_err = str(e) + (" - answers from other regions" if getattr(self, "_nt_partial", False) else "")
            logger.info("%s status is DOWN. Cause: %s", self.url, e)
            self.peer_ok = None
            PEER_OK.pop(self.url, None)
            FAKE_N.pop(self.url, None)
            INFLATED.pop(self.url, None)
            STALE.pop(self.url, None)
            CID_OK.pop(self.url, None)
            _probe_save()
            debug_down: HistoryData = {
                "url": self.url,
                "ip": next(iter(self.ips)) if self.ips else "",
                "time": int(t1),
                "info": str(e),
                "status": 0,
            }
            persistence.raw_data.appendleft(debug_down)
            _nt_down_why_set(self.url, e)
            if _nt_down_label(str(e)) == "Rejected":  # its own error message: it may say it's private or whitelist-only
                _closed_seen(self.url, str(e))
            self.is_down()
        if self.uptime == 0:
            pass  # interval is set in is_up()/is_down()
        self.update_uptime()
        self._emit_events()

    def update_scheme_from_bep_34(self) -> None:
        valid_bep_34, bep_34_info = scraper.get_bep_34(self.host)
        if not valid_bep_34:  # No valid BEP34, attempting existing URL
            return
        if not bep_34_info:
            logger.info("Hostname denies connection via BEP34, removing tracker %s", self.url)
            self.to_be_deleted = True
            raise RuntimeError("Host denied connection according to BEP34, removed")

        logger.info(
            "Tracker %s sets protocol and port preferences from BEP34: %s",
            self.url,
            bep_34_info,
        )
        parsed_url = parse.urlparse(self.url)
        first_bep_34_protocol, first_bep_34_port = bep_34_info[0]

        if first_bep_34_protocol == "udp":
            if parsed_url.scheme == "udp" and parsed_url.port == first_bep_34_port:
                return
            self.url = parsed_url._replace(
                scheme="udp",
                netloc=f"{parsed_url.hostname}:{first_bep_34_port}",
            ).geturl()
            return

        if first_bep_34_protocol == "tcp":
            if parsed_url.scheme in ("http", "https"):
                if parsed_url.port == first_bep_34_port:
                    return
                self.url = parsed_url._replace(
                    netloc=f"{parsed_url.hostname}:{first_bep_34_port}",
                ).geturl()
                return

            # Switching from UDP to TCP: probe HTTPS then HTTP to find correct scheme
            candidate_url = parsed_url._replace(netloc=f"{parsed_url.hostname}:{first_bep_34_port}")
            failover_ip = next(iter(self.ips), "") if self.ips else ""
            http_result = scraper.attempt_https_http(failover_ip, candidate_url, log_to_submitted=False)
            if http_result is not None:
                self.url = http_result.url

    def clear_tracker(self, reason: str) -> None:
        self.countries, self.networks, self.country_codes = None, None, None
        self.latency = None
        self._prev_checked = self.last_checked
        self.last_checked = int(time())
        _nt_down_why_set(self.url, reason)
        self.is_down()
        self.update_uptime()
        if self.uptime == 0:
            pass  # interval is set in is_up()/is_down()
        debug: HistoryData = {
            "url": self.url,
            "ip": "",
            "time": int(time()),
            "status": 0,
            "info": reason,
        }
        persistence.raw_data.appendleft(debug)

    def validate_url(self) -> None:
        uchars = re.compile(r"^[a-zA-Z0-9_\-\./:]+$")
        url = parse.urlparse(self.url)
        if url.scheme not in ["udp", "http", "https"]:
            raise RuntimeError("Tracker URLs have to start with 'udp://', 'http://' or 'https://'")
        netloc = url.netloc
        assert isinstance(netloc, str)
        if uchars.match(netloc):
            url = url._replace(path="/announce")
            new_url = url.geturl()
            assert isinstance(new_url, str)
            self.url = new_url
        else:
            raise RuntimeError("Invalid announce URL")

    def update_uptime(self) -> None:
        # Recency-weighted availability * recency-weighted stability.
        # historic appends newest at the right.
        # Half-life 336 slots (7 days of 30-minute slots).
        # One maintenance outage = two flips = tiny penalty.
        # Constant flapping = flip_rate ~1 = reliability collapses.
        n = len(self.historic)
        if n == 0:
            self.uptime = 0.0
            return
        availability, stability = _nt_avail_stab(self.historic)
        self.uptime = availability * stability * 100.0
        # new trackers earn a top score: the ceiling rises from 80 to 100 over the first 7 days (336 slots) of history
        self.uptime = min(self.uptime, 80.0 + 20.0 * min(1.0, n / 336.0))
        if (FAM_FAILS.get(self.url) or {}).get("n", 0) >= PEER_FAIL_LIMIT:  # dead published family N times in a row
            self.uptime = min(self.uptime, 50)  # still works on its other family
        elif PEER_FAILS.get(self.url, 0) < PEER_FAIL_LIMIT and _peer_fam_bad(self.url):  # one family shares no peers
            self.uptime = min(self.uptime, 50)  # Up/Broken like a dead address: same cap
        if FAKE_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT:  # fake peers in N of its last PEER_WINDOW checks
            self.uptime = 0  # fake peers = not a usable tracker (was: capped at PEER_FAIL_CAP)
        if PEER_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT:  # failed the peer test N times in a row
            self.uptime = 0  # no peers handed out = effectively down
        # Evict trackers that are dead, or too flaky to be of use as measured from Wellington:
        #   - listed 3+ days, AND
        #   - no successful check in 5 days, OR (144+ checks, i.e. 3+ days, and availability x stability under 15%:
        #     answers too rarely or drops out too often to be of use; peer/latency penalties are not counted here).
        # Milder flappers stay listed (orange) so users can see them. Auto-bans expire after 30 days (see ingest.py).
        now_ts = int(time())
        # Private or whitelist-only: its own replies said so CLOSED_LIMIT checks in a row, and it fails the peer test (or is
        # down). It can never work as a public tracker: removed and banned like any other removal, whatever its age.
        cl = CLOSED.get(self.url) or {}
        if cl.get("n", 0) >= CLOSED_LIMIT and (self.status == 0 or PEER_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT):
            _NT_DEL_REASON[self.url] = cl["why"]
            logger.info("Evicting %s (%s, %d checks in a row)", self.url, cl["why"], cl["n"])
            self.to_be_deleted = True
            self._nt_ban()
            return
        # Up/Bad (no peers or fake peers, not a dead address) for UPBAD_DAYS in a row, and still failing now: fixed or gone.
        ub = _nt_upbad_days(self.url, now_ts)
        fake = FAKE_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT
        failing = fake or PEER_FAILS.get(self.url, 0) >= PEER_FAIL_LIMIT
        share = _nt_bad_share(self.url, now_ts) if ub is not None else 0.0
        listed_days = (now_ts - int(self.added or now_ts)) / 86400.0
        what = "returned fake peers" if fake else "handed out no peers"
        why = None
        if not _peer_rule_applies(self.url):
            ub = None  # paused for HTTP(S): see HTTP_PEER_TEST_TRUSTED
        if ub is not None and failing and ub >= UPBAD_DAYS:
            why = "%s for %d days (Up/Bad)" % (what, int(ub))
        elif ub is not None and failing and share >= BAD_SHARE and listed_days >= BAD_SHARE_DAYS:
            why = "%s %d%% of the last %d days (Up/Bad)" % (what, round(100 * share), BAD_SHARE_DAYS)
        if why:
            _NT_DEL_REASON[self.url] = why
            logger.info("Evicting %s (Up/Bad: %.1f days in a row, %.0f%% of the last week)", self.url, ub, 100 * share)
            self.to_be_deleted = True
            self._nt_ban()
            return
        # down or Up/Bad for REMOVE_DAYS in one stretch, however it's split between the two, and still not working now
        ud = _nt_useless_days(self.url, now_ts)
        listed = (now_ts - int(self.added or now_ts)) / 86400.0
        if ud is not None and self.status != 0 and not _peer_rule_applies(self.url):
            ud = None  # answering but failing the (paused) HTTP peer test: not counted as "not working"
        if ud is not None and ud >= REMOVE_DAYS and listed >= 3 and (self.status == 0 or failing):
            _NT_DEL_REASON[self.url] = "not working for %d days: down, or answering without handing out peers" % int(ud)
            logger.info("Evicting %s (down or Up/Bad for %.1f days)", self.url, ud)
            self.to_be_deleted = True
            self._nt_ban()
            return
        # Up/Junk for JUNK_DAYS and still Junk now (the same score the status rule uses: after latency, before interval points)
        jd = _nt_junk_days(self.url, now_ts)
        dead_fam = (FAM_FAILS.get(self.url) or {}).get("n", 0) >= PEER_FAIL_LIMIT
        junk_now = float(self.uptime or 0) - _nt_lat_penalty(_nt_region_avg(self.url)) < 50
        if jd is not None and jd >= JUNK_DAYS and (junk_now or dead_fam):
            fam = str((FAM_FAILS.get(self.url) or {}).get("fam", "?"))[-1]
            _NT_DEL_REASON[self.url] = ("its IPv%s address dead or its score under 50 (Up/Broken, Up/Junk) for %d days" % (fam, int(jd))
                                        if dead_fam else "too unreliable: Up/Junk (score under 50) for %d days" % int(jd))
            logger.info("Evicting %s (Up/Junk or Up/Broken for %.1f days)", self.url, jd)
            self.to_be_deleted = True
            self._nt_ban()
            return
        age_days = (now_ts - int(self.added or now_ts)) / 86400.0
        dead_days = (now_ts - int(self.last_uptime or 0)) / 86400.0
        if age_days >= 3 and (dead_days >= REMOVE_DAYS or (n >= 144 and availability * stability < 0.15)):
            # the event, the removed page and the ban list say which of the two it was
            _NT_DEL_REASON[self.url] = ("no answer for %d days" % int(dead_days) if dead_days >= REMOVE_DAYS else
                                        "too unreliable: %.0f%% once dropouts are counted (under 15%%)" % (100.0 * availability * stability))
            logger.info(
                "Evicting %s (score=%.2f%%, availability=%.1f%%, no success for %.1f days, samples=%s, age_days=%.1f)",
                self.url,
                self.uptime,
                availability * 100.0,
                dead_days,
                n,
                age_days,
            )
            self.to_be_deleted = True
            self._nt_ban()

    def _nt_ban(self) -> None:
        """Ban the host (data/denylist.txt): 30 days the first time it's removed, 90 the second, for good after that.
        A fresh entry unless it already has an active ban."""
        try:
            host = (self.host or "").strip().lower()
            if not host or _nt_banned(host):
                return
            r = REMOVED.get(host)
            prior = int(r.get("count", 1)) if r else 0  # removals before this one
            # earlier removals of other names on the same server count too, so a new name doesn't start the ladder again
            mine = _nt_ip_keys(list(getattr(self, "ips", None) or []) + list((getattr(self, "recent_ips", None) or {}).keys()))
            prior += sum(int(o.get("count", 1)) for h, o in REMOVED.items()
                         if h != host and mine & _nt_ip_keys(o.get("ips")) and not any(w in str(o.get("network") or "").lower() for w in _CDN_WORDS))
            days = BAN_STEPS[min(prior, len(BAN_STEPS) - 1)]
            with open(_DENY_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"{host}\n" if days is None else (f"{host} {int(time())}\n" if days == 30 else f"{host} {int(time())} {days}\n"))
        except OSError:
            logger.exception("failed to append denylist for %s", self.url)

    def update_ips(self) -> None:
        self.ips = []
        temp_ips: set[str] = set()
        try:
            for res in socket.getaddrinfo(self.host, None):
                temp_ips.add(str(res[4][0]))
        except OSError:
            pass
        if temp_ips:  # Order IPs per protocol, IPv6 first
            parsed_ips: list[IPv4Address | IPv6Address] = []
            for ip in temp_ips:
                parsed_ips.append(ip_address(ip))
            # is_global alone also accepts some reserved, multicast and site-local addresses.
            for ip in parsed_ips:
                if not scraper.ip_is_public(ip):
                    # cross-check: ask Cloudflare and Quad9 directly; delete only if neither gives a public address
                    pub = _nt_public_ips(self.host)
                    if pub is None or any(_nt_ip_public(x) for x in pub):
                        self.ips = None
                        raise RuntimeError(f"IP {ip} is not globally routable, but the cross-check says {sorted(pub) if pub else 'no resolver answered'}: check skipped, tracker kept")
                    self.ips = None
                    self.to_be_deleted = True
                    if len(_NT_DEL_REASON) > 200:
                        _NT_DEL_REASON.clear()
                    _NT_DEL_REASON[self.url] = "its hostname no longer points to a public IP address"
                    raise RuntimeError(f"IP {ip} is not globally routable, removed")
            for ip in parsed_ips:
                if ip.version == 6:
                    self.ips.append(str(ip))
            for ip in parsed_ips:
                if ip.version == 4:
                    self.ips.append(str(ip))
        elif not self.ips:
            self.ips = None
            raise RuntimeError("Can't resolve IP")

    def refresh_recent_ips(self) -> None:
        now = int(time())
        if self.ips:
            for ip in self.ips:
                self.recent_ips[ip] = now
        self.recent_ips = {ip: ts for ip, ts in self.recent_ips.items() if now - ts <= IP_HISTORY_WINDOW}

    def update_ipapi_data(self) -> None:
        self.countries, self.networks, self.country_codes = [], [], []
        if self.ips:
            for ip in self.ips:
                ip_data = self.ip_api(ip).splitlines()
                if len(ip_data) == 3:
                    self.countries.append(ip_data[0])
                    self.country_codes.append(ip_data[1].lower())
                    self.networks.append(ip_data[2])

    # --- Adaptive (NTP-style) check interval, three tiers ----------------------------------
    #   healthy (no failure in the last 12 slots / 6 h): 30 -> 60 -> 90 -> 120 min, one step per success
    #   flaky   (up or down, but a failure in the last 6 h): every 15 min
    #   dead    (down, and no success in the last 6 h): every 30 min, no point hammering it
    # History is stored as wall-clock-aligned 30-minute slots. Two checks in the same slot
    # merge, and a failure anywhere in a slot makes the whole slot down. When a check lands
    # after a longer gap, the missing slots are filled with the previous state, or split
    # evenly between old and new state if it changed. The gap is capped so an outage of
    # this server itself can't hand out free uptime.
    SLOT = 1800
    ADAPT_MAX = 14400
    NORMAL_MAX = 3600
    PREMIUM_SCORE = 95  # displayed score 95 or above
    PREMIUM_CLEAN = 43200
    PREMIUM_LADDER = ((5400, 12), (7200, 24), (9000, 30), (10800, 36), (12600, 42), (14400, 48))  # (interval s, clean hours)

    def _emit_events(self) -> None:
        """Compare this tracker's state with last time and log any change to the event feed."""
        try:
            url, now = self.url, int(time())
            if self.status == 1:
                bad = []
                if PEER_FAILS.get(url, 0) >= PEER_FAIL_LIMIT:
                    bad.append("hands out no peers (3+ of its last 6 peer tests failed)")
                if FAKE_FAILS.get(url, 0) >= PEER_FAIL_LIMIT:
                    bad.append("returns fake peers (3+ of its last 6 checks)")
                df = FAM_FAILS.get(url) or {}
                if df.get("n", 0) >= PEER_FAIL_LIMIT:
                    bad.append(f"its published IPv{str(df.get('fam', '?'))[-1]} address is dead")
                pfb = _peer_fam_bad(url) if PEER_FAILS.get(url, 0) < PEER_FAIL_LIMIT else None
                if pfb:
                    bad.append(f"its IPv{pfb[-1]} side doesn't share peers (3+ of its last 6 IPv{pfb[-1]} peer tests failed)")
                if bad:
                    st = "up_bad"
                else:  # same ladder as the page/API, with a little hysteresis so borderline trackers don't flap
                    sc = round(float(self.uptime or 0) + _nt_iv_penalty(url))  # the interval penalty is only for ranking
                    ms = _nt_region_avg(url) or self.latency or 0
                    pst = (LAST_STATE.get(url) or {}).get("st")
                    if sc < (52 if pst == "up_junk" else 50):
                        st = "up_junk"
                    elif _nt_region_avg(url) is None and _nt_is_new(self, sc, 0):  # just added: no real latency yet
                        st = "up_good"
                    elif ms >= (290 if pst == "up_slow" else 300) and _nt_reliable(self, pst):
                        st = "up_slow"
                    elif sc < (91 if pst == "up_unreliable" else 90):
                        st = "up_good" if _nt_is_new(self, sc, ms) else ("up_slow" if (_nt_region_avg(self.url) or 0) >= 200 and round(float(self.uptime or 0) + _nt_iv_penalty(self.url) + _nt_lat_penalty(_nt_region_avg(self.url))) >= (91 if pst == "up_unreliable" else 90) else "up_unreliable")  # slow = under 90 from latency alone;  # new = held back only by the age ceiling
                    else:
                        st = "up_good"
            else:
                bad, st = [], "down"
            df = FAM_FAILS.get(url) or {}
            dead = [df["fam"]] if st != "down" and df.get("n", 0) >= PEER_FAIL_LIMIT and df.get("fam") else []
            prev = LAST_STATE.get(url)
            if self.to_be_deleted:
                why = _NT_DEL_REASON.pop(url, "no answer for 5+ days, or too unreliable: under 15% once dropouts are counted")
                _event(url, "removed", "removed from the list (" + why + ")")
                _removed_add(self, why)
                LAST_STATE.pop(url, None)
                _jsave(LAST_STATE, _LAST_STATE_FILE)
                return
            cur = {"st": st, "bad": bad, "dead": dead}
            if prev is None:
                if now - int(self.added or 0) < 86400:
                    _event(url, "added", "added to the list")
                cur["since"] = now
            elif {k: prev.get(k) for k in ("st", "bad", "dead")} != cur:
                ago = _dur(prev.get("since") or now).replace("\u2007", "").strip()
                if st == "down" and prev["st"] != "down":
                    _nt_quiet(self) or _event(url, "down", f"went Down ({getattr(self, '_last_err', 'no answer')})")
                elif st != "down" and prev["st"] == "down":
                    _nt_quiet(self) or _event(url, "up", f"is back Up after {ago} down" + (f", but {_nt_bad_lbl(bad)}: {'; '.join(bad)}" if bad else (f", but {_NT_LBL[st]}: {_nt_why(st, self)}" if st in _NT_LBL else "")))
                elif st == "up_bad" and prev["st"] != "up_bad":
                    _event(url, "bad", "is " + _nt_bad_lbl(bad) + ": " + "; ".join(bad))
                elif st == "up_bad" and prev.get("bad") != bad:
                    _event(url, "bad", "is " + ("still Up/Bad, now" if _nt_bad_lbl(bad) == "Up/Bad" else "now Up/Broken") + ": " + "; ".join(bad))
                elif st == "up_good" and prev["st"] != "up_good":
                    _event(url, "good", "is Up/Good again")
                elif st in _NT_LBL and prev["st"] != st:
                    _event(url, "bad", f"is {_NT_LBL[st]}: {_nt_why(st, self)}")
                if st != "down" and prev["st"] != "down":
                    for f in set(dead) - set(prev.get("dead") or []):
                        _event(url, "family", f"IPv{f[-1]} address confirmed dead")
                    for f in set(prev.get("dead") or []) - set(dead):
                        _event(url, "family", f"IPv{f[-1]} address answering again")
                cur["since"] = now if prev["st"] != st else prev.get("since", now)
            else:
                cur["since"] = prev.get("since", now)
            cur.update(_bad_track(prev, st, now, bad))
            if prev != cur:
                LAST_STATE[url] = cur
                _jsave(LAST_STATE, _LAST_STATE_FILE)
        except Exception:
            logger.exception("event feed: failed for %s", self.url)

    def _offline_skip(self) -> None:
        """This monitor is offline: record nothing for the tracker and look again in ~5 minutes."""
        self.last_checked = int(time()) - int(self.interval or 900) + 300
        logger.info("%s check skipped: this monitor is offline", self.url)

    def _record(self, status: int) -> None:
        now = int(time())
        LAST_REC[self.url] = now  # the newest slot's time, for the daily summary
        # history can't be older than the tracker: trim legacy per-check entries beyond its age in slots
        cap = max(1, (now - int(self.added or 0)) // self.SLOT + 2) if self.added else None
        while cap and len(self.historic) > cap:
            self.historic.popleft()
        prev = int(getattr(self, "_prev_checked", 0) or 0)
        if not len(self.historic) or prev <= 0:
            self.historic.append(status)
            return
        gap = now // self.SLOT - prev // self.SLOT
        if gap <= 0:
            self.historic[-1] = min(self.historic[-1], status)
            return
        gap = min(gap, self.ADAPT_MAX // self.SLOT + 1)
        last = self.historic[-1]
        fill = gap - 1
        old = fill // 2 if status != last else fill
        for _ in range(old):
            self.historic.append(last)
        for _ in range(fill - old):
            self.historic.append(status)
        self.historic.append(status)

    def _next_interval(self) -> int:
        recent = list(self.historic)[-12:]
        if self.status == 0:
            if 1 in recent:
                return 900  # just dropped / flapping: watch closely
            # dead ramp: 60 min, then +30 min per failed check, up to ADAPT_MAX
            return min(self.ADAPT_MAX, max(3600, (self.interval // 1800 + 1) * 1800))
        if 0 in recent:
            return 900
        if len(recent) < 3 or self.interval < 1800:
            return 1800
        # Premium ladder: score >= 95 and a clean record that grows with the interval (the longer
        # we look away, the longer the tracker must have been flawless). Everyone else tops out at NORMAL_MAX.
        step = (self.interval // 1800 + 1) * 1800
        if step <= self.NORMAL_MAX:
            return step
        if int(float(getattr(self, "_nt_base", self.uptime) or 0) + 0.5) < self.PREMIUM_SCORE:
            return self.NORMAL_MAX
        h = list(self.historic)
        clean = (len(h) - 1 - max(i for i, v in enumerate(h) if v == 0) if 0 in h else len(h)) * self.SLOT
        if self.last_downtime:
            clean = min(clean, int(time()) - int(self.last_downtime))
        best = self.NORMAL_MAX
        for iv, hrs in self.PREMIUM_LADDER:
            if iv <= step and clean >= hrs * 3600:
                best = iv
        return best

    def is_up(self) -> None:
        self.status = 1
        self.last_uptime = int(time())
        self._record(1)
        self.interval = self._next_interval()

    def is_down(self) -> None:
        self.status = 0
        self.last_downtime = int(time())
        self._record(0.5 if getattr(self, "_nt_partial", False) else 0)
        self._nt_partial = False
        self.interval = self._next_interval()

    @staticmethod
    def ip_api(ip: str) -> str:
        try:
            response = cast(HTTPResponse, request.urlopen("http://ip-api.com/line/" + ip + "?fields=country,countryCode,isp"))
            tracker_info = response.read().decode("utf-8")
            sleep(1.35)  # Respect the queries per minute limit of IP-API
        except OSError:
            tracker_info = "Error"
        return tracker_info


def format_uptime_and_downtime_time(trackers_unprocessed: list[Tracker]) -> list[Tracker]:
    for tracker in trackers_unprocessed:
        if tracker.status == 1:
            tracker.peer_ok = PEER_OK.get(tracker.url)
            tracker.peer_fails = PEER_FAILS.get(tracker.url, 0)
            tracker.fake_n = FAKE_N.get(tracker.url)
            tracker.fake_fails = FAKE_FAILS.get(tracker.url, 0)
            tracker.deadfam = FAM_FAILS.get(tracker.url)
            tracker.fams = FAMS.get(tracker.url)
            tracker.inflated = INFLATED.get(tracker.url)
            tracker.stale = STALE.get(tracker.url)
            tracker.cid_ok = CID_OK.get(tracker.url)
            tracker.status_epoch = tracker.last_downtime
            if not tracker.last_downtime:
                tracker.status_readable = "Up"
            else:
                tracker.status_readable = "Up " + _dur(tracker.last_downtime)
        elif tracker.status == 0:
            tracker.status_epoch = sys.maxsize
            tracker.down_why = DOWN_WHY.get(tracker.url)
            tracker.down_label = _nt_down_label(tracker.down_why)
            if not tracker.last_uptime:
                tracker.status_readable = "Down"
            else:
                tracker.status_readable = "Down " + _dur(tracker.last_uptime)

    _attach_extras(trackers_unprocessed)
    return trackers_unprocessed


def _q15(epoch):
    """Round a 'down for / working for' duration to the nearest 15 min (min 15): checks run at 15-min resolution."""
    if not epoch:
        return epoch
    now = int(time())
    d = max(0, now - int(epoch))
    return now - max(900, int((d + 450) // 900) * 900)


def _dur(epoch):
    """Status duration as 'Dd HHh' (whole hours elapsed), e.g. '3d 04h'."""
    d = max(0, int(time()) - int(epoch))
    if d < 3600:
        return f"{d // 60:2d}m".replace(" ", "\u2007")  # under an hour: minutes
    h = f"{d % 86400 // 3600:2d}h".replace(" ", "\u2007")  # figure-space pad: digits line up
    return f"{d // 86400}d {h}" if d >= 86400 else h


# Peer-test results by URL, kept in memory (the web page builds Tracker objects from the DB, which has no column for this).
PEER_OK: dict = {}


# Consecutive peer-test failures per URL, persisted so a restart doesn't wipe the penalty.
import json as _json, os as _os
PEER_FAIL_LIMIT = 3      # this many FAILs in a row ...
PEER_FAIL_CAP = 50.0     # ... caps the score here (1 star, red, out of every score-based API list)
_PEER_FAILS_FILE = "data/peer_fails.json"
try:
    with open(_PEER_FAILS_FILE) as _f:
        PEER_FAILS: dict = _json.load(_f)
except Exception:
    PEER_FAILS = {}


def _peer_fail_set(url, n):
    if PEER_FAILS.get(url, 0) == n:
        return
    if n:
        PEER_FAILS[url] = n
    else:
        PEER_FAILS.pop(url, None)
    try:
        tmp = _PEER_FAILS_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(PEER_FAILS, f)
        _os.replace(tmp, _PEER_FAILS_FILE)
    except OSError:
        pass


# Fake-peer / inflated-count checks (random-hash authenticity tests).
FAKE_N: dict = {}      # url -> fake peers seen at latest test
INFLATED: dict = {}    # url -> (seeders, leechers) reported when the truth is 1/1
_FAKE_FAILS_FILE = "data/fake_fails.json"
try:
    with open(_FAKE_FAILS_FILE) as _f:
        FAKE_FAILS: dict = _json.load(_f)
except Exception:
    FAKE_FAILS = {}


# Fake peers: judged over the last PEER_WINDOW probe results like the peer test, not a streak, so returning
# fake peers every other check can't dodge it. FAKE_FAILS[url] = fakes in that window (consumers unchanged).
_FAKE_HIST_FILE = "data/fake_hist.json"
try:
    with open(_FAKE_HIST_FILE) as _f:
        FAKE_HIST: dict = _json.load(_f)
except Exception:
    FAKE_HIST = {}


def _fake_hist_add(url, fake):
    h = FAKE_HIST.get(url)
    if h is None:  # seed from the old streak counter
        h = [1] * min(FAKE_FAILS.get(url, 0), PEER_WINDOW)
    h = (h + [1 if fake else 0])[-PEER_WINDOW:]
    FAKE_HIST[url] = h
    try:
        tmp = _FAKE_HIST_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(FAKE_HIST, f)
        _os.replace(tmp, _FAKE_HIST_FILE)
    except OSError:
        pass
    _ctr_set(FAKE_FAILS, _FAKE_FAILS_FILE, url, h.count(1))


def _ctr_set(d, path, url, n):
    if d.get(url, 0) == n:
        return
    if n:
        d[url] = n
    else:
        d.pop(url, None)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(d, f)
        _os.replace(tmp, path)
    except OSError:
        pass


# Informational tracker-hygiene checks (no score effect).
STALE: dict = {}   # url -> True if it keeps handing out a peer after 'stopped'
CID_OK: dict = {}  # url -> True if it rejects a made-up UDP connection ID, False if it accepts one


# Persist latest probe results so restarts don't blank the status marks.
_PROBE_FILE = "data/probe_state.json"


def _probe_save():
    try:
        tmp = _PROBE_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump({"peer_ok": PEER_OK, "fake_n": FAKE_N, "inflated": INFLATED, "stale": STALE, "cid_ok": CID_OK}, f)
        _os.replace(tmp, _PROBE_FILE)
    except OSError:
        pass


try:
    with open(_PROBE_FILE) as _f:
        _st = _json.load(_f)
    PEER_OK.update(_st.get("peer_ok", {}))
    FAKE_N.update(_st.get("fake_n", {}))
    INFLATED.update({k: (tuple(v) if v else v) for k, v in _st.get("inflated", {}).items()})
    STALE.update(_st.get("stale", {}))
    CID_OK.update(_st.get("cid_ok", {}))
except Exception:
    pass


# Dual-stack test. FAMS: {url: {"v4": bool, "v6": bool}}; FAM_FAILS: {url: {"n": consecutive fails, "fam": "v4"|"v6"}}
_FAM_FAILS_FILE = "data/fam_fails.json"
_FAMS_FILE = "data/fams.json"


def _jload(path):
    try:
        with open(path) as f:
            return _json.load(f)
    except Exception:
        return {}


def _jsave(d, path):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(d, f)
        _os.replace(tmp, path)
    except OSError:
        pass


FAM_FAILS: dict = _jload(_FAM_FAILS_FILE)
FAMS: dict = _jload(_FAMS_FILE)
_DOWN_WHY_FILE = "data/down_why.json"
DOWN_WHY: dict = _jload(_DOWN_WHY_FILE)


def _nt_down_label(raw):
    """Short label for a recorded failure cause. None if unrecognised (the page then shows plain Down)."""
    r = str(raw or "").lower()
    if not r:
        return None
    if "tracker error message" in r or "error while announcing" in r or "error while trying to get a connection response" in r:
        return "Rejected"  # the tracker answered with its own error message (first: the message may mention timeouts etc.)
    if "try again" in r or "name or service" in r or "resolve" in r or "nodename" in r or "getaddrinfo" in r or "no address" in r:
        return "DNS"
    if "timeout" in r or "timed out" in r:
        return "Timeout"
    if "refused" in r or "connection failed" in r or "unreachable" in r or "reset" in r:
        return "Refused"
    if "ssl" in r or "tls" in r or "certificate" in r:
        return "TLS"
    if "http" in r and __import__("re").search(r"\b[45]\d\d\b", r):
        return "HTTP error"
    return None


# Sections of /fix, by problem. One rule for the table, the tracker page and the Telegram messages.
FIX_DOWN = {"DNS": "down-dns", "Timeout": "down-timeout", "Refused": "down-refused", "TLS": "down-tls", "HTTP error": "down-http",
            "Rejected": "down-rejected"}
FORCE_CHECK: set = set()  # URLs a visitor asked to check again now (rate-limited in ntextra); the check loop takes them next


def _nt_fix_for_event(ev):
    """The /fix section for a feed event, or None if it isn't a problem."""
    kind, text = ev.get("type"), str(ev.get("text") or "")
    low = text.lower()
    if kind == "down":
        return FIX_DOWN.get(_nt_down_label(text), "down")
    if kind == "family":
        return "dead-address" if "dead" in low else None
    if kind == "up" and "but" not in low:
        return None
    if kind not in ("bad", "up"):
        return None
    if "fake peers" in low:
        return "fake-peers"
    if "no peers" in low:
        return "no-peers"
    if "address is dead" in low or "side doesn't share peers" in low:
        return "dead-address"
    if "up/slow" in low:
        return "slow"
    if "up/unreliable" in low or "up/junk" in low:
        return "unreliable"
    return None


def _nt_down_why_set(url, raw):
    try:
        DOWN_WHY[url] = str(raw)[:200]
        _jsave(DOWN_WHY, _DOWN_WHY_FILE)
    except Exception:
        pass




FAM_RECOVER = 2  # good checks in a row before a confirmed-dead address counts as answering again (3 failures confirm it)
FAM_HIST_N = 20  # per-family answers kept for the tracker page ("IPv6 answered 2 of the last 20 checks")
_FAM_HIST_FILE = "data/fam_hist.json"
FAM_HIST: dict = _jload(_FAM_HIST_FILE)  # url -> {"v4": [1, 0, ...], "v6": [...]}, oldest first


def _fam_set(url, fam, res=None):
    if res is not None and FAMS.get(url) != res:
        if res:
            FAMS[url] = res
        else:
            FAMS.pop(url, None)
        _jsave(FAMS, _FAMS_FILE)
    if res:
        h = FAM_HIST.setdefault(url, {})
        for k, v in res.items():
            h[k] = (h.get(k, []) + [1 if v else 0])[-FAM_HIST_N:]
        _jsave(FAM_HIST, _FAM_HIST_FILE)
    cur = FAM_FAILS.get(url)
    if fam:
        new = {"n": (cur or {}).get("n", 0) + 1 if (cur or {}).get("fam") == fam else 1, "fam": fam}
    elif cur and cur.get("n", 0) >= PEER_FAIL_LIMIT:
        # confirmed dead: one lucky answer doesn't bring it back, FAM_RECOVER good checks in a row do
        ok = cur.get("ok", 0) + 1
        new = None if ok >= FAM_RECOVER else dict(cur, ok=ok)
    else:
        new = None
    if new != cur:
        if new:
            FAM_FAILS[url] = new
        else:
            FAM_FAILS.pop(url, None)
        _jsave(FAM_FAILS, _FAM_FAILS_FILE)


# Peer test: judge the last PEER_WINDOW conclusive results (n/a ignored), not just a streak.
# PEER_FAILS[url] = fails within that window, so every existing consumer (>= 3 -> Junk / score 0) is unchanged.
PEER_WINDOW = 6
_PEER_HIST_FILE = "data/peer_hist.json"
try:
    with open(_PEER_HIST_FILE) as _f:
        PEER_HIST: dict = _json.load(_f)
except Exception:
    PEER_HIST = {}


# Peer results are also kept per family ("v4"/"v6"), since a tracker can share peers on one and not the other.
# "?" holds results from before families were told apart; it counts only until a real family has PEER_WINDOW // 2 results.
_PEER_HIST_FAM_FILE = "data/peer_hist_fam.json"
PEER_HIST_FAM: dict = _jload(_PEER_HIST_FAM_FILE)  # url -> {"v4": [1, 0, ...], "v6": [...], "?": [...]}, oldest first


def _peer_fams(url):
    """The per-family histories that count. A family counts once it has PEER_WINDOW // 2 results (one result is no
    evidence); until one does, the pre-split '?' history decides, or for a new tracker whatever results there are."""
    h = PEER_HIST_FAM.get(url) or {}
    real = {k: v for k, v in h.items() if k != "?" and len(v) >= PEER_WINDOW // 2}
    if real:
        return real
    if h.get("?"):
        return {"?": h["?"]}
    return {k: v for k, v in h.items() if k != "?" and v}


def _peer_fam_bad(url):
    """'v4'/'v6' if that family fails the peer test (3+ of its last 6) while another family passes: partly broken."""
    fams = {k: v for k, v in _peer_fams(url).items() if k != "?"}
    bad = [k for k, v in fams.items() if v.count(0) >= PEER_FAIL_LIMIT]
    good = [k for k, v in fams.items() if v.count(0) < PEER_FAIL_LIMIT and len(v) >= PEER_WINDOW // 2]
    return bad[0] if bad and good else None


def _peer_hist_add(url, ok, fam=None):
    h = PEER_HIST.get(url)
    if h is None:  # seed from the old streak counter
        h = [0] * min(PEER_FAILS.get(url, 0), PEER_WINDOW)
    hf = PEER_HIST_FAM.setdefault(url, {})
    if not hf and h:
        hf["?"] = list(h)  # results from before families were told apart
    h = (h + [1 if ok else 0])[-PEER_WINDOW:]
    PEER_HIST[url] = h
    key = fam if fam in ("v4", "v6") else "?"
    hf[key] = (hf.get(key, []) + [1 if ok else 0])[-PEER_WINDOW:]
    try:
        tmp = _PEER_HIST_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(PEER_HIST, f)
        _os.replace(tmp, _PEER_HIST_FILE)
    except OSError:
        pass
    _jsave(PEER_HIST_FAM, _PEER_HIST_FAM_FILE)
    # "hands out no peers" (Up/Bad) only when every family that counts fails; one failing family is _peer_fam_bad
    fams = _peer_fams(url)
    _peer_fail_set(url, min(v.count(0) for v in fams.values()) if fams else h.count(0))


# A peer test that's never conclusive (only our first test client is ever answered) would freeze its last 6 results
# for good. After PEER_NA_DAYS with no conclusive result, while the test works for other trackers, each check counts
# as a failure: it can't show that it shares peers.
PEER_NA_DAYS = 7
_PEER_LAST_FILE = "data/peer_last.json"
try:
    with open(_PEER_LAST_FILE) as _f:
        PEER_LAST: dict = _json.load(_f)  # url -> last conclusive peer test (or when we started waiting for one)
except Exception:
    PEER_LAST = {}
_PEER_ANY = [0.0]  # last conclusive peer test for any tracker: the test itself works


def _peer_conclusive(url, ok, now=None):
    now = now or time()
    if ok is not None:
        _PEER_ANY[0] = now
        if now - PEER_LAST.get(url, 0) < 3600:
            return  # saved at most hourly
        PEER_LAST[url] = int(now)
    elif url not in PEER_LAST:
        PEER_LAST[url] = int(now)  # start waiting from now
    elif now - PEER_LAST[url] >= PEER_NA_DAYS * 86400 and now - _PEER_ANY[0] < 3600:
        logger.info("%s peer test inconclusive for %d+ days: counted as a failure", url, PEER_NA_DAYS)
        _peer_hist_add(url, False)
        return
    else:
        return
    try:
        tmp = _PEER_LAST_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(PEER_LAST, f)
        _os.replace(tmp, _PEER_LAST_FILE)
    except OSError:
        pass


_ONLINE = [0.0, True]
_FAILSTREAK = [0]  # consecutive failed checks across all trackers (reset by any success)


def _monitor_online() -> bool:
    """Is this monitor itself online (network AND DNS)? Asked only when a tracker check fails.
    'online' is cached 5 min, 'offline' 60 s; 2+ failures in a row with no success between bypass the cache,
    so an outage is spotted within one failed check."""
    import socket as _s
    now = time()
    _FAILSTREAK[0] += 1
    age = now - _ONLINE[0]
    if (_ONLINE[1] and age < 300 and _FAILSTREAK[0] < 2) or (not _ONLINE[1] and age < 60):
        return _ONLINE[1]
    ok = False
    for host in ("1.1.1.1", "8.8.8.8", "9.9.9.9", "2606:4700:4700::1111", "2001:4860:4860::8888"):
        try:
            with _s.create_connection((host, 443), timeout=3):
                ok = True
                break
        except OSError:
            continue
    if ok:  # network works; also require working DNS, or every tracker lookup fails and gets blamed on the trackers
        ok = False
        for name in ("one.one.one.one", "dns.google", "quad9.net"):
            try:
                _s.getaddrinfo(name, 443)
                ok = True
                break
            except OSError:
                continue
    _ONLINE[0], _ONLINE[1] = now, ok
    if ok:
        _FAILSTREAK[0] = 0  # confirmed online: these failures are the trackers' own
    else:
        logger.warning("monitor has no internet or DNS: tracker failures are not being recorded")
    return ok


# ---- extras: tracker's own announce interval, latency by region, stats, same-operator groups ----
_ANN_IV_FILE = "data/ann_iv.json"
_REGION_FILE = "data/region_lat.json"
ANN_IV: dict = _jload(_ANN_IV_FILE)
REGION_LAT: dict = _jload(_REGION_FILE)


_WARN_FILE = "data/warnings.json"
WARNINGS: dict = _jload(_WARN_FILE)  # url -> {"msg", "t"}: the 'warning message' in its last successful reply


def _warn_set(url, msg):
    """Keep the tracker's own warning (e.g. 'Require passkey'), or clear it once a reply has none. Saved only on change."""
    msg = str(msg).strip()[:200] if msg else ""
    cur = (WARNINGS.get(url) or {}).get("msg", "")
    if msg == cur:
        return
    if msg:
        WARNINGS[url] = {"msg": msg, "t": int(time())}
    else:
        WARNINGS.pop(url, None)
    _jsave(WARNINGS, _WARN_FILE)


# The HTTP(S) peer test was reviewed on 2026-10-05 (10 of 27 HTTP trackers failed it vs 3 of 49 UDP): reproduced by hand,
# every failing one was genuinely broken (records nobody, fixed counts, shares peers only within one IP, local-only
# retrackers) and a working one passed. Set to False to stop HTTP(S) trackers being removed for it while re-checking.
HTTP_PEER_TEST_TRUSTED = True


# The peer test runs over whichever family answered and keeps one result, so a tracker that shares peers on IPv4 but not
# on IPv6 (tracker.farted.net, 2026-10-05) gets recorded as failing outright. Until results are kept per family, failing
# it doesn't remove a tracker that publishes both families (single-family trackers are unaffected).
PEER_TEST_PER_FAMILY = True  # results are kept per family since 2026-10-06 (_peer_fam_bad)


def _peer_rule_applies(url):
    if not (HTTP_PEER_TEST_TRUSTED or str(url).startswith("udp")):
        return False
    alive = [f for f, ok in (FAMS.get(url) or {}).items() if ok]
    if len(alive) >= 2:
        if not PEER_TEST_PER_FAMILY:
            return False
        # answering on both families: no removal for the peer test until each family has its own evidence (tracker.farted.net
        # was removed on its pre-split history 20 minutes after per-family testing started, though IPv4 passed)
        h = PEER_HIST_FAM.get(url) or {}
        if not all(len(h.get(f, [])) >= PEER_WINDOW // 2 for f in alive):
            return False
    return True


REMOVE_DAYS = 5  # Down (no answer) or Up/Bad (no or fake peers) this long and it's removed and banned for 30 days
UPBAD_DAYS = REMOVE_DAYS  # one clock for both
BAN_STEPS = (30, 90, None)  # ban days for a host's 1st, 2nd and 3rd+ removal (None: for good)
_DENY_FILE = "data/denylist.txt"


def _nt_ban_entries():
    """The denylist as (host, since, days): 'host' = for good (None, None); 'host <epoch>' = 30 days; 'host <epoch> <days>'."""
    try:
        lines = open(_DENY_FILE, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        p = ln.split()
        if not p or p[0].startswith("#"):
            continue
        if len(p) > 1 and p[1].isdigit():
            out.append((p[0].lower(), int(p[1]), int(p[2]) if len(p) > 2 and p[2].isdigit() else 30))
        else:
            out.append((p[0].lower(), None, None))
    return out


def _nt_banned(host, now=None):
    """Is the host banned right now (a permanent entry, or a dated one that hasn't run out)?"""
    now = now or time()
    host = (host or "").lower()
    return any(h == host and (since is None or now - since <= days * 86400) for h, since, days in _nt_ban_entries())


BAD_BRIDGE = 12 * 3600  # an Up/Good spell shorter than this doesn't restart the Up/Bad clock


def _nt_upbad_days(url, now=None):
    """Days it's been Up/Bad for no peers or fake peers (a dead address doesn't count), or None. Counted from the start of
    the current bad stretch: recoveries under BAD_BRIDGE don't reset it, so briefly passing a test can't dodge removal."""
    s = LAST_STATE.get(url) or {}
    if s.get("st") != "up_bad" or not any("no peers" in b or "fake peers" in b for b in (s.get("bad") or [])):
        return None
    now = now or time()
    start = s.get("bad_since", s.get("since"))
    return (now - int(now if start is None else start)) / 86400.0


def _useless(st, bad):
    """Not working for anyone: down, or answering but handing out no peers or fake ones."""
    return st == "down" or _peer_bad(st, bad)


def _nt_useless_days(url, now=None):
    """Days it's been down or Up/Bad (no/fake peers) in one stretch, either way round (spells of working under
    BAD_BRIDGE don't break it), or None if it's working now. Closes the gap where 4.9 days down then 4.9 days Up/Bad
    never reached either 5-day clock."""
    s = LAST_STATE.get(url) or {}
    if not _useless(s.get("st"), s.get("bad")):
        return None
    now = now or time()
    start = s.get("useless_since", s.get("since"))
    return (now - int(now if start is None else start)) / 86400.0


def _useless_seed(trackers, now=None):
    """For states saved before useless_since existed: Up/Bad now, after a run of failed checks just before it went Up/Bad,
    counts from the start of that run. Run once from the check loop (it needs each tracker's history)."""
    now = now or time()
    changed = False
    for t in trackers:
        s = LAST_STATE.get(t.url)
        if not isinstance(s, dict) or "useless_since" in s or not _useless(s.get("st"), s.get("bad")):
            continue
        start = int(s.get("since", now))
        if s.get("st") != "down":
            h = list(t.historic or [])
            newest = int(LAST_REC.get(t.url) or t.last_checked or now) // Tracker.SLOT
            first_bad = newest - (newest - start // Tracker.SLOT)  # slot where the Up/Bad spell began
            i = len(h) - 1 - (newest - first_bad) - 1  # the slot just before it
            skipped = 0  # answering spells under BAD_BRIDGE (e.g. its first hours back, before 3 failed peer tests) don't count
            while i >= 0 and float(h[i]) > 0 and skipped < BAD_BRIDGE // Tracker.SLOT:
                skipped += 1
                i -= 1
            zeros = 0
            while i >= 0 and float(h[i]) == 0:
                zeros += 1
                i -= 1
            if zeros:
                start -= (skipped + zeros) * Tracker.SLOT
        s["useless_since"] = start
        changed = True
    if changed:
        _jsave(LAST_STATE, _LAST_STATE_FILE)


JUNK_DAYS = 30  # Up/Junk or Up/Broken this long and it's removed: long enough for overload to pass or an address to be fixed
JUNK_BRIDGE = 24 * 3600  # spells out of it shorter than this don't restart that count


def _broken(st, bad):
    """Up/Broken: its only faults are a dead IPv4/IPv6 address or one family not sharing peers (the feed ladder: up_bad)."""
    return st == "up_bad" and bool(bad) and all("address is dead" in b or "side doesn't share peers" in b for b in bad)


def _poor(st, bad):
    """Up/Junk or Up/Broken: one 30-day clock for both, so flipping between them doesn't restart it."""
    return st == "up_junk" or _broken(st, bad)


def _nt_junk_days(url, now=None):
    """Days it's been Up/Junk or Up/Broken in one stretch (spells out of it under JUNK_BRIDGE don't break it), or None."""
    s = LAST_STATE.get(url) or {}
    if not _poor(s.get("st"), s.get("bad")):
        return None
    now = now or time()
    start = s.get("junk_since", s.get("since"))
    return (now - int(now if start is None else start)) / 86400.0


BAD_SHARE_DAYS, BAD_SHARE = 7, 0.8  # Up/Bad for 80% of the last 7 days is removed too, whatever its good spells


def _peer_bad(st, bad):
    return st == "up_bad" and any("no peers" in b or "fake peers" in b for b in (bad or []))


def _nt_bad_share(url, now=None):
    """Share of the last BAD_SHARE_DAYS it was Up/Bad for no or fake peers (closed spells in bad_log, plus the current one)."""
    s = LAST_STATE.get(url) or {}
    now = now or time()
    w0, span = now - BAD_SHARE_DAYS * 86400, BAD_SHARE_DAYS * 86400
    tot = sum(max(0, min(e, now) - max(b, w0)) for b, e in (s.get("bad_log") or []))
    if _peer_bad(s.get("st"), s.get("bad")):
        tot += max(0, now - max(int(s.get("since", now)), w0))
    return tot / span


def _bad_track(prev, st, now, bad=None):
    """The bad-stretch fields for a tracker's new state: bad_since (start of the stretch), bad_left (when it last
    stopped being Up/Bad, kept while a return within BAD_BRIDGE would continue the stretch) and bad_log (its closed
    Up/Bad spells over the last BAD_SHARE_DAYS)."""
    d = _bad_stretch(prev, st, now)
    d.update(_stretch(prev, st, now, _poor, "junk", JUNK_BRIDGE, bad))
    d.update(_stretch(prev, st, now, _useless, "useless", BAD_BRIDGE, bad))
    prev = prev or {}
    log = [x for x in (prev.get("bad_log") or []) if now - x[1] <= BAD_SHARE_DAYS * 86400]
    if _peer_bad(prev.get("st"), prev.get("bad")) and not _peer_bad(st, bad):
        log.append([int(prev.get("since", now)), int(now)])
    if log:
        d["bad_log"] = log
    return d


def _bad_stretch(prev, st, now):
    return _stretch(prev, st, now, "up_bad", "bad", BAD_BRIDGE)


def _stretch(prev, st, now, state, key, bridge, bad=None):
    """<key>_since: start of the current stretch in `state` (a state name, or a predicate on (st, bad)), where spells out
    of it shorter than `bridge` don't break it; <key>_left: when it last left, kept while a return would continue it."""
    prev = prev or {}
    since_k, left_k = key + "_since", key + "_left"
    inside = state if callable(state) else (lambda s, b: s == state)
    was = inside(prev.get("st"), prev.get("bad"))
    start = prev.get(since_k)
    if start is None and was:
        start = prev.get("since")
    recent = prev.get(left_k) is not None and now - int(prev[left_k]) < bridge and start is not None
    if inside(st, bad):
        return {since_k: int(start) if (was or recent) and start is not None else now}
    if was:
        return {since_k: int(start if start is not None else now), left_k: now}
    if recent:
        return {since_k: int(start), left_k: int(prev[left_k])}
    return {}


def _bad_log_seed(now=None):
    """Fill in bad_log (closed Up/Bad spells over the last week) from the event history for states saved before it existed."""
    now = now or time()
    changed = False
    for url, s in LAST_STATE.items():
        if not isinstance(s, dict) or "bad_log" in s:
            continue
        log, start = [], None
        for e in EVENTS:
            if e.get("url") != url:
                continue
            txt, kind, t = str(e.get("text") or ""), e.get("type"), int(e.get("t") or 0)
            if (kind == "bad" and txt.startswith("is Up/Bad")) or (kind == "up" and ", but Up/Bad" in txt):
                start = t if start is None else start
            elif start is not None and kind in ("good", "down", "up", "bad") and not txt.startswith("is still Up/Bad"):
                log.append([start, t])
                start = None
        log = [x for x in log if now - x[1] <= BAD_SHARE_DAYS * 86400]
        if log:
            s["bad_log"] = log
            changed = True
    if changed:
        _jsave(LAST_STATE, _LAST_STATE_FILE)


def _bad_seed(now=None):
    """Fill in bad_since/bad_left from the event history for states saved before they existed."""
    now = now or time()
    changed = False
    for url, s in LAST_STATE.items():
        if "bad_since" in s or not isinstance(s, dict):
            continue
        start = left = None
        for e in EVENTS:
            if e.get("url") != url:
                continue
            txt, kind, t = str(e.get("text") or ""), e.get("type"), int(e.get("t") or 0)
            if (kind == "bad" and txt.startswith("is Up/Bad")) or (kind == "up" and ", but Up/Bad" in txt):
                if start is None or left is None or t - left >= BAD_BRIDGE:
                    start = t
                left = None
            elif start is not None and left is None and kind in ("good", "down", "up", "bad") and not txt.startswith("is still Up/Bad"):
                left = t
        if s.get("st") == "up_bad":
            s["bad_since"] = start if start is not None and left is None else s.get("since", int(now))
            changed = True
        elif start is not None and left is not None and now - left < BAD_BRIDGE:
            s["bad_since"], s["bad_left"] = start, left
            changed = True
    if changed:
        _jsave(LAST_STATE, _LAST_STATE_FILE)


_CLOSED_FILE = "data/closed.json"
CLOSED: dict = _jload(_CLOSED_FILE)  # url -> {"why", "n"}: replies in a row saying it's private or whitelist-only
CLOSED_LIMIT = 3


def _nt_closed_reason(msg):
    """Why a tracker's own message means it can't be public, or None. Strong signals only."""
    m = str(msg or "").lower()
    if any(k in m for k in ("passkey", "authkey", "auth key")):
        return "a private tracker: it asks for a passkey"
    if any(k in m for k in ("not authorized", "not authorised", "unregistered torrent", "torrent not registered", "not registered with this tracker", "whitelist")):
        return "it only serves its own torrents (a whitelist)"
    return None


def _closed_seen(url, msg):
    """Count a reply that says the tracker is private or whitelist-only; any other reply resets the count."""
    why = _nt_closed_reason(msg)
    cur = CLOSED.get(url)
    if why:
        CLOSED[url] = {"why": why, "n": (cur or {}).get("n", 0) + 1}
    elif cur is None:
        return
    else:
        CLOSED.pop(url)
    _jsave(CLOSED, _CLOSED_FILE)


def _ann_iv_set(url, iv):
    if ANN_IV.get(url) != iv:
        ANN_IV[url] = iv
        _jsave(ANN_IV, _ANN_IV_FILE)


_REGION_TS_FILE = "data/region_ts.json"  # {url: {region: [[ts, ms], ...]}}
_rs = _jload(_REGION_TS_FILE)
REGION_SAMPLES: dict = _rs if all(isinstance(v, dict) and all(isinstance(x, list) for x in v.values()) for v in _rs.values()) else {}


def _region_set(url, d):
    """One figure per region: median of its last 12 samples (48 h), whichever server that region's exit was using."""
    now = int(time())
    per = REGION_SAMPLES.setdefault(url, {})
    for k, v in (d or {}).items():
        per.setdefault(k.split(":")[0].strip(), []).append([now, int(v)])
    for reg in list(per):
        per[reg] = [x for x in per[reg] if now - x[0] <= 172800][-12:]
        if not per[reg]:
            per.pop(reg)
    med = {reg: sorted(x[1] for x in ss)[len(ss) // 2] for reg, ss in per.items()}
    REGION_LAT[url] = {k: med[k] for k in sorted(med, key=lambda x: (x != "Oceania", x))}
    _jsave(REGION_LAT, _REGION_FILE)
    _jsave(REGION_SAMPLES, _REGION_TS_FILE)
    _lat_hist_add(url, now)


# Latency history for the tracker page: {url: {region: [[ts, ms], ...]}}, the region's current median
# at most once per LAT_HIST_STEP, kept LAT_HIST_DAYS. Seeded from the 48 h of samples above.
_LAT_HIST_FILE = "data/lat_hist.json"
LAT_HIST_STEP = 7200
LAT_HIST_DAYS = 30
_lh = _jload(_LAT_HIST_FILE)
LAT_HIST: dict = _lh if isinstance(_lh, dict) else {}


def _lat_hist_add(url, now):
    per = LAT_HIST.get(url)
    if per is None:  # first time: start from the recent samples
        per = LAT_HIST[url] = {reg: [[ts - ts % LAT_HIST_STEP, ms] for ts, ms in ss] for reg, ss in (REGION_SAMPLES.get(url) or {}).items()}
        for reg, ss in per.items():
            per[reg] = list({x[0]: x for x in ss}.values())
    changed = False
    slot = now - now % LAT_HIST_STEP
    for reg, ms in (REGION_LAT.get(url) or {}).items():
        h = per.setdefault(reg, [])
        if h and h[-1][0] >= slot:
            continue
        h.append([slot, int(ms)])
        changed = True
    if changed:
        for u in list(LAT_HIST):  # prune old samples, and trackers removed from the list (nothing new for 30 days)
            p = LAT_HIST[u]
            for reg in list(p):
                p[reg] = [x for x in p[reg] if now - x[0] <= LAT_HIST_DAYS * 86400]
                if not p[reg]:
                    p.pop(reg)
            if not p:
                LAT_HIST.pop(u)
        _jsave(LAT_HIST, _LAT_HIST_FILE)


# Daily summary, kept forever (the rest of the history is rolling): {url: [[day, up %, slots, {region: ms}], ...]}.
# Worked out from historic (one value per 30-minute slot, the newest at the tracker's last record), so nothing is
# written per check, and days missed while the app was down are filled in later (up to the 30 days historic holds).
# A day is added once the tracker's history has moved past it. Latency is the median of that day's LAT_HIST values,
# each already a 48 h median: a typical figure, not that day's own median. Trackers removed from the list keep theirs.
_DAILY_FILE = "data/daily.json"
LAST_REC: dict = {}  # url -> time of its newest slot (in memory; after a restart, last_checked stands in)
_DAILY_SAVE: dict = {}  # "t": time of the last save, "dirty": unsaved days (recomputed from historic if lost)


def _daily_load(path):
    """Load the permanent summary. A file that exists but can't be read is moved aside, never overwritten."""
    if not _os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            d = _json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    bad = "%s.bad-%d" % (path, int(time()))
    try:
        _os.replace(path, bad)
    except OSError:
        pass
    logger.error("daily summary: %s unreadable, kept as %s; starting a new one", path, bad)
    return {}


DAILY: dict = _daily_load(_DAILY_FILE)


def _day(ts):
    from time import gmtime, strftime
    return strftime("%Y-%m-%d", gmtime(ts))


def daily_update(trackers, now=None):
    """Add each tracker's completed UTC days that DAILY doesn't have yet. Saves at most every 15 minutes."""
    now = int(now or time())
    for t in trackers:
        h = list(t.historic or [])
        newest = int(LAST_REC.get(t.url) or t.last_checked or 0) // Tracker.SLOT  # slot number of the newest value
        if not h or newest <= 0:
            continue
        cur = _day(newest * Tracker.SLOT)
        rows = DAILY.get(t.url) or []
        last = rows[-1][0] if rows else ""
        if last and last >= _day(newest * Tracker.SLOT - 86400):
            continue  # up to date: every day before the newest slot's is in
        days: dict = {}
        for p, v in enumerate(reversed(h)):
            d = _day((newest - p) * Tracker.SLOT)
            if d >= cur:
                continue  # the newest slot's day isn't over yet
            if d <= last:
                break
            c = days.setdefault(d, [0, 0])
            c[0] += 1 if float(v) > 0 else 0
            c[1] += 1
        if not days:
            continue
        lat = LAT_HIST.get(t.url) or {}
        for d in sorted(days):
            up, n = days[d]
            lm = {}
            for reg, ss in lat.items():
                v = sorted(ms for ts, ms in ss if _day(ts) == d)
                if v:
                    lm[reg] = v[len(v) // 2]
            rows.append([d, round(100.0 * up / n, 1), n, lm])
        DAILY[t.url] = rows
        _DAILY_SAVE["dirty"] = True
    if _DAILY_SAVE.get("dirty") and now - _DAILY_SAVE.get("t", 0) >= 900:
        _jsave(DAILY, _DAILY_FILE)
        _DAILY_SAVE.update(t=now, dirty=False)


_PSL2 = {"co.nz", "org.nz", "net.nz", "co.uk", "org.uk", "com.au", "net.au", "org.au", "co.jp", "com.br", "com.cn",
         "net.cn", "org.cn", "com.tr", "co.za", "com.ru", "co.in", "co.kr", "com.tw", "com.hk"}
_DDNS = {"duckdns.org", "ydns.eu", "ddnsfree.com", "ddns.net", "no-ip.org", "no-ip.com", "dynu.net", "kro.kr", "hopto.org",
         "zapto.org", "mooo.com", "eu.org", "github.io", "afraid.org", "dns.army", "linkpc.net", "servehttp.com",
         "sytes.net", "myftp.org", "freeddns.org", "dnsfor.me", "airdns.org"}


def _base_domain(host):
    p = (host or "").lower().strip(".").split(".")
    return ".".join(p[-3:]) if len(p) >= 3 and ".".join(p[-2:]) in _PSL2 else ".".join(p[-2:])


def _runs(seq, val):
    out, n = [], 0
    for s in seq:
        if s == val:
            n += 1
        elif n:
            out.append(n)
            n = 0
    if n:
        out.append(n)
    return out


def _nt_avail_stab(historic):
    """Recency-weighted availability and stability (each 0..1): the two factors of the score.
    Half-life 336 slots (7 days of 30-minute slots). stability = (1 - flip rate) squared."""
    seq = list(reversed(historic or []))  # newest first
    n = len(seq)
    weighted = total_w = flips_w = pairs_w = 0.0
    for age, s in enumerate(seq):
        w = 0.5 ** (age / 336.0)
        weighted += float(s) * w
        total_w += w
        if age + 1 < n:
            pairs_w += w
            if s != seq[age + 1]:
                flips_w += w
    availability = weighted / total_w if total_w else 0.0
    flip_rate = flips_w / pairs_w if pairs_w else 0.0
    return availability, (1.0 - flip_rate) ** 2


def _stats(t):
    h = [int(x) for x in (t.historic or [])]
    w = h[-336:]
    st = {"days": round(len(h) * 0.5 / 24, 1)}
    if w:
        st["avail7"] = round(100.0 * sum(w) / len(w), 1)
        st["outages7"] = len(_runs(w, 0))
    if h:
        st["avail_all"] = round(100.0 * sum(h) / len(h), 1)
        _av, _sb = _nt_avail_stab(t.historic)
        st["avail_weighted"] = round(100.0 * _av, 1)
        st["stability"] = round(100.0 * _sb, 1)
        _base = 100.0 * _av * _sb
        st["base"] = round(_base, 1)
        st["base_r"] = int(_base + 0.5)  # rounded once from the real figure, same as the displayed score
        if len(h) < 336:
            st["ceiling_r"] = int(80.0 + 20.0 * len(h) / 336.0 + 0.5)
            st["ceiling"] = round(80.0 + 20.0 * len(h) / 336.0, 1)  # new-tracker ceiling, rises to 100 over 7 days
        _ms = _nt_region_avg(t.url)
        _pen = _nt_lat_penalty(_ms)
        _ivp = _nt_iv_penalty(t.url)
        st["lat_ms"] = _ms
        st["lat_penalty"] = round(_pen, 1)
        st["iv_penalty"] = int(_ivp)
        st["iv"] = ANN_IV.get(t.url)
        if min(_base, st.get("ceiling", 100.0)) - _pen - _ivp - float(t.uptime or 0) > 1.0:
            st["capped"] = True  # a quality-test cap (fake peers, dead IP family, failed peer test) is holding the score down
        r = _runs(h, 0)
        st["longest_h"] = max(r) * 0.5 if r else 0
    ph = PEER_HIST.get(t.url)
    if ph:
        st["peer"] = f"{sum(ph)}/{len(ph)}"
    st["ann_iv"] = ANN_IV.get(t.url)
    return st


def _attach_extras(trackers):
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen_ip, seen_dom = {}, {}
    for t in trackers:
        u = t.url
        find(u)
        cf = any("cloudflare" in str(n).lower() for n in (t.networks or []))
        ri = getattr(t, "recent_ips", None) or {}
        ips = set(t.ips or []) | set(ri.keys() if isinstance(ri, dict) else ri)
        if not cf:  # anycast CDN IPs are shared by unrelated sites
            for ip in ips:
                if ip in seen_ip:
                    parent[find(u)] = find(seen_ip[ip])
                else:
                    seen_ip[ip] = u
        d = _base_domain(t.host)
        if d and d not in _DDNS:
            if d in seen_dom:
                parent[find(u)] = find(seen_dom[d])
            else:
                seen_dom[d] = u
    groups = {}
    for t in trackers:
        groups.setdefault(find(t.url), []).append(t.url)
    for t in trackers:
        t.region_lat = REGION_LAT.get(t.url)
        if t.latency and _nt_region_avg(t.url):
            t.latency = _nt_region_avg(t.url)
        t.stats = _stats(t)
        t.group_id = find(t.url)
        t.operator_peers = sorted(x for x in groups[t.group_id] if x != t.url)
    return trackers


# ---- event feed: state changes, newest last (last 500) ----
_EVENTS_FILE = "data/events.json"
_LAST_STATE_FILE = "data/last_state.json"
_ev = _jload(_EVENTS_FILE)
EVENTS: list = _ev if isinstance(_ev, list) else []
LAST_STATE: dict = _jload(_LAST_STATE_FILE)

# ---- removed trackers, kept for good so their page can say what happened: {host: {url, t, reason, added, country, network}} ----
_REMOVED_FILE = "data/removed.json"
_REMOVED_EXISTED = _os.path.exists(_REMOVED_FILE)  # the event-history migration only runs before the file first exists
_rm = _jload(_REMOVED_FILE)
REMOVED: dict = _rm if isinstance(_rm, dict) else {}


def _removed_add(t, reason, now=None):
    host = (t.host or "").lower()
    if not host:
        return
    if CLOSED.pop(t.url, None) is not None:
        _jsave(CLOSED, _CLOSED_FILE)
    prev = REMOVED.get(host)
    hist = "".join("1" if float(x) >= 1 else ("h" if float(x) > 0 else "0") for x in list(getattr(t, "historic", None) or [])[-336:])
    REMOVED[host] = {"url": t.url, "t": int(now or time()), "reason": reason, "added": int(t.added or 0),
                     "country": (t.countries or [""])[0], "network": (t.networks or [""])[0],
                     "count": (int(prev.get("count", 1)) if prev else 0) + 1,  # removals so far, for longer bans
                     "ips": sorted(set(getattr(t, "ips", None) or []) | set((getattr(t, "recent_ips", None) or {}).keys())),
                     "hist": hist}  # its last week of uptime, restored if it's reinstated
    _jsave(REMOVED, _REMOVED_FILE)


_CDN_WORDS = ("cloudflare", "akamai", "fastly", "cloudfront", "amazon.com", "google", "microsoft", "incapsula", "imperva", "sucuri")


def _nt_ip_key(ip):
    """What identifies a server for a ban: an IPv4 address exactly, an IPv6 address by its /64 (a server's IPv6 block:
    picking another address in it takes seconds). Any spelling of an address gives the same key."""
    import ipaddress
    try:
        a = ipaddress.ip_address(str(ip).strip("[]"))
    except ValueError:
        return None
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.version == 6:
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def _nt_ip_keys(ips):
    return {k for k in (_nt_ip_key(x) for x in (ips or [])) if k}


def _nt_ban_ips(ips, skip_host=None, listed_ips=()):
    """The banned tracker (removed by this site, ban still running) that shares one of these addresses, or None.
    Addresses of CDNs and shared front ends are never treated as banned (thousands of unrelated sites share them), nor
    addresses a listed tracker still uses."""
    from newtrackon import ntextra
    ips = _nt_ip_keys(ips) - _nt_ip_keys(listed_ips)
    if not ips:
        return None
    for host, r in REMOVED.items():
        if host == skip_host or not r.get("ips"):
            continue
        if any(w in str(r.get("network") or "").lower() for w in _CDN_WORDS):
            continue
        if ips & _nt_ip_keys(r["ips"]):
            b = ntextra._ban(host)
            if b and b["active"]:
                return host
    return None


def _removed_ips_seed():
    """Bans from before addresses were saved: look the banned names up once (a handful of DNS lookups) and keep the
    public addresses. Run once from the check loop."""
    from newtrackon import ntextra
    n = 0
    for host, r in REMOVED.items():
        if "ips" in r:
            continue
        b = ntextra._ban(host)
        if not (b and b["active"]):
            continue
        found = set()
        try:
            for res in socket.getaddrinfo(host, None):
                ip = str(res[4][0])
                if scraper.ip_is_public(ip):
                    found.add(ip)
        except OSError:
            pass
        r["ips"] = sorted(found)
        n += 1
    if n:
        _jsave(REMOVED, _REMOVED_FILE)


def _removed_seed():
    """Removals from before this record existed, from the event history (which keeps only the last 500 events)."""
    n = 0
    # once removed.json exists it's the record: re-running this would recreate records deliberately deleted (trackers
    # reinstated because our own checks were at fault) from their old "removed" events
    for e in (EVENTS if not _REMOVED_EXISTED else []):
        h = str(e.get("host") or "").lower()
        if e.get("type") == "removed" and h and int(e.get("t") or 0) > int((REMOVED.get(h) or {}).get("t") or 0):
            txt = str(e.get("text") or "")
            why = txt[len("removed from the list ("):-1] if txt.startswith("removed from the list (") and txt.endswith(")") else txt
            REMOVED[h] = {"url": e.get("url"), "t": int(e["t"]), "reason": why, "added": 0, "country": "", "network": ""}
            n += 1
    for h, r in REMOVED.items():  # how many times each was removed, for records from before the count existed
        if "count" not in r:
            r["count"] = max(1, sum(1 for e in EVENTS if e.get("type") == "removed" and str(e.get("host") or "").lower() == h))
            n += 1
    if n:
        _jsave(REMOVED, _REMOVED_FILE)


_removed_seed()
_bad_seed()
_bad_log_seed()


def _event(url, kind, text):
    from urllib.parse import urlparse as _up
    EVENTS.append({"t": int(time()), "url": url, "host": _up(url).hostname or url, "type": kind, "text": text})
    del EVENTS[:-500]
    _jsave(EVENTS, _EVENTS_FILE)
    logger.info("EVENT %s %s %s", kind, url, text)
    _notify(EVENTS[-1])


# ---- Telegram alerts for chosen trackers / event types (config: data/notify.json, re-read on every event) ----
_NOTIFY_FILE = "data/notify.json"
_NT_ICON = {"down": "\U0001F534", "up": "\U0001F7E2", "good": "\U0001F7E2", "bad": "\U0001F7E0", "family": "\U0001F7E0",
            "added": "\U0001F195", "removed": "\U0001F5D1"}


def _notify(ev):
    try:
        with open(_NOTIFY_FILE) as f:
            cfg = _json.load(f)
    except Exception:
        return
    tg = cfg.get("telegram") or {}
    if not tg.get("token") or not tg.get("chat_id"):
        return
    trs = [str(t).lower() for t in cfg.get("trackers", ["*"])]
    if "*" not in trs and not any(t in ev["url"].lower() for t in trs):
        return
    if ev["type"] not in cfg.get("types", list(_NT_ICON)):
        return
    text = f'{_NT_ICON.get(ev["type"], "*")} {ev["host"]} {ev["text"]}\n{ev["url"]}'
    fix = _nt_fix_for_event(ev)
    if fix:
        text += f"\nHow to fix: https://newtrackon.co.nz/fix#{fix}"

    def send():
        import urllib.parse
        import urllib.request
        try:
            data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text, "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f'https://api.telegram.org/bot{tg["token"]}/sendMessage', data=data, timeout=15).read()
        except Exception as e:
            logger.warning("telegram notify failed: %s", type(e).__name__)

    __import__("threading").Thread(target=send, daemon=True).start()


# --- nt: latency column = mean of region medians; latency penalty on score ---
def _nt_region_avg(url):
    d = REGION_LAT.get(url) or {}
    v = [int(x) for x in d.values() if isinstance(x, (int, float))]
    return round(sum(v) / len(v)) if len(v) >= 2 else None


def _nt_iv_penalty(url):
    """Points off the score for an announce interval far from the usual 30-60 minutes (it asks clients to announce):
    under 5 min -10, under 15 min -5, over 2 h -5, over 6 h -10. Like the latency penalty it lowers the score, but the
    status ladders add it back, so it never makes a tracker look Unreliable."""
    iv = ANN_IV.get(url)
    if not isinstance(iv, int) or iv <= 0:
        return 0.0
    if iv < 300:
        return 10.0
    if iv < 900:
        return 5.0
    if iv > 21600:
        return 10.0
    if iv > 7200:
        return 5.0
    return 0.0


def _nt_lat_penalty(ms):
    # gentle up to 1 s (0.02/ms over 150 ms, max 17), steeper beyond (0.05/ms), capped at 40
    if not ms or ms <= 150:
        return 0.0
    if ms <= 1000:
        return (ms - 150) * 0.02
    return min(40.0, 17.0 + (ms - 1000) * 0.05)


_nt_orig_uu = Tracker.update_uptime


def _nt_uu(self, *a, **k):
    r = _nt_orig_uu(self, *a, **k)
    self._nt_base = self.uptime  # reliability score before the latency penalty (used for premium checks)
    try:
        p = _nt_lat_penalty(_nt_region_avg(self.url)) + _nt_iv_penalty(self.url)
        if p and self.uptime:
            self.uptime = max(0.0, float(self.uptime) - p)
    except Exception:
        pass
    return r


Tracker.update_uptime = _nt_uu


def _nt_reliable(t, pst=None):
    # reliability score (availability x stability, before the latency penalty) is 90 or more
    try:
        a, s = _nt_avail_stab(t.historic)
        return round(a * s * 100) >= (89 if pst == "up_slow" else 90)
    except Exception:
        return True


def _nt_bad_lbl(bad):
    # a dead IP family on its own is Up/Broken (same rule as the page); anything else is Up/Bad
    try:
        b = [str(x).lower() for x in (bad or [])]
        if b and all(("ipv4" in x or "ipv6" in x) and ("peer" not in x or "side doesn't share peers" in x) for x in b):
            return "Up/Broken"
    except Exception:
        pass
    return "Up/Bad"


_NT_LBL = {"up_junk": "Up/Junk", "up_slow": "Up/Slow", "up_unreliable": "Up/Unreliable"}


def _nt_why(st, t):
    sc = round(float(t.uptime or 0))
    if st == "up_slow":
        _ms = _nt_region_avg(t.url) or t.latency or 0
        return f"averages {_ms} ms across regions" if _ms >= 290 else f"reliable, but {_ms} ms latency pulls its score to {sc}, under 90"
    if st == "up_junk":
        return f"score {sc}, under 50"
    return f"score {sc}, under 90"


def _nt_quiet(t):
    """Junk trackers (score under 50) flap all day; don't log their down/up churn to the feed."""
    try:
        return round(float(t.uptime or 0)) < 50
    except Exception:
        return False


# ---- second opinion before blaming a tracker: our DNS or our network path may be the problem ----
_NT_SKIPS: dict = {}
_NT_SIGNAL_FILE = "data/local_faults.json"


def _nt_signal(kind, host):
    try:
        d = _jload(_NT_SIGNAL_FILE)
        d = d if isinstance(d, list) else []
        d.append({"t": int(time()), "kind": kind, "host": host})
        _jsave(d[-200:], _NT_SIGNAL_FILE)
    except Exception:
        pass


_NT_DEL_REASON: dict = {}


def _nt_dns_skip(r, i):
    while True:
        n = r[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def _nt_public_ips(host):
    """Addresses 1.1.1.1 and 9.9.9.9 give for host (A and AAAA). None if neither resolver answered at all."""
    import socket as _s, struct as _st, random as _r, ipaddress as _ipa
    try:
        q = b"".join(bytes([len(x)]) + x.encode() for x in host.strip(".").split(".")) + b"\0"
    except Exception:
        return None
    out, answered = set(), False
    for srv in ("1.1.1.1", "9.9.9.9"):
        for qt in (1, 28):
            tid = _r.getrandbits(16)
            pkt = _st.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + q + _st.pack(">HH", qt, 1)
            sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            sk.settimeout(3)
            try:
                sk.sendto(pkt, (srv, 53))
                r = sk.recv(4096)
                rid, fl, qd, an = _st.unpack(">HHHH", r[:8])
                if rid != tid or not fl & 0x8000:
                    continue
                answered = True
                i = 12
                for _ in range(qd):
                    i = _nt_dns_skip(r, i) + 4
                for _ in range(an):
                    i = _nt_dns_skip(r, i)
                    typ, _c, _t, rdl = _st.unpack(">HHIH", r[i:i + 10])
                    i += 10
                    if (typ, rdl) in ((1, 4), (28, 16)):
                        out.add(str(_ipa.ip_address(r[i:i + rdl])))
                    i += rdl
            except Exception:
                pass
            finally:
                sk.close()
    return out if answered else None


def _nt_ip_public(x):
    return scraper.ip_is_public(x)


def _nt_public_dns_has(host):
    """Ask 1.1.1.1 / 9.9.9.9 directly (bypassing the local resolver) whether the name exists (A or AAAA)."""
    import socket as _s, struct as _st, random as _r
    try:
        q = b"".join(bytes([len(p)]) + p.encode() for p in host.strip(".").split(".")) + b"\0"
    except Exception:
        return False
    for srv in ("1.1.1.1", "9.9.9.9"):
        for qt in (1, 28):
            tid = _r.getrandbits(16)
            pkt = _st.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + q + _st.pack(">HH", qt, 1)
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.settimeout(3)
            try:
                s.sendto(pkt, (srv, 53))
                r = s.recv(1500)
                if len(r) >= 12:
                    rid, fl, _qd, an = _st.unpack(">HHHH", r[:8])
                    if rid == tid and (fl & 0xF) == 0 and an > 0:
                        return True
            except OSError:
                pass
            finally:
                s.close()
    return False


def _nt_reachable_elsewhere(url):
    """Does the tracker answer through any of the VPN exits?"""
    try:
        import socket as _s
        for e in (scraper._exits() or {}).values():
            src = e.get("src4")
            if src and scraper._rtt_via(url, _s.AF_INET, src) is not None:
                return True
    except Exception:
        pass
    return False


def _nt_local_fault(t, err):
    """True = the failure is ours (DNS lying, or our path broken while VPN exits reach the tracker): skip, don't record.
    Capped at 6 skips in a row (~30 min) so a tracker that has banned this server still gets recorded eventually."""
    host = (getattr(t, "host", "") or "").lower()
    msg = str(err).lower()
    if _NT_SKIPS.get(t.url, 0) >= 6:
        _NT_SKIPS.pop(t.url, None)
        return False
    kind = None
    if "resolve" in msg and host and _nt_public_dns_has(host):
        kind = "dns"
    elif "timeout" in msg and not _nt_vps_retry_ok(t.url) and _nt_reachable_elsewhere(t.url):  # still dead from here, alive elsewhere
        kind = "path"
    if not kind:
        _NT_SKIPS.pop(t.url, None)
        return False
    _nt_signal(kind, host)
    if kind == "path":  # down from Wellington, up elsewhere: record it as half an outage instead of skipping
        t._nt_partial = True
        logger.warning("%s PARTIAL: '%s' from Wellington but answers via VPN exits - recorded as half down", t.url, err)
        return False
    _NT_SKIPS[t.url] = _NT_SKIPS.get(t.url, 0) + 1
    logger.warning("%s LOCAL FAULT (%s): '%s' here, but it works from elsewhere - not counted", t.url, kind, err)
    return True


def _nt_is_new(t, sc=None, ms=None):
    """Still in probation (under 7 days of history, score held under the age ceiling) and reliable on its own record:
    healthy, just new. Judged on the uncapped availability x stability, so the ceiling alone can never make a new tracker
    look unreliable; real misses still can. (sc and ms are unused, kept for the callers.)"""
    age = (time() - int(getattr(t, "added", 0) or 0)) / 86400.0
    h = list(getattr(t, "historic", None) or [])
    if age >= 7 or len(h) >= 336:
        return False
    availability, stability = _nt_avail_stab(h)
    return availability * stability * 100.0 >= 90.0


def _nt_vps_retry_ok(url):
    """Re-try from this server first: if it answers now, the failure was a blip, not our network path."""
    try:
        import socket as _s
        return scraper._rtt_via(url, _s.AF_INET, None) is not None
    except Exception:
        return False


# ---- display order: up trackers by score then latency; down trackers last, least time down first ----
_nt_ae_orig = _attach_extras


def _attach_extras(trackers):
    r = _nt_ae_orig(trackers)
    try:
        _now = time()
        trackers.sort(key=lambda t: (0, -round(float(t.uptime or 0)), t.latency or 99999) if t.status == 1 else (1, _now - int(t.last_uptime or 0), 0))
    except Exception:
        pass
    return r


# ---- a tracker's own records follow the tracker, not its URL: when a better protocol replaces it (UDP for HTTP),
# everything stored under the old URL moves to the new one ----
_URL_STORES = (("PEER_FAILS", "_PEER_FAILS_FILE"), ("FAKE_FAILS", "_FAKE_FAILS_FILE"), ("FAKE_HIST", "_FAKE_HIST_FILE"),
               ("FAM_FAILS", "_FAM_FAILS_FILE"), ("FAMS", "_FAMS_FILE"), ("DOWN_WHY", "_DOWN_WHY_FILE"),
               ("FAM_HIST", "_FAM_HIST_FILE"), ("PEER_HIST", "_PEER_HIST_FILE"), ("PEER_LAST", "_PEER_LAST_FILE"),
               ("ANN_IV", "_ANN_IV_FILE"), ("REGION_LAT", "_REGION_FILE"), ("WARNINGS", "_WARN_FILE"),
               ("CLOSED", "_CLOSED_FILE"), ("REGION_SAMPLES", "_REGION_TS_FILE"), ("LAT_HIST", "_LAT_HIST_FILE"),
               ("LAST_STATE", "_LAST_STATE_FILE"), ("DAILY", "_DAILY_FILE"))
_URL_MEMORY = ("PEER_OK", "FAKE_N", "INFLATED", "STALE", "CID_OK", "LAST_REC", "_NT_SKIPS")  # in memory (probe ones saved together)


def _nt_migrate_url(old, new):
    """Move everything stored under the old URL to the new one (latency history, daily summary, peer/address/fake-peer
    history, state and its clocks, interval, warnings...). Returns the stores that had something."""
    if not old or not new or old == new:
        return []
    g = globals()
    moved = []
    for name, file_var in _URL_STORES:
        d = g.get(name)
        if not isinstance(d, dict) or old not in d:
            continue
        v = d.pop(old)
        if name == "DAILY" and new in d:  # keep both, one row per day (the new URL's rows win a clash)
            days = {r[0]: r for r in v}
            days.update({r[0]: r for r in d[new]})
            v = [days[k] for k in sorted(days)]
        d[new] = v
        _jsave(d, g[file_var])
        moved.append(name)
    for name in _URL_MEMORY:
        d = g.get(name)
        if isinstance(d, dict) and old in d:
            d[new] = d.pop(old)
            moved.append(name)
    _probe_save()
    logger.info("Moved %s's records to %s: %s", old, new, ", ".join(moved) or "nothing")
    return moved
