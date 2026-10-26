"""Test info hashes never hold byte 0x26 ('&'). Some trackers (retracker.x2k.ru, tr2.trkb.ru) percent-decode the
query string before splitting it on '&', so a hash holding that byte reaches them cut short and is refused as
"Invalid info_hash": a false Down for about 7.5% of random hashes (checked by hand against x2k.ru on 2026-10-14:
5 of 5 hashes with 0x26 refused, none of the hashes with '=', '#', '%', '+' or neither)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import unquote_to_bytes, urlparse

import pytest

from newtrackon import scraper

def _raw_param(url: str, name: str) -> bytes:
    """A query parameter's bytes, split on the raw '&' as a correct tracker does."""
    for part in urlparse(url).query.split("&"):
        k, _, v = part.partition("=")
        if k == name:
            return unquote_to_bytes(v.replace("+", " "))
    raise KeyError(name)


def test_helper_redraws_until_there_is_no_ampersand(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter([b"&" + b"x" * 19, b"y" * 19 + b"&", b"z" * 20])
    monkeypatch.setattr(scraper, "urandom", lambda n: next(draws))
    assert scraper.test_info_hash() == b"z" * 20


def test_helper_hashes_are_20_random_bytes_without_0x26() -> None:
    hashes = {scraper.test_info_hash() for _ in range(3000)}
    assert len(hashes) == 3000  # still random
    assert all(len(h) == 20 and 0x26 not in h for h in hashes)


def test_http_announce_never_sends_an_encoded_ampersand_in_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    got = []

    def get(url):
        got.append(url)
        return SimpleNamespace(status_code=200), b"d8:intervali1800e5:peers0:e"
    draws = iter([b"&" * 20, b"q" * 20])     # the first draw would have been refused by those trackers
    monkeypatch.setattr(scraper, "urandom", lambda n: next(draws) if n == 20 else os.urandom(n))
    monkeypatch.setattr(scraper, "memory_limited_get", get)
    scraper._announce_http_once("http://x.example:6969/announce")
    assert "%26" not in got[0].split("info_hash=", 1)[1].split("&", 1)[0]
    assert _raw_param(got[0], "info_hash") == b"q" * 20


def test_no_bare_random_info_hash_left_in_the_scraper() -> None:
    src = open(scraper.__file__).read()
    assert src.count("urandom(20)") == 1  # only inside test_info_hash itself
