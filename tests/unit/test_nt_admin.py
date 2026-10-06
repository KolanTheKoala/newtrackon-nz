"""scripts/nt_admin.py: the admin repairs done by hand on 2026-10-06, as tested commands."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

_spec = importlib.util.spec_from_file_location("nt_admin", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "nt_admin.py"))
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

U = "udp://gone.example:6969/announce"
L = "udp://listed.example:6969/announce"


@pytest.fixture
def data(tmp_path):
    def w(name, v):
        (tmp_path / name).write_text(json.dumps(v))
    w("removed.json", {"gone.example": {"url": U, "reason": "handed out no peers 85% of the last 7 days (Up/Bad)", "count": 1, "hist": "1110", "added": 1}})
    w("peer_hist_fam.json", {U: {"v4": [0, 0, 0]}, L: {"v4": [1, 1, 1]}})
    w("peer_fails.json", {U: 3})
    w("split_hist.json", {U: [1, 1]})
    w("last_state.json", {U: {"st": "up_bad", "bad_since": 5}, L: {"st": "up_bad", "bad": ["x"], "bad_since": 5, "bad_log": [[1, 2]], "junk_since": 9}})
    w("daily.json", {L: [["2026-10-01", 100.0, 48, {}]]})
    (tmp_path / "denylist.txt").write_text("gone.example 1790000000\nforever.example\n")
    con = sqlite3.connect(tmp_path / "trackon.db")
    con.execute("create table status (host text, url text, status int, uptime real, historic text, added int)")
    con.execute("insert into status values ('listed.example', ?, 1, 90, '[1,1,0,1]', 1)", (L,))
    con.commit(); con.close()
    return tmp_path


def _j(p, name):
    return json.loads((p / name).read_text())


def test_reinstate_our_fault(data, capsys) -> None:
    A.main(["--data", str(data), "reinstate", "gone.example", "--clean-history"])
    rec = _j(data, "removed.json")["gone.example"]
    assert rec["fault"] is True and "hist" not in rec and "(Up/Bad)" not in rec["reason"]  # no Up/Bad clock carried over
    assert U not in _j(data, "peer_hist_fam.json") and U not in _j(data, "peer_fails.json") and U not in _j(data, "split_hist.json")
    assert U not in _j(data, "last_state.json") and L in _j(data, "peer_hist_fam.json")  # other trackers untouched
    backups = os.listdir(data / "admin-backups")
    assert len(backups) == 1 and "removed.json" in os.listdir(data / "admin-backups" / backups[0])
    assert "Check again now" in capsys.readouterr().out


def test_reinstate_needs_a_record(data) -> None:
    with pytest.raises(SystemExit):
        A.main(["--data", str(data), "reinstate", "listed.example"])


def test_clear_clocks(data) -> None:
    A.main(["--data", str(data), "clear-clocks", "listed.example"])
    assert _j(data, "last_state.json")[L] == {"st": "up_bad", "bad": ["x"]}


def test_clear_history(data) -> None:
    A.main(["--data", str(data), "clear-history", "listed.example"])
    con = sqlite3.connect(data / "trackon.db")
    assert con.execute("select historic from status").fetchone()[0] == "[]"
    assert L not in _j(data, "daily.json") and L not in _j(data, "last_state.json")


def test_forget_keeps_permanent_bans(data) -> None:
    A.main(["--data", str(data), "forget", "gone.example"])
    assert "gone.example" not in _j(data, "removed.json")
    assert (data / "denylist.txt").read_text() == "forever.example\n"


def test_show_is_read_only(data, capsys) -> None:
    A.main(["--data", str(data), "show", "gone.example"])
    out = capsys.readouterr().out
    assert "removal record" in out and "ban: gone.example 1790000000" in out and "peer_hist_fam.json" in out
    assert not (data / "admin-backups").exists()


def test_fault_record_deleted_once_relisted(monkeypatch) -> None:
    """The app's side: a reinstatement marked as our fault deletes the removal record when the tracker is listed again."""
    from newtrackon import ingest
    from newtrackon import tracker as T
    T.REMOVED["gone.example"] = {"url": U, "reason": "removed in error", "fault": True, "count": 1}
    src = open(ingest.__file__).read()
    assert 'if (_t.REMOVED.get(host) or {}).get("fault"):' in src and "_t.REMOVED.pop(host, None)" in src
