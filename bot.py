"""Discord bot: /checklist <eBird checklist> posts every public Macaulay Library photo."""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from alerts import (
    CONFIRMATION,
    RARITY_LEVELS,
    AlertStore,
    Subscription,
)
from ebird_media import (
    COMPACT_FLAG,
    STATUS_REJECTED,
    VERBOSE_FLAG,
    AssetDetails,
    ChecklistError,
    Photo,
    SORT_BEST,
    SORT_OBS,
    SORT_RECENT,
    RareReport,
    attach_photos,
    build_rare_reports,
    extract_flags,
    fetch_asset_details,
    fetch_checklist_photos,
    fetch_notable,
    fetch_rare_reports,
    fetch_user_details,
    notable_key,
    notable_status,
    parse_asset_id,
    parse_checklist_id,
    pick_compact_flag,
    region_name,
    resolve_region_code,
    select_fields,
)

load_dotenv()

MAX_PHOTOS_POSTED = 50  # keep one command from flooding a channel
FIELD_VALUE_MAX = 300            # display cap for one metadata value (Discord allows 1024)
EMBED_COLOR = discord.Color.from_str("#4a7628")  # eBird green


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# User-installable: the commands travel with your Discord account, so they work
# in DMs, group DMs, and servers the bot itself hasn't joined. This requires
# "User Install" to be enabled in the Developer Portal (Installation →
# Installation Contexts); without it Discord rejects the sync and the bot falls
# back to guild-install automatically.
USER_INSTALL = _env_flag("USER_INSTALL", True)


def command_scopes(user_install: bool):
    """(where commands may run, how the app may be installed) for a global sync."""
    return (
        app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=user_install
        ),
        app_commands.AppInstallationType(guild=True, user=user_install),
    )


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "")
    try:
        return max(minimum, min(int(raw), maximum)) if raw else default
    except ValueError:
        print(f"{name}={raw!r} isn't a number — using {default}.")
        return default


# Rare bird alerts: poll every watched region on this cadence and DM what's new.
ALERT_INTERVAL_SECONDS = _env_int("ALERT_INTERVAL_SECONDS", 300, 60, 3600)
# eBird's window is by *observation* date, so it has to be wider than the poll
# interval: checklists are often submitted hours or days after the sighting.
ALERT_WINDOW_DAYS = _env_int("ALERT_WINDOW_DAYS", 3, 1, 30)
ALERT_BUILD_MAX = 40   # most reports to look up per region per poll
ALERT_DM_MAX = 10      # most DMs to one subscriber per poll
ALERT_FAILURE_LIMIT = 3  # consecutive DM failures before a subscription pauses
SUBSCRIPTIONS_PATH = Path(__file__).resolve().parent / "subscriptions.json"

store = AlertStore(SUBSCRIPTIONS_PATH)

RARITY_COLORS = {  # match the tier emoji so an alert reads at a glance
    "Mega rarity": discord.Color.from_str("#c62828"),
    "Very rare": discord.Color.from_str("#ef6c00"),
    "Rare": discord.Color.from_str("#f9a825"),
    "Scarce": discord.Color.from_str("#2e7d32"),
}


# Learned identities: Discord links (via /iam) and display names seen in any
# command's results, so "@mention" and "Mark Zorthesosen" work as user refs.
REGISTRY_PATH = Path(__file__).resolve().parent / "aliases.json"
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_NAMELIKE_RE = re.compile(r"^@?[^\d:/]+$")  # words only: no digits, no URLs


def _load_registry() -> dict:
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return {"discord": dict(data.get("discord", {})), "names": dict(data.get("names", {}))}
    except (OSError, ValueError):
        return {"discord": {}, "names": {}}


_registry = _load_registry()


def _save_registry() -> None:
    try:
        REGISTRY_PATH.write_text(
            json.dumps(_registry, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as error:
        print(f"Couldn't save {REGISTRY_PATH.name}: {error}")


def learn_names(pairs) -> None:
    """Remember (display name, USER… ID) pairs seen in results."""
    changed = False
    for display, user_id in pairs:
        display = (display or "").strip()
        if not display or not user_id or display == user_id:
            continue
        key = display.lower()
        if _registry["names"].get(key, [None])[0] != user_id:
            _registry["names"][key] = [user_id, display]
            changed = True
    if changed:
        _save_registry()


def resolve_alias(text: str) -> str | None:
    """Turn a Discord @mention or a learned display name into a USER… ID.

    Returns None when `text` isn't mention/name-shaped and should go to the
    library resolver (USER IDs, asset links, digits) untouched.
    """
    stripped = text.strip()
    mention = _MENTION_RE.fullmatch(stripped)
    if mention:
        linked = _registry["discord"].get(mention.group(1))
        if linked:
            return linked
        raise ChecklistError(
            "That Discord account isn't linked to an eBird user yet — "
            "they can link it with `/iam` (USER… ID or one of their ML asset links)."
        )
    if not _NAMELIKE_RE.fullmatch(stripped):
        return None
    needle = stripped.lstrip("@").strip().lower()
    names = _registry["names"]
    if needle in names:
        return names[needle][0]
    hits = {tuple(entry) for key, entry in names.items() if needle in key}
    if len(hits) == 1:
        return next(iter(hits))[0]
    if hits:
        shown = "; ".join(sorted(display for _, display in hits)[:5])
        raise ChecklistError(f"“{stripped}” matches several people I know — {shown}. Be more specific.")
    raise ChecklistError(
        f"I don't recognize “{stripped}” yet. Names are learned automatically — run any "
        "command once with their `USER…` ID or one of their ML asset links, or have "
        "them link themselves with `/iam`."
    )


class ChecklistBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.alert_task: asyncio.Task | None = None

    def _scope(self, contexts, installs) -> None:
        for command in self.tree.get_commands():
            command.allowed_contexts = contexts
            command.allowed_installs = installs

    async def setup_hook(self) -> None:
        self.alert_task = self.loop.create_task(alert_loop(), name="rare-bird-alerts")
        raw = os.getenv("GUILD_ID", "")
        tokens = [token for token in re.split(r"[,\s]+", raw) if token]
        if not all(token.isdigit() for token in tokens):
            raise SystemExit(f"GUILD_ID must be numeric server IDs (comma-separated), got: {raw!r}")
        # Guild copies go out first and carry no context/install overrides: those
        # fields only mean something for global commands, and copy_global_to
        # shares them with the originals, so they must be synced before we set them.
        self._scope(None, None)
        for guild_id in tokens:
            # Guild copies appear instantly, which is what makes testing bearable.
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                print(
                    f"Can't register commands in server {guild_id}: the bot either "
                    "isn't in that server or was invited without the "
                    "applications.commands scope. Re-invite it with the OAuth URL "
                    "from the README (scope=bot+applications.commands), then restart."
                )
        # Always register globally as well: guild-scoped commands never show up
        # in DMs, so this is the registration that makes DM and user-install use
        # possible. It can take up to an hour to propagate the first time.
        self._scope(*command_scopes(USER_INSTALL))
        try:
            await self.tree.sync()
            where = "servers, DMs, and anywhere you install it" if USER_INSTALL else "servers and DMs"
            print(f"Commands registered globally for {where}.")
        except discord.HTTPException as error:
            if not USER_INSTALL:
                print(f"Global command sync failed ({error}); DM commands may be unavailable.")
                return
            print(
                f"Global sync with user install failed ({error}). Enable it at "
                "https://discord.com/developers/applications → your app → Installation → "
                "Installation Contexts → tick 'User Install', then restart. "
                "Falling back to guild install for now."
            )
            self._scope(*command_scopes(False))
            try:
                await self.tree.sync()
                print("Registered globally without user install; servers and DMs still work.")
            except discord.HTTPException as retry_error:
                print(f"Global command sync still failing ({retry_error}).")


bot = ChecklistBot()


@bot.event
async def on_ready() -> None:
    watching = len(store.active())
    alerts = (
        f"{watching} rare bird alert subscription(s), polled every "
        f"{ALERT_INTERVAL_SECONDS // 60} min"
        if watching else "no rare bird alert subscriptions yet"
    )
    print(f"Logged in as {bot.user} — /checklist is ready; {alerts}")


def build_embed(
    photo: Photo, *, verbose: bool = False, checklist_id: str = "", show_rating: bool = False
) -> discord.Embed:
    lines = []
    if photo.sci_name:
        lines.append(f"*{photo.sci_name}*")
    lines.append(f"[macaulaylibrary.org/asset/{photo.asset_id}]({photo.asset_url})")
    if checklist_id:
        lines.append(f"[ebird.org/checklist/{checklist_id}](https://ebird.org/checklist/{checklist_id})")
    if show_rating and photo.rating_display:
        lines.append(f"⭐ {photo.rating_display}")
    if photo.unconfirmed:
        lines.append("⚠️ *Unconfirmed — pending eBird review*")
    embed = discord.Embed(
        title=photo.common_name,
        url=photo.asset_url,
        description="\n".join(lines),
        color=EMBED_COLOR,
    )
    embed.set_image(url=photo.image_url(1200))
    embed.set_footer(text=f"ML{photo.asset_id} • © {photo.photographer} • Macaulay Library")
    if verbose:
        for label, value in photo.metadata_fields():
            if len(value) > FIELD_VALUE_MAX:
                value = value[:FIELD_VALUE_MAX - 1] + "…"
            embed.add_field(name=label, value=value, inline=True)
    return embed


@bot.tree.command(
    name="checklist",
    description="Post all public Macaulay Library photos from an eBird checklist",
)
@app_commands.describe(
    checklist="Checklist URL or ID, e.g. S378216909 — prefix with -vvv for full photo metadata"
)
async def checklist_command(interaction: discord.Interaction, checklist: str) -> None:
    await interaction.response.defer()
    flags, rest = extract_flags(checklist.split())
    compact = pick_compact_flag(flags) is not None
    verbose = VERBOSE_FLAG in flags and not compact
    try:
        sub_id = parse_checklist_id(" ".join(rest))
        photos = await fetch_checklist_photos(sub_id)
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return

    checklist_url = f"https://ebird.org/checklist/{sub_id}"
    if not photos:
        await interaction.followup.send(
            f"No public photos found on <{checklist_url}> — the checklist may be "
            "private, have no photos yet, or the ID may be wrong."
        )
        return

    learn_names((photo.photographer, photo.user_id) for photo in photos)
    species_count = len({photo.common_name for photo in photos})
    first = photos[0]
    detail_bits = [bit for bit in (first.photographer, first.obs_date, first.location) if bit]
    plural = "s" if len(photos) != 1 else ""
    await interaction.followup.send(
        f"**{len(photos)} public photo{plural}** · "
        f"{species_count} species · {' · '.join(detail_bits)}\n<{checklist_url}>"
    )

    # one message per photo so each can be forwarded individually
    for photo in photos[:MAX_PHOTOS_POSTED]:
        await interaction.followup.send(
            embed=build_embed(photo, verbose=verbose, show_rating=compact)
        )

    if len(photos) > MAX_PHOTOS_POSTED:
        await interaction.followup.send(
            f"…showing the first {MAX_PHOTOS_POSTED} of {len(photos)} photos — "
            f"see the rest at <{checklist_url}>"
        )


@bot.tree.command(
    name="checkmedia",
    description="Post one Macaulay Library photo with all its metadata, including camera EXIF",
)
@app_commands.describe(
    media="Macaulay Library asset link or ML number — flags -c, -cc, -ccc trim the metadata (see README)"
)
async def checkmedia_command(interaction: discord.Interaction, media: str) -> None:
    await interaction.response.defer()
    flags, rest = extract_flags(media.split())
    compact_flag = pick_compact_flag(flags)
    try:
        asset_id = parse_asset_id(" ".join(rest))
        details = await fetch_asset_details(asset_id)
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return

    learn_names([(details.photo.photographer, details.photo.user_id)])
    await interaction.followup.send(embed=build_asset_embed(details, compact_flag))


def build_asset_embed(details: AssetDetails, compact_flag: str | None) -> discord.Embed:
    """The /checkmedia-style embed for one asset, honoring the compact flags."""
    if compact_flag:
        embed = build_embed(details.photo, checklist_id=details.checklist_id, show_rating=True)
        if details.media_type and details.media_type != "photo":
            embed.set_image(url=None)
        for label, value, is_camera in select_fields(details, compact_flag):
            name = f"📷 {label}" if is_camera else label
            embed.add_field(name=name, value=value[:1024], inline=True)
        return embed

    embed = build_embed(details.photo, verbose=True)
    if details.media_type and details.media_type != "photo":
        # audio/video assets have no still image; the link plays the media
        embed.set_image(url=None)
        embed.add_field(name="Media type", value=details.media_type, inline=True)
    if details.checklist_id:
        embed.add_field(
            name="Checklist",
            value=f"[{details.checklist_id}](https://ebird.org/checklist/{details.checklist_id})",
            inline=True,
        )
    if details.exif:
        for label, value in details.exif:
            if len(embed.fields) >= 25:  # Discord's per-embed field limit
                break
            embed.add_field(name=f"📷 {label}", value=value[:1024], inline=True)
    else:
        embed.add_field(name="📷 Camera metadata", value="None available for this asset", inline=False)
    return embed


async def _send_user_photos(
    interaction: discord.Interaction,
    user: str,
    count: int,
    sort: str,
    header: str,
    species: str = "",
    species_group: bool = False,
    region: str = "",
) -> None:
    """Shared body of /top, /recent, /sp; `header` is formatted with n and sp."""
    flags, rest = extract_flags(user.split())
    if species:
        species_flags, species_rest = extract_flags(species.split())
        flags |= species_flags
        species = " ".join(species_rest)
    compact_flag = pick_compact_flag(flags)
    try:
        user_ref = " ".join(rest)
        user_ref = resolve_alias(user_ref) or user_ref
        result = await fetch_user_details(
            user_ref, count=count, sort=sort,
            include_exif=compact_flag != COMPACT_FLAG,
            species_query=species or None,
            species_group=species_group,
            all_media=bool(species),
            region=region,
        )
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return
    learn_names(
        [(result.display_name, result.user_id)]
        + [(d.photo.photographer, d.photo.user_id) for d in result.details]
    )
    if not result.details:
        target = f" of {result.species_display}" if result.species_display else ""
        if result.user_id:
            await interaction.followup.send(
                f"No public media{target} found for `{result.user_id}` — check the ID, "
                "or pass one of their Macaulay Library asset links."
            )
        else:
            await interaction.followup.send(f"No public media{target} found.")
        return

    catalog = f"https://media.ebird.org/catalog?sort={sort}"
    if result.user_id:
        catalog += f"&userId={result.user_id}"
    if not species:
        catalog += "&mediaType=photo"
    if result.species_code:
        catalog += f"&taxonCode={result.species_code}"
    if result.region:
        catalog += f"&regionCode={result.region}"
    bits = [f"**{header.format(n=len(result.details), sp=result.species_display)}**"]
    if result.display_name:
        bits.append(result.display_name)
    bits.append(f"[full gallery](<{catalog}>)")
    await interaction.followup.send(" · ".join(bits))
    for details in result.details:
        await interaction.followup.send(embed=build_asset_embed(details, compact_flag))


@bot.tree.command(
    name="sp",
    description="Post a user's media of one species (common or scientific name)",
)
@app_commands.describe(
    species="Species — common or scientific name, e.g. 'black oystercatcher'",
    user="Optional: USER… ID, name, @mention, or their asset link — omit for the global best",
    count="How many to post (1–50, default 10)",
    group="Match every species with this in its name (e.g. all puffins; global if no user)",
    region="Limit species matches and media to a region — code (US-WA) or name (washington)",
)
async def sp_command(
    interaction: discord.Interaction,
    species: str,
    user: str = "",
    count: app_commands.Range[int, 1, 50] = 10,
    group: bool = False,
    region: str = "",
) -> None:
    await interaction.response.defer()
    await _send_user_photos(
        interaction, user, count, SORT_BEST, "{n} media of {sp}",
        species=species, species_group=group, region=region,
    )


@bot.tree.command(
    name="top",
    description="Post an eBird user's top highest-rated photos",
)
@app_commands.describe(
    user="Their USER… ID or any ML asset link by them — flags -c, -cc, -ccc as in /checkmedia",
    count="How many photos to post (1–50, default 10)",
)
async def top_command(
    interaction: discord.Interaction,
    user: str,
    count: app_commands.Range[int, 1, 50] = 10,
) -> None:
    await interaction.response.defer()
    await _send_user_photos(interaction, user, count, SORT_BEST, "Top {n} rated photos")


@bot.tree.command(
    name="recent",
    description="Post an eBird user's most recently uploaded photos",
)
@app_commands.describe(
    user="Their USER… ID or any ML asset link by them — flags -c, -cc, -ccc as in /checkmedia",
    count="How many photos to post (1–50, default 10)",
    obs="Sort by observation date/time instead of upload date",
)
async def recent_command(
    interaction: discord.Interaction,
    user: str,
    count: app_commands.Range[int, 1, 50] = 10,
    obs: bool = False,
) -> None:
    await interaction.response.defer()
    if obs:
        await _send_user_photos(
            interaction, user, count, SORT_OBS, "{n} most recent photos by observation date"
        )
    else:
        await _send_user_photos(
            interaction, user, count, SORT_RECENT, "{n} most recently uploaded photos"
        )


DIGEST_BUDGET = 3900  # leave room under Discord's 4096-char description limit


def build_rare_digest(region_code: str, days: int, reports: list[RareReport]) -> discord.Embed:
    """Every report condensed into one embed, no photos."""
    entries: list[str] = []
    used = 0
    for report in reports:
        location = report.location if len(report.location) <= 44 else report.location[:43] + "…"
        observer = report.observer if len(report.observer) <= 24 else report.observer[:23] + "…"
        context = " · ".join(bit for bit in (report.obs_dt, location, observer) if bit)
        camera = " 📷" if report.details else ""
        entry = (
            f"{report.rarity_emoji} **{report.common_name}**{camera} · {report.rarity_label}\n"
            f"{context} · [checklist]({report.checklist_url})"
        )
        if used + len(entry) + 2 > DIGEST_BUDGET:
            entries.append(f"…and {len(reports) - len(entries)} more")
            break
        entries.append(entry)
        used += len(entry) + 2
    embed = discord.Embed(
        title=f"Rare birds in {region_code}",
        description="\n\n".join(entries),
        color=EMBED_COLOR,
    )
    with_photos = sum(1 for report in reports if report.details)
    embed.set_footer(
        text=f"{len(reports)} eBird-confirmed report(s) · last {days} days · "
             f"📷 = has a photo ({with_photos})"
    )
    return embed


def build_rare_embed(report: RareReport, compact_flag: str | None) -> discord.Embed:
    """A rare-bird alert: the photo, who/where/when, and how rare it is."""
    embed = build_embed(
        report.details.photo, checklist_id=report.checklist_id, show_rating=True
    )
    embed.title = f"{report.rarity_emoji} {report.common_name}"
    embed.add_field(name="Rarity", value=report.rarity_display, inline=False)
    if compact_flag == COMPACT_FLAG:
        return embed

    embed.add_field(name="Observed", value=report.obs_dt or "—", inline=True)
    if report.location:
        embed.add_field(name="Location", value=report.location[:1024], inline=True)
    if report.observer:
        embed.add_field(name="Observer", value=report.observer[:1024], inline=True)
    embed.add_field(name="Status", value=report.status_display, inline=True)
    if report.reports_in_window > 1:
        embed.add_field(
            name="Other reports", value=f"{report.reports_in_window} in the window", inline=True
        )
    if compact_flag:
        camera = [(label, value) for label, value, is_cam in select_fields(report.details, compact_flag) if is_cam]
    else:
        camera = list(report.details.exif)
    for label, value in camera:
        if len(embed.fields) >= 25:  # Discord's per-embed field limit
            break
        embed.add_field(name=f"📷 {label}", value=value[:1024], inline=True)
    return embed


@bot.tree.command(
    name="rare",
    description="Recent eBird-confirmed rare bird reports with photos for a region",
)
@app_commands.describe(
    region="eBird region — code (US-WA, US-WA-033) or name (washington); -c/-cc/-ccc flags allowed",
    count="How many reports to post (1–25, default 10)",
    days="How many days back to search (1–30, default 14)",
    repeats="Allow several reports of the same species (default: most recent of each)",
    text="Text-only: drop the photo requirement and post one summary embed",
)
async def rare_command(
    interaction: discord.Interaction,
    region: str,
    count: app_commands.Range[int, 1, 25] = 10,
    days: app_commands.Range[int, 1, 30] = 14,
    repeats: bool = False,
    text: bool = False,
) -> None:
    await interaction.response.defer()
    flags, rest = extract_flags(region.split())
    compact_flag = pick_compact_flag(flags)
    try:
        region_code, reports = await fetch_rare_reports(
            " ".join(rest), count=count, days=days, unique_species=not repeats,
            require_photo=not text,
            include_exif=not text and compact_flag != COMPACT_FLAG,
        )
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return
    if not reports:
        qualifier = "" if text else " with public photos"
        await interaction.followup.send(
            f"No eBird-confirmed rarities{qualifier} in `{region_code}` "
            f"over the last {days} days."
        )
        return

    if text:
        await interaction.followup.send(embed=build_rare_digest(region_code, days, reports))
        return

    learn_names([(r.details.photo.photographer, r.details.photo.user_id) for r in reports if r.details])
    plural = "ies" if len(reports) != 1 else "y"
    await interaction.followup.send(
        f"**{len(reports)} confirmed rarit{plural} with photos** · `{region_code}` · "
        f"last {days} days · [region page](<https://ebird.org/region/{region_code}>)"
    )
    for report in reports:
        await interaction.followup.send(embed=build_rare_embed(report, compact_flag))


@bot.tree.command(
    name="iam",
    description="Link your Discord account to your eBird identity for @mention and name lookups",
)
@app_commands.describe(user="Your USER… ID or any of your Macaulay Library asset links")
async def iam_command(interaction: discord.Interaction, user: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        result = await fetch_user_details(user.strip(), count=1, include_exif=False)
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return
    _registry["discord"][str(interaction.user.id)] = result.user_id
    _save_registry()
    learn_names([(result.display_name, result.user_id)])
    await interaction.followup.send(
        f"Linked! You are **{result.display_name}** (`{result.user_id}`) — your @mention "
        "and display name now work in /top, /recent, and /sp."
    )


def build_alert_embed(
    report: RareReport, subscription: Subscription, kind: str
) -> discord.Embed:
    """One rare-bird alert, sized for a DM."""
    confirming = kind == CONFIRMATION
    lines = []
    if report.sci_name:
        lines.append(f"*{report.sci_name}*")
    lines.append(report.rarity_display)
    if confirming:
        lines.append("eBird has now reviewed and accepted this record.")
    embed = discord.Embed(
        title=("✅ Confirmed: " if confirming else f"{report.rarity_emoji} ") + report.common_name,
        url=report.checklist_url,
        description="\n".join(lines),
        color=RARITY_COLORS.get(report.rarity_label, EMBED_COLOR),
    )
    embed.add_field(name="Status", value=report.status_display, inline=True)
    if report.obs_dt:
        embed.add_field(name="Observed", value=report.obs_dt, inline=True)
    if report.how_many:
        embed.add_field(name="Count", value=str(report.how_many), inline=True)
    if report.location:
        place = f"[{report.location}]({report.map_url})" if report.map_url else report.location
        embed.add_field(name="Location", value=place[:1024], inline=False)
    if report.observer:
        embed.add_field(name="Observer", value=report.observer[:1024], inline=True)
    embed.add_field(
        name="Checklist",
        value=f"[{report.checklist_id}]({report.checklist_url})",
        inline=True,
    )
    if report.details:
        photo = report.details.photo
        embed.set_image(url=photo.image_url(1200))
        embed.add_field(
            name="Photo",
            value=f"[ML{photo.asset_id}]({photo.asset_url}) © {photo.photographer}",
            inline=False,
        )
    embed.set_footer(
        text=f"{subscription.display_region} · {subscription.rarity_label} · /unalert to stop"
    )
    return embed


async def deliver_alert(subscription: Subscription, embed: discord.Embed) -> bool:
    """DM one alert; False when Discord wouldn't take it."""
    try:
        user = bot.get_user(int(subscription.user_id))
        if user is None:
            user = await bot.fetch_user(int(subscription.user_id))
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.NotFound):
        return False  # DMs closed, or the account is gone
    except (discord.HTTPException, ValueError, AttributeError) as error:
        print(f"Alert DM to {subscription.user_id} failed: {error!r}")
        return False


async def poll_region(
    region: str, subscriptions: list[Subscription], session: aiohttp.ClientSession
) -> bool:
    """Alert every subscriber of this region about what they haven't seen. True if state changed."""
    observations = await fetch_notable(region, days=ALERT_WINDOW_DAYS, session=session)
    per_species = Counter(obs.get("speciesCode") for obs in observations)

    # one entry per (checklist, species), keeping the latest review state
    latest: dict[str, dict] = {}
    for obs in observations:
        if notable_status(obs) == STATUS_REJECTED:
            continue  # a record reviewers threw out is not an alert
        key = notable_key(obs)
        prior = latest.get(key)
        if prior is None or (obs.get("obsDt") or "") > (prior.get("obsDt") or ""):
            latest[key] = obs

    # only build reports somebody is actually owed: rarity and photo lookups cost requests
    owed = [
        obs for key, obs in latest.items()
        if any(sub.pending_kind(key, notable_status(obs)) for sub in subscriptions)
    ]
    if not owed:
        return False
    owed.sort(key=lambda obs: obs.get("obsDt") or "", reverse=True)
    pairs = await attach_photos(owed[:ALERT_BUILD_MAX], session=session)
    reports = await build_rare_reports(
        region, pairs, session=session, per_species=per_species
    )
    reports.sort(key=lambda report: report.obs_dt)  # DM oldest first, so DMs read in order

    changed = False
    for subscription in subscriptions:
        sent = 0
        for report in reports:
            key = f"{report.checklist_id}:{report.species_code}"
            kind = subscription.pending_kind(key, report.status)
            if kind is None:
                continue
            if not subscription.wants_rarity(report.rarity_label):
                # record it anyway, or every poll would rebuild the same report
                subscription.mark_seen(key, report.obs_dt, report.status)
                changed = True
                continue
            if sent >= ALERT_DM_MAX:
                break
            if await deliver_alert(subscription, build_alert_embed(report, subscription, kind)):
                subscription.mark_seen(key, report.obs_dt, report.status)
                subscription.alerts_sent += 1
                subscription.last_alert = datetime.now(timezone.utc).isoformat(timespec="seconds")
                subscription.failures = 0
                sent += 1
            else:
                subscription.failures += 1
                if subscription.failures >= ALERT_FAILURE_LIMIT:
                    subscription.paused = True
                    print(
                        f"Pausing alerts for {subscription.user_id} in {subscription.region}: "
                        f"{subscription.failures} DM failures in a row."
                    )
                changed = True
                break  # stop hammering a mailbox that is not accepting DMs
            changed = True
    return changed


async def run_alert_poll() -> None:
    """One sweep over every watched region."""
    regions = store.regions()
    if not regions:
        return
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=ALERT_WINDOW_DAYS + 1)
    ).strftime("%Y-%m-%d %H:%M")
    changed = False
    async with aiohttp.ClientSession() as session:
        for region in regions:
            watchers = [sub for sub in store.active() if sub.region == region]
            if not watchers:
                continue
            try:
                changed |= await poll_region(region, watchers, session)
            except ChecklistError as error:
                print(f"Alert poll for {region} failed: {error}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                print(f"Alert poll for {region} failed: {error!r}")
    for subscription in store.subscriptions:
        changed |= subscription.prune(cutoff)
    if changed:
        store.save()


async def alert_loop() -> None:
    """Poll for new rarities forever; one bad sweep must never kill the loop."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await run_alert_poll()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the loop outranks any single failure
            print(f"Alert poll crashed: {error!r}")
        await asyncio.sleep(ALERT_INTERVAL_SECONDS)


@bot.tree.command(
    name="alert",
    description="DM me whenever a rare bird is reported in a region",
)
@app_commands.describe(
    region="eBird region: a code (US-WA, US-WA-033) or a name (king county wa)",
    rarity="Only alert at this tier or rarer (default: anything eBird flags)",
    confirmations="Also DM when a report you were alerted to is later accepted by eBird",
)
@app_commands.choices(
    rarity=[app_commands.Choice(name=label, value=level) for level, label in RARITY_LEVELS]
)
async def alert_command(
    interaction: discord.Interaction,
    region: str,
    rarity: app_commands.Choice[int] | None = None,
    confirmations: bool = False,
) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        async with aiohttp.ClientSession() as session:
            code = await resolve_region_code(region, session=session)
            label = await region_name(code, session=session)
            # seed with everything already in the window, so subscribing doesn't
            # dump days of backlog into the user's DMs
            backlog = await fetch_notable(code, days=ALERT_WINDOW_DAYS, session=session)
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return

    subscription = Subscription(
        user_id=str(interaction.user.id),
        region=code,
        region_label=label,
        min_rarity=rarity.value if rarity else 0,
        confirmations=confirmations,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    for obs in backlog:
        subscription.mark_seen(notable_key(obs), obs.get("obsDt") or "", notable_status(obs))
    store.add(subscription)
    store.save()

    minutes = ALERT_INTERVAL_SECONDS // 60
    summary = (
        f"Watching **{label}** (`{code}`) for you.\n"
        f"Tier: {subscription.rarity_label} · checked every {minutes} min · "
        f"confirmations: {'on' if confirmations else 'off'}\n"
        f"{len(backlog)} report(s) already in the window were marked as seen, so you'll "
        "only hear about new ones. Use `/alerts` to review or `/unalert` to stop."
    )
    greeting = discord.Embed(
        title=f"🔔 Rare bird alerts on for {label}",
        description=(
            f"You'll get a DM like this when a new rarity is reported.\n"
            f"Tier: {subscription.rarity_label}"
        ),
        color=EMBED_COLOR,
    )
    if not await deliver_alert(subscription, greeting):
        summary += (
            "\n\n⚠️ I couldn't DM you. Enable **Settings → Privacy & Safety → "
            "Direct Messages** for this server, or alerts will pause after "
            f"{ALERT_FAILURE_LIMIT} failures."
        )
    await interaction.followup.send(summary)


@bot.tree.command(name="alerts", description="Show your rare bird alert subscriptions")
async def alerts_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    mine = store.for_user(str(interaction.user.id))
    if not mine:
        await interaction.followup.send(
            "No alert subscriptions yet. Start one with `/alert region:king county wa`."
        )
        return
    lines = []
    for subscription in sorted(mine, key=lambda s: s.region):
        bits = [subscription.rarity_label, f"{subscription.alerts_sent} sent"]
        if subscription.confirmations:
            bits.append("confirmations on")
        if subscription.paused:
            bits.append("⚠️ paused (DMs failed; re-run `/alert` to resume)")
        if subscription.last_alert:
            bits.append(f"last {subscription.last_alert[:16].replace('T', ' ')}")
        lines.append(
            f"**{subscription.display_region}** (`{subscription.region}`)\n{' · '.join(bits)}"
        )
    embed = discord.Embed(
        title="🔔 Your rare bird alerts",
        description="\n\n".join(lines),
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"Checked every {ALERT_INTERVAL_SECONDS // 60} min · /unalert to stop one")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="unalert", description="Stop rare bird alerts for a region")
@app_commands.describe(region="Region to stop watching; leave empty to cancel all of them")
async def unalert_command(interaction: discord.Interaction, region: str = "") -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    if not region.strip():
        removed = store.remove_all(user_id)
        store.save()
        await interaction.followup.send(
            f"Cancelled {removed} alert subscription(s)." if removed
            else "You had no alert subscriptions."
        )
        return
    try:
        code = await resolve_region_code(region)
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return
    dropped = store.remove(user_id, code)
    store.save()
    if dropped is None:
        await interaction.followup.send(
            f"You weren't watching `{code}`. Use `/alerts` to see your subscriptions."
        )
        return
    await interaction.followup.send(f"Stopped alerts for **{dropped.display_region}** (`{code}`).")


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set — copy .env.example to .env and add your bot token."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
