"""Submissions are checked for tracker URLs: a message pasted into the box isn't queued word by word (2026-10-17)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from newtrackon import ingest, persistence

SPAM = ("Hi there, we came across your website and would like to offer our SEO service. Let me know if you would like "
        "to receive our pricing. Our proposal covers details for every page. Best regards, Robin")


@pytest.mark.parametrize("url", ["udp://tracker.example.org:6969/announce", "http://t.example.com/announce",
                                 "https://xn--bcher-kva.example:443/announce", "udp://93.184.216.34:6969/announce",
                                 "udp://[2001:db8::1]:6969/announce", "http://tracker.example.org",
                                 "wss://tracker.example.org"])
def test_tracker_urls_pass(url: str) -> None:
    assert ingest.is_tracker_url(url)


@pytest.mark.parametrize("word", ["robin", "regards,", "pricing.", "tracker.example.org", "ftp://tracker.example.org/",
                                  "udp://localhost:6969/announce", "udp://tracker.example.org:99999/announce",
                                  "udp://tracker.example.org:port/announce", "http://exa_mple.com/announce",
                                  "http://-bad.example/announce", "udp://tracker.123:6969/announce", "http:///announce"])
def test_other_words_fail(word: str) -> None:
    assert not ingest.is_tracker_url(word)


def test_only_urls_are_queued() -> None:
    with patch.object(ingest, "add_one_tracker_to_submitted_queue") as add:
        ingest.enqueue_new_trackers("Trackers:\nUDP://Tracker.Example.org:6969/announce\nthanks!\nudp://tracker.example.org:6969/announce")
    add.assert_called_once_with("udp://tracker.example.org:6969/announce")


def test_queue_entry_drops_junk_without_a_row() -> None:
    before = len(persistence.submitted_data)
    ingest.add_one_tracker_to_submitted_queue("robin")
    assert len(persistence.submitted_data) == before and ingest.submitted_queue.qsize() == 0


def test_old_junk_rows_are_dropped() -> None:
    persistence.submitted_data.extendleft([
        {"url": "udp://tracker.example.org:6969/announce", "time": 1, "ip": "", "status": 0, "info": ["x"]},
        {"url": "robin", "time": 1, "ip": "", "status": 0, "info": ["Invalid URL: cannot extract hostname"], "refused": True},
        {"time": 1, "ip": "", "status": 0, "info": ["no url field: left alone"]},
        {"url": "regards,", "time": 1, "ip": "", "status": 0, "info": ["Invalid URL: cannot extract hostname"], "refused": True},
    ])
    ingest._drop_junk_rows()
    assert [r.get("url") for r in persistence.submitted_data] == [None, "udp://tracker.example.org:6969/announce"]


class TestForm:
    H = {"X-Forwarded-For": "198.51.100.7"}

    def test_spam_message_is_refused_and_nothing_queued(self, flask_client: FlaskClient) -> None:
        with patch("newtrackon.ingest.enqueue_new_trackers") as enq:
            r = flask_client.post("/", data={"new_trackers": SPAM}, headers=self.H)
        assert r.status_code == 400 and "No tracker URLs found" in r.get_data(as_text=True)
        enq.assert_not_called()

    def test_a_message_with_one_link_in_it_is_refused(self, flask_client: FlaskClient) -> None:
        with patch("newtrackon.ingest.enqueue_new_trackers") as enq:
            r = flask_client.post("/api/add", data={"new_trackers": SPAM + " http://seo.example/offer"}, headers=self.H)
        assert r.status_code == 400 and b"looks like a message" in r.data
        enq.assert_not_called()

    def test_a_list_with_a_few_stray_words_is_accepted(self, flask_client: FlaskClient) -> None:
        with patch("newtrackon.ingest.enqueue_new_trackers") as enq:
            r = flask_client.post("/api/add", data={"new_trackers": "my trackers:\nudp://tracker.example.org:6969/announce"}, headers=self.H)
        assert r.status_code == 204
        enq.assert_called_once()
