from __future__ import annotations

import json
from collections import deque
from os import path, replace
from threading import get_ident
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
    if not path.exists(filename):
        return []
    with open(filename) as history_file:
        return cast("list[HistoryData]", json.load(history_file))


raw_data: deque[HistoryData] = deque(load_history(raw_history_file), maxlen=600)
submitted_data: deque[HistoryData] = deque(load_history(submitted_history_file), maxlen=600)


def save_deque_to_disk(obj: deque[HistoryData], filename: str) -> None:
    # write a temp file then rename, so a kill mid-write can't leave a truncated file that breaks startup
    tmp = f"{filename}.{get_ident()}.tmp"
    with open(tmp, "w") as history_file:
        json.dump(list(obj), history_file)
    replace(tmp, filename)
