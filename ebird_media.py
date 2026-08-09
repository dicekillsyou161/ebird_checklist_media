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
import re
import sys
from dataclasses import dataclass

import aiohttp

API_URL = "https://media.ebird.org/api/v2/search"
USER_AGENT = "ebird-checklist-discord-bot/1.0"
PAGE_SIZE = 100
MAX_PHOTOS = 400  # safety cap so one request can't paginate forever
VERBOSE_FLAG = "-vvv"

_CHECKLIST_RE = re.compile(r"\bS(\d{4,})\b", re.IGNORECASE)
_AGE_SEX_RE = re.compile(r"(adult|immature|juvenile|unknown)(Female|Male|Unknown)Count")


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
    rating: int | None
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

    @property
    def asset_url(self) -> str:
        return f"https://macaulaylibrary.org/asset/{self.asset_id}"

    def image_url(self, size: int = 1200) -> str:
        return f"https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{self.asset_id}/{size}"

    def metadata_fields(self, *, markdown: bool = True) -> list[tuple[str, str]]:
        """(label, value) pairs for every non-empty metadata detail."""
        coords = ""
        if self.latitude is not None and self.longitude is not None:
            plain = f"{self.latitude:.5f}, {self.longitude:.5f}"
            coords = (
                f"[{plain}](https://www.google.com/maps/search/?api=1"
                f"&query={self.latitude},{self.longitude})"
            ) if markdown else plain
        rating = ""
        if self.rating:
            rating = f"{self.rating}/5"
            if self.rating_count:
                plural = "s" if self.rating_count != 1 else ""
                rating += f" ({self.rating_count} rating{plural})"
        fields = [
            ("Observed", " · ".join(bit for bit in (self.obs_date, self.obs_time) if bit)),
            ("Location", self.location),
            ("Coordinates", coords),
            ("Age / sex", self.age_sex),
            ("Rating", rating),
            ("Size", f"{self.width} × {self.height} px" if self.width and self.height else ""),
            ("License", self.license_id),
            ("Species code", self.species_code),
            ("Notes", self.notes),
            ("Tags", self.tags),
        ]
        return [(label, value) for label, value in fields if value]


def extract_vvv(tokens: list[str]) -> tuple[bool, list[str]]:
    """Split a -vvv verbose flag out of user-supplied tokens."""
    rest = [token for token in tokens if token != VERBOSE_FLAG]
    return len(rest) != len(tokens), rest


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
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    try:
        items: list[dict] = []
        cursor: str | None = None
        while len(items) < MAX_PHOTOS:
            params = {"subId": sub_id, "mediaType": "photo", "count": str(PAGE_SIZE)}
            if cursor:
                params["initialCursorMark"] = cursor
            async with session.get(
                API_URL, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=25),
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


async def _main(argv: list[str]) -> int:
    verbose, args = extract_vvv(argv)
    if len(args) != 1:
        print("usage: python ebird_media.py [-vvv] <checklist URL or ID>", file=sys.stderr)
        return 2
    sub_id = parse_checklist_id(args[0])
    photos = await fetch_checklist_photos(sub_id)
    print(f"{len(photos)} public photo(s) on https://ebird.org/checklist/{sub_id}")
    for photo in photos:
        print(f"  ML{photo.asset_id}  {photo.common_name:<30} {photo.asset_url}")
        if verbose:
            for label, value in photo.metadata_fields(markdown=False):
                print(f"      {label}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
