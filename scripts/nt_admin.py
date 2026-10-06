"""Repairs to a tracker's stored state, for an admin, with the app STOPPED (it holds these files in memory).

    python scripts/nt_admin.py [--data DIR] show HOST
    python scripts/nt_admin.py [--data DIR] reinstate HOST [--clean-history]
    python scripts/nt_admin.py [--data DIR] clear-clocks HOST
    python scripts/nt_admin.py [--data DIR] clear-peers HOST
    python scripts/nt_admin.py [--data DIR] clear-history HOST
    python scripts/nt_admin.py [--data DIR] forget HOST

show           everything stored about the host (safe while the app runs).
reinstate      a removed tracker that was removed by a fault in our checks: clears its peer-test records and saved
               state, marks the removal as ours (once it's listed again the app deletes the record, so it doesn't count
               towards a longer ban), and with --clean-history drops the uptime history it would get back. Then press
               "Check again now" on its page (the VPS's nt-admin wrapper does that).
clear-clocks   drop a listed tracker's removal clocks (Up/Bad, not-working, Junk/Broken) and its Up/Bad log.
clear-peers    drop a tracker's peer-test records (all families, split, fake peers, unusable address, last seen).
clear-history  drop a listed tracker's uptime history (30-day slots and daily summary) and saved state.
forget         delete a removal record and its dated ban lines (permanent bans are left alone).

Every change first copies the files it touches to DIR/admin-backups/<time>-<command>-<host>/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from urllib.parse import urlparse

PEER_FILES = ("peer_fails.json", "peer_hist.json", "peer_hist_fam.json", "peer_last.json", "split_hist.json",
              "peer_seen.json", "nat_seen.json", "fake_fails.json", "fake_hist.json")
CLOCK_KEYS = ("bad_since", "bad_left", "bad_log", "useless_since", "useless_left", "junk_since", "junk_left")
SHOW_FILES = PEER_FILES + ("last_state.json", "fam_fails.json", "fam_hist.json", "fams.json", "closed.json", "warnings.json",
                           "down_why.json", "ann_iv.json", "confirm.json")


class Data:
    def __init__(self, root: str, cmd: str, host: str) -> None:
        self.root, self.cmd, self.host = root, cmd, host.lower()
        self.backup_dir: str | None = None
        self.changed: list[str] = []

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def load(self, name: str):
        try:
            with open(self.path(name)) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _backup(self, name: str) -> None:
        if not os.path.exists(self.path(name)):
            return
        if self.backup_dir is None:
            self.backup_dir = self.path(os.path.join("admin-backups", "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), self.cmd, self.host)))
            os.makedirs(self.backup_dir, exist_ok=True)
        dst = os.path.join(self.backup_dir, name)
        if not os.path.exists(dst):
            shutil.copy2(self.path(name), dst)

    def save(self, name: str, value) -> None:
        self._backup(name)
        with open(self.path(name), "r+" if os.path.exists(self.path(name)) else "w") as f:  # in place: same inode
            json.dump(value, f)
            f.truncate()
        self.changed.append(name)

    def urls(self) -> list[str]:
        """Every URL stored for this host, in any file (a tracker can have moved between protocols)."""
        found = set()
        for name in SHOW_FILES + ("daily.json", "lat_hist.json"):
            for k in self.load(name):
                if _host(k) == self.host:
                    found.add(k)
        for row in self.db_rows():
            found.add(row["url"])
        rec = self.load("removed.json").get(self.host) or {}
        if rec.get("url"):
            found.add(rec["url"])
        return sorted(found)

    def db_rows(self) -> list[sqlite3.Row]:
        if not os.path.exists(self.path("trackon.db")):
            return []
        con = sqlite3.connect(self.path("trackon.db"))
        con.row_factory = sqlite3.Row
        try:
            return [r for r in con.execute("select * from status") if (r["host"] or "").lower() == self.host]
        finally:
            con.close()

    def drop_urls(self, names: tuple[str, ...], urls: list[str]) -> None:
        for name in names:
            d = self.load(name)
            if any(u in d for u in urls):
                for u in urls:
                    d.pop(u, None)
                self.save(name, d)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def show(d: Data) -> None:
    for row in d.db_rows():
        h = json.loads(row["historic"] or "[]")
        print("listed: %s  status %s  score %.1f  history %d slots  added %s" % (
            row["url"], row["status"], float(row["uptime"] or 0), len(h), time.strftime("%Y-%m-%d", time.gmtime(row["added"] or 0))))
    rec = d.load("removed.json").get(d.host)
    if rec:
        print("removal record:", json.dumps({k: v for k, v in rec.items() if k != "hist"}), "| uptime history kept:", bool(rec.get("hist")))
    for line in _ban_lines(d):
        print("ban:", line)
    for u in d.urls():
        for name in SHOW_FILES:
            v = d.load(name).get(u)
            if v not in (None, {}, []):
                print("%-18s %s: %s" % (name, u, json.dumps(v)[:300]))


def _ban_lines(d: Data) -> list[str]:
    try:
        with open(d.path("denylist.txt")) as f:
            return [ln for ln in f.read().splitlines() if ln.split()[:1] == [d.host]]
    except FileNotFoundError:
        return []


def reinstate(d: Data, clean_history: bool) -> None:
    removed = d.load("removed.json")
    rec = removed.get(d.host)
    if not rec:
        sys.exit("%s has no removal record: nothing to reinstate" % d.host)
    urls = d.urls()
    d.drop_urls(PEER_FILES + ("last_state.json",), urls)
    rec["reason"] = "removed in error: our checks misjudged it (being reinstated)"
    rec["fault"] = True
    if clean_history:
        rec.pop("hist", None)
    d.save("removed.json", removed)
    print("%s ready: peer records and saved state cleared, removal marked as ours%s. Now press Check again now on its page."
          % (d.host, ", uptime history dropped" if clean_history else ""))


def clear_clocks(d: Data) -> None:
    ls = d.load("last_state.json")
    hit = False
    for u in d.urls():
        s = ls.get(u)
        if isinstance(s, dict) and any(k in s for k in CLOCK_KEYS):
            for k in CLOCK_KEYS:
                s.pop(k, None)
            hit = True
    if hit:
        d.save("last_state.json", ls)
    print("%s: removal clocks %s" % (d.host, "cleared" if hit else "none running"))


def clear_peers(d: Data) -> None:
    d.drop_urls(PEER_FILES, d.urls())
    print("%s: peer-test records cleared" % d.host)


def clear_history(d: Data) -> None:
    rows = d.db_rows()
    if not rows:
        sys.exit("%s isn't listed" % d.host)
    d._backup("trackon.db")
    con = sqlite3.connect(d.path("trackon.db"))
    try:
        con.execute("update status set historic = '[]' where lower(host) = ?", (d.host,))
        con.commit()
    finally:
        con.close()
    d.changed.append("trackon.db")
    d.drop_urls(("daily.json", "last_state.json"), d.urls())
    print("%s: uptime history and saved state cleared" % d.host)


def forget(d: Data) -> None:
    removed = d.load("removed.json")
    had = removed.pop(d.host, None) is not None
    if had:
        d.save("removed.json", removed)
    try:
        with open(d.path("denylist.txt")) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    keep = [ln for ln in lines if not (ln.split()[:1] == [d.host] and len(ln.split()) > 1 and ln.split()[1].isdigit())]
    if keep != lines:
        d._backup("denylist.txt")
        with open(d.path("denylist.txt"), "r+") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
            f.truncate()
        d.changed.append("denylist.txt")
    print("%s: removal record %s, dated bans %s" % (d.host, "deleted" if had else "none", "lifted" if keep != lines else "none"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data", help="the app's data directory (default: data)")
    ap.add_argument("command", choices=("show", "reinstate", "clear-clocks", "clear-peers", "clear-history", "forget"))
    ap.add_argument("host")
    ap.add_argument("--clean-history", action="store_true", help="reinstate: drop the uptime history it would get back")
    a = ap.parse_args(argv)
    d = Data(a.data, a.command, a.host)
    {"show": show, "clear-clocks": clear_clocks, "clear-peers": clear_peers, "clear-history": clear_history,
     "forget": forget}.get(a.command, lambda d: reinstate(d, a.clean_history))(d)
    if d.backup_dir:
        print("backup:", d.backup_dir)


if __name__ == "__main__":
    main()
