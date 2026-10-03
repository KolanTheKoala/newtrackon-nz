"""Region filters for the API lists: where a tracker is ("located in") and how fast it answers from there ("fast from").

Regions follow how the internet is wired rather than continents: the Middle East, Africa, Russia and Central Asia
mostly exchange traffic in Europe (Marseille, Frankfurt, London, Amsterdam, Stockholm); South and Central America
mostly reach the world through North America (Miami); Asia and Oceania share their hubs (Singapore, Hong Kong,
Tokyo, Sydney). "Fast from" uses the four places latency is measured from: americas, europe, asia, oceania.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import NamedTuple

REGIONS: dict[str, str] = {
    "americas": "Americas",
    "europe": "Europe, Middle East & Africa",
    "asia-pacific": "Asia-Pacific",
}

# "Fast from" uses the places latency is actually measured from (keys of the per-tracker region latency data).
# Asia and Oceania are separate here: no tracker is close to both (NZ to Asia is 250+ ms), so one combined speed
# list would either leave out NZ trackers or hand Asian users trackers that are slow for them.
FAST_FROM: dict[str, str] = {
    "americas": "Americas",
    "europe": "Europe, Middle East & Africa",
    "asia": "Asia",
    "oceania": "Oceania",
}
MEASURED_FROM: dict[str, tuple[str, ...]] = {
    "americas": ("North America",),
    "europe": ("Europe",),
    "asia": ("Asia",),
    "oceania": ("Oceania",),
    "asia-pacific": ("Asia", "Oceania"),  # legacy (first version): fast from both, 250 ms; kept so old links still work
}

# Default "fast" limit per region. Asia-Pacific spans the Pacific (Singapore-NZ ~130 ms, Tokyo-NZ ~170 ms), so almost
# nothing is under 150 ms from both its probe points; 250 ms keeps the list useful. Override with fast_from_ms=.
DEFAULT_FAST_FROM_MS: dict[str, int] = {"americas": 150, "europe": 150, "asia": 150, "oceania": 150, "asia-pacific": 250}

_AMERICAS = """ag ai ar aw bb bl bm bo bq br bs bz ca cl co cr cu cw dm do ec fk gd gf gp gs gt gy hn ht jm kn ky lc
    mf mq ms mx ni pa pe pm pr py sr sv sx tc tt um us uy vc ve vg vi"""
_EUROPE = """ad al at ax ba be bg by ch cy cz de dk ee es fi fo fr gb gg gi gl gr hr hu ie im is it je li lt lu lv mc
    md me mk mt nl no pl pt ro rs ru se si sj sk sm ua va xk
    am az ge kg kz tj tm uz
    ae bh il iq ir jo kw lb om ps qa sa sy tr ye
    ao bf bi bj bw cd cf cg ci cm cv dj dz eg eh er et ga gh gm gn gq gw ke km lr ls ly ma mg ml mr mu mw mz na ne
    ng re rw sc sd sh sl sn so ss st sz td tg tn tz ug yt za zm zw"""
_ASIA_PACIFIC = """af bd bn bt cn hk id in io jp kh kp kr la lk mm mn mo mv my np ph pk sg th tl tw vn
    as au cc ck cx fj fm gu ki mh mp nc nf nr nu nz pf pg pn pw sb tk to tv vu wf ws"""
# No region: Antarctica and uninhabited territories, and pseudo-codes some geolocation services return.
NO_REGION = frozenset("aq bv hm tf eu ap".split())

COUNTRY_REGION: dict[str, str] = {
    cc: region
    for region, codes in (("americas", _AMERICAS), ("europe", _EUROPE), ("asia-pacific", _ASIA_PACIFIC))
    for cc in codes.split()
}


def regions_of(country_codes: Iterable[str] | None) -> set[str]:
    """Regions a tracker is located in: one per IP address country (a multi-homed tracker can be in several)."""
    return {r for cc in (country_codes or []) if (r := COUNTRY_REGION.get(str(cc).strip().lower()))}


class RegionFilter(NamedTuple):
    located_in: frozenset[str]  # empty = no location filter
    fast_from: str | None
    fast_from_ms: int | None  # None = the region's default

    @property
    def active(self) -> bool:
        return bool(self.located_in or self.fast_from)

    def matches(self, url: str, country_codes: Iterable[str] | None, region_latency: Mapping[str, Mapping[str, float]]) -> bool:
        if self.located_in and not (regions_of(country_codes) & self.located_in):
            return False
        if self.fast_from:
            lat = region_latency.get(url) or {}
            points = MEASURED_FROM[self.fast_from]
            ms = [lat.get(p) for p in points]
            if any(not isinstance(x, (int, float)) for x in ms):
                return False  # not measured (yet) from every point in the region: not known to be fast
            limit = self.fast_from_ms or DEFAULT_FAST_FROM_MS[self.fast_from]
            if max(ms) >= limit:  # type: ignore[type-var]
                return False
        return True


NO_FILTER = RegionFilter(frozenset(), None, None)


def _valid() -> str:
    return ", ".join(REGIONS)


def parse_region_filter(args: Mapping[str, str]) -> RegionFilter:
    """Read region / fast_from / fast_from_ms from query arguments. Raises ValueError with a user-facing message."""
    located: set[str] = set()
    for part in (args.get("region") or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in REGIONS:
            raise ValueError(f"Unknown region '{part.strip()}'. Valid regions: {_valid()}")
        located.add(name)
    fast = (args.get("fast_from") or "").strip().lower() or None
    if fast is not None and fast not in MEASURED_FROM:
        raise ValueError(f"Unknown fast_from '{args.get('fast_from', '').strip()}'. Valid: {', '.join(FAST_FROM)}")
    raw_ms = (args.get("fast_from_ms") or "").strip()
    try:
        ms = int(raw_ms) if raw_ms else None
    except ValueError:
        raise ValueError("fast_from_ms must be a whole number of milliseconds") from None
    if ms is not None and not 1 <= ms <= 2000:
        raise ValueError("fast_from_ms must be between 1 and 2000")
    return RegionFilter(frozenset(located), fast, ms)
