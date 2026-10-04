"""NZ mirror extras: /api/details (full JSON state per tracker) and /api/clean (Up/Good, filtered, one per operator)."""
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
        return "green" if round(float(t.uptime or 0)) >= 90 else "orange"
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
    if bad:
        # only fault is a dead IPv4/IPv6 address: still works on the other family, so "broken", not "bad"
        return ("up_broken" if all(x.startswith("dead_ipv") for x in bad) else "up_bad"), bad
    sc = round(float(t.uptime or 0))
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
        if (T._nt_region_avg(t.url) or 0) >= 200 and round(float(t.uptime or 0) + T._nt_lat_penalty(T._nt_region_avg(t.url))) >= 90:  # 200 ms = where latency turns orange
            return "up_slow", bad
        return "up_unreliable", bad
    return "up_good", bad


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
                      "passed": sum(ph), "of": len(ph)},
        "fake_peers": {"latest": T.FAKE_N.get(t.url), "streak": T.FAKE_FAILS.get(t.url, 0)},
        "stale_peers": bool(T.STALE.get(t.url)),
        "spoof_proof": True if t.url.startswith("http") else T.CID_OK.get(t.url),
        "announce_interval_s": T.ANN_IV.get(t.url),
        "check_interval_s": t.interval,
        "stats": getattr(t, "stats", {}),
        "down_reason": (T._nt_down_label(T.DOWN_WHY.get(t.url)) if t.status != 1 else None),
        "down_cause": (T.DOWN_WHY.get(t.url) if t.status != 1 else None),
        "same_operator_as": getattr(t, "operator_peers", []),
        "ips": list(t.ips or []), "countries": list(t.countries or []), "networks": list(t.networks or []),
        "added": t.added, "last_checked": t.last_checked,
    }


_BASE = "https://newtrackon.co.nz"


def _feed():
    import hashlib
    from datetime import datetime, timezone
    from xml.sax.saxutils import escape
    evs = list(T.EVENTS)
    tr = (request.args.get("tracker") or "").lower()
    types = {x.strip() for x in (request.args.get("type") or "").split(",") if x.strip()}
    if tr:
        evs = [e for e in evs if tr in e["url"].lower()]
    if types:
        evs = [e for e in evs if e["type"] in types]
    evs = evs[-100:][::-1]
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now = int(datetime.now(timezone.utc).timestamp())
    out = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
           '<title>newTrackon NZ: tracker events</title>',
           '<subtitle>Trackers going down, coming back, turning Up/Bad or recovering</subtitle>',
           f'<link href="{_BASE}/"/>', f'<link rel="self" href="{escape(_BASE + request.full_path.rstrip("?"))}"/>',
           f'<id>{_BASE}/feed.xml</id>', f'<updated>{iso(evs[0]["t"] if evs else now)}</updated>',
           '<author><name>newtrackon.co.nz</name></author>']
    for e in evs:
        eid = hashlib.md5(f'{e["t"]}|{e["url"]}|{e["type"]}|{e["text"]}'.encode()).hexdigest()
        out.append(f'<entry><title>{escape(e.get("host", e["url"]) + " " + e["text"])}</title>'
                   f'<link href="{_BASE}/"/><id>tag:newtrackon.co.nz,2026:{eid}</id><updated>{iso(e["t"])}</updated>'
                   f'<category term="{escape(e["type"])}"/><content type="text">{escape(e["url"] + ": " + e["text"])}</content></entry>')
    out.append('</feed>')
    return Response("\n".join(out), mimetype="application/atom+xml", headers={"Access-Control-Allow-Origin": "*"})


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


def _uptime_days(historic, days=20):
    """Share of up slots per 24 h, oldest first, counted back from now; only whole days that have data."""
    h = [int(x) for x in (historic or [])]
    out = []
    for k in range(min(days, len(h) // _SLOTS_PER_DAY)):
        chunk = h[len(h) - _SLOTS_PER_DAY * (k + 1):len(h) - _SLOTS_PER_DAY * k]
        out.append({"ago": k, "pct": round(100 * sum(1 for x in chunk if x > 0) / len(chunk))})
    return out[::-1]


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


FIX_TITLES = {"no-peers": "Hands out no peers", "fake-peers": "Returns fake peers", "dead-address": "Dead IPv4 or IPv6 address",
              "unreliable": "Drops out (Up/Unreliable, Up/Junk)", "slow": "Slow (Up/Slow)", "down-timeout": "Down: timeout",
              "down-refused": "Down: connection refused", "down-dns": "Down: DNS", "down-tls": "Down: TLS / certificate",
              "down-http": "Down: HTTP error", "down": "Down: no usable answer"}


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
        return "dead-address"
    if st in ("up_unreliable", "up_junk"):
        return "unreliable"
    if st == "up_slow":
        return "slow"
    return None


def _evidence(t, d):
    """What the checks saw, in plain words, for the tracker page's problem box."""
    out = []
    fix = _fix_anchor(t)
    if fix is None:
        return out
    if fix == "no-peers":
        out.append("Peer test passed %d of the last %d times: a second test client wasn't told about the first." % (d["peer_test"]["passed"], d["peer_test"]["of"]))
    if fix == "fake-peers":
        out.append("Returned %s peer(s) for a random torrent only this site knows, %d checks in a row."
                   % (d["fake_peers"]["latest"] if d["fake_peers"]["latest"] is not None else "unknown", d["fake_peers"]["streak"]))
    if fix == "dead-address":
        df = T.FAM_FAILS.get(t.url) or {}
        fam = str(df.get("fam", "?"))[-1]
        ips = [ip for ip in (t.ips or []) if (":" in ip) == (fam == "6")]
        out.append("Its IPv%s address%s %s didn't answer in %d checks in a row, while IPv%s did."
                   % (fam, "es" if len(ips) > 1 else "", ", ".join(ips) or "(published in DNS)", df.get("n", 0), "4" if fam == "6" else "6"))
    if fix.startswith("down"):
        out.append("Last error: %s" % (T.DOWN_WHY.get(t.url) or "no answer"))
        out.append("Last successful check: %s." % (_ago(t.last_uptime) + " ago" if t.last_uptime else "none recorded"))
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
    if t is None:
        abort(404)
    now = time.time()
    _recheck_all[:] = [x for x in _recheck_all if now - x < 3600]
    last = _recheck_host.get(host, 0)
    if now - last < 3600:
        return redirect("/tracker/%s?recheck=wait&m=%d" % (host, (3600 - (now - last)) // 60 + 1), 303)
    if len(_recheck_all) >= RECHECK_PER_HOUR:
        return redirect("/tracker/%s?recheck=busy" % host, 303)
    _recheck_host[host] = now
    _recheck_all.append(now)
    T.FORCE_CHECK.add(t.url)
    return redirect("/tracker/%s?recheck=queued" % host, 303)


def _fix_page():
    from flask import render_template
    return render_template("static/fix.jinja", active="", titles=FIX_TITLES)


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


_RANK_DAYS = 14  # historic keeps 1000 slots (~20.8 days), so 14 days is the longest window every ranked tracker has
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
        abort(404)
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
        "tracker.jinja", t=t, d=d, status_text=_STATUS_TEXT.get(d["status"], d["status"]), row=_rowcls(t),
        days=_uptime_days(t.historic), recent=h[-96:], history_days=round(len(h) / _SLOTS_PER_DAY, 1),
        chart=_latency_chart(T.LAT_HIST.get(t.url), now),
        events=[dict(e, ago=_ago(e["t"])) for e in reversed(T.EVENTS) if e.get("url") == t.url or e.get("host") == t.host][:30],
        added_ago=_ago(t.added or now), checked_ago=_ago(t.last_checked or now), checked_txt=_ago_txt(t.last_checked or now),
        now=now, title="Is %s down? Live tracker status" % t.host,
        description=desc, ld=ld, down=down, down_for=down_for,
        fix=_fix_anchor(t), fix_title=FIX_TITLES.get(_fix_anchor(t) or ""), evidence=_evidence(t, d),
        recheck=request.args.get("recheck"), recheck_m=request.args.get("m", type=int),
        recheck_last=_recheck_host.get(host),
    )


def register(app):
    app.add_url_rule("/tracker/<host>", "nt_tracker", _tracker_page)
    app.add_url_rule("/tracker/<host>/recheck", "nt_recheck", _recheck, methods=["POST"])
    app.add_url_rule("/fix", "nt_fix", _fix_page)
    app.add_url_rule("/rankings", "nt_rankings", _rankings_page)
    app.add_url_rule("/tools", "nt_tools", _tools_page)
    app.add_url_rule("/badge/<host>", "nt_badge", _badge)
    app.jinja_env.globals["nt_health"] = _monitor_health

    @app.route("/api/tracker/<host>")
    def api_tracker(host):
        host = host.lower()
        t = next((x for x in _trackers() if (x.host or "").lower() == host), None)
        r = jsonify(_detail(t)) if t is not None else jsonify({"error": "not listed", "host": host})
        if t is None:
            r.status_code = 404
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r
    app.jinja_env.globals["nt_fix"] = _fix_anchor
    app.jinja_env.globals["nt_fix_titles"] = FIX_TITLES
    app.jinja_env.globals["nt_tags"] = _filter_tags
    app.jinja_env.globals["nt_now"] = lambda: int(__import__("time").time())
    app.jinja_env.globals["nt_events"] = lambda n=10, days=None: list(reversed(
        [e for e in T.EVENTS if days is None or e.get("t", 0) >= __import__("time").time() - days * 86400][-n:]))
    app.jinja_env.globals["nt_rowcls"] = _rowcls
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
