"""NZ mirror extras: /api/details (full JSON state per tracker) and /api/clean (Up/Good, filtered, one per operator)."""
from flask import Response, jsonify, request

from newtrackon import db
from newtrackon import tracker as T
from newtrackon.regions import parse_region_filter, regions_of


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


def register(app):
    app.jinja_env.globals["nt_now"] = lambda: int(__import__("time").time())
    app.jinja_env.globals["nt_events"] = lambda n=10: list(reversed(T.EVENTS[-n:]))
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
