"""scripts/nt_crosscheck.py: which differences between the site and the independent probe count as disagreements."""

from __future__ import annotations

import importlib.util
import os

import pytest

_spec = importlib.util.spec_from_file_location("nt_crosscheck", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "nt_crosscheck.py"))
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


def _t(status="up_good", problems=(), families=None, by=None):
    return {"host": "x.example", "url": "udp://x.example:1/announce", "status": status, "problems": list(problems),
            "families": families or {"v4": "ok"}, "peer_test": {"by_family": by or {}}}


@pytest.mark.parametrize(("site", "probe", "want"), [
    (_t(), {"reach": {"v4": [[True, True, True]]}, "peer": {"v4": ["pass", "pass", "pass"]}}, []),
    (_t("down"), {"reach": {"v4": [[True, True, True]]}, "peer": {}}, ["down"]),
    (_t("down"), {"reach": {"v4": [[True, False, True]]}, "peer": {}}, []),  # flaky: not a clear disagreement
    (_t(), {"reach": {"v4": [[False, False, False]]}, "peer": {}}, ["no-answer"]),
    (_t(families={"v4": "ok", "v6": "dead"}), {"reach": {"v4": [[True] * 3], "v6": [[True] * 3]}, "peer": {}}, ["fam-dead"]),
    (_t("up_bad", ["no_peers"]), {"reach": {"v4": [[True] * 3]}, "peer": {"v4": ["pass", "pass", "err"]}}, ["peer-pass"]),
    (_t("up_bad", ["no_peers"]), {"reach": {"v4": [[True] * 3]}, "peer": {"v4": ["pass", "fail", "pass"]}}, []),  # split swarm luck
    (_t(by={"v4": {"passed": 6, "of": 6}}), {"reach": {"v4": [[True] * 3]}, "peer": {"v4": ["fail", "fail", "fail"]}}, ["peer-fail"]),
    (_t(by={"v4": {"passed": 3, "of": 6}}), {"reach": {"v4": [[True] * 3]}, "peer": {"v4": ["fail", "fail", "fail"]}}, []),  # the site isn't sure either
])
def test_disagreements(site, probe, want) -> None:
    assert [d.split(":")[0] for d in C.disagreements(site, probe)] == want


def test_a_must_be_our_own_address() -> None:
    C.OURS.update({"v4": "160.30.240.158", "v6": "2401:c060:1010:4007::"})
    assert C.ours("160.30.240.158") and C.ours("2401:c060:1010:4007::9")
    assert not C.ours("172.17.0.1") and not C.ours("104.21.83.32")
