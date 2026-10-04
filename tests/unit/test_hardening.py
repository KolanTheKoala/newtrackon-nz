"""Hardening: probes only connect to public addresses, and submissions are limited per client address."""

from __future__ import annotations

import socket
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
import requests
from flask.testing import FlaskClient
from freezegun import freeze_time

from newtrackon import scraper


class TestIpIsPublic:
    @pytest.mark.parametrize("ip", ["93.184.216.34", "2606:4700:4700::1111", "::ffff:93.184.216.34"])
    def test_public(self, ip: str) -> None:
        assert scraper.ip_is_public(ip) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "100.64.0.1",  # carrier-grade NAT
            "169.254.1.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1%eth0",
            "fd7d:76ee:e68f:a993::1",
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "not-an-ip",
        ],
    )
    def test_not_public(self, ip: str) -> None:
        assert scraper.ip_is_public(ip) is False

    def test_require_public_raises_oserror(self) -> None:
        with pytest.raises(OSError, match="non-public"):
            scraper.require_public(("127.0.0.1", 6969))


class TestHttpConnectGuard:
    """DNS rebinding: update_ips saw a public address, but the name resolves to loopback when the probe connects."""

    def test_guard_is_installed_in_urllib3(self) -> None:
        import urllib3.util.connection as u3c

        assert u3c.create_connection is scraper._public_create_connection

    def test_connect_to_rebound_loopback_is_refused(self) -> None:
        rebound = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))]
        with (
            patch("newtrackon.scraper.socket.getaddrinfo", return_value=rebound),
            patch("newtrackon.scraper._u3c_create_connection") as real_connect,
        ):
            with pytest.raises(OSError, match="no public address"):
                scraper._public_create_connection(("tracker.example.com", 8080))
            real_connect.assert_not_called()

    def test_http_announce_to_rebound_loopback_fails_without_connecting(self) -> None:
        rebound = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))]
        with (
            patch("newtrackon.scraper.socket.getaddrinfo", return_value=rebound),
            patch("newtrackon.scraper._u3c_create_connection") as real_connect,
        ):
            with pytest.raises(requests.exceptions.ConnectionError):
                scraper.memory_limited_get("http://tracker.example.com:8080/announce")
            real_connect.assert_not_called()

    def test_connects_by_public_address_only(self) -> None:
        mixed = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        with (
            patch("newtrackon.scraper.socket.getaddrinfo", return_value=mixed),
            patch("newtrackon.scraper._u3c_create_connection", return_value="sock") as real_connect,
        ):
            assert scraper._public_create_connection(("tracker.example.com", 443), 10) == "sock"
            real_connect.assert_called_once_with(("93.184.216.34", 443), 10)


class TestUdpConnectGuard:
    def test_udp_announce_to_loopback_never_connects(self) -> None:
        loop = [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 6969))]
        fake_sock = MagicMock()
        with (
            patch("newtrackon.scraper.socket.getaddrinfo", return_value=loop),
            patch("newtrackon.scraper.socket.socket", return_value=fake_sock),
        ):
            with pytest.raises(RuntimeError):
                scraper.announce_udp("udp://tracker.example.com:6969/announce")
        fake_sock.connect.assert_not_called()


@pytest.fixture
def limits() -> Generator[None]:
    from newtrackon import views

    views._submit_log.clear()
    with patch("newtrackon.ingest.enqueue_new_trackers"):
        yield
    views._submit_log.clear()


def _urls(n: int, start: int = 0) -> str:
    return " ".join(f"udp://t{i}.example.com:6969/announce" for i in range(start, start + n))


@pytest.mark.usefixtures("limits")
class TestSubmissionLimits:
    STRANGER = {"X-Forwarded-For": "203.0.113.5"}

    def _add(self, client: FlaskClient, text: str, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        return client.post("/api/add", data={"new_trackers": text}, headers=self.STRANGER if headers is None else headers)

    def test_twenty_per_hour_then_429(self, flask_client: FlaskClient) -> None:
        for i in range(20):
            assert self._add(flask_client, _urls(1, i)).status_code == 204
        r = self._add(flask_client, _urls(1, 99))
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "3600"

    def test_too_many_urls_in_one_request(self, flask_client: FlaskClient) -> None:
        r = self._add(flask_client, _urls(501))
        assert r.status_code == 429
        assert b"500" in r.get_data()

    def test_hourly_url_budget(self, flask_client: FlaskClient) -> None:
        assert self._add(flask_client, _urls(400)).status_code == 204
        assert self._add(flask_client, _urls(101, 400)).status_code == 429
        assert self._add(flask_client, _urls(100, 400)).status_code == 204

    def test_limit_resets_after_an_hour(self, flask_client: FlaskClient) -> None:
        with freeze_time("2026-01-01 00:00:00") as clock:
            for i in range(20):
                assert self._add(flask_client, _urls(1, i)).status_code == 204
            assert self._add(flask_client, _urls(1, 50)).status_code == 429
            clock.tick(3601)
            assert self._add(flask_client, _urls(1, 51)).status_code == 204

    def test_addresses_are_counted_separately(self, flask_client: FlaskClient) -> None:
        for i in range(20):
            assert self._add(flask_client, _urls(1, i)).status_code == 204
        assert self._add(flask_client, _urls(1, 99), {"X-Forwarded-For": "203.0.113.6"}).status_code == 204

    def test_rightmost_forwarded_address_is_used(self, flask_client: FlaskClient) -> None:
        """A client can't dodge the limit by sending its own X-Forwarded-For: Caddy's (rightmost) entry counts."""
        for i in range(20):
            spoof = {"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.5"}
            assert self._add(flask_client, _urls(1, i), spoof).status_code == 204
        assert self._add(flask_client, _urls(1, 99)).status_code == 429

    def test_local_scripts_are_exempt(self, flask_client: FlaskClient) -> None:
        """Direct or via Caddy from localhost (the VPS's own submit scripts): no limit, no per-request cap."""
        for i in range(25):
            assert self._add(flask_client, _urls(1, i), {}).status_code == 204
        assert self._add(flask_client, _urls(600), {"X-Forwarded-For": "127.0.0.1"}).status_code == 204

    def test_servers_own_public_address_is_exempt(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper, "my_ipv6", "2001:db8::10")
        for i in range(25):
            assert self._add(flask_client, _urls(1, i), {"X-Forwarded-For": "2001:db8::10"}).status_code == 204

    def test_form_over_limit_shows_message(self, flask_client: FlaskClient) -> None:
        r = flask_client.post("/", data={"new_trackers": _urls(501)}, headers=self.STRANGER)
        assert r.status_code == 429
        assert b"Too many trackers in one submission" in r.get_data()


class TestTemplatesEscape:
    def test_tracker_text_is_escaped_on_the_main_page(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        from time import time

        from newtrackon import tracker as T

        evil = '<script>alert(1)</script>'
        monkeypatch.setattr(T, "EVENTS", [{"t": int(time()), "url": "udp://x.example:1/announce", "host": "x.example", "type": "down", "text": "went Down (" + evil + ")"}])
        html = flask_client.get("/").get_data(as_text=True)
        assert evil not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
