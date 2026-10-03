import pprint
import random
import socket
import string
import struct
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from logging import getLogger
from os import urandom
import threading
from time import time
from typing import NamedTuple, TypedDict, cast
from urllib.parse import ParseResult, urlencode, urlparse

import requests
from dns import resolver
from dns.exception import DNSException
from dns.rdata import Rdata
from urllib3.exceptions import HTTPError
from urllib3.response import HTTPResponse

from newtrackon.bdecode import BDecodedValue, bdecode
from newtrackon.persistence import HistoryData, submitted_data
from newtrackon.utils import ProtocolPref, build_httpx_url, process_txt_prefs

# Round-trip time of the last successful UDP announce, per thread (connect request -> reply only,
# so DNS lookups and retry timeouts are not counted as latency).
rtt = threading.local()

# Socket address types for getaddrinfo results
SockAddr = tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]
AddrInfo = tuple[socket.AddressFamily, socket.SocketKind, int, str, SockAddr]


class ScraperResult(NamedTuple):
    """Result from a successful tracker scrape attempt."""

    interval: int
    url: str
    latency: int


class AttemptResult(NamedTuple):
    """Result from an individual protocol attempt (HTTP/UDP)."""

    status: int
    interval: int | None
    url: str
    latency: int


HTTP_PORT: int = 6881
UDP_PORT: int = 30461
MAX_PEERS = 10  # a random info hash has no real swarm; anything beyond noise is a tracker injecting fake peers
PEER_ID_PREFIX = "-qB5230-"
PEER_ID_CHARS = string.ascii_letters + string.digits + "-_.~"
my_ipv4: str | None = None
my_ipv6: str | None = None
SCRAPING_HEADERS: dict[str, str] = {
    "User-Agent": "qBittorrent/4.3.9",
    "Accept-Encoding": "gzip",
    "Connection": "close",
}
MAX_RESPONSE_SIZE: int = 1024 * 1024  # 1MB

logger = getLogger("newtrackon")

to_redact: list[str] = [str(HTTP_PORT), str(UDP_PORT)]


class PeerInfo(TypedDict):
    IP: str
    port: int


HTTPAnnounceResponse = dict[str, BDecodedValue | str | list[PeerInfo]]


class UDPAnnounceResponse(TypedDict):
    interval: int
    leechers: int
    seeds: int
    peers: list[PeerInfo]


def attempt_submitted(url: str) -> ScraperResult:
    submitted_url = urlparse(url)
    try:
        failover_ip: str = str(socket.getaddrinfo(submitted_url.hostname, None)[0][4][0])
    except OSError:
        failover_ip = ""

    valid_bep_34, bep_34_info = get_bep_34(submitted_url.hostname)

    if valid_bep_34:  # Hostname has a valid TXT record as per BEP34
        if not bep_34_info:
            logger.info(
                "Hostname denies connection via BEP34, giving up on submitted tracker %s",
                url,
            )
            submitted_data.appendleft(
                {
                    "url": url,
                    "time": int(time()),
                    "status": 0,
                    "ip": failover_ip,
                    "info": ["Host denied connection according to BEP34"],
                }
            )
            raise RuntimeError
        logger.info(
            "Tracker %s sets protocol and port preferences from BEP34: %s",
            url,
            bep_34_info,
        )
        return attempt_from_txt_prefs(submitted_url, failover_ip, bep_34_info)
    # No valid BEP34, attempting all protocols
    return attempt_all_protocols(submitted_url, failover_ip)


def attempt_from_txt_prefs(submitted_url: ParseResult, failover_ip: str, txt_prefs: list[ProtocolPref]) -> ScraperResult:
    for protocol, port in txt_prefs:
        preferred_url = submitted_url._replace(netloc=f"{submitted_url.hostname}:{port}")
        if protocol == "udp":
            result = attempt_udp(failover_ip, preferred_url.netloc)
            if result.status and result.interval is not None:
                return ScraperResult(result.interval, result.url, result.latency)
        elif protocol == "tcp":
            http_result = attempt_https_http(failover_ip, preferred_url)
            if http_result is not None:
                return ScraperResult(http_result.interval, http_result.url, http_result.latency)

    logger.info(
        "All DNS TXT protocol preferences failed, giving up on submitted tracker %s",
        submitted_url.geturl(),
    )
    raise RuntimeError


def attempt_all_protocols(submitted_url: ParseResult, failover_ip: str) -> ScraperResult:
    # UDP scrape
    if submitted_url.port:  # If the tracker netloc has a port, try with UDP
        result = attempt_udp(failover_ip, submitted_url.netloc)
        if result.status and result.interval is not None:
            return ScraperResult(result.interval, result.url, result.latency)

        logger.info("%s UDP failed", result.url)

    # HTTPS and HTTP scrape
    http_result = attempt_https_http(failover_ip, submitted_url)
    if http_result is not None:
        return ScraperResult(http_result.interval, http_result.url, http_result.latency)
    logger.info(
        "All protocols failed, giving up on submitted tracker %s",
        submitted_url.geturl(),
    )
    raise RuntimeError


def attempt_https_http(failover_ip: str, url: ParseResult, log_to_submitted: bool = True) -> ScraperResult | None:
    # HTTP scrape first (plain HTTP preferred over HTTPS)
    http_result = attempt_httpx(failover_ip, url, tls=False, log_to_submitted=log_to_submitted)
    if http_result.status and http_result.interval is not None:
        return ScraperResult(http_result.interval, http_result.url, http_result.latency)

    logger.info("%s HTTP failed", http_result.url)

    # HTTPS scrape as last resort
    https_result = attempt_httpx(failover_ip, url, tls=True, log_to_submitted=log_to_submitted)
    if https_result.status and https_result.interval is not None:
        return ScraperResult(https_result.interval, https_result.url, https_result.latency)

    logger.info("%s HTTPS failed", https_result.url)
    return None


def attempt_httpx(failover_ip: str, submitted_url: ParseResult, tls: bool = True, log_to_submitted: bool = True) -> AttemptResult:
    http_url = build_httpx_url(submitted_url, tls)
    pp = pprint.PrettyPrinter(width=999999, compact=True)
    t1 = time()
    latency = 0
    status = 0
    interval: int | None = None
    info: list[str] = []
    try:
        http_response = announce_http(http_url)
        latency = int((time() - t1) * 1000)
        pretty_data = redact_origin(pp.pformat(http_response))
        info = [pretty_data]
        status = 1
        raw_interval = http_response.get("interval")
        if isinstance(raw_interval, int):
            interval = raw_interval
    except RuntimeError as e:
        info = [redact_origin(str(e))]
        status = 0
    if log_to_submitted:
        debug_http: HistoryData = {"url": http_url, "time": int(t1), "ip": failover_ip, "info": info, "status": status}
        submitted_data.appendleft(debug_http)
    return AttemptResult(status, interval, http_url, latency)


def attempt_udp(failover_ip: str, tracker_netloc: str) -> AttemptResult:
    pp = pprint.PrettyPrinter(width=999999, compact=True)
    udp_url = "udp://" + tracker_netloc + "/announce"
    t1 = time()
    latency = 0
    status = 0
    interval: int | None = None
    info: list[str] = []
    ip = failover_ip
    try:
        parsed_response, resolved_ip = announce_udp(udp_url)
        latency = int((time() - t1) * 1000)
        pretty_data = redact_origin(pp.pformat(parsed_response))
        info = [pretty_data]
        status = 1
        interval = parsed_response["interval"]
        if resolved_ip is not None:
            ip = resolved_ip
    except RuntimeError as e:
        error_msg = str(e)
        info = [error_msg]
        status = 0
        if error_msg == "Can't resolve IP":
            ip = ""
    udp_attempt_result: HistoryData = {"url": udp_url, "time": int(t1), "ip": ip, "info": info, "status": status}
    submitted_data.appendleft(udp_attempt_result)
    return AttemptResult(status, interval, udp_url, latency)


def get_bep_34(hostname: str | None) -> tuple[bool, list[ProtocolPref] | None]:
    """Querying for http://bittorrent.org/beps/bep_0034.html"""
    if hostname is None:
        return False, None
    try:
        answer: resolver.Answer = resolver.resolve(hostname, "TXT")
        for rdata in cast(Iterable[Rdata], answer):
            record_text = str(rdata).strip('"')
            if record_text.startswith("BITTORRENT"):
                return True, process_txt_prefs(record_text)
    except DNSException:
        pass
    return False, None


def generate_peer_id() -> bytes:
    return (PEER_ID_PREFIX + "".join(random.choices(PEER_ID_CHARS, k=12))).encode()


def parse_http_tracker_response(data: bytes) -> HTTPAnnounceResponse:
    """Decode and interpret a bencoded HTTP tracker response."""
    bdecoded_response = bdecode(data)
    response: HTTPAnnounceResponse = {}
    if not isinstance(bdecoded_response, dict):
        raise TypeError("Could not extract the bencoded dict, probably invalid format")
    for key, value in bdecoded_response.items():
        response[key.decode()] = value

    for key, ip_family in (("peers", socket.AF_INET), ("peers6", socket.AF_INET6)):
        if key not in response:
            continue
        peers = response[key]
        if isinstance(peers, bytes):
            response[key] = decode_binary_peers_list(peers, 0, ip_family)
        elif not isinstance(peers, list):
            raise RuntimeError(f"Invalid peer list for '{key}': expected a list, got {type(peers).__name__}")

    for key in ("seeds", "leechers", "complete", "incomplete"):
        if key not in response:
            continue
        count = response[key]
        if type(count) is not int:
            raise RuntimeError(f"Invalid peer count for '{key}': expected an integer, got {type(count).__name__}")
        if count < 0:
            raise RuntimeError(f"Tracker reported negative peer count for '{key}': {count}")

    if "external ip" in response:
        external_ip = response["external ip"]
        if isinstance(external_ip, bytes):
            external_ip_length = len(external_ip)
            if external_ip_length == 4:
                response["external ip"] = socket.inet_ntop(socket.AF_INET, external_ip)
            elif external_ip_length == 16:
                response["external ip"] = socket.inet_ntop(socket.AF_INET6, external_ip)
            else:
                raise RuntimeError("Invalid external IP size")

    for key, value in response.items():
        if isinstance(value, bytes):
            response[key] = value.decode()

    return response


def decode_binary_peers_list(buf: bytes, offset: int, ip_family: int) -> list[PeerInfo]:
    peers: list[PeerInfo] = []
    peer_length = 6 if ip_family == socket.AF_INET else 18
    binary_response = memoryview(buf)
    while offset != len(buf):
        if len(buf) < offset + peer_length:
            return peers
        ip_address = bytes(binary_response[offset : offset + peer_length - 2])
        ip_str = socket.inet_ntop(ip_family, ip_address)
        offset += peer_length - 2
        port = struct.unpack_from("!H", buf, offset)[0]
        offset += 2
        peers.append({"IP": ip_str, "port": port})
    return peers


def announce_http(url: str) -> HTTPAnnounceResponse:
    logger.info("%s Scraping HTTP(S)", url)
    thash = urandom(20)

    args_dict = {
        "info_hash": thash,
        "peer_id": (_pid := generate_peer_id()),
        "port": HTTP_PORT,
        "uploaded": 0,
        "downloaded": 0,
        "left": 0,
        "compact": 1,
        "ipv6": my_ipv6,
        "ipv4": my_ipv4,
    }
    arguments = urlencode(args_dict)
    url = url + "?" + arguments
    try:
        response, content = memory_limited_get(url)
    except RuntimeError:
        raise
    except requests.Timeout:
        raise RuntimeError("HTTP timeout")
    except requests.ConnectionError:
        raise RuntimeError("HTTP connection failed")
    except HTTPError, requests.RequestException:
        raise RuntimeError("Unhandled HTTP error")
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} status code returned")

    elif not content:
        raise RuntimeError("Got empty HTTP response")

    else:
        try:
            tracker_response = parse_http_tracker_response(content)
        except (EOFError, OSError, RuntimeError, TypeError, ValueError) as e:
            raise RuntimeError(f"Failed bdecoding HTTP response: {e}")

    if "failure reason" in tracker_response:
        raise RuntimeError(f"Tracker error message: {tracker_response['failure reason']}")
    if "peers" not in tracker_response and "peers6" not in tracker_response:
        raise RuntimeError(f"Invalid response, both 'peers' and 'peers6' field are missing: {tracker_response}")
    check_peer_count(tracker_response)
    rtt.probe = ("http", thash, url.split("?", 1)[0], None, _pid)
    logger.info("%s response: %s", url, tracker_response)
    return tracker_response


def check_peer_count(response: Mapping[str, object]) -> None:
    total = sum(len(cast(Sequence[object], response.get(key, []))) for key in ("peers", "peers6"))
    total += sum(cast(int, response.get(key, 0)) for key in ("seeds", "leechers", "complete", "incomplete"))
    if total > MAX_PEERS:
        raise RuntimeError(f"Tracker rejected for reporting more than {MAX_PEERS} peers for a random info hash")


def announce_udp(udp_url: str) -> tuple[UDPAnnounceResponse, str | None]:
    parsed_tracker = urlparse(udp_url)
    logger.info("%s Scraping UDP", udp_url)
    thash = urandom(20)
    peer_id = generate_peer_id()
    ip: str | None = None
    try:
        getaddr_responses: Sequence[AddrInfo] = socket.getaddrinfo(
            parsed_tracker.hostname, parsed_tracker.port, 0, socket.SOCK_DGRAM
        )
    except OSError as err:
        raise RuntimeError(f"UDP error: {err}")

    last_error = RuntimeError("UDP announce failed")
    for attempt in range(2):
        logger.info("%s UDP attempt %d", udp_url, attempt + 1)

        sock: socket.socket | None = None
        _order = list(getaddr_responses)
        if attempt and len({r[0] for r in _order}) > 1:  # retry on the other IP family: one dead A/AAAA can't mark the tracker down
            _order = [r for r in _order if r[0] != _order[0][0]] + [r for r in _order if r[0] == _order[0][0]]
        for res in _order:
            af, socktype, proto, _, sa = res
            ip = str(sa[0])
            try:
                sock = socket.socket(af, socktype, proto)
                sock.settimeout(10)
            except OSError:
                sock = None
                continue
            try:
                sock.connect(sa)
            except OSError:
                sock.close()
                sock = None
                continue
            break
        if sock is None:
            raise RuntimeError("UDP connection error")

        try:
            # Get connection ID
            req, transaction_id = udp_create_binary_connection_request()
            _t0 = time()
            sock.sendall(req)
            buf = sock.recv(2048)
            _rtt_ms = int((time() - _t0) * 1000)
            connection_id = udp_parse_connection_response(buf, transaction_id)

            # Announce
            req, transaction_id = udp_create_announce_request(connection_id, thash, peer_id)
            sock.sendall(req)
            buf = sock.recv(2048)
            ip_family = sock.family
            sock.close()

            parsed_response = udp_parse_announce_response(buf, transaction_id, ip_family)
            check_peer_count(parsed_response)
            rtt.ms = _rtt_ms
            rtt.probe = ("udp", thash, af, sa, peer_id)
            logger.info("%s response: %s", udp_url, parsed_response)
            return parsed_response, ip
        except ConnectionRefusedError:
            last_error = RuntimeError("UDP connection failed")
        except TimeoutError:
            last_error = RuntimeError("UDP timeout")
        except OSError as err:
            last_error = RuntimeError(f"UDP error: {err}")
        except RuntimeError as err:
            last_error = err
        sock.close()

    raise last_error


def udp_create_binary_connection_request() -> tuple[bytes, int]:
    connection_id = 0x41727101980  # default connection id
    action = 0x0  # action (0 = give me a new connection id)
    transaction_id = udp_get_transaction_id()
    buf = struct.pack("!q", connection_id)  # first 8 bytes is connection id
    buf += struct.pack("!i", action)  # next 4 bytes is action
    buf += struct.pack("!i", transaction_id)  # next 4 bytes is transaction id
    return buf, transaction_id


def udp_parse_connection_response(buf: bytes, sent_transaction_id: int) -> int | None:
    if len(buf) < 16:
        raise RuntimeError(f"Wrong response length getting connection id: {len(buf)}")
    action = struct.unpack_from("!i", buf)[0]  # first 4 bytes is action

    res_transaction_id = struct.unpack_from("!i", buf, 4)[0]  # next 4 bytes is transaction id
    if res_transaction_id != sent_transaction_id:
        raise RuntimeError(
            f"Transaction ID doesn't match in connection response. Expected {sent_transaction_id}, got {res_transaction_id}"
        )

    if action == 0x0:
        connection_id = struct.unpack_from("!q", buf, 8)[0]  # unpack 8 bytes from byte 8, should be the connection_id
        return connection_id
    elif action == 0x3:
        error = struct.unpack_from("!s", buf, 8)
        raise RuntimeError(f"Error while trying to get a connection response: {error}")


def udp_create_announce_request(connection_id: int | None, thash: bytes, peer_id: bytes, left: int = 0, port: int = 0x76FD, event: int = 2) -> tuple[bytes, int]:
    action = 0x1  # action (1 = announce)
    transaction_id = udp_get_transaction_id()
    buf = struct.pack("!q", connection_id)  # first 8 bytes is connection id
    buf += struct.pack("!i", action)  # next 4 bytes is action
    buf += struct.pack("!i", transaction_id)  # followed by 4 byte transaction id
    buf += struct.pack("!20s", thash)  # hash
    buf += struct.pack("!20s", peer_id)
    buf += struct.pack("!q", 0x0)  # number of bytes downloaded
    buf += struct.pack("!q", left)  # number of bytes left
    buf += struct.pack("!q", 0x0)  # number of bytes uploaded
    buf += struct.pack("!i", event)  # event: 0 none, 1 completed, 2 started, 3 stopped
    buf += struct.pack("!i", 0x0)  # IP address set to 0. Response received to the sender of this packet
    key = udp_get_transaction_id()  # Unique key randomized by client
    buf += struct.pack("!i", key)
    buf += struct.pack("!i", -1)  # Number of peers required. Set to -1 for default
    buf += struct.pack("!H", port)  # port on which response will be sent
    return buf, transaction_id


def udp_parse_announce_response(buf: bytes, sent_transaction_id: int, ip_family: socket.AddressFamily) -> UDPAnnounceResponse:
    if len(buf) < 20:
        raise RuntimeError(f"Wrong response length while announcing: {len(buf)}")
    action = struct.unpack_from("!i", buf)[0]  # first 4 bytes is action
    res_transaction_id = struct.unpack_from("!i", buf, 4)[0]  # next 4 bytes is transaction id
    if res_transaction_id != sent_transaction_id:
        raise RuntimeError(
            f"Transaction ID doesnt match in announce response! Expected {sent_transaction_id}, got {res_transaction_id}"
        )
    if action == 0x1:
        offset = 8  # next 4 bytes after action is transaction_id, so data doesnt start till byte 8
        interval = struct.unpack_from("!i", buf, offset)[0]
        offset += 4
        leechers = struct.unpack_from("!i", buf, offset)[0]
        offset += 4
        seeds = struct.unpack_from("!i", buf, offset)[0]
        offset += 4
        for key, count in (("leechers", leechers), ("seeds", seeds)):
            if count < 0:
                raise RuntimeError(f"Tracker reported negative peer count for '{key}': {count}")
        peers = decode_binary_peers_list(buf, offset, ip_family)
        return {"interval": interval, "leechers": leechers, "seeds": seeds, "peers": peers}
    # an error occured, try and extract the error string
    error = struct.unpack_from("!s", buf, 8)
    raise RuntimeError(f"Error while annoucing: {error}")


def udp_get_transaction_id() -> int:
    return int(random.randrange(0, 255))


def get_server_ip(ip_version: str) -> str:
    return subprocess.check_output(["curl", "-s", "-" + ip_version, "https://icanhazip.com/"]).decode("utf-8").strip()


def memory_limited_get(url: str) -> tuple[requests.Response, bytes]:
    response = requests.get(url, headers=SCRAPING_HEADERS, timeout=10, stream=True, allow_redirects=False)
    raw = cast(HTTPResponse, response.raw)
    content = raw.read(MAX_RESPONSE_SIZE + 1, decode_content=True)
    if len(content) > MAX_RESPONSE_SIZE:
        raise RuntimeError("HTTP response size above 1 MB")
    return response, content


def redact_origin(response: str) -> str:
    if my_ipv4:
        response = response.replace(my_ipv4, "v4-redacted")
    if my_ipv6:
        response = response.replace(my_ipv6, "v6-redacted")
    for port in to_redact:
        response = response.replace(port, "redacted")
    return response


def _probe_peers(resp):
    return [x for x in list(resp.get("peers", []) or []) + list(resp.get("peers6", []) or []) if isinstance(x, dict)]


def _probe_eval(resp, want, extra):
    peers = _probe_peers(resp)
    # Only our own probe clients can know this random hash: anything else is fake by the tracker.
    extra["foreign"] = sum(1 for x in peers if x.get("port") not in (want, want + 1))
    seeds = resp.get("seeds", resp.get("complete"))
    leech = resp.get("leechers", resp.get("incomplete"))
    extra["inflated"] = (seeds, leech) if isinstance(seeds, int) and isinstance(leech, int) and (seeds > 2 or leech > 2) else None
    return any(x.get("port") == want for x in peers)


def _probe_src(family):
    """Source IP for the second test client (the AirVPN exit), from data/probe_src.json; None = no tunnel."""
    try:
        import json as _pj
        with open("data/probe_src.json") as f:
            d = _pj.load(f)
        return d.get("ip6" if family == socket.AF_INET6 else "ip4") or None
    except Exception:
        return None


_PREF_FILE = "data/probe_pref.json"
try:
    with open(_PREF_FILE) as _pf:
        _PREF = __import__("json").load(_pf)
except Exception:
    _PREF = {}


def _pref_set(key, src):
    """Remember which VPN exit this tracker last answered, so it is tried first next time."""
    if _PREF.get(key) == src:
        return
    _PREF[key] = src
    try:
        with open(_PREF_FILE + ".tmp", "w") as f:
            __import__("json").dump(_PREF, f)
        __import__("os").replace(_PREF_FILE + ".tmp", _PREF_FILE)
    except OSError:
        pass


def _probe_srcs(family, key=None):
    """Every VPN exit source IP for this family: the one this tracker last answered first, the rest shuffled."""
    try:
        import json as _pj
        import random as _pr
        with open("data/probe_src.json") as f:
            d = _pj.load(f)
        k = "ip6" if family == socket.AF_INET6 else "ip4"
        l = [x.get(k) for x in d.get("all", [])] or [d.get(k)]
        l = [x for x in l if x]
        _pr.shuffle(l)
        pref = _PREF.get(key) if key else None
        if pref in l:
            l.remove(pref)
            l.insert(0, pref)
        return l if len(l) > 1 else l * 2  # single exit: try it twice
    except Exception:
        return []


class _SrcAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, src, **kw):
        self._src = src
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kw):
        kw["source_address"] = (self._src, 0)
        return super().init_poolmanager(*args, **kw)


def _udp_session(family, sa, src):
    s = socket.socket(family, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        if src:
            s.bind((src, 0))
        s.connect(sa)
        req, tid = udp_create_binary_connection_request()
        s.sendall(req)
        cid = udp_parse_connection_response(s.recv(2048), tid)
    except Exception:
        s.close()
        raise

    def ann(peer_id, left, port, event, th):
        rq, t = udp_create_announce_request(cid, th, peer_id, left=left, port=port, event=event)
        s.sendall(rq)
        return udp_parse_announce_response(s.recv(2048), t, s.family)

    return s, ann


def _two_distinct(r):
    """Same-IP fallback only counts if the tracker stored both clients separately (1+ seeder AND 1+ leecher).
    Trackers that key peers by IP alone merge them into one entry and would fake a pass."""
    s = r.get("seeds", r.get("complete"))
    l = r.get("leechers", r.get("incomplete"))
    return isinstance(s, int) and isinstance(l, int) and s >= 1 and l >= 1


def peer_probe() -> bool | None:
    """Authenticity tests on the random hash the check just announced from the VPS (as seeder A).
    The second client B announces from a DIFFERENT IP (the AirVPN exit), so trackers that merge
    clients sharing an IP can't fail unfairly. No tunnel -> peer test is n/a (never a fail).
    peer test  - B must be handed A;  fake peers - nothing else may appear; counts ~1/1;
    stale      - after A sends 'stopped', B must no longer be handed A;
    cid (UDP)  - an announce with a made-up connection ID must be refused (BEP 15)."""
    p = getattr(rtt, "probe", None)
    extra = {"foreign": None, "inflated": None, "stale": None, "cid_ok": None}
    rtt.probe_extra = extra
    if not p:
        return None
    kind, thash, a, b, pid_a = p
    pid_b = generate_peer_id()
    sleep = __import__("time").sleep
    if kind == "udp":
        want = 0x76FD
        ok = None
        sb = sa_ = None
        fb = False
        try:
            _pk = f"{b[0]}:{b[1]}"
            for src in _probe_srcs(a, _pk):  # preferred exit first, then the rest until one answers
                try:
                    sb, annb = _udp_session(a, b, src)
                    ok = _probe_eval(annb(pid_b, 1, want + 1, 2, thash), want, extra)
                    _pref_set(_pk, src)
                    break
                except Exception:
                    if sb:
                        sb.close()
                    sb = None
                    sleep(0.5)
            if ok is None:  # VPN exit ignored/rate-limited: fall back to a VPS client; only a PASS counts
                s2 = None
                try:
                    s2, an2 = _udp_session(a, b, None)
                    r2 = an2(pid_b, 1, want + 1, 2, thash)
                    if _two_distinct(r2) and _probe_eval(r2, want, extra):
                        if sb:
                            sb.close()
                        ok, sb, annb, s2, fb = True, s2, an2, None, True
                except Exception:
                    pass
                finally:
                    if s2:
                        s2.close()
            sa_, anna = _udp_session(a, b, None)
            try:
                if ok:
                    anna(pid_a, 0, want, 3, thash)
                    extra["stale"] = any(x.get("port") == want for x in _probe_peers(annb(pid_b, 1, want + 1, 0, thash)))
                if sb:
                    annb(pid_b, 1, want + 1, 3, thash)
            except Exception:
                pass
            if fb and extra.get("stale"):  # same-IP pass whose peer outlives "stopped" = echoed IP entry, not real peer sharing
                ok, extra["stale"], extra["inconclusive"] = None, None, True  # a fallback result only ever counts as a pass, never a fail
            try:
                bogus = struct.unpack("!q", urandom(8))[0]
                rq, t = udp_create_announce_request(bogus, urandom(20), generate_peer_id(), left=1, port=want + 2, event=2)
                sa_.settimeout(3)
                sa_.sendall(rq)
                try:
                    udp_parse_announce_response(sa_.recv(2048), t, sa_.family)
                    extra["cid_ok"] = False
                except Exception:
                    extra["cid_ok"] = True
            except OSError:
                pass
        except Exception:
            pass
        finally:
            for s_ in (sb, sa_):
                if s_:
                    s_.close()
        return ok

    want = HTTP_PORT
    from urllib.parse import urlparse as _up
    _u = _up(a)
    host, hport = _u.hostname, _u.port or (443 if _u.scheme == "https" else 80)

    def hsess(src):
        s_ = requests.Session()
        ad = _SrcAdapter(src)
        s_.mount("http://", ad)
        s_.mount("https://", ad)
        return s_

    def hann(sess, peer_id, left, port, event):
        args = {"info_hash": thash, "peer_id": peer_id, "port": port, "uploaded": 0, "downloaded": 0, "left": left, "compact": 1}
        if event:
            args["event"] = event
        response = sess.get(a + "?" + urlencode(args), headers=SCRAPING_HEADERS, timeout=10, allow_redirects=False)
        content = response.content[:65536]
        if response.status_code != 200 or not content:
            raise RuntimeError("probe: HTTP error")
        r = parse_http_tracker_response(content)
        if "failure reason" in r:
            raise RuntimeError("probe: failure reason")
        return r

    # A and B must share an address family: trackers only hand out peers of the asker's family.
    fam = sa_s = None
    for f in (socket.AF_INET, socket.AF_INET6):
        if not _probe_src(f):
            continue
        try:
            socket.getaddrinfo(host, hport, f)
            s_try = hsess("0.0.0.0" if f == socket.AF_INET else "::")  # A = VPS, pinned to this family
            hann(s_try, pid_a, 0, want, "started")
        except Exception:
            continue  # this family is dead: try the other
        fam, sa_s = f, s_try
        break
    if fam is None:
        return None
    ok = None
    fb = False
    sb_s = None
    for src in _probe_srcs(fam, a):  # preferred exit first, then the rest until one answers
        try:
            s_try = hsess(src)
            ok = _probe_eval(hann(s_try, pid_b, 1, want + 1, "started"), want, extra)
            sb_s = s_try
            _pref_set(a, src)
            break
        except Exception:
            sleep(0.5)
    if ok is None:  # VPN exit ignored/rate-limited: fall back to a VPS client; only a PASS counts
        try:
            sv = hsess("0.0.0.0" if fam == socket.AF_INET else "::")
            r2 = hann(sv, pid_b, 1, want + 1, "started")
            if _two_distinct(r2) and _probe_eval(r2, want, extra):
                ok, sb_s, fb = True, sv, True
        except Exception:
            pass
    if ok is None:
        return None
    try:
        if ok:
            hann(sa_s, pid_a, 0, want, "stopped")
            try:  # also retire the main check's own registration (other family / ipv4+ipv6 params)
                memory_limited_get(a + "?" + urlencode({"info_hash": thash, "peer_id": pid_a, "port": want, "uploaded": 0,
                    "downloaded": 0, "left": 0, "compact": 1, "event": "stopped", "ipv6": my_ipv6, "ipv4": my_ipv4}))
            except Exception:
                pass
            extra["stale"] = any(x.get("port") == want for x in _probe_peers(hann(sb_s, pid_b, 1, want + 1, "")))
        hann(sb_s, pid_b, 1, want + 1, "stopped")
    except Exception:
        pass
    if fb and extra.get("stale"):  # same-IP pass whose peer outlives "stopped" = echoed IP entry, not real peer sharing
        ok, extra["stale"], extra["inconclusive"] = None, None, True  # a fallback result only ever counts as a pass, never a fail
    return ok


def family_probe(url):
    """Dual-stack test. A family only counts as dead if NO vantage point gets an answer on it:
    the VPS (twice) and then every VPN exit. One lossy path can't fake a dead family.
    Returns {"v4": bool, "v6": bool} for the published families only."""
    from urllib.parse import urlparse as _up
    p = _up(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    res = {}
    for fam, name, wild in ((socket.AF_INET, "v4", "0.0.0.0"), (socket.AF_INET6, "v6", "::")):
        try:
            addr = socket.getaddrinfo(p.hostname, port, fam)[0][4]
        except OSError:
            continue
        srcs = [None, None] + list(dict.fromkeys(_probe_srcs(fam)))
        ok = False
        for i, src in enumerate(srcs):
            try:
                if p.scheme == "udp":
                    s_, an = _udp_session(fam, addr, src)
                    try:
                        an(generate_peer_id(), 1, 0x76FF, 0, urandom(20))
                    finally:
                        s_.close()
                else:
                    ss = requests.Session()
                    ad = _SrcAdapter(src or wild)
                    ss.mount("http://", ad)
                    ss.mount("https://", ad)
                    q = {"info_hash": urandom(20), "peer_id": generate_peer_id(), "port": 0x76FF, "uploaded": 0,
                         "downloaded": 0, "left": 1, "compact": 1}
                    r = ss.get(url + "?" + urlencode(q), headers=SCRAPING_HEADERS, timeout=6, allow_redirects=False)
                    parse_http_tracker_response(r.content[:65536])
                ok = True
                break
            except Exception:
                if i == 0:
                    __import__("time").sleep(1)
        res[name] = ok
    return res


# ---- latency by region: handshake via each VPN exit minus that tunnel's own round trip ----
_EXITS = [0.0, {}]
_TUN_RTT: dict = {}


def _exits():
    if time() - _EXITS[0] > 60:
        try:
            with open("data/exits.json") as f:
                _EXITS[1] = __import__("json").load(f)
        except Exception:
            _EXITS[1] = {}
        _EXITS[0] = time()
    return _EXITS[1]


def _tunnel_rtt(src, fam):
    """Round trip VPS <-> AirVPN server: DNS queries to the in-tunnel resolver, median of 3, cached 60 s."""
    k = (src, fam)
    c = _TUN_RTT.get(k)
    if c and time() - c[0] < 60:
        return c[1]
    gw = "10.128.0.1" if fam == socket.AF_INET else "fd7d:76ee:e68f:a993::1"
    q = urandom(2) + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06airvpn\x03org\x00\x00\x01\x00\x01"
    samples = []
    for _ in range(3):
        s = socket.socket(fam, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            s.bind((src, 0))
            t0 = time()
            s.sendto(q, (gw, 53))
            s.recv(512)
            samples.append((time() - t0) * 1000)
        except OSError:
            pass
        finally:
            s.close()
    v = sorted(samples)[len(samples) // 2] if samples else None
    if v is not None:  # never cache a failure
        _TUN_RTT[k] = (time(), v)
    return v


def _rtt_via(url, fam, src):
    p = urlparse(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    addr = socket.getaddrinfo(p.hostname, port, fam)[0][4]
    if p.scheme == "udp":
        s = socket.socket(fam, socket.SOCK_DGRAM)
        s.settimeout(3)
        try:
            if src:
                s.bind((src, 0))
            s.connect(addr)
            best = None
            s.settimeout(2)
            for _ in range(3):  # a lost sample is skipped, not fatal
                try:
                    req, tid = udp_create_binary_connection_request()
                    t0 = time()
                    s.sendall(req)
                    buf = s.recv(2048)
                    ms = (time() - t0) * 1000
                    udp_parse_connection_response(buf, tid)
                    best = ms if best is None else min(best, ms)
                    if best is not None and _ >= 1:
                        break
                except (OSError, RuntimeError):
                    continue
            return best
        finally:
            s.close()
    return _http_rtt(url, p, addr, src)


def _http_rtt(url, p, addr, src):
    """HTTP(S) announce round trip with TCP and TLS set up *before* the clock starts: one request/response,
    no handshakes, whether or not the server keeps connections alive. Best of 2 samples, like UDP."""
    import ssl
    q = urlencode({"info_hash": urandom(20), "peer_id": generate_peer_id(), "port": 0x76FF, "uploaded": 0,
                   "downloaded": 0, "left": 1, "compact": 1})
    host = p.hostname if not p.port else f"{p.hostname}:{p.port}"
    ua = (SCRAPING_HEADERS or {}).get("User-Agent", "newTrackon") if isinstance(SCRAPING_HEADERS, dict) else "newTrackon"
    req = (f"GET {p.path or '/'}?{q} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {ua}\r\n"
           f"Accept: */*\r\nConnection: close\r\n\r\n").encode()
    best = None
    for _ in range(2):
        s = socket.socket(addr and (socket.AF_INET6 if len(addr) == 4 else socket.AF_INET), socket.SOCK_STREAM)
        s.settimeout(6)
        try:
            if src:
                s.bind((src, 0))
            s.connect(addr)
            if p.scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                s = ctx.wrap_socket(s, server_hostname=p.hostname)
            t0 = time()
            s.sendall(req)
            first = s.recv(1)
            if not first:
                continue
            ms = (time() - t0) * 1000
            buf = first  # only a genuine tracker answer counts: HTTP 200 + bencoded body (not a CDN error page)
            while b"\r\n\r\n" not in buf and len(buf) < 16384:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            if not head.startswith(b"HTTP/") or b" 200" not in head.split(b"\r\n", 1)[0]:
                continue
            if not body:
                body = s.recv(256)
            if b"chunked" in head.lower() and b"\r\n" in body:
                body = body.split(b"\r\n", 1)[1]
            if not body.lstrip().startswith(b"d"):
                continue
            best = ms if best is None else min(best, ms)
        except OSError:
            continue
        finally:
            s.close()
    return best


def _lbl(e, name):
    """'Region: City' for a VPN exit, e.g. 'Europe: Alblasserdam'."""
    city = (e.get("location") or e.get("server") or name).split(",")[0].strip()
    return f"{e['region']}: {city}" if e.get("region") else city


def region_latency(url, fams=None):
    """{exit city: ms} - the tracker's latency as seen from each AirVPN exit location (estimate, +-10-20 ms)."""
    from concurrent.futures import ThreadPoolExecutor
    ex = _exits()
    if not ex:
        return {}
    fam = socket.AF_INET if (not fams or fams.get("v4")) else socket.AF_INET6
    try:  # is the tracker this very server? (local addresses skip the VPN routing and answer over loopback)
        _ip = socket.getaddrinfo(urlparse(url).hostname, None, fam)[0][4][0]
        _t = socket.socket(fam, socket.SOCK_DGRAM)
        try:
            _t.bind((_ip, 0))
            is_self = True
        finally:
            _t.close()
    except OSError:
        is_self = False

    def one(item):
        name, e = item
        src = e.get("src4" if fam == socket.AF_INET else "src6")
        if not src:
            return None
        if is_self:  # the tracker is this server: from that city the round trip is just the tunnel leg
            tun = _tunnel_rtt(src, fam)
            return (_lbl(e, name), max(1, int(round(tun)))) if tun else None
        try:
            tr = None
            for _ in range(2):  # one retry: a single lost packet shouldn't lose the city
                try:
                    tr = _rtt_via(url, fam, src)
                except Exception:
                    tr = None
                if tr is not None:
                    break
            tun = _tunnel_rtt(src, fam)
            if tr is None or tun is None:
                return None
            label = _lbl(e, name)
            return label, max(1, int(round(tr - tun)))
        except Exception:
            return None

    out = {}
    try:  # this server, measured the same way, so every city in the tooltip is comparable
        # self-hosted tracker: loopback means nothing, so count Oceania as a typical NZ user's trip to Wellington
        own = 20 if is_self else _rtt_via(url, fam, None)
        if own is not None:
            out["Oceania: Wellington"] = max(1, int(round(own)))
    except Exception:
        pass
    with ThreadPoolExecutor(max(1, len(ex))) as pool:
        out.update(r for r in pool.map(one, ex.items()) if r)
    return out
