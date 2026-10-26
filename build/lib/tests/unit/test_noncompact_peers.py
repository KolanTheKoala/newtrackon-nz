"""Peer test on a non-compact reply. Some trackers (l2.mxrepo.com) answer with a list of peer dictionaries even when
asked for compact, and bdecode gives those bytes keys (b'ip', b'port'). Read as str keys, our own two test clients had
no address and no port and counted as fake peers: a false Up/Bad (found 2026-10-14)."""

from __future__ import annotations

from collections import OrderedDict

from newtrackon import scraper

WANT = 25186


def _peer(ip: bytes, port: int, pid: bytes = b"-qB5230-xxxxxxxxxxxx") -> OrderedDict:
    return OrderedDict({b"id": pid, b"ip": ip, b"port": port})


def test_our_two_clients_in_bytes_key_dicts_are_not_fake() -> None:
    resp = {"interval": 1800, "peers": [_peer(b"2401:c060:1010:4007::", WANT), _peer(b"203.0.113.9", WANT + 1)]}
    extra: dict = {}
    ok = scraper._probe_eval(resp, WANT, extra, a_ok=lambda ip: ip == "2401:c060:1010:4007::")
    assert ok is True and extra["foreign"] == 0 and extra["seen"]["a"] == "ok" and extra["seen"]["self"] is True


def test_a_real_stranger_in_bytes_key_dicts_still_counts() -> None:
    resp = {"peers": [_peer(b"2401:c060:1010:4007::", WANT), _peer(b"198.51.100.7", 51413, b"-TR4050-yyyyyyyyyyyy")]}
    extra: dict = {}
    scraper._probe_eval(resp, WANT, extra, a_ok=lambda ip: ip == "2401:c060:1010:4007::")
    assert extra["foreign"] == 1


def test_str_key_peers_unchanged() -> None:
    resp = {"peers": [{"IP": "192.0.2.1", "port": WANT}, {"IP": "192.0.2.2", "port": WANT + 1}, {"IP": "0.0.0.0", "port": 0}]}
    extra: dict = {}
    ok = scraper._probe_eval(resp, WANT, extra, a_ok=lambda ip: ip == "192.0.2.1")
    assert ok is True and extra["foreign"] == 1  # 0.0.0.0:0 is a made-up peer (tracker.theoks.net does that)


def test_peer_ip_and_port_come_through() -> None:
    p = scraper._probe_peers({"peers": [_peer(b"192.0.2.5", 6881)], "peers6": [_peer(b"2001:db8::5", 6882)]})
    assert [(scraper._peer_ip(x), x["port"]) for x in p] == [("192.0.2.5", 6881), ("2001:db8::5", 6882)]
