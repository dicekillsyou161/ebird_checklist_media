"""Fetch public Macaulay Library photos for an eBird checklist.

Data source: the public JSON search API behind media.ebird.org (Macaulay
Library media search), filtered by checklist via ``subId``. No API key is
required, but requests must send a non-browser User-Agent and
``Accept: application/json`` — browser-like clients are served an
interactive anti-bot challenge instead of JSON.

Run standalone to test without Discord:

    python ebird_media.py S378216909
    python ebird_media.py -vvv S378216909   # include each photo's metadata
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

import aiohttp

API_URL = "https://media.ebird.org/api/v2/search"
ASSET_PAGE = "https://macaulaylibrary.org/asset/{asset_id}"
TAXON_FIND_URL = "https://api.ebird.org/v2/ref/taxon/find"
REGION_FIND_URL = "https://api.ebird.org/v2/ref/region/find"
REGION_LIST_URL = "https://api.ebird.org/v2/ref/region/list/{kind}/{parent}"
SPPLIST_URL = "https://api.ebird.org/v2/product/spplist/{region_code}"
TAXONOMY_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird"
NOTABLE_URL = "https://api.ebird.org/v2/data/obs/{region_code}/recent/notable"
REGION_INFO_URL = "https://api.ebird.org/v2/ref/region/info/{region_code}"
HISTORIC_URL = "https://api.ebird.org/v2/data/obs/{region_code}/historic/{y}/{m}/{d}"
DAY_STATS_URL = "https://api.ebird.org/v2/product/stats/{region_code}/{y}/{m}/{d}"
NOTABLE_MAX_DAYS = 30  # eBird's cap on the `back` window
RARE_SCAN_MAX = 120    # most candidate reports to check for usable media
# A fetched notable feed is reused for this long, shared by every caller. Keep
# it well under the alert poll interval so alerts never run on stale data.
NOTABLE_CACHE_SECONDS = 120
_NOTABLE_CACHE: dict[tuple[str, int], tuple[float, list[dict]]] = {}

# Where eBird's regional review stands on a flagged record.
STATUS_CONFIRMED = "confirmed"  # a reviewer accepted it
STATUS_PENDING = "pending"      # flagged, nobody has looked yet
STATUS_REJECTED = "rejected"    # reviewed and not accepted

# Rarity tiers by seasonal report frequency: of ~30 sampled days in the same
# season across the prior RARITY_YEARS years, on what share was the species
# reported (reviewer-accepted records) in this region? This mirrors eBird's own
# frequency measure and its season-specific filters; prior years only, so a
# mega that fifty people are chasing right now can't read as common.
RARITY_TIERS = (
    (0.1, "Mega rarity", "🔴"),   # never reported in the sampled season-days
    (7.0, "Very rare", "🟠"),     # 1-2 of ~30 days
    (20.0, "Rare", "🟡"),         # up to ~1 day in 5
    (45.0, "Scarce", "🟢"),       # up to ~4 days in 9
    (float("inf"), "Locally notable", "⚪"),
)
RARITY_YEARS = 6              # how many prior years the season sample spans
RARITY_OFFSETS = (-14, -7, 0, 7, 14)  # sampled days around today's date, each year
RARITY_MIN_EFFORT = 60        # checklists across the sampled days; below this a
                              # county's baseline escalates to its state, etc.
# eBird's own key for its public web autocomplete widgets (not a personal API key)
EBIRD_WEB_KEY = "jfekjedvescr"
SORT_BEST = "rating_rank_desc"     # Macaulay "Best quality" ranking
SORT_RECENT = "upload_date_desc"   # most recently uploaded
SORT_OBS = "obs_date_desc"         # most recent observation date/time
USER_AGENT = "ebird-checklist-discord-bot/1.0"
PAGE_SIZE = 100
MAX_PHOTOS = 400  # safety cap so one request can't paginate forever
GROUP_GLOBAL_MAX = 40  # most species a userless group:True search will fan out to
VERBOSE_FLAG = "-vvv"          # add every metadata detail
COMPACT_CAMERA_FLAG = "-c"     # compact + key camera settings + observed/location
COMPACT_BRIEF_FLAG = "-cc"     # compact + focal length + observed/location
COMPACT_FLAG = "-ccc"          # cut to species + links + rating only
FLAGS = {VERBOSE_FLAG, COMPACT_CAMERA_FLAG, COMPACT_BRIEF_FLAG, COMPACT_FLAG}

# What each partial-compact flag adds on top of the species+links+rating base.
# ("camera", …) labels come from the asset page EXIF; ("base", …) from the search API.
COMPACT_SELECTIONS = {
    COMPACT_CAMERA_FLAG: (
        ("camera", "Focal length"),
        ("camera", "Exposure"),
        ("camera", "Aperture"),
        ("camera", "ISO"),
        ("base", "Observed"),
        ("base", "Location"),
    ),
    COMPACT_BRIEF_FLAG: (
        ("camera", "Focal length"),
        ("base", "Observed"),
        ("base", "Location"),
    ),
    COMPACT_FLAG: (),
}

_CHECKLIST_RE = re.compile(r"\bS(\d{4,})\b", re.IGNORECASE)
_AGE_SEX_RE = re.compile(r"(adult|immature|juvenile|unknown)(Female|Male|Unknown)Count")
_ASSET_URL_RE = re.compile(r"asset/(\d{5,})", re.IGNORECASE)
_ASSET_ML_RE = re.compile(r"\bML(\d{5,})\b", re.IGNORECASE)
_ASSET_BARE_RE = re.compile(r"\b(\d{5,})\b")
_USER_ID_RE = re.compile(r"\bUSER(\d+)\b", re.IGNORECASE)
_PROFILE_URL_RE = re.compile(r"/profile/[A-Za-z0-9_\-=%]+")
_REGION_CODE_RE = re.compile(r"^[A-Za-z]{2}(-[A-Za-z0-9]{1,5}){0,2}$")

# The asset page embeds parsed EXIF as {description:"…",exifTagCode:"…"} pairs
# inside its minified Nuxt state. A pair whose value the minifier hoisted into
# a variable (rare) is simply skipped.
_EXIF_ARRAY_RE = re.compile(r"[\"']?exif[\"']?:\[")
_EXIF_PAIR_RE = re.compile(r'\{description:"((?:[^"\\]|\\.)*)",exifTagCode:"([a-z0-9_]+)"\}')
_EXIF_LABELS = {
    "make": "Camera make",
    "model": "Camera model",
    "lens_model": "Lens",
    "focal_length": "Focal length",
    "exposure_time": "Exposure",
    "f_number": "Aperture",
    "iso": "ISO",
    "flash": "Flash",
    "create_dt": "Taken",
    "latitude": "GPS latitude",
    "longitude": "GPS longitude",
}
_EXIF_ORDER = list(_EXIF_LABELS)
# width/height already appear as the search API's Size field; shutter_speed is
# exposure_time again, rounded differently ("1/2499 sec" vs "1/2500 sec").
_EXIF_SKIP = {"width", "height", "shutter_speed"}


class ChecklistError(Exception):
    """A checklist ID couldn't be parsed, or the photo lookup failed."""


@dataclass(frozen=True)
class Photo:
    asset_id: int
    common_name: str
    sci_name: str
    photographer: str
    obs_date: str
    location: str
    rating: float | None
    unconfirmed: bool = False  # rarity still pending eBird regional review
    rating_count: int | None = None
    species_code: str = ""
    license_id: str = ""
    width: int | None = None
    height: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    obs_time: str = ""
    age_sex: str = ""
    notes: str = ""
    tags: str = ""
    user_id: str = ""  # eBird USER… ID of the photographer

    @property
    def asset_url(self) -> str:
        return f"https://macaulaylibrary.org/asset/{self.asset_id}"

    def image_url(self, size: int = 1200) -> str:
        return f"https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{self.asset_id}/{size}"

    @property
    def rating_display(self) -> str:
        if not self.rating:
            return ""
        text = f"{round(self.rating, 1):g}/5"
        if self.rating_count:
            plural = "s" if self.rating_count != 1 else ""
            text += f" ({self.rating_count} rating{plural})"
        return text

    def metadata_fields(self, *, markdown: bool = True) -> list[tuple[str, str]]:
        """(label, value) pairs for every non-empty metadata detail."""
        coords = ""
        if self.latitude is not None and self.longitude is not None:
            plain = f"{self.latitude:.5f}, {self.longitude:.5f}"
            coords = (
                f"[{plain}](https://www.google.com/maps/search/?api=1"
                f"&query={self.latitude},{self.longitude})"
            ) if markdown else plain
        fields = [
            ("Observed", " · ".join(bit for bit in (self.obs_date, self.obs_time) if bit)),
            ("Location", self.location),
            ("Coordinates", coords),
            ("Age / sex", self.age_sex),
            ("Rating", self.rating_display),
            ("Size", f"{self.width} × {self.height} px" if self.width and self.height else ""),
            ("License", self.license_id),
            ("Species code", self.species_code),
            ("Notes", self.notes),
            ("Tags", self.tags),
        ]
        return [(label, value) for label, value in fields if value]


@dataclass(frozen=True)
class AssetDetails:
    photo: Photo
    media_type: str
    checklist_id: str
    exif: tuple[tuple[str, str], ...]  # (label, value) camera metadata


@dataclass(frozen=True)
class RareReport:
    common_name: str
    sci_name: str
    species_code: str
    obs_dt: str          # "2026-08-13 17:48" as eBird reports it
    location: str
    observer: str
    checklist_id: str
    reports_in_window: int   # other reports of this species in the same window
    rarity_label: str
    rarity_emoji: str
    rarity_share: float | None  # % of sampled season-days with a report, None if unknown
    rarity_note: str            # e.g. "2+ confirmed reports in season since 2020 in US-WA-033"
    details: AssetDetails | None   # None in text mode, where photos aren't fetched
    status: str = STATUS_CONFIRMED
    latitude: float | None = None
    longitude: float | None = None
    how_many: int | None = None    # birds reported, when the observer counted
    county: str = ""               # subnational2Name, e.g. "King"
    state: str = ""                # subnational1Name, e.g. "Washington"

    @property
    def checklist_url(self) -> str:
        return f"https://ebird.org/checklist/{self.checklist_id}"

    @property
    def map_url(self) -> str:
        if self.latitude is None or self.longitude is None:
            return ""
        return (
            "https://www.google.com/maps/search/?api=1"
            f"&query={self.latitude},{self.longitude}"
        )

    @property
    def confirmed(self) -> bool:
        return self.status == STATUS_CONFIRMED

    @property
    def status_display(self) -> str:
        """eBird's own terms: a record is Confirmed or Unconfirmed until rejected."""
        if self.status == STATUS_CONFIRMED:
            return "✅ Confirmed"
        if self.status == STATUS_REJECTED:
            return "❌ Not accepted"
        return "⏳ Unconfirmed"

    @property
    def rarity_display(self) -> str:
        if self.rarity_share is None:
            return self.rarity_label
        return f"{self.rarity_label} · {self.rarity_note}"


@dataclass(frozen=True)
class UserMedia:
    display_name: str
    user_id: str
    species_code: str     # "" when not filtered by species
    species_display: str  # e.g. "Black Oystercatcher - Haematopus bachmani"
    details: list[AssetDetails]
    region: str = ""      # eBird region code when the search was region-limited


def parse_asset_id(text: str) -> int:
    """Accept a Macaulay Library asset URL, an ML number, or bare digits."""
    for pattern in (_ASSET_URL_RE, _ASSET_ML_RE):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    if _CHECKLIST_RE.search(text):
        raise ChecklistError(
            "That looks like an eBird *checklist* — use `/checklist` for those. "
            "This wants a Macaulay Library asset, e.g. "
            "`https://macaulaylibrary.org/asset/662698120` or `ML662698120`."
        )
    match = _ASSET_BARE_RE.search(text)
    if match:
        return int(match.group(1))
    raise ChecklistError(
        "Couldn't find a Macaulay Library asset in that. Pass a link like "
        "`https://macaulaylibrary.org/asset/662698120`, or `ML662698120`."
    )


def _extract_exif(page_html: str) -> tuple[tuple[str, str], ...]:
    match = _EXIF_ARRAY_RE.search(page_html)
    if not match:
        return ()
    window = page_html[match.end():match.end() + 8000]
    tags: dict[str, str] = {}
    for raw_desc, code in _EXIF_PAIR_RE.findall(window):
        if code not in _EXIF_SKIP:
            tags.setdefault(code, json.loads(f'"{raw_desc}"'))
    ordered = [c for c in _EXIF_ORDER if c in tags] + [c for c in tags if c not in _EXIF_ORDER]
    return tuple(
        (_EXIF_LABELS.get(code, code.replace("_", " ").capitalize()), tags[code])
        for code in ordered
    )


async def _search(session: aiohttp.ClientSession, **params) -> list[dict]:
    """One call to the media search API, returning the raw item list."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    query = {key: str(value) for key, value in params.items()}
    async with session.get(
        API_URL, params=query, headers=headers, timeout=aiohttp.ClientTimeout(total=25)
    ) as resp:
        if resp.status != 200:
            raise ChecklistError(
                f"Macaulay Library search returned HTTP {resp.status} — try again in a minute."
            )
        if "json" not in (resp.headers.get("Content-Type") or ""):
            raise ChecklistError(
                "Macaulay Library returned a non-JSON response "
                "(possibly an anti-bot challenge — see README notes on the User-Agent)."
            )
        page = await resp.json()
    if not isinstance(page, list):
        raise ChecklistError("Unexpected response shape from Macaulay Library search.")
    return page


async def _fetch_exif(
    session: aiohttp.ClientSession, asset_id: int
) -> tuple[tuple[str, str], ...]:
    """Camera metadata from the asset page; best-effort, empty on any failure."""
    try:
        async with session.get(
            ASSET_PAGE.format(asset_id=asset_id),
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status == 200:
                return _extract_exif(await resp.text())
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return ()


def _item_to_details(item: dict, exif: tuple[tuple[str, str], ...]) -> AssetDetails:
    return AssetDetails(
        photo=_to_photo(item),
        media_type=item.get("mediaType") or "",
        checklist_id=item.get("ebirdChecklistId") or "",
        exif=exif,
    )


async def fetch_asset_details(
    asset_id: int, *, session: aiohttp.ClientSession | None = None
) -> AssetDetails:
    """Look up one Macaulay Library asset: search-API metadata + page EXIF."""
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        page = await _search(session, assetId=asset_id, unconfirmed="incl")
        # the API silently ignores unknown params, so confirm we got *this* asset
        item = next((it for it in page if it.get("assetId") == asset_id), None)
        if item is None:
            raise ChecklistError(
                f"No public Macaulay Library asset `ML{asset_id}` found — "
                "it may be restricted, deleted, or the number may be wrong."
            )
        return _item_to_details(item, await _fetch_exif(session, asset_id))
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ChecklistError(f"Couldn't reach Macaulay Library ({error.__class__.__name__}).") from error
    finally:
        if owns_session:
            await session.close()


async def resolve_user(text: str, *, session: aiohttp.ClientSession) -> str:
    """Turn user input into a USER… ID: accepts the ID itself, bare digits,
    or any Macaulay Library asset link/number by that person."""
    match = _USER_ID_RE.search(text)
    if match:
        return f"USER{match.group(1)}"
    if _PROFILE_URL_RE.search(text):
        raise ChecklistError(
            "eBird profile pages need a sign-in, so I can't read the user ID from "
            "that link. Pass one of their Macaulay Library asset links instead "
            "(e.g. `ML662698120`), or their `USER…` ID."
        )
    if _ASSET_URL_RE.search(text) or _ASSET_ML_RE.search(text):
        asset_id = parse_asset_id(text)
        page = await _search(session, assetId=asset_id, unconfirmed="incl")
        item = next((it for it in page if it.get("assetId") == asset_id), None)
        if item and item.get("userId"):
            return item["userId"]
        raise ChecklistError(f"Couldn't find the photographer of `ML{asset_id}`.")
    digits = re.fullmatch(r"\s*(\d{4,})\s*", text)
    if digits:
        return f"USER{digits.group(1)}"
    raise ChecklistError(
        "Couldn't work out an eBird user from that. Pass their `USER…` ID or "
        "one of their Macaulay Library asset links (e.g. `ML662698120`)."
    )


async def _find_taxa(
    query: str, session: aiohttp.ClientSession, *, cat: str = "species", limit: int = 25
) -> list[dict]:
    """Raw fuzzy taxon matches [{code, name}, …] for a name query."""
    params = {"locale": "en", "cat": cat, "limit": str(limit), "key": EBIRD_WEB_KEY, "q": query}
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    async with session.get(
        TAXON_FIND_URL, params=params, headers=headers,
        timeout=aiohttp.ClientTimeout(total=25),
    ) as resp:
        if resp.status != 200:
            raise ChecklistError(f"Species lookup returned HTTP {resp.status} — try again in a minute.")
        matches = await resp.json(content_type=None)
    return matches if isinstance(matches, list) else []


def _merge_key(sort: str):
    """Cross-species ordering for merged global-group results."""
    if sort == SORT_OBS:
        return lambda item: item.get("obsDt") or ""
    if sort == SORT_RECENT:
        return lambda item: item.get("assetId") or 0

    def quality(item: dict) -> float:
        # approximates Macaulay's Bayesian quality rank: pull low-vote
        # ratings toward a prior of 3.0 with weight 10
        rating = item.get("rating") or 0
        votes = item.get("ratingCount") or 0
        return (rating * votes + 3.0 * 10) / (votes + 10)

    return quality


_REGION_LIST_CACHE: dict[tuple[str, str], list[dict]] = {}
# words to drop when reading a place name: "king county wa" == "king wa"
_ADMIN_WORDS = {"county", "co", "parish", "borough", "municipality", "state", "province"}


async def _region_list(kind: str, parent: str, session: aiohttp.ClientSession) -> list[dict]:
    """[{code, name}, …] of a region's children (cached); empty if unavailable."""
    key = (kind, parent)
    if key in _REGION_LIST_CACHE:
        return _REGION_LIST_CACHE[key]
    rows: list[dict] = []
    try:
        async with session.get(
            REGION_LIST_URL.format(kind=kind, parent=parent),
            params={"key": EBIRD_WEB_KEY},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status == 200:
                payload = await resp.json(content_type=None)
                if isinstance(payload, list):
                    rows = payload
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    _REGION_LIST_CACHE[key] = rows
    return rows


async def _find_regions(query: str, session: aiohttp.ClientSession) -> list[dict]:
    """Raw fuzzy region matches; names look like 'King, Washington, United States (US)'."""
    async with session.get(
        REGION_FIND_URL, params={"q": query, "key": EBIRD_WEB_KEY},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=25),
    ) as resp:
        if resp.status != 200:
            return []
        matches = await resp.json(content_type=None)
    return matches if isinstance(matches, list) else []


def _first_segment(name: str) -> str:
    return name.split(",")[0].strip().lower()


async def _resolve_parent(part: str, session: aiohttp.ClientSession) -> str:
    """A state/province (or country) code for 'wa', 'washington', 'new york'."""
    text = part.strip().lower()
    if not text:
        return ""
    if len(text) == 2 and text.isalpha():
        countries = {row["code"] for row in await _region_list("country", "world", session)}
        if text.upper() in countries:
            return text.upper()
        states = {row["code"] for row in await _region_list("subnational1", "US", session)}
        if f"US-{text.upper()}" in states:  # a bare 2-letter US state abbreviation
            return f"US-{text.upper()}"
        return ""
    for row in await _region_list("subnational1", "US", session):
        if row["name"].lower() == text:
            return row["code"]
    for match in await _find_regions(text, session):
        if _first_segment(match["name"]) == text and match["code"].count("-") <= 1:
            return match["code"]
    return ""


async def _resolve_child(name: str, parent_code: str, session: aiohttp.ClientSession) -> str:
    """A county/subnational2 code by name within a state."""
    text = name.strip().lower()
    children = await _region_list("subnational2", parent_code, session)
    for row in children:
        if row["name"].lower() == text:
            return row["code"]
    partial = [row for row in children if row["name"].lower().startswith(text)]
    return partial[0]["code"] if len(partial) == 1 else ""


async def resolve_region(text: str, *, session: aiohttp.ClientSession) -> str:
    """Turn a code or place name into an eBird region code.

    Accepts 'US-WA-033', 'US-WA', 'WA', 'washington', and county-with-state
    forms like 'king county wa' or 'king county washington'.
    """
    raw = " ".join(text.replace(",", " ").split())
    if not raw:
        raise ChecklistError("Which region? Give a code (US-WA) or a name (king county wa).")
    if "-" in raw and _REGION_CODE_RE.fullmatch(raw):
        code = raw.upper()
        countries = {row["code"] for row in await _region_list("country", "world", session)}
        if countries and code.split("-")[0] not in countries:
            raise ChecklistError(
                f"“{raw}” isn't a valid eBird region code; codes start with a country "
                "like US-WA or US-WA-033."
            )
        return code

    words = [w for w in raw.lower().split()]
    trimmed = [w for w in words if w.strip(".") not in _ADMIN_WORDS] or words
    if len(trimmed) == 1 and len(trimmed[0]) == 2 and trimmed[0].isalpha():
        code = await _resolve_parent(trimmed[0], session)
        if code:
            return code
    # "<county> <state>": try each split, longest county name last
    for split in range(1, len(trimmed)):
        parent = await _resolve_parent(" ".join(trimmed[split:]), session)
        if not parent:
            continue
        child = await _resolve_child(" ".join(trimmed[:split]), parent, session)
        if child:
            return child

    # whole-name matches the finder handles badly: it ranks a same-named county
    # above a state, and never returns countries at all
    whole = " ".join(trimmed)
    for row in await _region_list("subnational1", "US", session):
        if row["name"].lower() == whole:
            return row["code"]
    for row in await _region_list("country", "world", session):
        if row["name"].lower() == whole:
            return row["code"]

    matches = await _find_regions(raw, session)
    if matches:
        query = raw.lower()
        ranked = sorted(
            enumerate(matches),
            key=lambda pair: (_first_segment(pair[1]["name"]) != query,
                              pair[1]["code"].count("-"), pair[0]),
        )
        best = ranked[0][1]
        if _first_segment(best["name"]) == query:
            rivals = [
                m for _, m in ranked[1:]
                if _first_segment(m["name"]) == query
                and m["code"].count("-") == best["code"].count("-")
            ]
            if rivals:
                names = "; ".join(m["name"] for m in [best] + rivals[:4])
                raise ChecklistError(
                    f"“{raw}” matches several regions; add a state or use a code. "
                    f"Candidates: {names}"
                )
        return best["code"]
    raise ChecklistError(
        f"Couldn't find region “{raw}”; use an eBird code (US, US-WA, US-WA-033), "
        "a name like “washington”, or a county with its state like “king county wa”."
    )


async def resolve_region_code(
    text: str, *, session: aiohttp.ClientSession | None = None
) -> str:
    """`resolve_region` for callers that don't already hold a session."""
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        return await resolve_region(text, session=session)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ChecklistError(f"Couldn't reach eBird ({error.__class__.__name__}).") from error
    finally:
        if owns_session:
            await session.close()


_REGION_NAME_CACHE: dict[str, str] = {}


async def region_name(region_code: str, *, session: aiohttp.ClientSession) -> str:
    """"Washington, United States" for US-WA; the code itself if eBird won't say."""
    if region_code in _REGION_NAME_CACHE:
        return _REGION_NAME_CACHE[region_code]
    name = region_code
    try:
        async with session.get(
            REGION_INFO_URL.format(region_code=region_code),
            params={"key": EBIRD_WEB_KEY},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                payload = await resp.json(content_type=None)
                if isinstance(payload, dict) and payload.get("result"):
                    name = str(payload["result"])
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass  # cosmetic; the code reads fine on its own
    _REGION_NAME_CACHE[region_code] = name
    return name


_REGION_SPECIES_CACHE: dict[str, frozenset[str]] = {}


async def _region_species(region_code: str, session: aiohttp.ClientSession) -> frozenset[str]:
    """Species codes ever recorded in a region (cached per process)."""
    cached = _REGION_SPECIES_CACHE.get(region_code)
    if cached is not None:
        return cached
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    async with session.get(
        SPPLIST_URL.format(region_code=region_code), params={"key": EBIRD_WEB_KEY},
        headers=headers, timeout=aiohttp.ClientTimeout(total=25),
    ) as resp:
        if resp.status != 200:
            raise ChecklistError(
                f"Unknown region `{region_code}` — use an eBird region code "
                "(US, US-WA, US-WA-033) or a region name."
            )
        codes = await resp.json(content_type=None)
    result = frozenset(codes) if isinstance(codes, list) else frozenset()
    _REGION_SPECIES_CACHE[region_code] = result
    return result


_REGION_TAXA_CACHE: dict[str, list[dict]] = {}


async def _region_taxa(region_code: str, session: aiohttp.ClientSession) -> list[dict]:
    """[{code, name}, …] for every species recorded in a region (cached).

    Built from the region's species list plus taxonomy names, because the
    global fuzzy finder caps at 150 matches in taxonomic order — filtering
    those by region would wrongly drop late-order families (e.g. warblers).
    """
    cached = _REGION_TAXA_CACHE.get(region_code)
    if cached is not None:
        return cached
    codes = sorted(await _region_species(region_code, session))
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    taxa: list[dict] = []
    for start in range(0, len(codes), 200):
        chunk = ",".join(codes[start:start + 200])
        async with session.get(
            TAXONOMY_URL, params={"fmt": "json", "species": chunk, "key": EBIRD_WEB_KEY},
            headers=headers, timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status != 200:
                raise ChecklistError(
                    f"Taxonomy lookup returned HTTP {resp.status} — try again in a minute."
                )
            rows = await resp.json(content_type=None)
        for row in rows or []:
            taxa.append({
                "code": row.get("speciesCode"),
                "name": f"{row.get('comName')} - {row.get('sciName')}",
            })
    _REGION_TAXA_CACHE[region_code] = taxa
    return taxa


async def resolve_species(
    query: str, *, session: aiohttp.ClientSession, regional_taxa: list[dict] | None = None
) -> tuple[str, str]:
    """Match a common or scientific name to (taxonCode, display name).

    An exact name match wins outright; otherwise a lone hit is used as-is,
    and multiple hits raise a did-you-mean error rather than silently
    searching the wrong species. With `regional_taxa`, candidates come from
    that region's species first, falling back to the global list.
    """
    lowered = query.strip().lower()
    if regional_taxa is not None:
        by_common = [t for t in regional_taxa if lowered in t["name"].partition(" - ")[0].lower()]
        by_sci = [t for t in regional_taxa if lowered in t["name"].partition(" - ")[2].lower()]
        pool = by_common or by_sci
        if pool:
            for match in pool:
                common, _, scientific = match["name"].partition(" - ")
                if lowered in (common.lower(), scientific.lower()):
                    return match["code"], match["name"]
            if len(pool) == 1:
                return pool[0]["code"], pool[0]["name"]
            preview = "; ".join(m["name"] for m in pool[:5])
            raise ChecklistError(
                f"“{query}” matches {len(pool)} species in that region — use a more "
                f"exact name, or set `group:True`. Closest matches: {preview} …"
            )
        # nothing in the region matched the text; fall back to the global list
    matches = await _find_taxa(query, session)
    if not matches:
        raise ChecklistError(
            f"No species matched “{query}” — try the exact common or scientific name."
        )
    for match in matches:
        common, _, scientific = match["name"].partition(" - ")
        if lowered in (common.lower(), scientific.lower()):
            return match["code"], match["name"]
    if len(matches) == 1:
        return matches[0]["code"], matches[0]["name"]
    more = "+" if len(matches) == 25 else ""
    preview = "; ".join(m["name"] for m in matches[:5])
    raise ChecklistError(
        f"“{query}” matches {len(matches)}{more} species — use a more exact name, "
        "or set `group:True` to search everything matching it in their library. "
        f"Closest matches: {preview} …"
    )


async def _paged_user_search(
    session: aiohttp.ClientSession, user_id: str, *, sort: str, all_media: bool,
    region_code: str = "",
) -> list[dict]:
    """Page through a user's media in server sort order, up to MAX_PHOTOS items."""
    items: list[dict] = []
    cursor: str | None = None
    while len(items) < MAX_PHOTOS:
        params: dict = {"userId": user_id, "sort": sort, "count": PAGE_SIZE, "unconfirmed": "incl"}
        if not all_media:
            params["mediaType"] = "photo"
        if region_code:
            params["regionCode"] = region_code
        if cursor:
            params["initialCursorMark"] = cursor
        page = await _search(session, **params)
        items.extend(page)
        if len(page) < PAGE_SIZE:
            break
        cursor = page[-1].get("cursorMark")
        if not cursor:
            break
    return items[:MAX_PHOTOS]


async def fetch_user_details(
    user_ref: str,
    *,
    count: int = 10,
    include_exif: bool = True,
    sort: str = SORT_BEST,
    species_query: str | None = None,
    species_group: bool = False,
    all_media: bool = False,
    region: str = "",
    session: aiohttp.ClientSession | None = None,
) -> UserMedia:
    """A user's public media, ranked by `sort`, optionally one species only.

    Photos only unless `all_media`. With `species_group`, `species_query` is
    a name substring matched against every species in the user's library
    (their most recent/best MAX_PHOTOS items) instead of one resolved taxon.
    EXIF fetches are skipped when the caller won't show camera fields.
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        user_id = ""
        if user_ref:
            user_id = await resolve_user(user_ref, session=session)
        elif not species_query:
            raise ChecklistError("Give me a user, a species, or both.")
        region_code = ""
        regional_taxa: list[dict] | None = None
        if region:
            region_code = await resolve_region(region, session=session)
            regional_taxa = await _region_taxa(region_code, session)
        species_code = species_display = ""
        if species_query and species_group and user_id:
            needle = species_query.strip().lower()
            species_display = f"“{species_query.strip()}”"
            everything = await _paged_user_search(
                session, user_id, sort=sort, all_media=all_media, region_code=region_code
            )
            # prefer common-name matches so "puffin" doesn't drag in Puffinus shearwaters
            by_common = [
                item for item in everything
                if needle in ((item.get("taxonomy") or {}).get("comName") or "").lower()
            ]
            by_sci = [
                item for item in everything
                if needle in ((item.get("taxonomy") or {}).get("sciName") or "").lower()
            ]
            items = (by_common or by_sci)[:count]
        elif species_query and species_group:
            # global group: one query per matched species, merged and re-ranked
            needle = species_query.strip().lower()
            if regional_taxa is not None:
                candidates = regional_taxa
            else:
                candidates = await _find_taxa(species_query, session, cat="species,spuh", limit=150)
                if not candidates:
                    raise ChecklistError(
                        f"No species matched “{species_query}” — try the exact common or scientific name."
                    )
            # prefer common-name matches so "puffin" doesn't drag in Puffinus shearwaters
            by_common = [m for m in candidates if needle in m["name"].partition(" - ")[0].lower()]
            by_sci = [m for m in candidates if needle in m["name"].partition(" - ")[2].lower()]
            named = by_common or by_sci
            if not named:
                if regional_taxa is not None:
                    raise ChecklistError(
                        f"No species matching “{species_query}” has been recorded in `{region_code}`."
                    )
                named = candidates
            if len(named) > GROUP_GLOBAL_MAX:
                more = "+" if len(candidates) == 150 else ""
                raise ChecklistError(
                    f"“{species_query}” is too broad for a group search here "
                    f"({len(named)}{more} taxa match) — add a user, narrow the group, "
                    "or use a smaller region."
                )
            species_display = f"“{species_query.strip()}” ({len(named)} taxa)"
            semaphore = asyncio.Semaphore(4)

            async def one_taxon(code: str) -> list[dict]:
                async with semaphore:
                    params: dict = {"sort": sort, "count": count, "unconfirmed": "incl", "taxonCode": code}
                    if not all_media:
                        params["mediaType"] = "photo"
                    if region_code:
                        params["regionCode"] = region_code
                    return await _search(session, **params)

            pages = await asyncio.gather(*(one_taxon(m["code"]) for m in named))
            merged = [item for page in pages for item in page]
            merged.sort(key=_merge_key(sort), reverse=True)
            items = merged[:count]
        else:
            if species_query:
                species_code, species_display = await resolve_species(
                    species_query, session=session, regional_taxa=regional_taxa
                )
            params: dict = {"sort": sort, "count": count, "unconfirmed": "incl"}
            if user_id:
                params["userId"] = user_id
            if not all_media:
                params["mediaType"] = "photo"
            if species_code:
                params["taxonCode"] = species_code
            if region_code:
                params["regionCode"] = region_code
            items = (await _search(session, **params))[:count]
        if region_code and species_display:
            species_display += f" in {region_code}"
        if include_exif and items:
            semaphore = asyncio.Semaphore(4)

            async def grab(item: dict) -> tuple[tuple[str, str], ...]:
                async with semaphore:
                    return await _fetch_exif(session, item["assetId"])

            exifs = await asyncio.gather(*(grab(item) for item in items))
        else:
            exifs = [()] * len(items)
        name = ""
        if user_id:
            name = (items[0].get("userDisplayName") if items else "") or user_id
        return UserMedia(
            display_name=name,
            user_id=user_id,
            species_code=species_code,
            species_display=species_display,
            details=[_item_to_details(i, e) for i, e in zip(items, exifs)],
            region=region_code,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ChecklistError(f"Couldn't reach Macaulay Library ({error.__class__.__name__}).") from error
    finally:
        if owns_session:
            await session.close()


def extract_flags(tokens: list[str]) -> tuple[set[str], list[str]]:
    """Split recognized flags (-vvv, -c, -cc, -ccc) out of user-supplied tokens."""
    present = {token for token in tokens if token in FLAGS}
    rest = [token for token in tokens if token not in FLAGS]
    return present, rest


def pick_compact_flag(flags: set[str]) -> str | None:
    """The compact-family flag to honor; the most-compact one wins if several."""
    for flag in (COMPACT_FLAG, COMPACT_BRIEF_FLAG, COMPACT_CAMERA_FLAG):
        if flag in flags:
            return flag
    return None


def select_fields(details: AssetDetails, flag: str) -> list[tuple[str, str, bool]]:
    """(label, value, is_camera) rows for a partial-compact selection, skipping absent data."""
    base = dict(details.photo.metadata_fields(markdown=False))
    camera = dict(details.exif)
    rows = []
    for source, label in COMPACT_SELECTIONS.get(flag, ()):
        value = (camera if source == "camera" else base).get(label)
        if value:
            rows.append((label, value, source == "camera"))
    return rows


def parse_checklist_id(text: str) -> str:
    """Accept a bare ID (S378216909) or any eBird checklist URL containing one."""
    match = _CHECKLIST_RE.search(text)
    if not match:
        raise ChecklistError(
            "Couldn't find a checklist ID in that. Pass an ID like `S378216909` "
            "or a URL like `https://ebird.org/checklist/S378216909`."
        )
    return f"S{match.group(1)}"


def _summarize_age_sex(age_sex: dict) -> str:
    parts = []
    for key, count in age_sex.items():
        if not count:
            continue
        match = _AGE_SEX_RE.fullmatch(key)
        if not match:
            continue
        age, sex = match.group(1), match.group(2).lower()
        words = [word for word in (age, sex) if word != "unknown"]
        parts.append(f"{count} {' '.join(words) or 'unreported'}")
    return " · ".join(parts)


def _format_obs_time(raw) -> str:
    if isinstance(raw, int) and raw > 0:
        return f"{raw // 100:02d}:{raw % 100:02d}"
    return ""


def _to_photo(item: dict) -> Photo:
    taxonomy = item.get("taxonomy") or {}
    location = item.get("location") or {}
    place_bits = [location.get("name"), location.get("subnational1Name"), location.get("countryName")]
    tags = item.get("tags")
    if isinstance(tags, list):
        tags = ", ".join(str(tag) for tag in tags)
    return Photo(
        asset_id=item["assetId"],
        common_name=taxonomy.get("comName") or taxonomy.get("sciName") or "Unknown species",
        sci_name=taxonomy.get("sciName") or "",
        photographer=item.get("userDisplayName") or "Unknown photographer",
        obs_date=item.get("obsDtDisplay") or "",
        location=", ".join(bit for bit in place_bits if bit),
        rating=item.get("rating"),
        unconfirmed=item.get("valid") is False,
        rating_count=item.get("ratingCount"),
        species_code=taxonomy.get("speciesCode") or "",
        license_id=item.get("licenseId") or "",
        width=item.get("width"),
        height=item.get("height"),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        obs_time=_format_obs_time(item.get("obsTime")),
        age_sex=_summarize_age_sex(item.get("ageSex") or {}),
        notes="; ".join(bit for bit in (item.get("caption"), item.get("mediaNotes")) if bit),
        tags=tags or "",
        user_id=item.get("userId") or "",
    )


def _group_by_species(photos: list[Photo]) -> list[Photo]:
    """Keep species in first-seen order, but make each species' photos consecutive."""
    order: dict[str, int] = {}
    for photo in photos:
        order.setdefault(photo.common_name, len(order))
    return sorted(photos, key=lambda photo: order[photo.common_name])


async def fetch_checklist_photos(
    sub_id: str, *, session: aiohttp.ClientSession | None = None
) -> list[Photo]:
    """Return every public photo on the checklist, grouped by species."""
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        items: list[dict] = []
        cursor: str | None = None
        while len(items) < MAX_PHOTOS:
            # unconfirmed=incl keeps rarities that are still pending review,
            # which the search index otherwise omits
            params = {
                "subId": sub_id,
                "mediaType": "photo",
                "count": str(PAGE_SIZE),
                "unconfirmed": "incl",
            }
            if cursor:
                params["initialCursorMark"] = cursor
            page = await _search(session, **params)
            items.extend(page)
            if len(page) < PAGE_SIZE:
                break
            cursor = page[-1].get("cursorMark")
            if not cursor:
                break
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ChecklistError(f"Couldn't reach Macaulay Library ({error.__class__.__name__}).") from error
    finally:
        if owns_session:
            await session.close()
    return _group_by_species([_to_photo(item) for item in items[:MAX_PHOTOS]])


# (scope, "MM-DD") -> season baseline; today's date is part of the key, so a
# long-running bot naturally refreshes as the season moves
_SEASON_CACHE: dict[tuple[str, str], dict] = {}


def _season_day(year: int, month: int, day: int, offset: int) -> date:
    """The anchor date shifted into another year, tolerating Feb 29."""
    while True:
        try:
            return date(year, month, day) + timedelta(days=offset)
        except ValueError:
            day -= 1


async def _get_json(url: str, session: aiohttp.ClientSession):
    try:
        async with session.get(
            url, params={"key": EBIRD_WEB_KEY},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass  # rarity is a nice-to-have; a failure just means "unknown"
    return None


async def _fetch_season_baseline(scope: str, session: aiohttp.ClientSession) -> dict:
    """Which species were reported in `scope` around this date in prior years.

    Samples RARITY_OFFSETS days around today's month/day in each of the prior
    RARITY_YEARS years (~30 past days) and records, per species, how many of
    those days it was reported on (reviewer-accepted records only). Daily
    checklist totals ride along so callers can judge whether there was enough
    birding effort here for the answer to mean anything.
    """
    anchor = date.today()
    key = (scope, f"{anchor.month:02d}-{anchor.day:02d}")
    cached = _SEASON_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_SEASON_CACHE) > 32:
        _SEASON_CACHE.clear()  # cheap bound; entries go stale daily anyway

    years = range(anchor.year - RARITY_YEARS, anchor.year)
    days = [
        _season_day(year, anchor.month, anchor.day, offset)
        for year in years
        for offset in RARITY_OFFSETS
    ]
    semaphore = asyncio.Semaphore(6)

    async def species_on(day: date):
        async with semaphore:
            return await _get_json(
                HISTORIC_URL.format(region_code=scope, y=day.year, m=day.month, d=day.day),
                session,
            )

    async def checklists_on(day: date):
        async with semaphore:
            stats = await _get_json(
                DAY_STATS_URL.format(region_code=scope, y=day.year, m=day.month, d=day.day),
                session,
            )
            return (stats or {}).get("numChecklists") or 0

    day_lists = await asyncio.gather(*(species_on(day) for day in days))
    # effort is gauged on the anchor day of each year: one stats call per year
    effort = await asyncio.gather(
        *(checklists_on(_season_day(year, anchor.month, anchor.day, 0)) for year in years)
    )

    day_counts: dict[str, int] = {}
    sampled = 0
    for observations in day_lists:
        if observations is None:
            continue  # fetch failed; don't count the day as sampled
        sampled += 1
        for code in {o.get("speciesCode") for o in observations if isinstance(o, dict)}:
            if code:
                day_counts[code] = day_counts.get(code, 0) + 1
    baseline = {
        "days": day_counts,
        "sampled": sampled,
        "since": years[0],
        "scope": scope,
        "checklists": sum(effort),
    }
    _SEASON_CACHE[key] = baseline
    return baseline


async def _season_baseline(region_code: str, session: aiohttp.ClientSession) -> dict:
    """The season baseline at the queried scale, escalating only when birding
    effort there is too thin to rank against (and saying so via 'scope')."""
    scope = region_code
    while True:
        baseline = await _fetch_season_baseline(scope, session)
        enough = baseline["sampled"] and baseline["checklists"] >= RARITY_MIN_EFFORT
        if enough or "-" not in scope:
            return baseline
        scope = scope.rsplit("-", 1)[0]  # county -> state -> country


async def _warm_rarity_baseline(region_code: str, session: aiohttp.ClientSession) -> None:
    """Build the region's season baseline once, so parallel lookups hit the cache."""
    await _season_baseline(region_code, session)


async def _rarity(
    region_code: str, species_code: str, session: aiohttp.ClientSession
) -> tuple[str, str, float | None, str]:
    """(label, emoji, share, note): how often this species is reported here in season.

    Frequency of reviewer-accepted reports, at the scale actually queried
    (a county ranks against itself when it has real coverage), in this
    season across prior years; eBird's own kind of measure.
    """
    baseline = await _season_baseline(region_code, session)
    sampled = baseline["sampled"]
    if not sampled:
        return "Notable sighting", "⚪", None, ""
    mine = baseline["days"].get(species_code, 0)
    share = 100 * mine / sampled
    # each sampled day the species appeared carries at least one accepted
    # record, so the day count is a floor on confirmed reports; "+" says so
    count = f"{mine}+ confirmed reports" if mine else "no confirmed reports"
    note = f"{count} in season since {baseline['since']} in {baseline['scope']}"
    for threshold, label, emoji in RARITY_TIERS:
        if share < threshold:
            return label, emoji, share, note
    return "Locally notable", "⚪", share, note


species_rarity = _rarity  # public name for callers outside this module


def notable_key(obs: dict) -> str:
    """One species on one checklist — the identity of a single rare-bird report."""
    return f"{obs.get('subId') or ''}:{obs.get('speciesCode') or ''}"


def notable_status(obs: dict) -> str:
    """Where eBird's review stands on a flagged record.

    Flagged observations enter the feed unreviewed (`obsValid` false,
    `obsReviewed` false) and flip to valid once a regional reviewer accepts
    them; reviewed-but-invalid means the record was not accepted.
    """
    if obs.get("obsValid"):
        return STATUS_CONFIRMED
    return STATUS_REJECTED if obs.get("obsReviewed") else STATUS_PENDING


async def fetch_notable(
    region_code: str,
    *,
    days: int = 14,
    session: aiohttp.ClientSession | None = None,
    max_age: float = NOTABLE_CACHE_SECONDS,
) -> list[dict]:
    """Raw notable-sightings feed for an already-resolved region code.

    Includes records at every review stage; use `notable_status` to tell them
    apart. `days` is capped at eBird's 30-day maximum.

    Every result is cached briefly and shared by all callers, so a region
    watched by several subscribers, or looked up by `/rare` just after a poll,
    costs one request rather than one per caller. Pass `max_age=0` to insist
    on fresh data, as the alert poll does.
    """
    days = max(1, min(days, NOTABLE_MAX_DAYS))
    key = (region_code.upper(), days)
    now = time.monotonic()
    for stale, (fetched_at, _) in list(_NOTABLE_CACHE.items()):
        if now - fetched_at > NOTABLE_CACHE_SECONDS:
            del _NOTABLE_CACHE[stale]  # feeds are large; don't hoard them
    cached = _NOTABLE_CACHE.get(key)
    if cached is not None and max_age > 0 and now - cached[0] <= max_age:
        return cached[1]

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(
            NOTABLE_URL.format(region_code=region_code),
            params={"key": EBIRD_WEB_KEY, "back": str(days), "detail": "full"},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise ChecklistError(
                    f"eBird returned HTTP {resp.status} for notable sightings in "
                    f"`{region_code}` — check the region."
                )
            observations = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ChecklistError(f"Couldn't reach eBird ({error.__class__.__name__}).") from error
    finally:
        if owns_session:
            await session.close()
    if not isinstance(observations, list):
        raise ChecklistError("Unexpected response from eBird's notable-sightings feed.")
    _NOTABLE_CACHE[key] = (time.monotonic(), observations)
    return observations


async def _first_photo(
    obs: dict, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore
) -> tuple[dict, dict | None]:
    """The observation paired with its first public photo, or None if it has none."""
    if not obs.get("hasRichMedia"):
        return obs, None
    async with semaphore:
        page = await _search(
            session, subId=obs["subId"], taxonCode=obs["speciesCode"],
            mediaType="photo", count=1, unconfirmed="incl",
        )
    return obs, (page[0] if page else None)


async def attach_photos(
    observations: list[dict], *, session: aiohttp.ClientSession, concurrency: int = 6
) -> list[tuple[dict, dict | None]]:
    """Pair every observation with a photo where one is actually indexed."""
    semaphore = asyncio.Semaphore(concurrency)
    return list(await asyncio.gather(
        *(_first_photo(obs, session, semaphore) for obs in observations)
    ))


async def build_rare_reports(
    region_code: str,
    selected: list[tuple[dict, dict | None]],
    *,
    session: aiohttp.ClientSession,
    per_species: Counter | None = None,
    include_exif: bool = False,
) -> list[RareReport]:
    """Turn (observation, photo) pairs into reports: camera data, rarity, text."""
    if not selected:
        return []
    if per_species is None:
        per_species = Counter(obs.get("speciesCode") for obs, _ in selected)
    if include_exif:
        exif_sem = asyncio.Semaphore(4)

        async def grab(item: dict | None) -> tuple[tuple[str, str], ...]:
            if not item:
                return ()
            async with exif_sem:
                return await _fetch_exif(session, item["assetId"])

        exifs = await asyncio.gather(*(grab(item) for _, item in selected))
    else:
        exifs = [()] * len(selected)

    await _warm_rarity_baseline(region_code, session)
    rarity_sem = asyncio.Semaphore(4)

    async def rarity_for(species_code: str):
        async with rarity_sem:
            return await _rarity(region_code, species_code, session)

    rarities = await asyncio.gather(*(rarity_for(obs["speciesCode"]) for obs, _ in selected))

    reports = []
    for (obs, item), exif, (label, emoji, share, note) in zip(selected, exifs, rarities):
        reports.append(RareReport(
            common_name=obs.get("comName") or "Unknown species",
            sci_name=obs.get("sciName") or "",
            species_code=obs.get("speciesCode") or "",
            obs_dt=obs.get("obsDt") or "",
            location=obs.get("locName") or "",
            observer=obs.get("userDisplayName") or "",
            checklist_id=obs.get("subId") or "",
            reports_in_window=per_species.get(obs.get("speciesCode"), 0),
            rarity_label=label,
            rarity_emoji=emoji,
            rarity_share=share,
            rarity_note=note,
            details=_item_to_details(item, exif) if item else None,
            status=notable_status(obs),
            latitude=obs.get("lat"),
            longitude=obs.get("lng"),
            how_many=obs.get("howMany"),
            county=obs.get("subnational2Name") or "",
            state=obs.get("subnational1Name") or "",
        ))
    return reports


async def fetch_rare_reports(
    region: str,
    *,
    count: int = 10,
    days: int = 14,
    unique_species: bool = True,
    include_exif: bool = False,
    require_photo: bool = True,
    confirmed_only: bool = False,
    session: aiohttp.ClientSession | None = None,
) -> tuple[str, list[RareReport]]:
    """Recent rarities in a region, most recent first.

    Both reviewer-accepted and still-unreviewed observations are returned
    (each report carries its `status`); records a reviewer rejected never
    are. `confirmed_only=True` keeps only accepted ones. By default each
    report must also have a retrievable public photo; with
    `require_photo=False` every report is listed, and `details` holds a
    photo only for those that actually have one.
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        region_code = await resolve_region(region, session=session)
        observations = await fetch_notable(region_code, days=days, session=session)
        per_species = Counter(o.get("speciesCode") for o in observations)
        # "confirmed" = a regional reviewer accepted the record; hasRichMedia
        # is eBird's own flag that something was attached (photo, audio or video)
        wanted = (
            (lambda o: bool(o.get("obsValid"))) if confirmed_only
            else (lambda o: notable_status(o) != STATUS_REJECTED)
        )
        candidates = [
            o for o in observations
            if wanted(o) and (o.get("hasRichMedia") or not require_photo)
        ]
        ordered: list[dict] = []
        seen_reports: set[tuple] = set()
        seen_species: set[str] = set()
        for obs in sorted(candidates, key=lambda o: o.get("obsDt") or "", reverse=True):
            key = (obs.get("subId"), obs.get("speciesCode"))
            if key in seen_reports:
                continue
            seen_reports.add(key)
            if unique_species:
                if obs.get("speciesCode") in seen_species:
                    continue
                seen_species.add(obs.get("speciesCode"))
            ordered.append(obs)

        semaphore = asyncio.Semaphore(6)

        # hasRichMedia doesn't guarantee an indexed public photo, so walk the
        # candidates newest-first and keep the ones that really have one
        selected: list[tuple[dict, dict | None]] = []
        if not require_photo:
            # keep every confirmed report, but still resolve a photo where eBird
            # flags one, so callers can mark which reports have imagery
            selected = list(await asyncio.gather(
                *(_first_photo(o, session, semaphore) for o in ordered[:count])
            ))
        else:
            pool = ordered[:RARE_SCAN_MAX]
            batch = max(count, 8)
            for start in range(0, len(pool), batch):
                if len(selected) >= count:
                    break
                for obs, item in await asyncio.gather(
                    *(_first_photo(o, session, semaphore) for o in pool[start:start + batch])
                ):
                    if item is not None and len(selected) < count:
                        selected.append((obs, item))

        reports = await build_rare_reports(
            region_code, selected, session=session, per_species=per_species,
            include_exif=include_exif and require_photo,
        )
        return region_code, reports
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ChecklistError(f"Couldn't reach eBird ({error.__class__.__name__}).") from error
    finally:
        if owns_session:
            await session.close()


async def fetch_alert_reports(
    region_code: str,
    *,
    days: int = 3,
    limit: int = 25,
    skip: set[str] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> list[RareReport]:
    """Every flagged report in the window — confirmed *and* awaiting review.

    This is what the alert poller sends: one report per species per checklist,
    newest first, with a photo attached where one exists. `skip` holds
    `notable_key`s already delivered, filtered out before any per-report
    lookups so a quiet poll costs a single request.
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        observations = await fetch_notable(region_code, days=days, session=session)
        per_species = Counter(o.get("speciesCode") for o in observations)
        fresh: dict[str, dict] = {}
        for obs in sorted(observations, key=lambda o: o.get("obsDt") or "", reverse=True):
            key = notable_key(obs)
            if notable_status(obs) == STATUS_REJECTED or key in (skip or ()):
                continue
            fresh.setdefault(key, obs)
        selected = await attach_photos(list(fresh.values())[:limit], session=session)
        return await build_rare_reports(
            region_code, selected, session=session, per_species=per_species
        )
    finally:
        if owns_session:
            await session.close()


async def _main(argv: list[str]) -> int:
    flags, args = extract_flags(argv)
    verbose = VERBOSE_FLAG in flags
    compact_flag = pick_compact_flag(flags)
    if args and args[0].lower() == "alert":
        words = [t for t in args[1:] if not t.isdigit()]
        nums = [int(t) for t in args[1:] if t.isdigit()]
        if not words:
            print("usage: python ebird_media.py alert <region> [days] [limit]", file=sys.stderr)
            return 2
        region_code = await resolve_region_code(" ".join(words))
        reports = await fetch_alert_reports(
            region_code,
            days=nums[0] if nums else 3,
            limit=nums[1] if len(nums) > 1 else 25,
        )
        print(f"{len(reports)} alertable report(s) in {region_code}")
        for report in reports:
            mark = "✅" if report.confirmed else "⏳"
            photo = f" ML{report.details.photo.asset_id}" if report.details else ""
            print(f"  {report.obs_dt}  {mark} {report.rarity_emoji} "
                  f"{report.common_name:<26} {report.rarity_display}{photo}")
            print(f"      {report.location} · {report.observer} · {report.checklist_url}")
        return 0
    if args and args[0].lower() == "rare":
        rest = args[1:]
        tokens = ("photos", "confirmed", "text", "nophoto")  # last two: old defaults, now no-ops
        photo_mode = any(t.lower() == "photos" for t in rest)
        confirmed_mode = any(t.lower() == "confirmed" for t in rest)
        words = [t for t in rest if not t.isdigit() and t.lower() not in tokens]
        nums = [int(t) for t in rest if t.isdigit()]
        if not words:
            print(
                "usage: python ebird_media.py rare <region> [count] [days] [photos] [confirmed]",
                file=sys.stderr,
            )
            return 2
        region_code, reports = await fetch_rare_reports(
            " ".join(words),
            count=nums[0] if nums else 10,
            days=nums[1] if len(nums) > 1 else 14,
            require_photo=photo_mode,
            confirmed_only=confirmed_mode,
        )
        kind = "eBird-confirmed rarities" if confirmed_mode else "rarities"
        qualifier = " with photos" if photo_mode else ""
        print(f"{len(reports)} {kind}{qualifier} in {region_code}")
        for report in reports:
            print(f"  {report.obs_dt}  {report.rarity_emoji} {report.common_name:<26} "
                  f"{report.rarity_display}")
            asset = f"ML{report.details.photo.asset_id} · " if report.details else ""
            print(f"      {asset}{report.location} · {report.observer} · {report.checklist_url}")
        return 0
    user_mode = bool(args) and bool(_USER_ID_RE.search(args[0]) or _PROFILE_URL_RE.search(args[0]))
    if not args or (not user_mode and len(args) != 1):
        print(
            "usage: python ebird_media.py [-vvv | -c | -cc | -ccc] "
            "<checklist URL/ID | ML asset URL/number | USER… ID [count] [top|recent|obs] [species] "
            "| rare <region> [count] [days]>",
            file=sys.stderr,
        )
        return 2
    if user_mode:
        count, sort, group, region = 10, SORT_BEST, False, ""
        species_words: list[str] = []
        for token in args[1:]:
            if token.isdigit():
                count = int(token)
            elif token.lower() == "recent":
                sort = SORT_RECENT
            elif token.lower() == "obs":
                sort = SORT_OBS
            elif token.lower() == "top":
                sort = SORT_BEST
            elif token.lower() == "group":
                group = True
            elif token.lower().startswith("in:"):
                region = token[3:]
            else:
                species_words.append(token)
        result = await fetch_user_details(
            args[0], count=count, sort=sort, include_exif=False,
            species_query=" ".join(species_words) or None,
            species_group=group,
            all_media=bool(species_words),
            region=region,
        )
        kind = {
            SORT_RECENT: "most recently uploaded",
            SORT_OBS: "most recent by observation date",
        }.get(sort, "top rated")
        of_species = f" of {result.species_display}" if result.species_display else ""
        print(f"{len(result.details)} {kind} media{of_species} by {result.display_name} ({result.user_id})")
        for details in result.details:
            photo = details.photo
            flag = "  [UNCONFIRMED]" if photo.unconfirmed else ""
            kind_note = f"  ({details.media_type})" if details.media_type != "photo" else ""
            print(f"  ML{photo.asset_id}  {photo.common_name:<28} {photo.rating_display:<12} {photo.asset_url}{kind_note}{flag}")
        return 0
    if _ASSET_URL_RE.search(args[0]) or _ASSET_ML_RE.search(args[0]):
        details = await fetch_asset_details(parse_asset_id(args[0]))
        photo = details.photo
        flag = "  [UNCONFIRMED]" if photo.unconfirmed else ""
        print(f"ML{photo.asset_id}  {photo.common_name}  ({details.media_type}){flag}  {photo.asset_url}")
        if photo.sci_name:
            print(f"  {photo.sci_name}")
        if details.checklist_id:
            print(f"  Checklist: https://ebird.org/checklist/{details.checklist_id}")
        if compact_flag:
            if photo.rating_display:
                print(f"  Rating: {photo.rating_display}")
            for label, value, is_camera in select_fields(details, compact_flag):
                prefix = "[camera] " if is_camera else ""
                print(f"  {prefix}{label}: {value}")
            return 0
        for label, value in photo.metadata_fields(markdown=False):
            print(f"  {label}: {value}")
        for label, value in details.exif:
            print(f"  [camera] {label}: {value}")
        if not details.exif:
            print("  [camera] no camera metadata available")
        return 0
    sub_id = parse_checklist_id(args[0])
    photos = await fetch_checklist_photos(sub_id)
    print(f"{len(photos)} public photo(s) on https://ebird.org/checklist/{sub_id}")
    for photo in photos:
        flag = "  [UNCONFIRMED]" if photo.unconfirmed else ""
        print(f"  ML{photo.asset_id}  {photo.common_name:<30} {photo.asset_url}{flag}")
        if verbose:
            for label, value in photo.metadata_fields(markdown=False):
                print(f"      {label}: {value}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main(sys.argv[1:])))
    except ChecklistError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
