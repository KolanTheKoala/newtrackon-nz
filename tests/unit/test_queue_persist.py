"""The submission queue survives a restart: its URLs are saved to data/submit_queue.json and re-queued at startup."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from newtrackon import ingest
from newtrackon.tracker import Tracker


def _tracker(url: str) -> Tracker:
    # no DNS: skip the address lookups
    with patch.object(Tracker, "update_ips"), patch.object(Tracker, "refresh_recent_ips"):
        return Tracker.from_url(url)


@pytest.fixture(autouse=True)
def empty_queue() -> Generator[None]:
    def drain() -> None:
        while not ingest.submitted_queue.empty():
            ingest.submitted_queue.get_nowait()
            ingest.submitted_queue.task_done()
        ingest._in_flight.clear()
        ingest._restoring[0] = False

    drain()
    yield
    drain()


def _saved() -> list[str]:
    return json.loads(Path(ingest.QUEUE_FILE).read_text())


def _accept_everything(url: str) -> None:
    # stands in for the normal checks: put the URL straight in the queue
    ingest.submitted_queue.put_nowait(_tracker(url))
    ingest.save_queue()


class TestSaveQueue:
    def test_saves_queued_urls_in_order(self) -> None:
        for u in ("udp://a.example:1/announce", "udp://b.example:2/announce"):
            ingest.submitted_queue.put_nowait(_tracker(u))
        ingest.save_queue()
        assert _saved() == ["udp://a.example:1/announce", "udp://b.example:2/announce"]

    def test_item_being_processed_is_saved_until_done(self) -> None:
        ingest.submitted_queue.put_nowait(_tracker("udp://a.example:1/announce"))
        ingest.submitted_queue.put_nowait(_tracker("udp://b.example:2/announce"))
        seen: list[list[str]] = []

        def process(t: Tracker) -> None:
            ingest.save_queue()  # e.g. a new submission arrives while this one is being checked
            seen.append(_saved())

        with patch.object(ingest, "process_new_tracker", side_effect=process), patch.object(ingest, "save_deque_to_disk"):
            ingest.process_submitted_queue()
        assert seen[0] == ["udp://a.example:1/announce", "udp://b.example:2/announce"]  # a is in flight, still saved
        assert seen[1] == ["udp://b.example:2/announce"]
        assert _saved() == []

    def test_crash_while_processing_keeps_nothing_stuck(self) -> None:
        ingest.submitted_queue.put_nowait(_tracker("udp://a.example:1/announce"))
        with patch.object(ingest, "process_new_tracker", side_effect=RuntimeError("boom")), patch.object(ingest, "save_deque_to_disk"):
            with pytest.raises(RuntimeError):
                ingest.process_submitted_queue()
        assert _saved() == [] and ingest._in_flight == []

    def test_not_written_while_restoring(self) -> None:
        Path(ingest.QUEUE_FILE).write_text(json.dumps(["udp://old.example:1/announce"]))
        ingest._restoring[0] = True
        ingest.submitted_queue.put_nowait(_tracker("udp://new.example:1/announce"))
        ingest.save_queue()
        assert _saved() == ["udp://old.example:1/announce"]


class TestRestore:
    def test_restores_saved_urls_through_the_normal_checks(self) -> None:
        urls = ["udp://a.example:1/announce", "udp://b.example:2/announce", "udp://c.example:3/announce"]
        Path(ingest.QUEUE_FILE).write_text(json.dumps(urls))
        with patch.object(ingest, "add_one_tracker_to_submitted_queue") as add:
            ingest.restore_saved_queue()
        assert [c.args[0] for c in add.call_args_list] == urls

    def test_round_trip(self) -> None:
        urls = ["udp://a.example:1/announce", "http://b.example:2/announce"]
        Path(ingest.QUEUE_FILE).write_text(json.dumps(urls))
        with patch.object(ingest, "add_one_tracker_to_submitted_queue", side_effect=_accept_everything):
            ingest.restore_saved_queue()
        assert [t.url for t in list(ingest.submitted_queue.queue)] == urls
        assert _saved() == urls and ingest._restoring[0] is False

    def test_one_bad_url_doesnt_stop_the_rest(self) -> None:
        Path(ingest.QUEUE_FILE).write_text(json.dumps(["udp://a.example:1/announce", "udp://b.example:2/announce"]))
        calls: list[str] = []

        def add(url: str) -> None:
            calls.append(url)
            if "a.example" in url:
                raise RuntimeError("boom")

        with patch.object(ingest, "add_one_tracker_to_submitted_queue", side_effect=add):
            ingest.restore_saved_queue()
        assert calls == ["udp://a.example:1/announce", "udp://b.example:2/announce"] and ingest._restoring[0] is False

    @pytest.mark.parametrize("content", [None, "", "{not json", '{"a": 1}', "[1, 2]"])
    def test_missing_or_bad_file_is_ignored(self, content: str | None) -> None:
        if content is not None:
            Path(ingest.QUEUE_FILE).write_text(content)
        with patch.object(ingest, "add_one_tracker_to_submitted_queue") as add:
            ingest.restore_saved_queue()
        add.assert_not_called()
        assert ingest.submitted_queue.empty() and ingest._restoring[0] is False

    def test_run_starts_the_restore(self) -> None:
        assert "ingest.restore_saved_queue" in Path(__file__).parents[2].joinpath("run.py").read_text()
