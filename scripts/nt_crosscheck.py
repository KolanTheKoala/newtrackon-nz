"""Weekly independent cross-check of the site's verdicts (cron on the VPS, inside the live container).

Standard library only, on purpose: it shares no code with the checks it audits (scraper.py). For every listed tracker it
announces on every published address of each family (3 tries, resending lost packets), then runs a peer test: client A
from this server, client B from each VPN exit, and B must be handed A with this server's own address. It compares that
with /api/details and reports only clear disagreements, re-tested once 10 minutes later:
  down        the site says Down, but every address of some family answered all 3 tries
  no-answer   the site says it's up, but no address answered any try
  fam-dead    the site says a family is dead, but every address of it answered all 3 tries
  peer-pass   the site says Up/Bad for no peers, but every peer test passed on every family tested
  peer-fail   the site passes a family (2/3+ of its tests), but every one of our tests on it failed
Exit status 0 and no output when everything agrees; 1 with one line per disagreement otherwise.

    python scripts/nt_crosscheck.py --ours-v4 IP --ours-v6 IP [--api URL] [--recheck-after 600] [--only HOST...]
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import random
import socket
import ssl
import struct
import sys
import time
import urllib.request
from urllib.parse import quote_from_bytes, urlparse

EX = []
OURS = {"v4": None, "v6": None}


def bdec(b, i=0):
    c = b[i:i+1]
    if c == b"i":
        j = b.index(b"e", i); return int(b[i+1:j]), j + 1
    if c == b"l":
        i += 1; out = []
        while b[i:i+1] != b"e":
            v, i = bdec(b, i); out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1; out = {}
        while b[i:i+1] != b"e":
            k, i = bdec(b, i); v, i = bdec(b, i); out[k] = v
        return out, i + 1
    j = b.index(b":", i); n = int(b[i:j]); return b[j+1:j+1+n], j + 1 + n

def compact(buf, size):
    out = []
    for k in range(0, len(buf) - len(buf) % size, size):
        e = buf[k:k+size]
        ip = socket.inet_ntop(socket.AF_INET if size == 6 else socket.AF_INET6, e[:-2])
        out.append((ip, struct.unpack("!H", e[-2:])[0]))
    return out

def udp_announce(addr, fam, src, th, pid, port, left, event, to=5):
    s = socket.socket(fam, socket.SOCK_DGRAM); s.settimeout(to)
    try:
        if src: s.bind((src, 0))
        s.connect(addr)
        for attempt in range(2):
            tid = random.getrandbits(32)
            s.send(struct.pack("!QII", 0x41727101980, 0, tid))
            try:
                r = s.recv(2048); break
            except socket.timeout:
                if attempt: raise
        act, rt, cid = struct.unpack("!IIQ", r[:16])
        if act == 3: raise RuntimeError("error: " + r[8:].decode("utf8", "replace"))
        tid = random.getrandbits(32)
        s.send(struct.pack("!QII20s20sQQQIIIiH", cid, 1, tid, th, pid, 0, left, 0, event, 0, random.getrandbits(32), -1, port))
        r = s.recv(8192)
        act = struct.unpack("!I", r[:4])[0]
        if act == 3: raise RuntimeError("error: " + r[8:].decode("utf8", "replace"))
        _, _, iv, le, se = struct.unpack("!IIiii", r[:20]); body = r[20:]
        size = 6 if fam == socket.AF_INET or (len(body) % 18 and len(body) % 6 == 0) else 18
        return {"interval": iv, "seeds": se, "leech": le, "peers": compact(body, size)}
    finally:
        s.close()

class Conn(http.client.HTTPSConnection):
    def __init__(self, host, port, addr, src, tls, **kw):
        self._addr, self._src, self._tls = addr, src, tls
        super().__init__(host, port, timeout=8, context=ssl.create_default_context())
    def connect(self):
        s = socket.socket(socket.AF_INET6 if ":" in self._addr[0] else socket.AF_INET, socket.SOCK_STREAM); s.settimeout(8)
        if self._src: s.bind((self._src, 0))
        s.connect(self._addr)
        self.sock = self._context.wrap_socket(s, server_hostname=self.host) if self._tls else s

def http_announce(u, addr, fam, src, th, pid, port, left, event):
    p = urlparse(u)
    q = "info_hash=%s&peer_id=%s&port=%d&uploaded=0&downloaded=0&left=%d&compact=1&event=%s" % (quote_from_bytes(th), quote_from_bytes(pid), port, left, event)
    c = Conn(p.hostname, p.port or (443 if p.scheme == "https" else 80), addr, src, p.scheme == "https")
    try:
        c.request("GET", (p.path or "/") + ("&" if p.query else "?") + (p.query + "&" if p.query else "") + q, headers={"User-Agent": "qBittorrent/5.0.0", "Accept-Encoding": "identity"})
        r = c.getresponse(); b = r.read(200000)
    finally:
        c.close()
    if r.status != 200: raise RuntimeError("HTTP %d" % r.status)
    d, _ = bdec(b)
    if b"failure reason" in d: raise RuntimeError("failure: " + d[b"failure reason"].decode("utf8", "replace")[:60])
    peers = []
    for key, size in ((b"peers", 6), (b"peers6", 18)):
        v = d.get(key)
        if isinstance(v, bytes): peers += compact(v, size if not (size == 18 and len(v) % 18 and len(v) % 6 == 0) else 6)
        elif isinstance(v, list): peers += [(x.get(b"ip", b"").decode(), x.get(b"port")) for x in v]
    return {"interval": d.get(b"interval"), "seeds": d.get(b"complete"), "leech": d.get(b"incomplete"), "peers": peers,
            "warning": (d.get(b"warning message") or b"").decode("utf8", "replace")[:60] or None}

def announce(u, *a):
    return (udp_announce if u.startswith("udp") else http_announce)(*((a) if u.startswith("udp") else (u,) + a))

def addrs(u, fam):
    p = urlparse(u)
    try:
        return list(dict.fromkeys(x[4] for x in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), fam, socket.SOCK_DGRAM if p.scheme == "udp" else socket.SOCK_STREAM)))
    except socket.gaierror:
        return []

def err(e):
    return (str(e) if isinstance(e, RuntimeError) else type(e).__name__)[:50]



def ours(ip):
    """This server's own address (IPv4 exactly, IPv6 within its /64)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    want = OURS["v4" if a.version == 4 else "v6"]
    if not want:
        return a.is_global
    return a == ipaddress.ip_address(want) if a.version == 4 else a in ipaddress.ip_network(want + "/64", strict=False)


def pid():
    return b"-qB5000-" + os.urandom(6).hex()[:12].encode()


def probe(u):
    """{"reach": {fam: [[ok, ok, ok] per address]}, "peer": {fam: ["pass" | "fail" | "err"] per exit}}"""
    out = {"reach": {}, "peer": {}}
    udp = u.startswith("udp")
    for fk, fam in (("v4", socket.AF_INET), ("v6", socket.AF_INET6)):
        al = addrs(u, fam)
        if not al:
            continue
        good, reach = None, []
        for ad in al:
            tries = []
            for _ in range(3):
                try:
                    announce(u, ad, fam, None, os.urandom(20), pid(), 51001, 0, 2 if udp else "started")
                    tries.append(True)
                    good = good or ad
                except Exception:
                    tries.append(False)
                time.sleep(1)
            reach.append(tries)
        out["reach"][fk] = reach
        if not good:
            continue
        res = []
        for e in EX:
            src = e.get("ip4" if fk == "v4" else "ip6")
            if not src:
                continue
            th, pa, pb = os.urandom(20), pid(), pid()
            try:
                announce(u, good, fam, None, th, pa, 51001, 0, 2 if udp else "started")
                time.sleep(1)
                rb = announce(u, good, fam, src, th, pb, 51002, 1, 2 if udp else "started")
                res.append("pass" if any(po == 51001 and ours(ip) for ip, po in rb["peers"]) else "fail")
                for s_, p_, po, left in ((None, pa, 51001, 0), (src, pb, 51002, 1)):
                    try:
                        announce(u, good, fam, s_, th, p_, po, left, 3 if udp else "stopped")
                    except Exception:
                        pass
            except Exception:
                res.append("err")
            time.sleep(1)
        out["peer"][fk] = res
    return out


def disagreements(t, r):
    """Clear disagreements between the site's view of tracker t and our probe r."""
    out = []
    reach = r["reach"]
    fam_all = {f: bool(v) and all(all(x) for x in v) for f, v in reach.items()}
    fam_any = {f: any(any(x) for x in v) for f, v in reach.items()}
    if t["status"] == "down" and any(fam_all.values()):
        out.append("down: the site says Down, but %s answered every try" % "/".join(f for f, v in fam_all.items() if v))
    if t["status"] != "down" and reach and not any(fam_any.values()):
        out.append("no-answer: the site says %s, but no address answered any of our tries" % t["status"])
    for f, st in (t.get("families") or {}).items():
        if st == "dead" and fam_all.get(f):
            out.append("fam-dead: the site says %s is dead, but every %s address answered every try" % (f, f))
    verdicts = {f: [x for x in v if x != "err"] for f, v in r["peer"].items()}
    tested = {f: v for f, v in verdicts.items() if v}
    if "no_peers" in (t.get("problems") or []) and tested and all(all(x == "pass" for x in v) for v in tested.values()):
        out.append("peer-pass: the site says no peers, but all our peer tests passed (%s)" % ", ".join("%s %d/%d" % (f, len(v), len(v)) for f, v in tested.items()))
    by = (t.get("peer_test") or {}).get("by_family") or {}
    for f, v in tested.items():
        s = by.get(f)
        if s and s["of"] >= 3 and s["passed"] * 3 >= s["of"] * 2 and len(v) >= 2 and all(x == "fail" for x in v):
            out.append("peer-fail: the site passes %s (%d/%d), but all %d of our tests on it failed" % (f, s["passed"], s["of"], len(v)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8080/api/details")
    ap.add_argument("--data", default="data")
    ap.add_argument("--recheck-after", type=int, default=600)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--ours-v4", help="this server's public IPv4 (client A must come back with it)")
    ap.add_argument("--ours-v6", help="an address in this server's IPv6 /64")
    a = ap.parse_args(argv)
    try:
        EX.extend(json.load(open(os.path.join(a.data, "probe_src.json"))).get("all", []))
    except (OSError, ValueError):
        pass
    OURS.update({"v4": a.ours_v4, "v6": a.ours_v6})
    def site():
        return {t["host"]: t for t in json.load(urllib.request.urlopen(a.api, timeout=60))}
    first = {}
    for h, t in site().items():
        if a.only and h not in a.only:
            continue
        d = disagreements(t, probe(t["url"]))
        if d:
            first[h] = d
    if not first:
        return 0
    time.sleep(a.recheck_after)
    now = site()
    lines = []
    for h in first:
        t = now.get(h)
        if not t:
            continue
        for d in disagreements(t, probe(t["url"])):
            lines.append("%s: %s" % (h, d))
    for ln in lines:
        print(ln)
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
