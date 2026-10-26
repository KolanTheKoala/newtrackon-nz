"""Write sample .torrent files and their expected info hashes for tools.test.js (run before `node --test`).

Usage: python tests/js/make_fixtures.py <out_dir>
Generated independently of tools.js, with Python's own bencoder, so the JS is checked against an outside reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys


def benc(v: object) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted(((k.encode() if isinstance(k, str) else k), x) for k, x in v.items())
        return b"d" + b"".join(benc(k) + benc(x) for k, x in items) + b"e"
    raise TypeError(type(v))


def main(out: str) -> None:
    os.makedirs(out, exist_ok=True)
    rnd = random.Random(42)
    pieces = bytes(rnd.randrange(256) for _ in range(20 * 8))  # binary, not valid UTF-8
    v1_info = {"name": "café ☕ test.bin", "piece length": 16384, "pieces": pieces, "length": 131072}
    v1 = {
        "announce": "udp://down.example:6969/announce",
        "announce-list": [
            ["udp://down.example:6969/announce", "udp://good.example:1337/announce"],
            ["udp://GOOD.example:1337/announce"],  # duplicate (host case differs)
            ["http://unlisted.example:80/announce"],
        ],
        "comment": "Ünïcode comment",
        "created by": "fixture",
        "creation date": 1700000000,
        "url-list": ["https://webseed.example/file.bin"],
        "info": v1_info,
    }
    hybrid_info = {
        "name": "hybrid",
        "piece length": 16384,
        "meta version": 2,
        "file tree": {"a.bin": {"": {"length": 32768, "pieces root": bytes(rnd.randrange(256) for _ in range(32))}}},
        "pieces": bytes(rnd.randrange(256) for _ in range(40)),
        "length": 32768,
    }
    hybrid = {
        "announce": "udp://good.example:1337/announce",
        "info": hybrid_info,
        "piece layers": {bytes(rnd.randrange(256) for _ in range(32)): bytes(rnd.randrange(256) for _ in range(64))},
    }
    private = {"announce": "https://private.example/announce?passkey=abc", "info": dict(v1_info, private=1)}
    bare = {"info": dict(v1_info, name="no trackers")}

    manifest = {}
    for name, t in (("v1", v1), ("hybrid", hybrid), ("private", private), ("bare", bare)):
        data = benc(t)
        with open(os.path.join(out, name + ".torrent"), "wb") as f:
            f.write(data)
        info = benc(t["info"])
        manifest[name] = {
            "sha1": hashlib.sha1(info).hexdigest(),
            "sha256": hashlib.sha256(info).hexdigest() if t["info"].get("meta version") == 2 else None,
            "top_keys": sorted(k for k in t),
        }
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
