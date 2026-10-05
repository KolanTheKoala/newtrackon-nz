from datetime import UTC, datetime
from logging import ERROR, INFO, basicConfig, getLogger
from sys import stdout
from collections import deque
from collections.abc import Callable
from threading import Lock, Thread
from time import time

from flask import (
    Flask,
    Response,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
)
from werkzeug.routing import BaseConverter, Map

from newtrackon import db, ingest, persistence, scraper, utils
from newtrackon.regions import RegionFilter, parse_region_filter
from newtrackon.tracker import format_uptime_and_downtime_time

max_input_length: int = 1000000

# ---- submission limits, per client address, so one sender can't flood the check queue ----
SUBMIT_MAX_URLS = 500  # trackers in one submission
SUBMIT_HOURLY_REQUESTS = 20  # submissions per address per hour
SUBMIT_HOURLY_URLS = 500  # trackers per address per hour
_submit_log: dict[str, deque[tuple[float, int]]] = {}
_submit_lock = Lock()


def _client_ip() -> str:
    """The submitter's address. Behind Caddy every request arrives from 127.0.0.1 with X-Forwarded-For set by Caddy,
    which replaces any client-sent value (no trusted_proxies configured), so its rightmost entry is the real client."""
    remote = request.remote_addr or ""
    if remote in ("127.0.0.1", "::1"):
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd.strip():
            return fwd.split(",")[-1].strip()
    return remote


def _submission_refused(text: str) -> str | None:
    """None if the submission may be queued, else the reason it's refused. This server's own addresses are exempt."""
    ip = _client_ip()
    if ip in ("127.0.0.1", "::1") or ip in (scraper.my_ipv4, scraper.my_ipv6):
        return None
    n = len(set(text.lower().split()))
    if n > SUBMIT_MAX_URLS:
        return f"Too many trackers in one submission (the limit is {SUBMIT_MAX_URLS})"
    now = time()
    with _submit_lock:
        log = _submit_log.setdefault(ip, deque())
        while log and log[0][0] < now - 3600:
            log.popleft()
        if len(log) >= SUBMIT_HOURLY_REQUESTS or sum(c for _, c in log) + n > SUBMIT_HOURLY_URLS:
            return "Too many submissions from your address, please try again in an hour"
        log.append((now, n))
        if len(_submit_log) > 10000:
            for k in [k for k, v in _submit_log.items() if not v or v[-1][0] < now - 3600]:
                del _submit_log[k]
    return None

app = Flask(__name__)
app.template_folder = "tpl"
# Flask only auto-escapes .html templates; ours are .jinja, and they show text that comes from trackers (errors, networks)
app.jinja_env.autoescape = True


class RegexConverter(BaseConverter):
    def __init__(self, url_map: Map, *items: str) -> None:
        super().__init__(url_map)
        self.regex: str = items[0]


app.url_map.converters["regex"] = RegexConverter


@app.template_filter("format_timestamp")
def format_timestamp(timestamp: float | None) -> str:
    """Convert Unix timestamp to HH:MM:SS UTC format."""
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%H:%M:%S UTC")


@app.template_filter("format_date")
def format_date(timestamp: float | None) -> str:
    """Convert Unix timestamp to D-M-YYYY date format."""
    if timestamp is None:
        return ""
    dt = datetime.fromtimestamp(timestamp, tz=UTC)
    return f"{dt.day}-{dt.month}-{dt.year}"


basicConfig(
    level=INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    stream=stdout,
)
logger = getLogger("newtrackon")
getLogger("urllib3").setLevel(ERROR)  # Suppress urllib3 warnings, some weird servers can trigger them
logger.info("Server started")


@app.route("/")
def main(form_feedback: str | None = None, form_reason: str | None = None, banned: list | None = None) -> str:
    trackers_list = db.get_all_data()
    trackers_list = format_uptime_and_downtime_time(trackers_list)
    return render_template(
        "main.jinja", form_feedback=form_feedback, form_reason=form_reason, trackers=trackers_list, active="Status",
        banned=banned or [],
    )


def _banned_in(text: str) -> list[dict]:
    """Submitted trackers that this site removed and that are banned now, so the form can say so straight away and link
    to their page. Manual denylist entries are never mentioned (ntextra._ban only knows hosts this site removed)."""
    from urllib.parse import urlparse

    from newtrackon import ntextra

    out, seen = [], set()
    for word in text.lower().split()[:SUBMIT_MAX_URLS]:
        try:
            host = (urlparse(word).hostname or "").lower()
        except ValueError:
            continue
        if not host or host in seen:
            continue
        seen.add(host)
        b = ntextra._ban(host)
        if b and b["active"]:
            r = ntextra.T.REMOVED.get(host) or {}
            out.append({"host": host, "removed": ntextra._date(r.get("t", 0)), "reason": r.get("reason") or "",
                        "until": ntextra._date(b["until"]) if b["until"] else None})
    return out


@app.route("/", methods=["POST"])
def new_trackers():
    new_trackers = request.form.get("new_trackers")
    if new_trackers is None:
        abort(400)
    elif len(new_trackers) > max_input_length:
        abort(413)
    elif new_trackers == "":
        return main(form_feedback="EMPTY")
    elif refused := _submission_refused(new_trackers):
        return main(form_feedback="LIMIT", form_reason=refused), 429
    else:
        check_all_trackers = Thread(target=ingest.enqueue_new_trackers, args=(new_trackers,))
        check_all_trackers.daemon = True
        check_all_trackers.start()
    return main(form_feedback="SUCCESS", banned=_banned_in(new_trackers))


@app.route("/api/add", methods=["POST"])
def new_trackers_api():
    new_trackers = request.form.get("new_trackers")
    if not new_trackers:
        return abort(400)
    if len(new_trackers) > max_input_length:
        abort(413)
    if refused := _submission_refused(new_trackers):
        return Response(refused + "\n", status=429, headers={"Retry-After": "3600", "Access-Control-Allow-Origin": "*"})
    check_all_trackers = Thread(target=ingest.enqueue_new_trackers, args=(new_trackers,))
    check_all_trackers.daemon = True
    check_all_trackers.start()
    resp = Response(status=204, headers={"Access-Control-Allow-Origin": "*"})
    return resp


@app.route("/submitted")
def submitted():
    return render_template(
        "submitted.jinja",
        # Iterating a deque while rendering can cause RuntimeError: deque mutated during iteration, so we cast it to a list
        data=list(persistence.submitted_data),
        size=ingest.submitted_queue.qsize(),
        pending=ingest.pending(),
        confirming=ingest.confirming(),
        active="Submitted",
    )


@app.route("/faq")
def faq():
    return render_template("/static/faq.jinja", active="FAQ")


@app.route("/list")
def list_stable():
    return render_template("/static/list.jinja", active="List")


@app.route("/api")
def api_docs():
    return render_template("/static/api-docs.jinja", active="API")


@app.route("/raw")
def raw():
    # Iterating a deque while rendering can cause RuntimeError: deque mutated during iteration, so we cast it to a list
    return render_template("raw.jinja", data=list(persistence.raw_data), active="Raw data")


@app.route("/api/<int:percentage>")
def api_percentage(percentage: int, added_before: int | None = None) -> Response:
    if added_before is None:
        added_before = get_added_before_or_abort()
    include_upv4_only = request.args.get("include_ipv4_only_trackers", default="true").lower() not in ("false", "0")
    include_upv6_only = request.args.get("include_ipv6_only_trackers", default="true").lower() not in ("false", "0")
    if 0 <= percentage <= 100:
        formatted_list = db.get_api_data(
            "percentage", percentage, include_upv4_only, include_upv6_only, added_before,
            region_filter=get_region_filter_or_abort(), url_filter=get_quality_filter_or_abort(),
        )
        resp = make_response(formatted_list)
        resp = utils.add_api_headers(resp)
        return resp
    else:
        abort(
            Response(
                "The percentage has to be between 0 an 100",
                400,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        )


stable_min_age_days_default: int = 7


def get_quality_filter_or_abort() -> Callable[[str], bool] | None:
    from newtrackon import ntextra

    try:
        return ntextra.quality_filter(request.args)
    except ValueError as exc:
        abort(Response(str(exc), 400, headers={"Access-Control-Allow-Origin": "*"}))


def get_region_filter_or_abort() -> RegionFilter:
    try:
        return parse_region_filter(request.args)
    except ValueError as exc:
        abort(Response(str(exc), 400, headers={"Access-Control-Allow-Origin": "*"}))


def get_added_before_or_abort(default_min_age_days: int = 0) -> int | None:
    try:
        return utils.get_added_before_from_query_args(request.args, default_min_age_days=default_min_age_days)
    except ValueError as exc:
        abort(
            Response(
                str(exc),
                400,
                headers={"Access-Control-Allow-Origin": "*"},
            )
        )


@app.route("/api/stable")
def api_stable():
    return api_percentage(90, added_before=get_added_before_or_abort(stable_min_age_days_default))


@app.route("/api/best")
def api_best():
    return redirect("/api/stable", code=301)


@app.route("/api/all")
def api_all():
    return api_percentage(0, added_before=get_added_before_or_abort())


@app.route("/api/live")
@app.route("/api/udp")
@app.route("/api/http")
def api_multiple():
    resp = make_response(
        db.get_api_data(
            request.path, added_before=get_added_before_or_abort(), region_filter=get_region_filter_or_abort(),
            url_filter=get_quality_filter_or_abort(),
        )
    )
    resp = utils.add_api_headers(resp)
    return resp


@app.route("/map")
def tracker_map() -> str:
    return render_template("/static/map.jinja", active="Map")


@app.route("/about")
def about():
    return render_template("/static/about.jinja", active="About")


@app.route(r'/<regex(".*(?=\.)"):filename>.<regex("(png|svg|ico)"):filetype>')  # matches all favicons that should be in root
def favicon(filename: str, filetype: str) -> Response:
    return send_from_directory("static/imgs/", filename + "." + filetype)


@app.route(
    r'/<regex(".*(?=\.)"):filename>.<regex("(xml|json)"):filetype>'
)  # matches browserconfig and manifest that should be in root
def app_things(filename: str, filetype: str) -> Response:
    return send_from_directory("static/", filename + "." + filetype)


@app.route("/api.yml")
def openapi_def():
    return send_from_directory(".", "newtrackon-api.yml")


@app.before_request
def reject_announce_requests():
    if request.args.get("info_hash"):
        return abort(Response("newTrackon is not a tracker and cannot provide peers", 403))


from newtrackon import ntextra  # noqa: E402  NZ fork extras (/api/details, /api/clean)

ntextra.register(app)
