"""NZ fork extras: /api/details (full JSON state per tracker) and /api/clean (Up/Good, filtered, one per operator)."""
from flask import Response, jsonify, request

from newtrackon import db
from newtrackon import tracker as T
from newtrackon.regions import DEFAULT_FAST_FROM_MS, FAST_FROM, MEASURED_FROM, parse_region_filter, regions_of


def _trackers():
    return T.format_uptime_and_downtime_time(db.get_all_data())


def _reliable(t, ms):
    # reliability score (availability x stability, before the latency penalty) is 90 or more
    try:
        a, s = T._nt_avail_stab(t.historic)
        return round(a * s * 100) >= 90
    except Exception:
        return round(float(t.uptime or 0) + T._nt_lat_penalty(ms)) >= 90


def _statekey(t):
    try:
        return _state(t)[0]
    except Exception:
        return ""


_DYING_DAYS = T.REMOVE_DAYS - 2  # dark grey 2 days before removal (Down or Up/Bad for REMOVE_DAYS, tracker.py)


def _dying(t, now=None):
    """The tooltip for a tracker within 2 days of removal (Down or Up/Bad for REMOVE_DAYS - 2 days), else None."""
    import time
    now = now or time.time()
    if _rowcls(t) == "offline":
        days = (now - int(t.last_uptime or 0)) / 86400.0
        return "No answer for %d+ days: removed and banned after %d" % (int(days), T.REMOVE_DAYS) if days >= _DYING_DAYS else None
    ub = T._nt_upbad_days(t.url, now) if T._peer_rule_applies(t.url) else None
    if ub is not None and ub >= _DYING_DAYS:
        return "Up/Bad for %d+ days: removed and banned after %d unless fixed" % (int(ub), T.UPBAD_DAYS)
    ud = T._nt_useless_days(t.url, now)
    if ud is not None and getattr(t, "status", 1) == 1 and not T._peer_rule_applies(t.url):
        ud = None
    if ud is not None and ud >= _DYING_DAYS:
        return "Not working (down or Up/Bad) for %d+ days: removed and banned after %d unless fixed" % (int(ud), T.REMOVE_DAYS)
    jd = T._nt_junk_days(t.url, now)
    if jd is not None and jd >= T.JUNK_DAYS - 2:
        return "Up/Junk or Up/Broken for %d+ days: removed and banned after %d unless fixed" % (int(jd), T.JUNK_DAYS)
    return None


def _rowcls(t):
    # row colour follows the status: one rule for the page, the API and the feed
    try:
        s = _state(t)[0]
    except Exception:
        s = "up_good" if t.status == 1 else "down"
    if s == "down":
        return "offline"
    if s in ("up_good", "up_new"):
        return "green"
    if s == "up_slow":
        return "green" if round(float(t.uptime or 0) + T._nt_iv_penalty(t.url)) >= 90 else "orange"
    return "orange"


def _state(t):
    if t.status != 1:
        return "down", []
    bad = []
    if T.PEER_FAILS.get(t.url, 0) >= T.PEER_FAIL_LIMIT:
        bad.append("no_peers")
    if T.FAKE_FAILS.get(t.url, 0) >= T.PEER_FAIL_LIMIT:
        bad.append("fake_peers")
    df = T.FAM_FAILS.get(t.url) or {}
    if df.get("n", 0) >= T.PEER_FAIL_LIMIT:
        bad.append("dead_ipv" + str(df.get("fam", "?"))[-1])
    pfb = T._peer_fam_bad(t.url) if T.PEER_FAILS.get(t.url, 0) < T.PEER_FAIL_LIMIT else None
    if pfb:
        bad.append("nopeers_ipv" + pfb[-1])  # one family doesn't share peers, the other does
    if T.PEER_FAILS.get(t.url, 0) < T.PEER_FAIL_LIMIT and T._split(t.url):
        bad.append("split_swarm")  # separate servers not sharing swarms: works for clients on the same one
    if bad:
        # only partial faults (a dead address, one family not sharing, a split swarm): still useful, so "broken", not "bad"
        return ("up_broken" if all(x.startswith(("dead_ipv", "nopeers_ipv", "split_swarm")) for x in bad) else "up_bad"), bad
    sc = round(float(t.uptime or 0) + T._nt_iv_penalty(t.url))  # the interval penalty is only for ranking, not the status
    if sc < 50:
        return "up_junk", bad
    if T._nt_region_avg(t.url) is None and T._nt_is_new(t, sc, 0):  # just added: no real latency yet
        return "up_new", bad
    ms = T._nt_region_avg(t.url) or t.latency or 0
    if ms >= 300 and _reliable(t, ms):  # slow only if otherwise reliable; if not, it falls through to Up/Unreliable
        return "up_slow", bad
    if sc < 90:
        if T._nt_is_new(t, sc, ms):
            return "up_new", bad
        # under 90 only because of the latency penalty (reliability score is 90+): latency is the reason, not missed checks
        if (T._nt_region_avg(t.url) or 0) >= 200 and round(float(t.uptime or 0) + T._nt_iv_penalty(t.url) + T._nt_lat_penalty(T._nt_region_avg(t.url))) >= 90:  # 200 ms = where latency turns orange
            return "up_slow", bad
        return "up_unreliable", bad
    return "up_good", bad


def _addr_report(t, now=None):
    """Each published address of a tracker and whether it answers, from the checks: [{ip, fam, ok, since}], or [].
    'not answering' = failed its last 3+ tries and nothing in the last hour."""
    import time
    from urllib.parse import urlparse
    from newtrackon import scraper
    p = urlparse(t.url)
    port = p.port or (443 if p.scheme == "https" else 80)
    h = scraper.ADDR_HEALTH.get(scraper._addr_key(p.hostname, port)) or {}
    now = now or time.time()
    out = []
    for ip in sorted(t.ips or [], key=lambda x: (":" in x, x)):
        e = h.get(ip)
        if not e:
            continue
        bad = int(e.get("fails", 0)) >= 3 and now - int(e.get("ok") or 0) > 3600
        out.append({"ip": ip, "fam": "IPv6" if ":" in ip else "IPv4", "ok": not bad,
                    "since": (_ago(e["ok"]) + " ago") if e.get("ok") else "never seen answering"})
    return out


def _fam_answers(url):
    """{"v4": (answered, of), ...} over the last checks, for each published family that missed some; {} if none did."""
    out = {}
    for k, h in (T.FAM_HIST.get(url) or {}).items():
        if h and sum(h) < len(h):
            out[k] = (sum(h), len(h))
    return out


def _fams(t):
    f, df, out = T.FAMS.get(t.url) or {}, T.FAM_FAILS.get(t.url) or {}, {}
    for k in ("v4", "v6"):
        if k not in f:
            out[k] = "not_published"
        elif df.get("fam") == k and df.get("n", 0) >= T.PEER_FAIL_LIMIT:
            out[k] = "dead"
        else:
            out[k] = "ok"
    return out


def _detail(t):
    st, bad = _state(t)
    ph = T.PEER_HIST.get(t.url) or []
    up = t.status == 1
    return {
        "url": t.url, "host": t.host, "status": st, "problems": bad,
        "score": round(float(t.uptime or 0), 1),
        "latency_ms": t.latency if up else None,
        "latency_by_region_ms": T.REGION_LAT.get(t.url) or {},
        "regions": sorted(regions_of(t.country_codes)),
        "country_codes": list(dict.fromkeys(c.lower() for c in (t.country_codes or []) if c)),
        "families": _fams(t),
        "peer_test": {"latest": {True: "pass", False: "fail", None: "n/a"}[T.PEER_OK.get(t.url)] if up else "n/a",
                      "passed": sum(ph), "of": len(ph),
                      # per family, once tested on that family: {"v4": {"passed", "of"}, ...}
                      "by_family": {k: {"passed": sum(v), "of": len(v)} for k, v in sorted((T.PEER_HIST_FAM.get(t.url) or {}).items()) if k != "?" and v}},
        "fake_peers": {"latest": T.FAKE_N.get(t.url), "streak": T.FAKE_FAILS.get(t.url, 0)},
        "stale_peers": bool(T.STALE.get(t.url)),
        "spoof_proof": True if t.url.startswith("http") else T.CID_OK.get(t.url),
        "announce_interval_s": T.ANN_IV.get(t.url),
        "warning_message": (T.WARNINGS.get(t.url) or {}).get("msg"),
        "check_interval_s": t.interval,
        "stats": getattr(t, "stats", {}),
        "down_reason": (T._nt_down_label(T.DOWN_WHY.get(t.url)) if t.status != 1 else None),
        "down_cause": (T.DOWN_WHY.get(t.url) if t.status != 1 else None),
        "same_operator_as": getattr(t, "operator_peers", []),
        "ips": list(t.ips or []), "countries": list(t.countries or []), "networks": list(t.networks or []),
        "added": t.added, "last_checked": t.last_checked,
    }


_BASE = "https://newtrackon.co.nz"
# When the feed entries' content last changed (2026-10-05: tracker links added). Entries older than this get it as
# <updated>, so feed readers refresh their copies once; <published> keeps the event's own time for display.
_FEED_REV = 1791117960


def _wants_html():
    """True when the client asks for HTML before any XML (a browser following a link). Feed readers ask for XML or */*."""
    acc = (request.headers.get("Accept") or "").lower()
    if "text/html" not in acc:
        return False
    xml = [acc.find(x) for x in ("application/atom+xml", "application/rss+xml", "application/xml", "text/xml") if x in acc]
    return not xml or acc.find("text/html") < min(xml)


def _feed():
    import hashlib
    from datetime import datetime, timezone
    from urllib.parse import quote
    from xml.sax.saxutils import escape
    evs = list(T.EVENTS)
    tr = (request.args.get("tracker") or "").lower()
    types = {x.strip() for x in (request.args.get("type") or "").split(",") if x.strip()}
    if tr:
        evs = [e for e in evs if tr in e["url"].lower()]
    if types:
        evs = [e for e in evs if e["type"] in types]
    evs = evs[-100:][::-1]
    if _wants_html():  # a browser opening the link: a readable page (raw XML can't be clicked); feed readers get Atom
        from flask import make_response, render_template
        r = make_response(render_template("feed.jinja", evs=[dict(e, ago=_ago(e["t"]), date=_date(e["t"])) for e in evs],
                                          feed_url=_BASE + request.full_path.rstrip("?"), tracker=tr, title="Tracker events feed"))
        r.headers["Vary"] = "Accept"
        return r
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now = int(datetime.now(timezone.utc).timestamp())
    out = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
           '<title>newTrackon NZ: tracker events</title>',
           '<subtitle>Trackers going down, coming back, turning Up/Bad or recovering</subtitle>',
           f'<link href="{_BASE}/"/>', f'<link rel="self" href="{escape(_BASE + request.full_path.rstrip("?"))}"/>',
           f'<id>{_BASE}/feed.xml</id>', f'<updated>{iso(max(evs[0]["t"], _FEED_REV) if evs else now)}</updated>',
           '<author><name>newtrackon.co.nz</name></author>']
    for e in evs:
        eid = hashlib.md5(f'{e["t"]}|{e["url"]}|{e["type"]}|{e["text"]}'.encode()).hexdigest()
        host = e.get("host") or e["url"]
        page = _BASE + "/tracker/" + quote(host, safe=".-")  # removed trackers keep a page too
        html = '<a href="%s">%s</a>: %s' % (escape(page, {'"': "&quot;"}), escape(e["url"]), escape(e["text"]))
        out.append(f'<entry><title>{escape(host + " " + e["text"])}</title>'
                   f'<link href="{escape(page)}"/><id>tag:newtrackon.co.nz,2026:{eid}</id>'
                   f'<published>{iso(e["t"])}</published><updated>{iso(max(e["t"], _FEED_REV))}</updated>'
                   f'<category term="{escape(e["type"])}"/><content type="html">{escape(html)}</content></entry>')
    out.append('</feed>')
    return Response("\n".join(out), mimetype="application/atom+xml", headers={"Access-Control-Allow-Origin": "*", "Vary": "Accept"})


_QUALITY_ARGS = ("good", "protocol", "ipv4_works", "ipv6_works", "passes_peer_test")


def quality_filter(args):
    """The main table's 'Show only' buttons as API options. Returns None (no filter) or a url -> bool predicate.
    Raises ValueError with a user-facing message on a bad value."""
    def flag(name):
        v = (args.get(name) or "").strip().lower()
        if v in ("", "0", "false", "no"):
            return False
        if v in ("1", "true", "yes"):
            return True
        raise ValueError(f"{name} must be true or false")
    good, v4, v6, peer = flag("good"), flag("ipv4_works"), flag("ipv6_works"), flag("passes_peer_test")
    proto = (args.get("protocol") or "").strip().lower()
    if proto not in ("", "udp", "http"):
        raise ValueError("protocol must be udp or http (http includes https)")
    if not (good or v4 or v6 or peer or proto):
        return None
    ok = set()
    for t in _trackers():
        if good and _state(t)[0] not in ("up_good", "up_new"):
            continue
        if proto and ("udp" if t.url.startswith("udp:") else "http") != proto:
            continue
        fams = _fams(t)
        if (v4 and fams["v4"] != "ok") or (v6 and fams["v6"] != "ok"):
            continue
        if peer and T.PEER_OK.get(t.url) is not True:
            continue
        ok.add(t.url)
    return ok.__contains__


def _filter_tags(t):
    """Space-separated tags for the main table's filter bar (data attributes on each row)."""
    lat = T.REGION_LAT.get(t.url) or {}
    fast = [k for k in FAST_FROM if all(isinstance(lat.get(p), (int, float)) and lat[p] < DEFAULT_FAST_FROM_MS[k] for p in MEASURED_FROM[k])]
    fams = [k for k, v in _fams(t).items() if v == "ok"]
    peer = {True: "pass", False: "fail"}.get(T.PEER_OK.get(t.url), "na")
    proto = "udp" if t.url.startswith("udp:") else "http"
    return {"proto": proto, "fam": " ".join(fams), "peer": peer,
            "region": " ".join(sorted(regions_of(t.country_codes))), "fast": " ".join(fast)}


_STATUS_TEXT = {"up_good": "Up/Good", "up_new": "Up/New", "up_slow": "Up/Slow", "up_unreliable": "Up/Unreliable",
                "up_junk": "Up/Junk", "up_bad": "Up/Bad", "up_broken": "Up/Broken", "down": "Down"}
_REGION_COLOR = {"Oceania": "#00e5ff", "Asia": "#ffb300", "Europe": "#ce93d8", "North America": "#66bb6a"}
_SLOTS_PER_DAY = 48  # historic holds one up/down value per 30 minutes


def _uptime_days(historic, days=30):
    """Share of up slots per 24 h, oldest first, counted back from now; only whole days that have data."""
    h = [int(x) for x in (historic or [])]
    out = []
    for k in range(min(days, len(h) // _SLOTS_PER_DAY)):
        chunk = h[len(h) - _SLOTS_PER_DAY * (k + 1):len(h) - _SLOTS_PER_DAY * k]
        out.append({"ago": k, "pct": round(100 * sum(1 for x in chunk if x > 0) / len(chunk))})
    return out[::-1]


_REGION_ORDER = ("Oceania", "Asia", "Europe", "North America")


def _daily_rows(url):
    """The permanent daily summary as dicts, oldest first."""
    return [{"day": d, "uptime": pct, "slots": n, "latency_ms": lat} for d, pct, n, lat in (T.DAILY.get(url) or [])]


def _long_term(url, min_days=30):
    """Long-term uptime for the tracker page: a strip of the last 365 days and a table by month, newest first.
    Only once the summary covers more than min_days (the 30-day charts already show less)."""
    from datetime import date
    rows = T.DAILY.get(url) or []
    if len(rows) <= min_days:
        return None
    months: dict = {}
    for d, pct, n, lat in rows:
        m = months.setdefault(d[:7], {"up": 0.0, "n": 0, "days": 0, "lat": {}})
        m["up"] += pct * n / 100.0
        m["n"] += n
        m["days"] += 1
        for reg, ms in lat.items():
            m["lat"].setdefault(reg, []).append(ms)
    out = []
    for k in sorted(months, reverse=True):
        m = months[k]
        regs = sorted(m["lat"], key=lambda r: (_REGION_ORDER.index(r) if r in _REGION_ORDER else 9, r))
        out.append({"month": date(int(k[:4]), int(k[5:]), 1).strftime("%b %Y"), "days": m["days"],
                    "pct": round(100.0 * m["up"] / m["n"], 1) if m["n"] else 0,
                    "lat": [(r, sorted(m["lat"][r])[len(m["lat"][r]) // 2]) for r in regs]})
    up = sum(pct * n / 100.0 for _, pct, n, _ in rows)
    slots = sum(n for _, _, n, _ in rows)
    return {"since": rows[0][0], "days": len(rows), "pct": round(100.0 * up / slots, 1) if slots else 0,
            "strip": [{"day": d, "pct": pct} for d, pct, _, _ in rows[-365:]], "months": out}


def _latency_chart(hist, now, w=900, h=220, pad=60):  # pad fits 3-digit labels at the phone font size
    """Per-region latency lines as SVG coordinates. A gap of 3+ missing samples breaks the line."""
    series = {reg: ss for reg, ss in (hist or {}).items() if ss}
    if not series:
        return None
    t0 = min(ss[0][0] for ss in series.values())
    t0 = min(t0, now - 2 * 86400)
    top = max(max(ms for _, ms in ss) for ss in series.values())
    top = max(50, int(top * 1.15 / 50 + 1) * 50)
    x = lambda ts: pad + (w - pad - 8) * (ts - t0) / max(1, now - t0)
    y = lambda ms: 8 + (h - 8 - 22) * (1 - ms / top)
    lines = []
    for reg in sorted(series, key=lambda r: (r != "Oceania", r)):
        parts, cur, prev = [], [], None
        for ts, ms in series[reg]:
            if prev is not None and ts - prev > 3 * T.LAT_HIST_STEP and cur:
                parts.append(cur)
                cur = []
            cur.append("%.1f,%.1f" % (x(ts), y(ms)))
            prev = ts
        parts.append(cur)
        lines.append({"region": reg, "color": _REGION_COLOR.get(reg, "#bbb"), "parts": [" ".join(c) for c in parts if len(c) > 1],
                      "dots": [c for c in parts if len(c) == 1], "last": series[reg][-1][1]})
    grid = [{"ms": v, "y": round(y(v), 1)} for v in range(0, top + 1, max(50, top // 4 // 50 * 50))]
    span_days = (now - t0) / 86400
    ticks = [{"x": round(x(now - d * 86400), 1), "label": "now" if d == 0 else "%dd ago" % d}
             for d in range(0, int(span_days) + 1, max(1, int(span_days) // 6 or 1))]
    for tk in ticks:  # labels at the edges are anchored inwards so they aren't cut off
        tk["anchor"] = "end" if tk["x"] > w - 60 else ("start" if tk["x"] < pad + 40 else "middle")
    return {"w": w, "h": h, "pad": pad, "lines": lines, "grid": grid, "ticks": ticks}


FIX_TITLES = {"no-peers": "Hands out no peers", "fake-peers": "Returns fake peers", "dead-address": "IPv4 or IPv6 broken",
              "unreliable": "Drops out (Up/Unreliable, Up/Junk)", "slow": "Slow (Up/Slow)", "down-timeout": "Down: timeout",
              "down-refused": "Down: connection refused", "down-dns": "Down: DNS", "down-tls": "Down: TLS / certificate",
              "down-http": "Down: HTTP error", "down-rejected": "Down: the tracker rejects requests", "down": "Down: no usable answer",
              "interval": "Announce interval too short or too long", "removed": "Removed and banned",
              "split-swarm": "Separate servers that don't share swarms (Up/Broken)"}


def _broken_why(t):
    """Tooltip for an Up/Broken status that isn't a dead address (the template words that one itself)."""
    if T._split(t.url):
        return ("Runs separate servers that don't share swarms: clients only meet peers that reach the same server. "
                "Works for some, so it stays listed. Score capped at 50.")
    f = T._peer_fam_bad(t.url)
    if f:
        o = "6" if f == "v4" else "4"
        return ("Its IPv%s side doesn't share peers (3+ of its last 6 IPv%s peer tests failed), so clients on IPv%s get no "
                "usable peers. Still works over IPv%s. Score capped at 50." % (f[-1], f[-1], f[-1], o))
    return "Works for some clients only. Score capped at 50."


def _fix_anchor(t):
    """The /fix section for this tracker's current problem, or None if it's healthy."""
    try:
        st, bad = _state(t)
    except Exception:
        return None
    if st == "down":
        return T.FIX_DOWN.get(T._nt_down_label(T.DOWN_WHY.get(t.url)), "down")
    if st == "up_bad":
        return "fake-peers" if "fake_peers" in bad else "no-peers"
    if st == "up_broken":
        return "split-swarm" if bad == ["split_swarm"] else "dead-address"
    if st in ("up_unreliable", "up_junk"):
        return "unreliable"
    if st == "up_slow":
        return "slow"
    return None


def _warning(url):
    """The tracker's own warning, with what it means in plain words: (message, meaning, fixable) or None."""
    w = (T.WARNINGS.get(url) or {}).get("msg")
    if not w:
        return None
    low = w.lower()
    if any(k in low for k in ("passkey", "authkey", "auth key", "login", "registered user", "private")):
        return (w, "It's a private tracker: it only works for its own members, so it can't work as a public tracker. Nothing to fix here.", False)
    if any(k in low for k in ("not authorized", "not authorised", "not registered", "unregistered", "whitelist", "not allowed", "unknown torrent", "not found")):
        return (w, "It only serves torrents on its own list (a whitelist), so it can't work as a public tracker unless it accepts any torrent.", False)
    if any(k in low for k in ("rate", "too many", "slow down", "limit")):
        return (w, "It's limiting how often clients may announce: some clients get turned away.", True)
    return (w, None, True)


def _interval_note(iv):
    """A plain note when the announce interval is unusual (informational: no effect on status or score)."""
    if not iv:
        return None
    if iv < 900:
        return ("too short: every client checks in every %s, which loads the tracker for no benefit. Around 30 minutes "
                "(1800 s, with a min interval of about 15 minutes) is usual" % ("%d s" % iv if iv < 60 else "%d min" % round(iv / 60)))
    if iv > 10800:
        return ("too long: clients check in only every %s, so they rarely get new peers from it. Around 30 minutes to "
                "an hour is usual" % ("%d h" % round(iv / 3600)))
    return None


def _evidence(t, d):
    """What the checks saw, in plain words, for the tracker page's problem box."""
    out = []
    fix = _fix_anchor(t)
    if fix is None:
        return out
    w = _warning(t.url)
    if w and fix in ("no-peers", "fake-peers", "unreliable"):
        out.append("The tracker itself says: \u201c%s\u201d. %s" % (w[0], w[1] or "That's its own message to clients, sent with each answer."))
    ud = T._nt_useless_days(t.url) if (fix or "").startswith("down") or fix in ("no-peers", "fake-peers") else None
    if ud is not None and ud >= 1 and ud > ((T._nt_upbad_days(t.url) or 0) + 0.5):
        out.append("Not working (down or Up/Bad) for %d days in all: a tracker that doesn't work for %d days, either way, is "
                   "removed and banned." % (int(ud), T.REMOVE_DAYS))
    if fix in ("no-peers", "fake-peers") and not T._peer_rule_applies(t.url):
        out.append("This site's peer test is being reviewed for trackers like this one, so for now failing it doesn't remove it.")
    ub = T._nt_upbad_days(t.url) if fix in ("no-peers", "fake-peers") and T._peer_rule_applies(t.url) else None
    if ub is not None:
        left = T.UPBAD_DAYS - ub
        out.append("Up/Bad for %s (passing again for under 12 hours doesn't reset this). Trackers that stay Up/Bad for %d days are removed and banned: %s."
                   % ("under a day" if ub < 1 else "%d day%s" % (int(ub), "" if int(ub) == 1 else "s"), T.UPBAD_DAYS,
                      "it's due now" if left <= 0 else "about %s left to fix it" % ("%d h" % max(1, round(left * 24)) if left < 1 else "%d day%s" % (int(left), "" if int(left) == 1 else "s"))))
    if fix == "no-peers" and T.PEER_LAST.get(t.url) and __import__("time").time() - T.PEER_LAST[t.url] >= T.PEER_NA_DAYS * 86400:
        out.append("No conclusive peer test for %d+ days: only our first test client is ever answered, so it can't show that "
                   "it shares peers. Each check now counts as a failed test." % T.PEER_NA_DAYS)
    if fix == "no-peers":
        out.append("Peer test passed %d of the last %d times: a second test client wasn't told about the first." % (d["peer_test"]["passed"], d["peer_test"]["of"]))
    if fix == "fake-peers":
        out.append("Returned %s peer(s) for a random torrent only this site knows, in %d of its last 6 checks."
                   % (d["fake_peers"]["latest"] if d["fake_peers"]["latest"] is not None else "unknown", d["fake_peers"]["streak"]))
    for fam, x in sorted((T.NAT_SEEN.get(t.url) or {}).items()):
        if fix in ("no-peers", "dead-address"):
            from newtrackon import scraper
            if scraper.ip_is_public(x["ip"]):
                out.append("Over IPv%s it sees every client as %s, an address that isn't theirs, and hands that out instead of "
                           "their real one, so nobody can connect to those peers. A proxy or CDN in front of it (such as Cloudflare) "
                           "hides clients' addresses, and the tracker doesn't read the real one from the header it passes on."
                           % (fam[-1], x["ip"]))
            else:
                out.append("Over IPv%s it sees every client as %s, a private address, and hands that out instead of their real "
                           "address, so nobody can connect to those peers. Something in front of it hides clients' addresses: "
                           "usually Docker's port proxy (no real IPv6 in the container), NAT, or a reverse proxy." % (fam[-1], x["ip"]))
    rare = T._split_info(t.url) if fix == "no-peers" and not T._split(t.url) else None
    if rare:
        out.append("It runs separate servers that don't share their swarms, and clients rarely reach the same one: in %d of its "
                   "last %d peer tests our second test client was handed back only itself, and only %d of %d tests passed. "
                   "The fix is under 'Separate servers that don't share swarms' on How to fix." % (rare["split"], rare["of"], rare["passed"], rare["tests"]))
    sp = T._split(t.url) if fix in ("split-swarm", "dead-address") else None
    if sp:
        out.append("It runs separate servers that don't share their swarms: in %d of its last %d peer tests our second test "
                   "client was handed back only itself, by a server that had never seen our first client, while %d of %d tests "
                   "passed. Clients only meet peers that happen to reach the same server." % (sp["split"], sp["of"], sp["passed"], sp["tests"]))
    pfb = T._peer_fam_bad(t.url) if fix == "dead-address" else None
    if pfb:
        fams = T._peer_fams(t.url)
        other = "v6" if pfb == "v4" else "v4"
        out.append("Over IPv%s it doesn't share peers: passed %d of its last %d IPv%s peer tests, while IPv%s passed %d of %d. "
                   "Clients on IPv%s get no peers from it." % (pfb[-1], fams[pfb].count(1), len(fams[pfb]), pfb[-1], other[-1],
                                                              (fams.get(other) or []).count(1), len(fams.get(other) or []), pfb[-1]))
    if fix == "dead-address" and (T.FAM_FAILS.get(t.url) or {}).get("n", 0) >= T.PEER_FAIL_LIMIT:
        df = T.FAM_FAILS.get(t.url) or {}
        fam = str(df.get("fam", "?"))[-1]
        ips = [ip for ip in (t.ips or []) if (":" in ip) == (fam == "6")]
        out.append("Its IPv%s address%s %s didn't answer in %d checks in a row, while IPv%s did."
                   % (fam, "es" if len(ips) > 1 else "", ", ".join(ips) or "(published in DNS)", df.get("n", 0), "4" if fam == "6" else "6"))
        a = _fam_answers(t.url).get("v" + fam)
        if a:
            out.append("IPv%s answered in %d of its last %d checks%s." % (fam, a[0], a[1], ": mostly dead, not just a blip" if a[0] * 4 <= a[1] else ""))
        if df.get("ok"):
            out.append("It answered again in the last check; one more good check in a row and it counts as working.")
    if fix == "down-rejected":
        out.append("It answers, but with an error message of its own instead of a tracker reply, so clients can't use it.")
    if fix.startswith("down"):
        out.append("Last error: %s" % (T.DOWN_WHY.get(t.url) or "no answer"))
        out.append("Last successful check: %s." % (_ago(t.last_uptime) + " ago" if t.last_uptime else "none recorded"))
    jd = T._nt_junk_days(t.url) if fix in ("unreliable", "dead-address") else None
    if jd is not None:
        left = T.JUNK_DAYS - jd
        out.append("Up/Junk or Up/Broken for %s. Trackers that stay Up/Junk (score under 50) or Up/Broken (IPv4 or IPv6 broken) for %d days are removed and banned: %s."
                   % ("under a day" if jd < 1 else "%d day%s" % (int(jd), "" if int(jd) == 1 else "s"), T.JUNK_DAYS,
                      "it's due now" if left <= 0 else "about %d day%s left to fix it" % (max(1, int(left)), "" if int(left) == 1 else "s")))
    if fix == "unreliable":
        h = [int(x) for x in (t.historic or [])][-336:]
        if h:
            out.append("Up in %d%% of checks over the last %s days; score %d." % (round(100 * sum(1 for x in h if x > 0) / len(h)), round(len(h) / 48, 1), round(float(t.uptime or 0))))
    if fix == "slow":
        lat = T.REGION_LAT.get(t.url) or {}
        if lat:
            out.append("Latency by region: " + ", ".join("%s %d ms" % (k, v) for k, v in lat.items()) + ".")
    return out


# "Check again now" on the tracker page: once per tracker per hour, and RECHECK_PER_HOUR in all
RECHECK_PER_HOUR = 20
_recheck_host: dict = {}
_recheck_all: list = []


def _recheck(host):
    from flask import abort, redirect
    import time
    host = host.lower()
    t = next((x for x in db.get_all_data() if (x.host or "").lower() == host), None)
    removed = T.REMOVED.get(host) if t is None else None
    if t is None and removed is None:
        abort(404)
    if removed is not None:
        ban = _ban(host)
        if ban and ban["active"] and ban["until"] is None:
            abort(404)  # permanently banned: no way back
    now = time.time()
    _recheck_all[:] = [x for x in _recheck_all if now - x < 3600]
    last = _recheck_host.get(host, 0)
    if now - last < 3600:
        return redirect("/tracker/%s?recheck=wait&m=%d" % (host, (3600 - (now - last)) // 60 + 1), 303)
    if len(_recheck_all) >= RECHECK_PER_HOUR:
        return redirect("/tracker/%s?recheck=busy" % host, 303)
    _recheck_host[host] = now
    _recheck_all.append(now)
    if removed is not None:  # checked like a new submission, past its ban: listed again if it works
        from threading import Thread

        from newtrackon import ingest
        Thread(target=ingest.reinstate, args=(host, removed["url"]), daemon=True).start()
        return redirect("/tracker/%s?recheck=queued" % host, 303)
    T.FORCE_CHECK.add(t.url)
    return redirect("/tracker/%s?recheck=queued" % host, 303)


def _fix_page():
    from flask import render_template
    return render_template("static/fix.jinja", active="Fix", titles=FIX_TITLES)


def _ago(epoch):
    return T._dur(epoch).replace("\u2007", "").strip()


def _ago_txt(epoch):
    """'just now' under a minute, else '<n> ago'."""
    import time
    return "just now" if time.time() - int(epoch or 0) < 60 else _ago(epoch) + " ago"


_MEASURED_FROM = ("Oceania", "Asia", "Europe", "North America")


def _monitor_health():
    """For the status and About pages: is this monitor working right now? Region names only, never exits or cities."""
    import time
    from newtrackon import persistence
    now = time.time()
    times = [int(r.get("time", 0) or 0) for r in list(persistence.raw_data) if isinstance(r, dict)]
    last = max(times) if times else 0
    latest: dict = {}
    for per in list(T.LAT_HIST.values()):
        for reg, ss in (per or {}).items():
            if ss:
                latest[reg] = max(latest.get(reg, 0), ss[-1][0])
    online = bool(T._ONLINE[1]) or now - T._ONLINE[0] > 3600  # the offline flag is only trusted while fresh
    return {
        "online": online,
        "checks_hour": sum(1 for x in times if x >= now - 3600),
        "last_ago": _ago_txt(last) if last else None,
        "regions": [r for r in _MEASURED_FROM if latest.get(r, 0) >= now - 6 * 3600],
    }


_RANK_DAYS = 14  # historic keeps 30 days, but trackers need 7 to rank; 14 days is the longest window every ranked tracker has
_RANK_MIN_DAYS = 7  # newer trackers aren't ranked: a short perfect record would beat a long good one


def _rank_row(t, **extra):
    st = _state(t)[0]
    return dict(host=t.host, url=t.url, status=st, status_text=_STATUS_TEXT.get(st, st), score=round(float(t.uptime or 0)),
                group=len(getattr(t, "operator_peers", None) or []) + 1, **extra)


def _rankings():
    import time
    now = int(time.time())
    ts = _trackers()
    reliable, streaks, fastest = [], [], {r: [] for r in _MEASURED_FROM}
    for t in ts:
        h = [int(x) for x in (t.historic or [])]
        st = _state(t)[0]
        if len(h) >= _RANK_MIN_DAYS * _SLOTS_PER_DAY:
            w = h[-_RANK_DAYS * _SLOTS_PER_DAY:]
            reliable.append(_rank_row(t, avail=round(100.0 * sum(w) / len(w), 2), outages=len(T._runs(w, 0)),
                                      days=round(len(w) / _SLOTS_PER_DAY, 1), lat=t.latency))
        if t.status == 1 and st in ("up_good", "up_new", "up_slow"):  # answering isn't enough: Up/Bad etc. aren't ranked
            since = int(t.last_downtime or t.added or now)
            streaks.append(_rank_row(t, since=since, for_=_ago(since)))
        if t.status == 1 and st in ("up_good", "up_new", "up_slow"):
            for reg, ms in (T.REGION_LAT.get(t.url) or {}).items():
                if reg in fastest and isinstance(ms, (int, float)):
                    fastest[reg].append(_rank_row(t, ms=int(ms)))
    reliable.sort(key=lambda r: (-r["avail"], r["outages"], -r["score"], r["lat"] or 9999))
    streaks.sort(key=lambda r: r["since"])
    for reg in fastest:
        fastest[reg].sort(key=lambda r: (r["ms"], -r["score"]))
        fastest[reg] = fastest[reg][:10]
    return {"reliable": reliable[:20], "streaks": streaks[:20], "fastest": fastest,
            "days": _RANK_DAYS, "min_days": _RANK_MIN_DAYS}


def _rankings_page():
    from flask import render_template
    return render_template("rankings.jinja", r=_rankings(), active="Rankings", title="Tracker rankings",
                           description="The most reliable public BitTorrent trackers over the last two weeks, the longest unbroken uptime, "
                                       "and the fastest from Oceania, Asia, Europe and North America. Checked from New Zealand.")


_BADGE_COLOR = {"up_good": "#2e9e4f", "up_slow": "#2e9e4f", "up_new": "#1f88c9", "down": "#6c757d"}


def _badge_text_width(text):
    """Rough rendered width in px of 11px Verdana: enough to size the badge without a font library."""
    narrow, wide = set("ilj.,:;|!'()[] ·"), set("mwMW@%")
    return int(sum(4 if c in narrow else 10 if c in wide else 7 for c in text)) + 10


def _badge_svg(left, right, color):
    from xml.sax.saxutils import escape
    lw, rw = _badge_text_width(left), _badge_text_width(right)
    w = lw + rw
    L, R = escape(left), escape(right)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{L}: {R}">'
        f"<title>{L}: {R}</title>"
        '<linearGradient id="g" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="c"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#c)"><rect width="{lw}" height="20" fill="#131a60"/><rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
        f'<rect width="{w}" height="20" fill="url(#g)"/></g>'
        '<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{lw / 2:.1f}" y="15" fill="#010101" fill-opacity=".3">{L}</text><text x="{lw / 2:.1f}" y="14">{L}</text>'
        f'<text x="{lw + rw / 2:.1f}" y="15" fill="#010101" fill-opacity=".3">{R}</text><text x="{lw + rw / 2:.1f}" y="14">{R}</text>'
        "</g></svg>"
    )


def _badge(host):
    host = host.lower()
    if host.endswith(".svg"):
        host = host[:-4]
    t = next((x for x in _trackers() if (x.host or "").lower() == host), None)
    if t is None:
        svg, code = _badge_svg("newTrackon NZ", "not listed", "#6c757d"), 404
    else:
        st = _state(t)[0]
        down, down_for = _is_it_down(t)
        right = ("Down \u00b7 " + down_for) if down else "%s \u00b7 %d" % (_STATUS_TEXT.get(st, st), round(float(t.uptime or 0)))
        svg, code = _badge_svg("newTrackon NZ", right, _BADGE_COLOR.get(st, "#e07b00")), 200
    # short cache: badges are embedded elsewhere and should follow the status within minutes
    return Response(svg, code, mimetype="image/svg+xml",
                    headers={"Cache-Control": "max-age=300", "Access-Control-Allow-Origin": "*", "X-Content-Type-Options": "nosniff"})


def _clients_page():
    from flask import render_template
    return render_template("clients.jinja", active="Clients", title="Use the trackers in your torrent client",
                           description="How to add newtrackon.co.nz's list of working BitTorrent trackers to qBittorrent, Transmission "
                                       "and Deluge, so it stays up to date by itself.")


def _tools_page():
    from flask import render_template
    return render_template("tools.jinja", active="Tools", title="Magnet booster and torrent fixer",
                           description="Add working, fast BitTorrent trackers to a magnet link or a .torrent file, and drop dead ones. "
                                       "Runs in your browser: your magnet or torrent never leaves your device.")


def _is_it_down(t):
    """The honest one-line answer for the tracker page and its search snippet."""
    import time
    now = int(time.time())
    if t.status == 1:
        since = int(t.last_downtime or t.added or now)
        return False, _ago(since)
    return True, _ago(int(t.last_uptime or t.added or now))


def _tracker_page(host):
    from flask import abort, render_template
    import time
    host = host.lower()
    t = next((x for x in _trackers() if (x.host or "").lower() == host), None)
    if t is None:
        r = T.REMOVED.get(host)
        if r is None:
            abort(404)
        return _removed_page(host, r)
    d = _detail(t)
    now = int(time.time())
    h = [int(x) for x in (t.historic or [])]
    from datetime import datetime, timezone
    from urllib.parse import quote
    down, down_for = _is_it_down(t)
    st_txt = _STATUS_TEXT.get(d["status"], d["status"])
    if down:
        desc = "%s is down: no answer for %s. Live uptime and latency history, checked from New Zealand around the clock." % (t.host, down_for)
    else:
        desc = "%s is up (%s, score %d%s) and has been for %s. Live uptime and latency history, checked from New Zealand around the clock." % (
            t.host, st_txt, round(float(t.uptime or 0)), (", %d ms" % d["latency_ms"]) if d.get("latency_ms") is not None else "", down_for)
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": "Is %s down? Live tracker status" % t.host,
          "description": desc, "url": "https://newtrackon.co.nz/tracker/" + quote(t.host, safe=".-"),
          "dateModified": datetime.fromtimestamp(int(t.last_checked or now), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return render_template(
        "tracker.jinja", t=t, d=d, fam_answers=_fam_answers(t.url), status_text=_STATUS_TEXT.get(d["status"], d["status"]), row=_rowcls(t),
        days=_uptime_days(t.historic), recent=h[-96:], history_days=round(len(h) / _SLOTS_PER_DAY, 1),
        chart=_latency_chart(T.LAT_HIST.get(t.url), now), long=_long_term(t.url),
        events=[dict(e, ago=_ago(e["t"])) for e in reversed(T.EVENTS) if e.get("url") == t.url or e.get("host") == t.host][:30],
        added_ago=_ago(t.added or now), checked_ago=_ago(t.last_checked or now), checked_txt=_ago_txt(t.last_checked or now),
        now=now, title="Is %s down? Live tracker status" % t.host,
        description=desc, ld=ld, down=down, down_for=down_for,
        fix=_fix_anchor(t), fix_title=FIX_TITLES.get(_fix_anchor(t) or ""), evidence=_evidence(t, d),
        warn=_warning(t.url), iv_note=_interval_note(d.get("announce_interval_s")), addrs=_addr_report(t),
        recheck=request.args.get("recheck"), recheck_m=request.args.get("m", type=int),
        recheck_last=_recheck_host.get(host),
    )


_BAN_DAYS = 30  # auto-bans from eviction expire after this (ingest.py)


def _ban(host, now=None):
    """The ban on a host this site removed: {"since", "until" (None = permanent), "active"}, or None.
    Hosts without a removal record are never shown: manual denylist entries stay private."""
    import time
    host = (host or "").lower()
    if host not in T.REMOVED:
        return None
    now = now or time.time()
    out = None
    for h, since, days in T._nt_ban_entries():
        if h != host:
            continue
        if since is None:
            b = {"since": None, "until": None, "active": True}
        else:
            b = {"since": since, "until": since + days * 86400, "active": now - since <= days * 86400}
        if out is None or b["active"] or (b["until"] or 0) > (out["until"] or 0):
            out = b
    return out


def _bans(now=None):
    """Active bans on trackers this site removed, soonest to expire first."""
    out = []
    for host, r in T.REMOVED.items():
        b = _ban(host, now)
        if b and b["active"]:
            out.append(dict(host=host, url=r.get("url"), removed=r.get("t"), reason=r.get("reason"), **b))
    return sorted(out, key=lambda x: (x["until"] is None, x["until"] or 0, x["host"]))


def _date(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%-d %b %Y")


def _removed_page(host, r):
    from flask import render_template
    import time
    now = int(time.time())
    ban = _ban(host, now)
    rows = T.DAILY.get(r.get("url")) or []
    days = [{"day": d, "pct": pct} for d, pct, _, _ in rows[-60:]]
    up = sum(pct * n / 100.0 for _, pct, n, _ in rows)
    slots = sum(n for _, _, n, _ in rows)
    when = _date(r["t"])
    desc = "%s is down: it was removed from the list on %s (%s). Its history, and whether it can be added again." % (host, when, r.get("reason") or "no reason recorded")
    return render_template(
        "tracker_removed.jinja", host=host, r=r, ban=ban, now=now, when=when, ago=_ago(r["t"]),
        recheck=request.args.get("recheck"), recheck_m=request.args.get("m", type=int),
        until=_date(ban["until"]) if ban and ban.get("until") else None,
        left=_ago(now - (ban["until"] - now)) if ban and ban.get("until") and ban["active"] else None,  # time left, via the "ago" formatter
        days=days, overall=round(100.0 * up / slots, 1) if slots else None, ndays=len(rows),
        since=rows[0][0] if rows else None,
        events=[dict(e, ago=_ago(e["t"])) for e in reversed(T.EVENTS) if (e.get("host") or "").lower() == host][:30],
        title="Is %s down? Removed from the list" % host, description=desc,
    )


def register(app):
    app.add_url_rule("/tracker/<host>", "nt_tracker", _tracker_page)
    app.add_url_rule("/tracker/<host>/recheck", "nt_recheck", _recheck, methods=["POST"])
    app.add_url_rule("/fix", "nt_fix", _fix_page)
    app.add_url_rule("/rankings", "nt_rankings", _rankings_page)
    app.add_url_rule("/tools", "nt_tools", _tools_page)
    app.add_url_rule("/clients", "nt_clients", _clients_page)
    app.add_url_rule("/badge/<host>", "nt_badge", _badge)
    app.jinja_env.globals["nt_health"] = _monitor_health

    @app.route("/api/removed")
    def api_removed():
        """Trackers this site removed whose 30-day ban is still running (their pages stay up, e.g. for the sitemap)."""
        r = jsonify([{"host": b["host"], "url": b["url"], "removed": b["removed"], "reason": b["reason"], "banned_until": b["until"]}
                     for b in _bans()])
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @app.route("/api/tracker/<host>")
    def api_tracker(host):
        host = host.lower()
        t = next((x for x in _trackers() if (x.host or "").lower() == host), None)
        if t is not None:
            r = jsonify(dict(_detail(t), daily=_daily_rows(t.url)))
        else:
            body = {"error": "not listed", "host": host}
            rm = T.REMOVED.get(host)
            if rm:
                ban = _ban(host)
                body["removed"] = {"url": rm.get("url"), "time": rm.get("t"), "reason": rm.get("reason"),
                                   "banned_until": (ban or {}).get("until") if ban and ban["active"] else None,
                                   "banned_permanently": bool(ban and ban["active"] and ban["until"] is None)}
                body["daily"] = _daily_rows(rm.get("url"))
            r = jsonify(body)
        if t is None:
            r.status_code = 404
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r
    app.jinja_env.globals["nt_fix"] = _fix_anchor
    app.jinja_env.globals["nt_fix_titles"] = FIX_TITLES
    app.jinja_env.globals["nt_tags"] = _filter_tags
    app.jinja_env.globals["nt_broken_why"] = _broken_why
    app.jinja_env.globals["nt_fam_nopeers"] = lambda t: T._peer_fam_bad(t.url) if T.PEER_FAILS.get(t.url, 0) < T.PEER_FAIL_LIMIT else None
    app.jinja_env.globals["nt_now"] = lambda: int(__import__("time").time())
    app.jinja_env.globals["nt_events"] = lambda n=10, days=None: list(reversed(
        [e for e in T.EVENTS if days is None or e.get("t", 0) >= __import__("time").time() - days * 86400][-n:]))
    app.jinja_env.globals["nt_rowcls"] = _rowcls
    app.jinja_env.globals["nt_dying"] = _dying
    app.jinja_env.globals["nt_bans"] = _bans
    app.jinja_env.globals["nt_host"] = lambda url: (__import__("urllib.parse").parse.urlparse(str(url or "")).hostname or "").lower()
    app.jinja_env.globals["nt_date"] = _date
    app.jinja_env.globals["nt_state"] = _statekey
    app.add_url_rule("/feed.xml", "nt_feed", _feed)
    app.add_url_rule("/feed", "nt_feed2", _feed)

    @app.route("/api/details")
    def api_details():
        try:
            rf = parse_region_filter(request.args)
        except ValueError as exc:
            return Response(str(exc), 400, mimetype="text/plain", headers={"Access-Control-Allow-Origin": "*"})
        r = jsonify([_detail(t) for t in _trackers() if not rf.active or rf.matches(t.url, t.country_codes, T.REGION_LAT)])
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @app.route("/api/clean")
    def api_clean():
        a = request.args
        try:
            min_score = float(a.get("min_score", 90))
            max_ms = int(a.get("max_latency", 0) or 0)
            min_age = float(a.get("min_age_days", 3))
        except ValueError:
            return Response("min_score / max_latency / min_age_days must be numbers", 400, mimetype="text/plain", headers={"Access-Control-Allow-Origin": "*"})
        try:
            rf = parse_region_filter(a)
        except ValueError as exc:
            return Response(str(exc), 400, mimetype="text/plain", headers={"Access-Control-Allow-Origin": "*"})
        need_v4 = a.get("require_ipv4", "false").lower() in ("1", "true")
        dedupe = a.get("dedupe", "true").lower() not in ("0", "false")
        pick = {}
        for t in _trackers():
            if _state(t)[0] not in ("up_good", "up_new", "up_slow", "up_unreliable") or round(float(t.uptime or 0)) < min_score or (__import__("time").time() - int(t.added or 0)) < min_age * 86400:
                continue
            fam = _fams(t)
            if "dead" in fam.values() or (need_v4 and fam["v4"] != "ok"):
                continue
            if max_ms and (t.latency is None or t.latency >= max_ms):
                continue
            if rf.active and not rf.matches(t.url, t.country_codes, T.REGION_LAT):
                continue
            key = getattr(t, "group_id", t.url) if dedupe else t.url
            cur = pick.get(key)
            if cur is None or (t.uptime, -(t.latency or 9999)) > (cur.uptime, -(cur.latency or 9999)):
                pick[key] = t
        urls = [t.url for t in sorted(pick.values(), key=lambda t: (-t.uptime, t.latency or 9999))]
        return Response("\n\n".join(urls) + ("\n" if urls else ""), mimetype="text/plain",
                        headers={"Access-Control-Allow-Origin": "*"})
