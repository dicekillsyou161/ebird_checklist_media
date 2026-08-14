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
from dataclasses import dataclass

import aiohttp

API_URL = "https://media.ebird.org/api/v2/search"
ASSET_PAGE = "https://macaulaylibrary.org/asset/{asset_id}"
TAXON_FIND_URL = "https://api.ebird.org/v2/ref/taxon/find"
# eBird's own key for its public web autocomplete widgets (not a personal API key)
EBIRD_WEB_KEY = "jfekjedvescr"
SORT_BEST = "rating_rank_desc"     # Macaulay "Best quality" ranking
SORT_RECENT = "upload_date_desc"   # most recently uploaded
SORT_OBS = "obs_date_desc"         # most recent observation date/time
USER_AGENT = "ebird-checklist-discord-bot/1.0"
PAGE_SIZE = 100
MAX_PHOTOS = 400  # safety cap so one request can't paginate forever
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
        ("camera", "Shutter speed"),
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
    "shutter_speed": "Shutter speed",
    "f_number": "Aperture",
    "iso": "ISO",
    "flash": "Flash",
    "create_dt": "Taken",
    "latitude": "GPS latitude",
    "longitude": "GPS longitude",
}
_EXIF_ORDER = list(_EXIF_LABELS)
_EXIF_SKIP = {"width", "height"}  # already shown via the search API's Size field


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
class UserMedia:
    display_name: str
    user_id: str
    species_code: str     # "" when not filtered by species
    species_display: str  # e.g. "Black Oystercatcher - Haematopus bachmani"
    details: list[AssetDetails]


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


async def resolve_species(query: str, *, session: aiohttp.ClientSession) -> tuple[str, str]:
    """Match a common or scientific name to (taxonCode, display name).

    An exact name match wins outright; otherwise a lone hit is used as-is,
    and multiple hits raise a did-you-mean error rather than silently
    searching the wrong species.
    """
    params = {"locale": "en", "cat": "species", "limit": "25", "key": EBIRD_WEB_KEY, "q": query}
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    async with session.get(
        TAXON_FIND_URL, params=params, headers=headers,
        timeout=aiohttp.ClientTimeout(total=25),
    ) as resp:
        if resp.status != 200:
            raise ChecklistError(f"Species lookup returned HTTP {resp.status} — try again in a minute.")
        matches = await resp.json(content_type=None)
    if not isinstance(matches, list) or not matches:
        raise ChecklistError(
            f"No species matched “{query}” — try the exact common or scientific name."
        )
    lowered = query.strip().lower()
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
    session: aiohttp.ClientSession, user_id: str, *, sort: str, all_media: bool
) -> list[dict]:
    """Page through a user's media in server sort order, up to MAX_PHOTOS items."""
    items: list[dict] = []
    cursor: str | None = None
    while len(items) < MAX_PHOTOS:
        params: dict = {"userId": user_id, "sort": sort, "count": PAGE_SIZE, "unconfirmed": "incl"}
        if not all_media:
            params["mediaType"] = "photo"
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
        user_id = await resolve_user(user_ref, session=session)
        species_code = species_display = ""
        if species_query and species_group:
            needle = species_query.strip().lower()
            species_display = f"“{species_query.strip()}”"
            everything = await _paged_user_search(session, user_id, sort=sort, all_media=all_media)
            items = [
                item for item in everything
                if needle in ((item.get("taxonomy") or {}).get("comName") or "").lower()
                or needle in ((item.get("taxonomy") or {}).get("sciName") or "").lower()
            ][:count]
        else:
            if species_query:
                species_code, species_display = await resolve_species(species_query, session=session)
            params: dict = {"userId": user_id, "sort": sort, "count": count, "unconfirmed": "incl"}
            if not all_media:
                params["mediaType"] = "photo"
            if species_code:
                params["taxonCode"] = species_code
            items = (await _search(session, **params))[:count]
        if include_exif and items:
            semaphore = asyncio.Semaphore(4)

            async def grab(item: dict) -> tuple[tuple[str, str], ...]:
                async with semaphore:
                    return await _fetch_exif(session, item["assetId"])

            exifs = await asyncio.gather(*(grab(item) for item in items))
        else:
            exifs = [()] * len(items)
        name = (items[0].get("userDisplayName") if items else "") or user_id
        return UserMedia(
            display_name=name,
            user_id=user_id,
            species_code=species_code,
            species_display=species_display,
            details=[_item_to_details(i, e) for i, e in zip(items, exifs)],
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


async def _main(argv: list[str]) -> int:
    flags, args = extract_flags(argv)
    verbose = VERBOSE_FLAG in flags
    compact_flag = pick_compact_flag(flags)
    user_mode = bool(args) and bool(_USER_ID_RE.search(args[0]) or _PROFILE_URL_RE.search(args[0]))
    if not args or (not user_mode and len(args) != 1):
        print(
            "usage: python ebird_media.py [-vvv | -c | -cc | -ccc] "
            "<checklist URL/ID | ML asset URL/number | USER… ID [count] [top|recent|obs] [species]>",
            file=sys.stderr,
        )
        return 2
    if user_mode:
        count, sort, group = 10, SORT_BEST, False
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
            else:
                species_words.append(token)
        result = await fetch_user_details(
            args[0], count=count, sort=sort, include_exif=False,
            species_query=" ".join(species_words) or None,
            species_group=group,
            all_media=bool(species_words),
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
