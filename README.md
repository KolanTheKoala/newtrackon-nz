# newTrackon NZ (newtrackon.co.nz)

The code behind [newtrackon.co.nz](https://newtrackon.co.nz/), a New Zealand instance of
[newTrackon](https://github.com/CorralPeltzer/newTrackon). It is upstream newTrackon at commit `e1a0104`
plus the NZ changes, packaged as one self-contained Docker project (app + Caddy).

What's different from upstream: a stricter status ladder (Up/Good, Up/Slow, Up/Unreliable, Up/Junk,
Up/Bad, Up/Broken), peer, fake-peer and dual-stack checks, latency measured from several regions,
an event feed with optional Telegram alerts, clean shutdown on SIGTERM, atomic history writes,
and NZ templates and styling. See `git log upstream/master..main` for the full diff.

## Running

```
docker compose up -d --build
```

Both containers use host networking: the app listens on 127.0.0.1:8080 and Caddy serves
`newtrackon.co.nz` (plus `ipv4.`/`ipv6.` test hosts) with automatic TLS. Change the hostnames in
`deploy/caddy/Caddyfile` to run it elsewhere.

- `deploy/caddy/Caddyfile`: proxy config. It imports untracked site-local snippets from
  `deploy/caddy/local/` (see the README there); files they serve go in `deploy/www-local/`.
- `deploy/www/`: static files (sitemap, IndexNow key, 404 page).
- App state lives in the `newtrackon_newtrackon-data` volume. Telegram alerts are configured in
  `data/notify.json` inside it and are off if that file is missing.

## Tests

Upstream's test suite, updated for the NZ behaviour, passes in full (686 tests), and needs no network:

```
pip install pytest freezegun
python -m pytest tests -q
```

`tests/conftest.py` gives every test a temporary `data/` directory and sets the NZ "is it our fault?"
checks (monitor online, public-resolver cross-check, local-fault check) to the answers upstream assumes;
`TestNZGuards` tests those checks themselves.

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
