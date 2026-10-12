"""Every store of per-tracker records is declared once (tracker._url_store), and everything that goes through all of
them (a URL move, the peer and protocol resets, a restart, the test fixtures, the admin tool) goes by that list."""

from __future__ import annotations

import json
import os
import re

import pytest

from newtrackon import store
from newtrackon import tracker as T

U, U2 = "udp://a.example:1/announce", "udp://b.example:2/announce"


def test_every_module_dict_is_a_store_or_said_not_to_be() -> None:
    dicts = {n for n, v in vars(T).items() if isinstance(v, dict) and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", n)}
    assert dicts - set(T._STORES) - set(T._NOT_STORES) - {"_STORES", "_NOT_STORES"} == set()
    assert not set(T._NOT_STORES) & set(T._STORES)


def test_the_admin_tools_peer_files_are_the_peer_stores() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("nt_admin", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "nt_admin.py"))
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    assert set(A.PEER_FILES) == {os.path.basename(s["file"]) for s in T._STORES.values() if s["peer"] and s["file"]}


def _fill() -> None:
    for n in T._STORES:
        getattr(T, n)[U] = ["x"]


def test_a_url_move_takes_every_store() -> None:
    _fill()
    moved = T._nt_migrate_url(U, U2)
    assert set(moved) == set(T._STORES)
    assert all(U not in getattr(T, n) and getattr(T, n)[U2] == ["x"] for n in T._STORES)
    for n, s in T._STORES.items():
        if s["file"]:
            assert json.load(open(s["file"]))[U2] == ["x"], n


def test_the_resets_clear_what_they_say() -> None:
    _fill()
    T._peer_reset(U)
    assert {n for n in T._STORES if U not in getattr(T, n)} == {n for n, s in T._STORES.items() if s["peer"]}
    _fill()
    T._protocol_reset(U)
    assert {n for n in T._STORES if U not in getattr(T, n)} == {n for n, s in T._STORES.items() if s["peer"] or s["protocol"]}


def test_the_probe_stores_come_back_after_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    for n, s in T._STORES.items():
        if s["probe"]:
            getattr(T, n)[U] = [1, 2] if n == "INFLATED" else True
    T._probe_save()
    for n in T._STORES:
        monkeypatch.setattr(T, n, {})
    T._probe_load()
    assert all(getattr(T, n)[U] == ((1, 2) if n == "INFLATED" else True) for n, s in T._STORES.items() if s["probe"])


def test_a_file_that_isnt_a_store_is_set_aside(caplog: pytest.LogCaptureFixture) -> None:
    with open("data/last_state.json", "w") as f:
        f.write("[1, 2]")
    assert T._store_load("LAST_STATE") == {}
    assert not os.path.exists("data/last_state.json") and any(f.startswith("last_state.json.bad-") for f in os.listdir("data"))
    assert "holds a list, not a dict" in caplog.text


def test_a_file_laid_out_wrong_is_set_aside() -> None:
    store.save({U: [1, 2]}, "data/region_ts.json")  # REGION_SAMPLES holds {url: {region: [samples]}}
    assert T._store_load("REGION_SAMPLES") == {} and not os.path.exists("data/region_ts.json")
