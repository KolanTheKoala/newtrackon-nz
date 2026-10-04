import json
import logging
import os
from collections import deque
from ipaddress import ip_address
from queue import Empty, Full, Queue
from threading import Lock
from time import time
from typing import NoReturn, cast
from urllib.parse import urlparse

from newtrackon import db
from newtrackon.persistence import (
    HistoryData,
    save_deque_to_disk,
    submitted_data,
    submitted_history_file,
)
from newtrackon.scraper import attempt_submitted
from newtrackon.tracker import Tracker

submitted_queue: Queue[Tracker] = Queue(maxsize=10000)
list_lock: Lock = Lock()

# The queue is kept in memory; its URLs are also saved here, so a restart doesn't drop submissions.
QUEUE_FILE = "data/submit_queue.json"
_queue_file_lock: Lock = Lock()
_in_flight: list[str] = []  # the URL being processed now: saved too, so a restart mid-check doesn't lose it
_restoring: list[bool] = [False]  # while the saved queue is being re-queued, don't overwrite the file

logger: logging.Logger = logging.getLogger("newtrackon")

_NT_PRI = {"udp": 0, "http": 1, "https": 2}


def _nt_pri(url: str) -> int:
    # protocol preference: udp first, http next, https last resort
    return _NT_PRI.get(urlparse(url).scheme, 3)


def log_grouped_ip_conflicts(conflicts: dict[str, set[str]], label: str, tracker_url: str) -> None:
    grouped: dict[tuple[str, ...], list[str]] = {}
    for ip, hosts in conflicts.items():
        key = tuple(sorted(hosts))
        grouped.setdefault(key, []).append(ip)
    for hosts, ips in grouped.items():
        ips_sorted = sorted(ips)
        hosts_str = ", ".join(hosts)
        logger.info(
            "Tracker %s denied, %s IP overlap with %s, ips=%s",
            tracker_url,
            label,
            hosts_str,
            ips_sorted,
        )


def collect_ip_conflicts(tracker_candidate: Tracker, trackers: list[Tracker]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    current_conflicts: dict[str, set[str]] = {}
    recent_conflicts: dict[str, set[str]] = {}
    candidate_host = urlparse(tracker_candidate.url).hostname
    candidate_ips = tracker_candidate.ips or []

    for tracker in trackers:
        if candidate_host and tracker.host == candidate_host:
            continue
        tracker_ips = set(tracker.ips or [])
        tracker_recent_ips = set((tracker.recent_ips or {}).keys())
        for ip in candidate_ips:
            if ip in tracker_ips:
                current_conflicts.setdefault(ip, set()).add(tracker.host)
            elif ip in tracker_recent_ips:
                recent_conflicts.setdefault(ip, set()).add(tracker.host)

    return current_conflicts, recent_conflicts


def log_ip_conflicts(tracker_candidate: Tracker, trackers: list[Tracker]) -> bool:
    if not tracker_candidate.ips:
        return False
    current_conflicts, recent_conflicts = collect_ip_conflicts(tracker_candidate, trackers)
    if current_conflicts:
        log_grouped_ip_conflicts(current_conflicts, "current", tracker_candidate.url)
    if recent_conflicts:
        log_grouped_ip_conflicts(recent_conflicts, "recent", tracker_candidate.url)
    return bool(current_conflicts or recent_conflicts)


def save_queue() -> None:
    if _restoring[0]:
        return
    with submitted_queue.mutex:
        urls = [t.url for t in cast("deque[Tracker]", submitted_queue.queue)]
    urls = list(dict.fromkeys(_in_flight + urls))
    with _queue_file_lock:
        try:
            tmp = f"{QUEUE_FILE}.tmp"
            with open(tmp, "w") as f:
                json.dump(urls, f)
            os.replace(tmp, QUEUE_FILE)
        except OSError:
            logger.exception("Could not save the submission queue")


PENDING_SHOWN = 600  # like the history: the submitted page lists at most this many waiting URLs


def pending(limit: int = PENDING_SHOWN) -> list[dict[str, object]]:
    """The submission queue for the submitted page, in processing order (position 1: being checked now, or next)."""
    with submitted_queue.mutex:
        waiting = list(cast("deque[Tracker]", submitted_queue.queue))
    out: list[dict[str, object]] = [{"url": u, "time": 0, "checking": True} for u in list(_in_flight)]
    seen = {str(p["url"]) for p in out}
    for t in waiting:
        if len(out) >= limit:
            break
        if t.url not in seen:
            seen.add(t.url)
            out.append({"url": t.url, "time": int(t.added or 0), "checking": False})
    for n, p in enumerate(out, 1):
        p["pos"] = n
    return out


def restore_saved_queue() -> None:
    """Re-queue the URLs saved before the last restart. They go through the normal checks again (denylist, duplicates, same server)."""
    try:
        with open(QUEUE_FILE) as f:
            saved = json.load(f)
        urls = [u for u in saved if isinstance(u, str)] if isinstance(saved, list) else []
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning("Saved submission queue unreadable, ignored")
        return
    if not urls:
        return
    logger.info("Restoring %d saved submissions", len(urls))
    _restoring[0] = True
    try:
        for url in urls:
            try:
                add_one_tracker_to_submitted_queue(url)
            except Exception:
                logger.exception("Could not restore saved submission %s", url)
    finally:
        _restoring[0] = False
    save_queue()
    logger.info("Restored saved submissions: %d of %d back in the queue", submitted_queue.qsize(), len(urls))


def enqueue_new_trackers(input_string: str) -> None:
    input_string = input_string.lower()
    new_trackers_list = sorted(dict.fromkeys(input_string.split()), key=_nt_pri)
    for url in new_trackers_list:
        logger.info("Tracker %s submitted to the queue", url)
        add_one_tracker_to_submitted_queue(url)


def _denylist_hosts() -> set[str]:
    # "host" on its own = permanent manual ban. "host <epoch>" = auto-ban, expires after 30 days.
    try:
        lines = open("data/denylist.txt", encoding="utf-8").read().splitlines()
    except OSError:
        return set()
    now = time()
    hosts: set[str] = set()
    for ln in lines:
        parts = ln.split()
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) > 1 and parts[1].isdigit() and now - int(parts[1]) > 30 * 86400:
            continue
        hosts.add(parts[0].lower())
    return hosts


def add_one_tracker_to_submitted_queue(url: str) -> None:
    host = urlparse(url).hostname
    if host and host.lower() in _denylist_hosts():
        logger.info("Tracker %s denied, host denylisted", url)
        return

    try:
        parsed_url = urlparse(url)
        if parsed_url.hostname:
            _ = ip_address(parsed_url.hostname)
            logger.info("Tracker %s denied, hostname is IP", url)
            return
    except ValueError:
        pass
    with submitted_queue.mutex:
        queued_trackers = list(cast("deque[Tracker]", submitted_queue.queue))
    for tracker_in_queue in queued_trackers:
        if urlparse(tracker_in_queue.url).netloc == urlparse(url).netloc and urlparse(tracker_in_queue.url).scheme == urlparse(url).scheme:
            logger.info("Tracker %s denied, already in the queue", url)
            return
    with list_lock:
        trackers_in_db = db.get_all_data()
    for tracker in trackers_in_db:
        if tracker.host == urlparse(url).hostname:
            if _nt_pri(url) < _nt_pri(tracker.url):
                logger.info("Tracker %s queued as a possible protocol upgrade of %s", url, tracker.url)
                break
            logger.info(
                "Tracker %s denied, already being tracked as %s",
                url,
                tracker.url,
            )
            return
    try:
        tracker_candidate = Tracker.from_url(url)
    except (RuntimeError, ValueError) as e:
        logger.info("Tracker %s preprocessing failed, reason: %s", url, e)
        return
    if tracker_candidate.ips and trackers_in_db and log_ip_conflicts(tracker_candidate, trackers_in_db):
        return
    try:
        submitted_queue.put_nowait(tracker_candidate)
    except Full:
        logger.info("Tracker %s denied, submission queue is full", url)
        return
    logger.info("Tracker %s added to the submitted queue", url)
    save_queue()


def process_submitted_queue() -> None:
    while True:
        try:
            tracker = submitted_queue.get_nowait()
        except Empty:
            break
        _in_flight[:] = [tracker.url]
        try:
            process_new_tracker(tracker)
            save_deque_to_disk(submitted_data, submitted_history_file)
        finally:
            _in_flight.clear()
            submitted_queue.task_done()
            save_queue()


def submission_worker() -> NoReturn:
    while True:
        tracker = submitted_queue.get()
        _in_flight[:] = [tracker.url]
        try:
            process_new_tracker(tracker)
            save_deque_to_disk(submitted_data, submitted_history_file)
        except Exception:
            logger.exception("Unhandled error while processing submitted tracker %s", tracker.url)
        finally:
            _in_flight.clear()
            submitted_queue.task_done()
            save_queue()


def process_new_tracker(tracker_candidate: Tracker) -> None:
    logger.info("Processing new tracker: %s", tracker_candidate.url)
    with list_lock:
        trackers_in_db = db.get_all_data()
    old: Tracker | None = None
    cand_host = urlparse(tracker_candidate.url).hostname
    for tracker in trackers_in_db:
        if tracker.host == cand_host:
            if _nt_pri(tracker_candidate.url) < _nt_pri(tracker.url):
                old = tracker
                continue
            logger.info(
                "Tracker %s denied, already being tracked as %s",
                tracker_candidate.url,
                tracker.url,
            )
            return
    with submitted_queue.mutex:
        waiting = list(cast("deque[Tracker]", submitted_queue.queue))
    if any(urlparse(q.url).hostname == cand_host and _nt_pri(q.url) < _nt_pri(tracker_candidate.url) for q in waiting):
        logger.info("Tracker %s deferred, a better-protocol version of the same host is queued", tracker_candidate.url)
        try:
            submitted_queue.put_nowait(tracker_candidate)
        except Full:
            pass
        save_queue()
        return
    if tracker_candidate.ips and trackers_in_db and log_ip_conflicts(tracker_candidate, trackers_in_db):
        return

    tracker_candidate.last_downtime = int(time())
    tracker_candidate.last_checked = int(time())
    try:
        (
            tracker_candidate.interval,
            tracker_candidate.url,
            tracker_candidate.latency,
        ) = attempt_submitted(tracker_candidate.url)
    except RuntimeError, ValueError:
        return
    if not tracker_candidate.interval:
        log_wrong_interval_denial("missing interval field")
        return
    # Any announce interval is accepted: this instance sets its own adaptive check interval and never uses the tracker's.
    tracker_candidate.update_ipapi_data()
    if old is not None:
        for a in ("historic", "added", "last_downtime", "last_uptime", "recent_ips"):
            if hasattr(old, a):
                try:
                    setattr(tracker_candidate, a, getattr(old, a))
                except Exception:
                    pass
    tracker_candidate.is_up()
    tracker_candidate.update_uptime()
    if old is not None:
        with list_lock:
            db.delete_tracker(old)
        logger.info("Tracker %s replaced by %s (preferred protocol)", old.url, tracker_candidate.url)
    db.insert_new_tracker(tracker_candidate)
    logger.info("New tracker %s added to newTrackon", tracker_candidate.url)


def log_wrong_interval_denial(reason: str) -> None:
    if not submitted_data:
        logger.warning("Interval rejection without submitted debug entry: %s", reason)
        return
    debug: HistoryData = submitted_data.popleft()
    info = debug["info"]
    first_info = info[0] if isinstance(info, list) and info else info if isinstance(info, str) else ""
    debug.update(
        {
            "status": 0,
            "info": [
                first_info,
                f"Tracker rejected for {reason}",
            ],
        }
    )
    submitted_data.appendleft(debug)


# --- show silent refusals on the Submitted page (same-server aliases, bare IPs, names that don't resolve)
import logging as _nt_logging
from time import time as _nt_time


class _NtRejectRows(_nt_logging.Filter):
    def filter(self, record):
        try:
            m, a = str(record.msg), (record.args or ())
            why = ip = None
            if m.startswith("Tracker %s denied, %s IP overlap with %s"):
                ips = [str(x) for x in (a[3] if isinstance(a[3], (list, tuple, set)) else [a[3]])]
                ip = next((x for x in ips if ":" not in x), ips[0] if ips else "")
                why = "Same server as %s, which is already listed" % a[2]
            elif m.startswith("Tracker %s denied, hostname is IP"):
                why = "Bare IP addresses are not accepted, a hostname is needed"
            elif m.startswith("Tracker %s preprocessing failed"):
                why = str(a[1])[:120]
            if why:
                url, now = str(a[0]), int(_nt_time())
                dup = any(r.get("url") == url and why in (r.get("info") or []) and now - int(r.get("time") or 0) < 86400 for r in list(submitted_data))
                if not dup:
                    submitted_data.appendleft({"url": url, "time": now, "ip": ip or "", "info": [why], "status": 0})
        except Exception:
            pass
        return True


logger.addFilter(_NtRejectRows())
