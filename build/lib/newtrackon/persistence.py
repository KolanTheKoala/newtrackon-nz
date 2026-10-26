from __future__ import annotations

from collections import deque
from typing import TypedDict, cast


class HistoryData(TypedDict):
    url: str
    time: int
    status: int
    ip: str
    info: list[str] | str


raw_history_file = "data/raw_data.json"
submitted_history_file = "data/submitted_data.json"


def load_history(filename: str) -> list[HistoryData]:
    """An unreadable file is moved aside and logged (newtrackon.store), never a crash at start."""
    from newtrackon import store
    d = store.load(filename, default=[])
    return cast("list[HistoryData]", d if isinstance(d, list) else [])


raw_data: deque[HistoryData] = deque(load_history(raw_history_file), maxlen=600)
submitted_data: deque[HistoryData] = deque(load_history(submitted_history_file), maxlen=600)


def save_deque_to_disk(obj: deque[HistoryData], filename: str) -> None:
    from newtrackon import store
    store.save(list(obj), filename)  # atomic and thread-safe; never raises
