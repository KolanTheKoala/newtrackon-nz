# newTrackon NZ (newtrackon.co.nz)

The code behind [newtrackon.co.nz](https://newtrackon.co.nz/), a New Zealand fork of
[newTrackon](https://github.com/CorralPeltzer/newTrackon): live health checks of public BitTorrent trackers.
It is upstream newTrackon at commit `e1a0104` plus the NZ changes, packaged as one self-contained
Docker project (app + Caddy). `git log upstream/master..main` shows every change.

## What this fork adds to upstream

**Checks.** Besides "does it answer", every check can test:

- **Peers**: a second client must be handed the peer we announced (from a VPN exit, if configured).
- **Fake peers, inflated counts, stale peers** (keeps peers that said they stopped).
- **IPv4 and IPv6 separately**: a dead published address family is flagged.
- **Spoofing**: whether a UDP tracker enforces the connection-ID handshake.

A failure is only blamed on the tracker when this monitor itself is fine: no internet or DNS here,
public resolvers disagreeing with ours, or the tracker answering through another region all mean
"our fault", and nothing is recorded.

**Score and status.** Score 0&ndash;100 = recency-weighted availability &times; stability, minus penalties
(latency, failed tests). New trackers are capped at 80, rising to 100 over their first 7 days. Statuses:
Up/Good, Up/Slow, Up/Unreliable, Up/New, Up/Junk, Up/Broken, Up/Bad, Down.

**Adaptive checking.** Healthy trackers every 30&ndash;60 minutes (up to 4 h for a long clean record at 95+),
flapping ones every 15 minutes, dead ones backing off to 4 h. History is kept per 30-minute slot, so how
often a tracker is checked doesn't skew its score.

**Monitor health** on the status and About pages: checks in the last hour, the latest check, which regions are
measuring, or a clear notice when the monitor itself is offline.

**Latency from four places**: Americas, Europe, Asia and Oceania (needs VPN exits; without them only
this server's own latency is measured).

**Pages**

| Page | |
|---|---|
| `/` | Current status of every tracker. Full table on desktop, one card per tracker on phones. |
| `/tracker/<host>` | "Is it down?" answer, uptime by day and for 48 h, latency history by region, score breakdown, details, recent events, "check again now", and a Follow link (Atom feed of that tracker's changes). |
| `/fix` | What each problem means and how a tracker operator fixes it. |
| `/list`, `/api` | Ready-made lists, with the same filters as the main table. |
| `/tools` | Magnet booster and torrent fixer: add the best trackers to a magnet link or a .torrent, drop Down ones. Runs in the browser; the info hash never changes and private torrents are left alone. |
| `/badge/<host>.svg` | Live status badge for a tracker's own site or README (snippet on each tracker page). |
| `/rankings` | Most reliable over the last 14 days, longest unbroken uptime, fastest from each region. |
| `/map` | Where the trackers are. |
| `/feed.xml` | Atom feed of status changes (`?tracker=<host>` for one tracker). Optional Telegram alerts. |

**API** (`/api.yml`, OpenAPI, version `2.0_NZ`): upstream's lists plus `/api/clean` (clean list for
torrent clients), `/api/details` (every tracker's full state as JSON) and `/api/tracker/<host>` (one tracker's). Lists take filters:
`region=` (americas, europe, asia-pacific), `fast_from=` / `fast_from_ms=`, `good`, `protocol`,
`ipv4_works`, `ipv6_works`, `passes_peer_test`.

**Hardening.** Probes only connect to public addresses and never follow redirects. Submissions are
limited per address (500 trackers per request; 20 requests and 500 trackers per hour), and the
submission queue survives restarts. All template output is escaped. Clean shutdown on SIGTERM;
history files are written atomically.

## Running

```
docker compose up -d --build
```

Both containers use host networking: the app listens on 127.0.0.1:8080 and Caddy serves
`newtrackon.co.nz` (plus `ipv4.`/`ipv6.` test hosts) with automatic TLS. Change the hostnames in
`deploy/caddy/Caddyfile` to run it elsewhere.

- `deploy/caddy/Caddyfile`: proxy config. It imports optional untracked snippets from
  `deploy/caddy/local/`; files they serve go in `deploy/www-local/`.
- `deploy/www/`: static files (sitemap, IndexNow key, 404 page).
- App state lives in the `newtrackon_newtrackon-data` volume.
- Optional, in that volume's `data/`: `notify.json` for Telegram alerts, `probe_src.json` for the
  VPN exits used by the second test client and regional latency. Without them those features are off.

## Tests

All Python tests pass, plus JavaScript tests for the browser tools (`tests/js`), with no network needed; GitHub Actions runs them on every push.

```
pip install . pytest freezegun
python -m pytest tests -q
```

`tests/conftest.py` gives every test a temporary `data/` directory and sets the "is it our fault?"
checks to the answers upstream assumes; `TestNZGuards` tests those checks themselves.

## Credit

newTrackon is the work of its original authors (MIT licence, see `LICENSE.txt`). The upstream README follows.

---

## Upstream: newTrackon

newTrackon is a service to monitor the status and health of existing open and public trackers that anyone can use. It
also allows to submit new trackers to add them to the list.

newTrackon is based on the abandoned [Trackon](http://repo.cat-v.org/trackon/) by [Uriel †](https://github.com/uriel).

**By default, newTrackon needs IPv4 and IPv6 internet connectivity, and the application won't start without both. Run
with arguments `--ignore-ipv6` or `--ignore-ipv4` to skip this check.**

## Arguments

run.py [--address ADDRESS] [--port PORT] [--ignore-ipv4]
[--ignore-ipv6]

optional arguments:

* `--address ADDRESS`  Address for the flask server
* `--port PORT`        Port for the flask server
* `--ignore-ipv4`      Ignore newTrackon server IPv4 detection
* `--ignore-ipv6`      Ignore newTrackon server IPv6 detection

## Running

### With Docker

Pull the image and create the container with

```
docker run -d -p 8080:8080 corralpeltzer/newtrackon --address=0.0.0.0
```

You can now access to the main page opening in your browser `http://localhost:8080`.

### With python

After cloning the repo, make sure you have a working Python 3.13 environment.

Install dependencies with pip:

```
pip install .
```
or with uv:
```
uv sync
```
Finally, run

```
python3 run.py
```

You can now access to the main page opening in your browser `http://localhost:8080`.

## Related

* [electromagnet](https://github.com/sdmtr/electromagnet), a Chrome extension to automatically add stable trackers to
  magnet links as you browse
* [transmission-trackers](https://github.com/telnetdoogie/transmission-trackers), a very simple python app (and docker image) that runs alongside transmission to update trackers on your public torrents

## Contributors

Feel free to make suggestions, create pull requests, report issues or any other feedback.

Contact me on [twitter](https://twitter.com/CorralPeltzer) or on corral.miguelangel@gmail.com
