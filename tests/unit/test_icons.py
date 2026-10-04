"""Every Font Awesome icon the templates and scripts use must be in the self-hosted subset (static/css/fa-subset.css)."""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "newtrackon"
NOT_ICONS = {"fa-solid", "fa-regular", "fa-brands", "fa-fw", "fa-spin", "fa-lg", "fa-sm", "fa-xs", "fa-2x", "fa-3x", "fa-subset"}  # fa-subset: the stylesheet's file name


def test_all_used_icons_are_in_the_subset() -> None:
    css = (PKG / "static/css/fa-subset.css").read_text()
    defined = set(re.findall(r"\.(fa-[a-z0-9-]+)(?=[,{])", css))
    used: set[str] = set()
    for f in list((PKG / "tpl").rglob("*.jinja")) + [p for p in (PKG / "static/js").glob("*.js") if ".min." not in p.name]:
        used |= set(re.findall(r"(?<![a-z-])(fa-[a-z0-9-]+)", f.read_text()))
    used -= NOT_ICONS
    assert not (used - defined), f"icons used but not in fa-subset.css: {sorted(used - defined)}"


def test_fonts_are_self_hosted_and_small() -> None:
    for name in ("fa-solid-sub.woff2", "fa-brands-sub.woff2"):
        f = PKG / "static/fonts" / name
        assert f.exists() and 0 < f.stat().st_size < 20_000, name
    css = (PKG / "static/css/fa-subset.css").read_text()
    assert "../fonts/fa-solid-sub.woff2" in css and "cdnjs" not in css
