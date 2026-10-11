"""Saving and loading the app's JSON files, safely when several threads save (the check loop, the submission worker and
web requests all do).

save() builds the whole JSON first, in one call that no other thread can interrupt half-way (json.dumps on plain dicts and
lists runs in C without letting another thread in; json.dump writes as it goes and fails with "dictionary changed size
during iteration" if another thread adds an entry meanwhile). It writes it to a temp file of this thread's own and renames
that over the old file, so a reader or a crash only ever sees the old file or the new one, and two threads saving the same
file can't write into one temp file. It never raises: a failed save is logged and what's in memory is saved next time.

load() gives the default for a missing file. An unreadable one is moved aside (<file>.bad-<time>) and logged, never quietly
taken as empty and then overwritten by the next save."""

from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger("newtrackon")
_LOCK = threading.Lock()


def save(obj, path: str) -> bool:
    try:
        with _LOCK:
            text = json.dumps(obj)
            tmp = "%s.%d.tmp" % (path, threading.get_ident())
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, path)
        return True
    except Exception:
        logger.exception("could not save %s", path)
        return False


def load(path: str, default=None):
    empty = {} if default is None else default
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return empty
    except Exception:
        bad = "%s.bad-%d" % (path, int(time.time()))
        try:
            os.replace(path, bad)
        except OSError:
            bad = None
        logger.exception("unreadable %s%s: starting it empty", path, ", moved to %s" % bad if bad else "")
        return empty
