"""newtrackon.store: saves that can't be broken by another thread, and loads that never silently drop a file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from newtrackon import store


def test_save_while_another_thread_adds_entries() -> None:
    d = {"k%d" % i: {"st": "up_good", "since": i} for i in range(20000)}

    def grow():  # adds entries until there are 150000 (json.dump failed on nearly every save meanwhile)
        i = 0
        while len(d) < 150000:
            d["n%d" % i] = i
            i += 1
    th = threading.Thread(target=grow)
    th.start()
    n = 0
    while th.is_alive():
        assert store.save(d, "data/a.json")
        n += 1
    th.join()
    assert n >= 1 and isinstance(json.load(open("data/a.json")), dict)  # at least one save raced the growth


def test_two_threads_saving_one_file() -> None:
    small, big = {"s%d" % i: i for i in range(500)}, {"b%d" % i: {"x": i} for i in range(30000)}
    ths = [threading.Thread(target=lambda x=x: [store.save(x, "data/b.json") for _ in range(30)]) for x in (small, big)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert json.load(open("data/b.json")) in (small, big)
    assert not [f for f in os.listdir("data") if f.endswith(".tmp")]


def test_unreadable_file_is_kept_aside_and_logged(caplog) -> None:
    open("data/c.json", "w").write('{"x": 1, "y"')
    assert store.load("data/c.json") == {}
    assert not os.path.exists("data/c.json") and [f for f in os.listdir("data") if f.startswith("c.json.bad-")]
    assert "unreadable data/c.json" in caplog.text
    assert store.load("data/none.json", default=[]) == []


def test_a_failed_save_never_raises(caplog) -> None:
    assert store.save({"x": 1}, "no-such-dir/x.json") is False and "could not save no-such-dir/x.json" in caplog.text


def test_probe_state_is_read_at_start(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "probe_state.json").write_text(json.dumps({"peer_ok": {"udp://a.example:1/announce": True}}))
    code = "from newtrackon import tracker as T; print(T.PEER_OK.get('udp://a.example:1/announce'))"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr[-500:]
