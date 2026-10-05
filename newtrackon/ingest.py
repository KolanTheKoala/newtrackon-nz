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
_restoring: list[bool] = [False]
_durations: deque[float] = deque(maxlen=30)  # seconds each recent queue item took, start to saved (for the ETAs)
_started: list[float] = [0.0]  # when the item being checked now started  # while the saved queue is being re-queued, don't overwrite the file

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
    rate = seconds_per_url()
    # time until its result: what's left of the one being checked now, then each one up to and including it
    left = max(rate * 0.2, rate - (time() - _started[0])) if _in_flight else 0.0
    ahead = 1 if _in_flight else 0
    for n, p in enumerate(out, 1):
        p["pos"] = n
        p["eta"] = 0 if p["checking"] else round(left + (n - ahead) * rate)
    return out


def seconds_per_url() -> float:
    """Average time per queue item over the last 30 (a URL can give several result rows, so rows don't measure it).
    15 s until 3 have been timed since the last restart."""
    d = list(_durations)
    return sum(d) / len(d) if len(d) >= 3 else 15.0


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


def normalise_url(url: str) -> str:
    """One spelling per host, so bans and duplicate checks can't be dodged: 'tracker.example.' (trailing dot) is
    'tracker.example', and a Unicode name is written in punycode ('bücher.example' -> 'xn--bcher-kva.example')."""
    try:
        p = urlparse(url)
        host = p.hostname or ""
    except ValueError:
        return url
    new = host.rstrip(".")
    if not new.isascii():
        try:
            new = new.encode("idna").decode("ascii")
        except UnicodeError:
            return url  # not a valid name: it won't resolve anyway
    if new == host:
        return url
    netloc = p.netloc
    i = netloc.lower().rfind(host)
    if i < 0:
        return url
    return p._replace(netloc=netloc[:i] + new + netloc[i + len(host):]).geturl()


def enqueue_new_trackers(input_string: str) -> None:
    input_string = input_string.lower()
    new_trackers_list = sorted(dict.fromkeys(normalise_url(u) for u in input_string.split()), key=_nt_pri)
    for url in new_trackers_list:
        logger.info("Tracker %s submitted to the queue", url)
        add_one_tracker_to_submitted_queue(url)


REINSTATE: dict[str, float] = {}  # host -> when its operator asked: one pass past its ban (covers both checks)
REINSTATE_TTL = 3 * 3600

# A new tracker must answer twice, CONFIRM_DELAY apart, before it's listed: one answer from a machine that then goes
# away for good (a home PC on a dynamic address) no longer gets it listed. {submitted url: {"t": first answer, "queued": bool}}
CONFIRM_DELAY = 1800
CONFIRM_FILE = "data/confirm.json"
try:
    with open(CONFIRM_FILE) as _cf:
        CONFIRM: dict[str, dict] = json.load(_cf)
except Exception:
    CONFIRM = {}


def _confirm_save() -> None:
    try:
        tmp = CONFIRM_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(CONFIRM, f)
        os.replace(tmp, CONFIRM_FILE)
    except OSError:
        logger.exception("could not save the confirmation list")


def confirm_due(now: float | None = None) -> None:
    """From the check loop: queue each tracker whose second check is due. Entries whose second check never ran are dropped."""
    now = now or time()
    changed = False
    for url, e in list(CONFIRM.items()):
        if e.get("queued"):
            if now - e["t"] > CONFIRM_DELAY + 4 * 3600:
                CONFIRM.pop(url, None)
                changed = True
            continue
        if now - e["t"] >= CONFIRM_DELAY:
            e["queued"] = True
            changed = True
            __import__("threading").Thread(target=add_one_tracker_to_submitted_queue, args=(url,), daemon=True).start()
    if changed:
        _confirm_save()


def confirming(now: float | None = None) -> list[dict[str, object]]:
    """Trackers waiting for their second check, for the submitted page, newest first."""
    now = now or time()
    return [{"url": u, "eta": max(0, int(e["t"] + CONFIRM_DELAY - now))}
            for u, e in sorted(CONFIRM.items(), key=lambda x: -x[1]["t"]) if not e.get("queued")]


def _mark_confirm_row(url: str) -> None:
    """The submitted page's row for a first answer: pending, not accepted yet."""
    if submitted_data and submitted_data[0].get("url") == url:
        row = submitted_data[0]
        info = row.get("info")
        first = info[0] if isinstance(info, list) and info else info if isinstance(info, str) else ""
        row.update({"status": 0, "confirm": True,
                    "info": [first, "Answered. It's checked again in 30 minutes, and listed if it answers then too."]})


def _reinstating(host: str) -> bool:
    t = REINSTATE.get((host or "").lower())
    return t is not None and time() - t < REINSTATE_TTL


def reinstate(host: str, url: str) -> None:
    """A removed tracker's operator pressed 'Check again now': check it like a new submission, past its ban.
    If it's accepted it's listed again and the ban is lifted; if not, nothing changes."""
    REINSTATE[host.lower()] = time()
    add_one_tracker_to_submitted_queue(url)


def _lift_ban(host: str) -> None:
    """Remove the host's dated (30-day) denylist lines. Permanent entries (no date) are never touched."""
    path = "data/denylist.txt"
    try:
        with open(path, "r+", encoding="utf-8") as f:
            lines = f.read().splitlines()
            keep = [ln for ln in lines if not (ln.split()[:1] == [host] and len(ln.split()) > 1 and ln.split()[1].isdigit())]
            if keep != lines:
                f.seek(0)
                f.write("\n".join(keep) + "\n")
                f.truncate()
    except OSError:
        logger.exception("could not lift the ban on %s", host)


def _denylist_hosts() -> set[str]:
    """Hosts banned right now: permanent entries, and dated ones (30 or 90 days) that haven't run out."""
    from newtrackon.tracker import _nt_ban_entries
    now = time()
    return {h for h, since, days in _nt_ban_entries() if since is None or now - since <= days * 86400}


def add_one_tracker_to_submitted_queue(url: str) -> None:
    url = normalise_url(url)
    host = urlparse(url).hostname
    if host and host.lower() in _denylist_hosts() and not _reinstating(host):
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
    if url in CONFIRM and not CONFIRM[url].get("queued"):
        logger.info("Tracker %s denied, already waiting for its second check", url)
        return
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
    if tracker_candidate.ips:  # a new name for a banned tracker's server is still that tracker
        from newtrackon.tracker import _nt_ban_ips
        listed = {ip for t in (trackers_in_db or []) for ip in (t.ips or [])}
        hit = _nt_ban_ips(tracker_candidate.ips, skip_host=(host or "").lower() if _reinstating(host or "") else None, listed_ips=listed)
        if hit:
            logger.info("Tracker %s denied, same server as banned %s", url, hit)
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
        _started[0] = time()
        try:
            process_new_tracker(tracker)
            save_deque_to_disk(submitted_data, submitted_history_file)
        except Exception:
            logger.exception("Unhandled error while processing submitted tracker %s", tracker.url)
        finally:
            _in_flight.clear()
            submitted_queue.task_done()
            save_queue()
            _durations.append(time() - _started[0])


def _closed_on_submit(url: str) -> str | None:
    """Private or whitelist-only, by the warning in the answer just recorded for this URL: the reason, or None."""
    import re
    from newtrackon.tracker import _nt_closed_reason
    row = submitted_data[0] if submitted_data else None
    if not row or row.get("status") != 1 or row.get("url") != url:
        return None
    m = re.search(r"'warning message': '([^']*)'", str(row.get("info") or ""))
    why = _nt_closed_reason(m.group(1)) if m else None
    return f"being {why}" if why and why.startswith("a ") else (f"saying {why}" if why else None)


def process_new_tracker(tracker_candidate: Tracker) -> None:
    logger.info("Processing new tracker: %s", tracker_candidate.url)
    submitted_url = tracker_candidate.url
    second = CONFIRM.pop(submitted_url, None)  # this is its second check (if it fails below, it simply isn't listed)
    if second is not None:
        _confirm_save()
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
    closed = _closed_on_submit(tracker_candidate.url)
    if closed:
        log_wrong_interval_denial(closed)
        return
    if CONFIRM_DELAY > 0 and (second is None or time() - second["t"] < CONFIRM_DELAY - 120):
        CONFIRM[submitted_url] = {"t": int(time()), "queued": False}
        _confirm_save()
        _mark_confirm_row(tracker_candidate.url)
        logger.info("Tracker %s answered: checked again in %d min before it's listed", submitted_url, CONFIRM_DELAY // 60)
        return
    # Any announce interval is accepted: this instance sets its own adaptive check interval and never uses the tracker's.
    tracker_candidate.update_ipapi_data()
    if _reinstating((cand_host or "").lower()):
        _restore_history(tracker_candidate, (cand_host or "").lower())
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
    host = (cand_host or "").lower()
    if _reinstating(host):
        REINSTATE.pop(host, None)
        _lift_ban(host)
        _keep_upbad_clock(host, tracker_candidate.url)
        logger.info("Tracker %s reinstated at its operator's request: ban lifted", tracker_candidate.url)


def _restore_history(t: Tracker, host: str) -> None:
    """A reinstated tracker gets back its last week of uptime and its listing date, so a tracker that's still flaky
    is judged at its next checks (the 15% rule) instead of starting clean."""
    from newtrackon import tracker as _t
    r = _t.REMOVED.get(host) or {}
    hist = str(r.get("hist") or "")
    if hist:
        t.historic = deque(({"1": 1, "h": 0.5}.get(c, 0) for c in hist), maxlen=_t.HISTORIC_SLOTS)
    if r.get("added"):
        t.added = int(r["added"])


def _keep_upbad_clock(host: str, url: str) -> None:
    """Removed for Up/Bad: carry its 5 days over, so a tracker that's still Up/Bad goes again at its next checks
    instead of getting a fresh 5 days. A recovery of 12 hours or more resets it as usual."""
    from newtrackon import tracker as _t
    r = _t.REMOVED.get(host) or {}
    why = str(r.get("reason") or "")
    if "(Up/Bad)" not in why:
        return
    now = int(time())
    bad = "returns fake peers (3+ checks in a row)" if "fake peers" in why else "hands out no peers (3+ of its last 6 peer tests failed)"
    _t.LAST_STATE[url] = {"st": "up_bad", "bad": [bad], "dead": [], "since": now, "bad_since": now - _t.REMOVE_DAYS * 86400}
    _t._jsave(_t.LAST_STATE, _t._LAST_STATE_FILE)


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


# --- show silent refusals on the Submitted page as Refused: same-server aliases, bare IPs, names that don't resolve,
# duplicates, banned hosts this site removed (other denylist entries stay private), a full queue
import logging as _nt_logging
from time import time as _nt_time


def _ban_reason(url: str) -> str | None:
    """Why a banned host was refused, for hosts this site removed; None (no row) for any other denylist entry."""
    from newtrackon import ntextra
    host = (urlparse(url).hostname or "").lower()
    b = ntextra._ban(host)
    if not b or not b["active"]:
        return None
    if b["until"] is None:
        return "Banned from the list"
    return "Banned until %s: removed from the list on %s" % (ntextra._date(b["until"]), ntextra._date(ntextra.T.REMOVED[host]["t"]))


class _NtRejectRows(_nt_logging.Filter):
    def filter(self, record):
        try:
            m, a = str(record.msg), (record.args or ())
            why = ip = ban_host = None
            if m.startswith("Tracker %s denied, %s IP overlap with %s"):
                ips = [str(x) for x in (a[3] if isinstance(a[3], (list, tuple, set)) else [a[3]])]
                ip = next((x for x in ips if ":" not in x), ips[0] if ips else "")
                why = "Same server as %s, which is already listed" % a[2]
            elif m.startswith("Tracker %s denied, hostname is IP"):
                why = "Bare IP addresses are not accepted, a hostname is needed"
            elif m.startswith("Tracker %s preprocessing failed"):
                why = str(a[1])[:120]
            elif _restoring[0]:
                pass  # re-queuing the saved queue at startup: not a submission
            elif m.startswith("Tracker %s denied, already in the queue"):
                why = "Already waiting in the queue"
            elif m.startswith("Tracker %s denied, already waiting for its second check"):
                why = "Already answered once: waiting for its second check"
            elif m.startswith("Tracker %s denied, already being tracked as %s"):
                why = "Already listed as %s" % a[1]
            elif m.startswith("Tracker %s denied, submission queue is full"):
                why = "The queue is full, please try again later"
            elif m.startswith("Tracker %s denied, host denylisted"):
                why = _ban_reason(str(a[0]))
                ban_host = (urlparse(str(a[0])).hostname or "").lower()
            elif m.startswith("Tracker %s denied, same server as banned %s"):
                b = _ban_reason("udp://%s:1/" % a[1])
                why = "Same server as the banned tracker %s. %s" % (a[1], b) if b else None
                ban_host = str(a[1])
            if why:
                url, now = str(a[0]), int(_nt_time())
                dup = any(r.get("url") == url and why in (r.get("info") or []) and now - int(r.get("time") or 0) < 86400 for r in list(submitted_data))
                if not dup:
                    row = {"url": url, "time": now, "ip": ip or "", "info": [why], "status": 0, "refused": True}
                    if ban_host:
                        row["ban_host"] = ban_host  # the banned tracker's page explains it
                    submitted_data.appendleft(row)
        except Exception:
            pass
        return True


logger.addFilter(_NtRejectRows())
