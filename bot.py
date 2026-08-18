"""Discord bot: /checklist <eBird checklist> posts every public Macaulay Library photo."""
from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import db
from alerts import (
    CONFIRMATION,
    RARITY_LEVELS,
    STATUS_MARKS,
    AlertStore,
    Subscription,
)
from ebird_media import (
    COMPACT_BRIEF_FLAG,
    COMPACT_CAMERA_FLAG,
    COMPACT_FLAG,
    STATUS_PENDING,
    STATUS_REJECTED,
    AssetDetails,
    ChecklistError,
    Photo,
    SORT_BEST,
    SORT_OBS,
    SORT_RECENT,
    RareReport,
    attach_photos,
    build_rare_reports,
    fetch_asset_details,
    fetch_checklist_photos,
    fetch_notable,
    fetch_rare_reports,
    fetch_user_details,
    notable_key,
    notable_status,
    parse_asset_id,
    parse_checklist_id,
    region_name,
    resolve_region_code,
    select_fields,
    species_rarity,
)

load_dotenv()

MAX_PHOTOS_POSTED = 50  # keep one command from flooding a channel
EMBEDS_PER_MESSAGE = 10  # Discord's per-message embed limit
FIELD_VALUE_MAX = 300            # display cap for one metadata value (Discord allows 1024)
EMBED_COLOR = discord.Color.from_str("#4a7628")  # eBird green
RARITY_COLORS = {  # only used when a subscriber opts into rarity labels
    "Mega rarity": discord.Color.from_str("#c62828"),
    "Very rare": discord.Color.from_str("#ef6c00"),
    "Rare": discord.Color.from_str("#f9a825"),
    "Scarce": discord.Color.from_str("#2e7d32"),
}

# One `detail` option on every command, in place of the old -c/-cc/-ccc/-vvv
# text flags. Values are the internal compact-flag constants; "full" means none.
DETAIL_FULL = "full"
DETAIL_DEFAULT = COMPACT_BRIEF_FLAG  # what you get when `detail` is left unset
DETAIL_CHOICES = [
    app_commands.Choice(
        name="Brief (default): focal length, when and where", value=COMPACT_BRIEF_FLAG
    ),
    app_commands.Choice(
        name="Camera: focal length, exposure, aperture, ISO, when and where",
        value=COMPACT_CAMERA_FLAG,
    ),
    app_commands.Choice(name="Full: every detail, plus camera EXIF", value=DETAIL_FULL),
    app_commands.Choice(name="Minimal: species, links and rating only", value=COMPACT_FLAG),
]
DETAIL_HELP = "How much metadata to show (default: brief)"


def detail_flag(choice: app_commands.Choice[str] | None) -> str | None:
    """The internal compact flag behind a `detail` choice.

    None means full detail; leaving the option unset gives the brief level.
    """
    if choice is None:
        return DETAIL_DEFAULT
    if choice.value == DETAIL_FULL:
        return None
    return choice.value


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

# The context/install API arrived in discord.py 2.4. On anything older the bot
# still runs; it just registers commands the old way (servers and DMs).
SCOPE_API = hasattr(app_commands, "AppCommandContext") and hasattr(
    app_commands, "AppInstallationType"
)


def command_scopes(user_install: bool):
    """(where commands may run, how the app may be installed) for a global sync."""
    if not SCOPE_API:
        return None, None
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

# Everything the bot remembers lives in one SQLite file. Old JSON state files
# are imported on first start and renamed *.migrated (see db.py).
HERE = Path(__file__).resolve().parent
DB = db.connect(HERE / "bot.db")

store = AlertStore(DB, legacy_json=HERE / "subscriptions.json")

# Learned identities: Discord links (via /iam) and display names seen in any
# command's results, so "@mention" and "Mark Zorthesosen" work as user refs.
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_NAMELIKE_RE = re.compile(r"^@?[^\d:/]+$")  # words only: no digits, no URLs


def _migrate_aliases() -> None:
    """One-time import of an aliases.json from before the database."""
    legacy = HERE / "aliases.json"
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except OSError:
        return  # no legacy file: nothing to migrate
    except ValueError as error:
        print(f"Ignoring unreadable {legacy.name}: {error}")
        return
    links = [
        (str(key), str(value))
        for key, value in (data.get("discord") or {}).items() if value
    ]
    names = [
        (str(key), str(entry[0]), str(entry[1]))
        for key, entry in (data.get("names") or {}).items()
        if isinstance(entry, (list, tuple)) and len(entry) == 2
    ]
    with DB:
        DB.executemany(
            "INSERT OR IGNORE INTO discord_links (discord_id, ebird_id) VALUES (?, ?)",
            links,
        )
        DB.executemany(
            "INSERT OR IGNORE INTO names (key, ebird_id, display) VALUES (?, ?, ?)",
            names,
        )
    try:
        legacy.replace(legacy.with_name(legacy.name + ".migrated"))
        print(f"Imported {len(links)} link(s) and {len(names)} name(s) from {legacy.name}.")
    except OSError as error:
        print(f"Imported {legacy.name} but couldn't rename it afterwards: {error}")


_migrate_aliases()


def learn_names(pairs) -> None:
    """Remember (display name, USER… ID) pairs seen in results."""
    rows = []
    for display, user_id in pairs:
        display = (display or "").strip()
        if not display or not user_id or display == user_id:
            continue
        rows.append((display.lower(), user_id, display))
    if rows:
        with DB:
            DB.executemany(
                "INSERT INTO names (key, ebird_id, display) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " ebird_id = excluded.ebird_id, display = excluded.display",
                rows,
            )


def link_discord(discord_id: str, ebird_id: str) -> None:
    """Remember which eBird account a Discord user is (/iam)."""
    with DB:
        DB.execute(
            "INSERT INTO discord_links (discord_id, ebird_id) VALUES (?, ?)"
            " ON CONFLICT(discord_id) DO UPDATE SET ebird_id = excluded.ebird_id",
            (discord_id, ebird_id),
        )


def linked_user(interaction: discord.Interaction) -> str:
    """The eBird ID this Discord account linked with /iam, or "" if none."""
    row = DB.execute(
        "SELECT ebird_id FROM discord_links WHERE discord_id = ?",
        (str(interaction.user.id),),
    ).fetchone()
    return row["ebird_id"] if row else ""


NO_USER_LINKED = (
    "Whose photos? Either name someone (their `USER…` ID, an @mention, or one of "
    "their Macaulay Library asset links), or run `/iam` once with your own ID or "
    "asset link and then you can leave `user` blank."
)


def resolve_alias(text: str) -> str | None:
    """Turn a Discord @mention or a learned display name into a USER… ID.

    Returns None when `text` isn't mention/name-shaped and should go to the
    library resolver (USER IDs, asset links, digits) untouched.
    """
    stripped = text.strip()
    mention = _MENTION_RE.fullmatch(stripped)
    if mention:
        row = DB.execute(
            "SELECT ebird_id FROM discord_links WHERE discord_id = ?",
            (mention.group(1),),
        ).fetchone()
        if row:
            return row["ebird_id"]
        raise ChecklistError(
            "That Discord account isn't linked to an eBird user yet — "
            "they can link it with `/iam` (USER… ID or one of their ML asset links)."
        )
    if not _NAMELIKE_RE.fullmatch(stripped):
        return None
    needle = stripped.lstrip("@").strip().lower()
    exact = DB.execute("SELECT ebird_id FROM names WHERE key = ?", (needle,)).fetchone()
    if exact:
        return exact["ebird_id"]
    hits = {
        (row["ebird_id"], row["display"])
        for row in DB.execute("SELECT key, ebird_id, display FROM names")
        if needle in row["key"]
    }
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
        if not SCOPE_API:
            return  # older discord.py: leave registration at its defaults
        for command in self.tree.get_commands():
            command.allowed_contexts = contexts
            command.allowed_installs = installs

    async def setup_hook(self) -> None:
        self.alert_task = self.loop.create_task(alert_loop(), name="rare-bird-alerts")
        print(f"discord.py {discord.__version__}; user-install API: {SCOPE_API}")
        try:
            await self._register_commands()
        except Exception as error:  # noqa: BLE001 - never crash-loop over registration
            print(
                f"Command registration failed ({error!r}). The bot is still running, "
                "but its commands may be missing or out of date."
            )

    async def _register_commands(self) -> None:
        raw = os.getenv("GUILD_ID", "")
        tokens = [token for token in re.split(r"[,\s]+", raw) if token]
        if not all(token.isdigit() for token in tokens):
            print(
                f"Ignoring GUILD_ID={raw!r}: it must be numeric server IDs, "
                "comma-separated. Registering globally only."
            )
            tokens = []
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


@bot.tree.error
async def on_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """Answer the interaction on any crash, so commands never hang on 'thinking'."""
    cause = getattr(error, "original", error)
    name = interaction.command.name if interaction.command else "?"
    print(f"Command /{name} failed: {cause!r}")
    traceback.print_exception(type(cause), cause, cause.__traceback__)
    message = (
        f"⚠️ `/{name}` hit an internal error ({type(cause).__name__}). "
        "The full traceback is in the bot's log (`journalctl -u ebird-discord-bot`)."
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass  # the interaction may have expired; the log still has the traceback


@bot.event
async def on_ready() -> None:
    watching = len(store.active())
    alerts = (
        f"{watching} rare bird alert subscription(s), polled every "
        f"{ALERT_INTERVAL_SECONDS // 60} min"
        if watching else "no rare bird alert subscriptions yet"
    )
    print(f"Logged in as {bot.user} — /checklist is ready; {alerts}")


class Delivery:
    """Where a command's output goes, so results stay out of busy channels.

    Run from a server, results are DMed to whoever asked: that keeps the
    channel clean and, unlike ephemeral replies, leaves real messages they can
    forward. Run from a DM, results simply post in place. If the user's DMs are
    shut, output falls back to dismissable (ephemeral) replies.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.mode = "dm" if interaction.guild is not None else "here"
        self._channel: discord.abc.Messageable | None = None

    @property
    def private_reply(self) -> bool:
        return self.interaction.guild is not None

    async def send(self, **kwargs) -> discord.Message:
        """One message of results; returns it so callers can edit it later."""
        if self.mode == "dm":
            try:
                if self._channel is None:
                    self._channel = (
                        self.interaction.user.dm_channel
                        or await self.interaction.user.create_dm()
                    )
                return await self._channel.send(**kwargs)
            except (discord.Forbidden, discord.HTTPException):
                self.mode = "ephemeral"  # deliver the rest here instead
                await self.notice(
                    "I couldn't DM you, so this is only visible to you here. Turn on "
                    "Settings → Privacy & Safety → Direct Messages from server members "
                    "to get results in your DMs, where they stay and can be forwarded."
                )
        return await self.interaction.followup.send(
            ephemeral=self.mode == "ephemeral", **kwargs
        )

    async def notice(self, text: str) -> None:
        """A short reply to whoever ran the command, never to the channel."""
        await self.interaction.followup.send(text, ephemeral=self.private_reply)

    async def finish(self, summary: str) -> None:
        """Tell the invoker where the results went, once they have gone."""
        if self.mode == "dm":
            await self.notice(summary)


async def defer_privately(interaction: discord.Interaction) -> Delivery:
    """Defer without showing anything in the channel, and pick a destination."""
    await interaction.response.defer(ephemeral=interaction.guild is not None)
    return Delivery(interaction)


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


GALLERY_MAX = 4  # Discord merges at most four same-url embeds into one card


def group_by_species(photos: list[Photo]) -> list[list[Photo]]:
    """Consecutive runs of one species; fetch order already keeps them together."""
    groups: list[list[Photo]] = []
    for photo in photos:
        if groups and groups[-1][0].common_name == photo.common_name:
            groups[-1].append(photo)
        else:
            groups.append([photo])
    return groups


def build_photo_embed(photo: Photo, *, detail: str | None = None) -> discord.Embed:
    """A checklist photo card at the chosen detail level.

    Checklist photos carry no EXIF (that would need a page fetch per asset), so
    the camera rows of the Camera and Brief levels have nothing to fill in and
    those levels show only when and where.
    """
    embed = build_embed(photo, verbose=detail is None, show_rating=True)
    if detail:
        stand_in = AssetDetails(photo=photo, media_type="photo", checklist_id="", exif=())
        for label, value, is_camera in select_fields(stand_in, detail):
            embed.add_field(
                name=f"📷 {label}" if is_camera else label,
                value=value[:FIELD_VALUE_MAX],
                inline=True,
            )
    return embed


def build_species_messages(
    photos: list[Photo], *, detail: str | None = None
) -> list[list[discord.Embed]]:
    """Embeds for one species, batched into messages.

    Embeds that share a `url` are rendered by Discord as a single card with a
    grid of images, so a species becomes one message instead of one per photo.
    Only the first photo contributes metadata; the rest add just their image.
    Beyond four photos the species spills into further gallery messages.
    """
    messages: list[list[discord.Embed]] = []
    for start in range(0, len(photos), GALLERY_MAX):
        batch = photos[start:start + GALLERY_MAX]
        anchor = batch[0].asset_url  # the shared url is what triggers the merge
        if start == 0:
            lead = build_photo_embed(batch[0], detail=detail)
            if len(photos) > 1:
                lead.set_footer(text=f"{lead.footer.text} • {len(photos)} photos")
        else:
            lead = discord.Embed(color=EMBED_COLOR)
            lead.set_image(url=batch[0].image_url(1200))
        lead.url = anchor
        embeds = [lead]
        for photo in batch[1:]:
            extra = discord.Embed(url=anchor, color=EMBED_COLOR)
            extra.set_image(url=photo.image_url(1200))
            embeds.append(extra)
        messages.append(embeds)
    return messages


@bot.tree.command(
    name="checklist",
    description="Post all public Macaulay Library photos from an eBird checklist",
)
@app_commands.describe(
    checklist="Checklist URL or ID, e.g. S378216909",
    detail=DETAIL_HELP,
)
@app_commands.choices(detail=DETAIL_CHOICES)
async def checklist_command(
    interaction: discord.Interaction,
    checklist: str,
    detail: app_commands.Choice[str] | None = None,
) -> None:
    delivery = await defer_privately(interaction)
    flag = detail_flag(detail)
    try:
        sub_id = parse_checklist_id(checklist)
        photos = await fetch_checklist_photos(sub_id)
    except ChecklistError as error:
        await delivery.notice(str(error))
        return

    checklist_url = f"https://ebird.org/checklist/{sub_id}"
    if not photos:
        await delivery.notice(
            f"No public photos found on <{checklist_url}> — the checklist may be "
            "private, have no photos yet, or the ID may be wrong."
        )
        return

    learn_names((photo.photographer, photo.user_id) for photo in photos)
    species_count = len({photo.common_name for photo in photos})
    first = photos[0]
    detail_bits = [bit for bit in (first.photographer, first.obs_date, first.location) if bit]
    plural = "s" if len(photos) != 1 else ""
    header = (
        f"**{len(photos)} public photo{plural}** · "
        f"{species_count} species · {' · '.join(detail_bits)}\n<{checklist_url}>"
    )
    if len(photos) > MAX_PHOTOS_POSTED:
        header += f"\n…showing the first {MAX_PHOTOS_POSTED} photos; the rest are at the link"

    # one page per species: its photos ride along as gallery cards on that page
    pages: list[list[discord.Embed]] = []
    posted = 0
    for group in group_by_species(photos):
        if posted >= MAX_PHOTOS_POSTED:
            break
        allowed = group[:MAX_PHOTOS_POSTED - posted]
        page: list[discord.Embed] = []
        for embeds in build_species_messages(allowed, detail=flag):
            if page and len(page) + len(embeds) > EMBEDS_PER_MESSAGE:
                pages.append(page)  # a species with very many photos spills over
                page = []
            page.extend(embeds)
        if page:
            pages.append(page)
        posted += len(allowed)

    if len(pages) == 1:
        await delivery.send(content=header, embeds=pages[0])
    else:
        pager = EmbedPager(pages, interaction.user.id)
        pager.message = await delivery.send(content=header, embeds=pages[0], view=pager)
    await delivery.finish(
        f"Sent {posted} photo(s) from `{sub_id}` to your DMs"
        + (" as one paged message." if len(pages) > 1 else ".")
    )


@bot.tree.command(
    name="checkmedia",
    description="Post one Macaulay Library photo with all its metadata, including camera EXIF",
)
@app_commands.describe(
    media="Macaulay Library asset link or ML number, e.g. ML662698120",
    detail=DETAIL_HELP,
)
@app_commands.choices(detail=DETAIL_CHOICES)
async def checkmedia_command(
    interaction: discord.Interaction,
    media: str,
    detail: app_commands.Choice[str] | None = None,
) -> None:
    await interaction.response.defer()
    compact_flag = detail_flag(detail)
    try:
        asset_id = parse_asset_id(media)
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
    compact_flag: str | None = None,
    delivery: Delivery | None = None,
) -> None:
    """Shared body of /top, /recent, /sp; `header` is formatted with n and sp."""
    delivery = delivery or Delivery(interaction)
    try:
        user_ref = user.strip()
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
        await delivery.notice(str(error))
        return
    learn_names(
        [(result.display_name, result.user_id)]
        + [(d.photo.photographer, d.photo.user_id) for d in result.details]
    )
    if not result.details:
        target = f" of {result.species_display}" if result.species_display else ""
        if result.user_id:
            await delivery.notice(
                f"No public media{target} found for `{result.user_id}` — check the ID, "
                "or pass one of their Macaulay Library asset links."
            )
        else:
            await delivery.notice(f"No public media{target} found.")
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
    pages = [build_asset_embed(details, compact_flag) for details in result.details]
    for number, embed in enumerate(pages, start=1):
        if len(pages) > 1:
            footer = embed.footer.text or ""
            page = f"{number}/{len(pages)}"
            embed.set_footer(text=f"{footer} · {page}" if footer else page)
    if len(pages) == 1:
        await delivery.send(content=" · ".join(bits), embed=pages[0])
        await delivery.finish("Sent 1 item to your DMs.")
        return
    pager = EmbedPager(pages, interaction.user.id)
    pager.message = await delivery.send(
        content=" · ".join(bits), embed=pages[0], view=pager
    )
    await delivery.finish(
        f"Sent {len(pages)} item(s) to your DMs as one paged message."
    )


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
    detail=DETAIL_HELP,
)
@app_commands.choices(detail=DETAIL_CHOICES)
async def sp_command(
    interaction: discord.Interaction,
    species: str,
    user: str = "",
    count: app_commands.Range[int, 1, 50] = 10,
    group: bool = False,
    region: str = "",
    detail: app_commands.Choice[str] | None = None,
) -> None:
    delivery = await defer_privately(interaction)
    await _send_user_photos(
        interaction, user, count, SORT_BEST, "{n} media of {sp}",
        species=species, species_group=group, region=region,
        compact_flag=detail_flag(detail), delivery=delivery,
    )


@bot.tree.command(
    name="top",
    description="Post an eBird user's top highest-rated photos",
)
@app_commands.describe(
    user="Their USER… ID, name, @mention, or ML asset link; omit for your own (see /iam)",
    count="How many photos to post (1–50, default 10)",
    detail=DETAIL_HELP,
)
@app_commands.choices(detail=DETAIL_CHOICES)
async def top_command(
    interaction: discord.Interaction,
    user: str = "",
    count: app_commands.Range[int, 1, 50] = 10,
    detail: app_commands.Choice[str] | None = None,
) -> None:
    delivery = await defer_privately(interaction)
    whose = user.strip() or linked_user(interaction)
    if not whose:
        await delivery.notice(NO_USER_LINKED)
        return
    await _send_user_photos(
        interaction, whose, count, SORT_BEST, "Top {n} rated photos",
        compact_flag=detail_flag(detail), delivery=delivery,
    )


@bot.tree.command(
    name="recent",
    description="Post an eBird user's most recently uploaded photos",
)
@app_commands.describe(
    user="Their USER… ID, name, @mention, or ML asset link; omit for your own (see /iam)",
    count="How many photos to post (1–50, default 10)",
    obs="Sort by observation date/time instead of upload date",
    detail=DETAIL_HELP,
)
@app_commands.choices(detail=DETAIL_CHOICES)
async def recent_command(
    interaction: discord.Interaction,
    user: str = "",
    count: app_commands.Range[int, 1, 50] = 10,
    obs: bool = False,
    detail: app_commands.Choice[str] | None = None,
) -> None:
    delivery = await defer_privately(interaction)
    whose = user.strip() or linked_user(interaction)
    if not whose:
        await delivery.notice(NO_USER_LINKED)
        return
    flag = detail_flag(detail)
    if obs:
        await _send_user_photos(
            interaction, whose, count, SORT_OBS, "{n} most recent photos by observation date",
            compact_flag=flag, delivery=delivery,
        )
    else:
        await _send_user_photos(
            interaction, whose, count, SORT_RECENT, "{n} most recently uploaded photos",
            compact_flag=flag, delivery=delivery,
        )


DIGEST_BUDGET = 3900  # leave room under Discord's 4096-char description limit
MAX_RARE_PHOTO_POSTS = 25  # photo mode is one message per report; digests page instead


def place_context(region_code: str, county: str, state: str) -> str:
    """What locates a report within the queried region: nothing for a county
    query, the county for a state query, county and state for a country."""
    depth = region_code.count("-")
    if depth >= 2:
        return ""
    if depth == 1:
        return county
    return ", ".join(bit for bit in (county, state) if bit)


def build_rare_digest(
    region_code: str, days: int, reports: list[RareReport], show_rarity: bool = False
) -> list[discord.Embed]:
    """The reports condensed into text pages, each within Discord's embed cap."""
    entries: list[str] = []
    for report in reports:
        location = report.location if len(report.location) <= 44 else report.location[:43] + "…"
        place = place_context(region_code, report.county, report.state)
        if place:
            location = f"{location} ({place})" if location else place
        observer = report.observer if len(report.observer) <= 24 else report.observer[:23] + "…"
        context = " · ".join(bit for bit in (report.obs_dt, location, observer) if bit)
        camera = " 📷" if report.details else ""
        pending = " ⚠️" if report.status == STATUS_PENDING else ""
        if show_rarity:
            name = (
                f"{report.rarity_emoji} **{report.common_name}**{pending}{camera}"
                f" · {report.rarity_label}"
            )
        else:
            name = f"**{report.common_name}**{pending}{camera}"
        entries.append(f"{name}\n{context} · [checklist]({report.checklist_url})")

    chunks: list[list[str]] = [[]]
    used = 0
    for entry in entries:
        if chunks[-1] and used + len(entry) + 2 > DIGEST_BUDGET:
            chunks.append([])
            used = 0
        chunks[-1].append(entry)
        used += len(entry) + 2

    with_photos = sum(1 for report in reports if report.details)
    unconfirmed = sum(1 for report in reports if report.status == STATUS_PENDING)
    pages: list[discord.Embed] = []
    first = 1
    for number, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"Rare birds in {region_code}",
            description="\n\n".join(chunk),
            color=EMBED_COLOR,
        )
        bits = [
            f"{len(reports)} report(s)",
            f"last {days} days",
            f"📷 = has a photo ({with_photos})",
        ]
        if unconfirmed:
            bits.append(f"⚠️ = unconfirmed ({unconfirmed})")
        if len(chunks) > 1:
            bits.insert(0, f"Page {number}/{len(chunks)} · reports {first}–{first + len(chunk) - 1}")
        embed.set_footer(text=" · ".join(bits))
        first += len(chunk)
        pages.append(embed)
    return pages


# Slightly under 15 minutes: an ephemeral digest is edited through the
# interaction webhook, whose token expires then; quitting first means the
# timeout edit that disables the buttons still goes through.
PAGER_TIMEOUT = 840


class EmbedPager(discord.ui.View):
    """Back/Next buttons that page one message through embeds (or embed groups).

    A page is either one embed or a list of embeds shown together, so a
    /checklist species gallery (several embeds merged into one card) can be
    a single page.
    """

    def __init__(
        self, pages: list[discord.Embed | list[discord.Embed]], owner_id: int
    ) -> None:
        super().__init__(timeout=PAGER_TIMEOUT)
        self.pages = [
            [page] if isinstance(page, discord.Embed) else list(page) for page in pages
        ]
        self.owner_id = owner_id
        self.index = 0
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.back.disabled = self.index == 0
        self.forward.disabled = self.index == len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embeds=self.pages[self.index], view=self)

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = max(self.index - 1, 0)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pass  # position indicator only; never enabled

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def forward(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = min(self.index + 1, len(self.pages) - 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass  # the message may be gone; nothing to clean up


def build_rare_embed(
    report: RareReport, compact_flag: str | None, show_rarity: bool = False
) -> discord.Embed:
    """One rare-bird report: the photo, who/where/when; rarity tier on request."""
    embed = build_embed(
        report.details.photo, checklist_id=report.checklist_id, show_rating=True
    )
    if show_rarity:
        embed.title = f"{report.rarity_emoji} {report.common_name}"
        embed.add_field(name="Rarity", value=report.rarity_display, inline=False)
    else:
        embed.title = report.common_name
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
    description="Recent rare bird reports for a region, confirmed or not",
)
@app_commands.describe(
    region="eBird region — code (US-WA, US-WA-033) or name (king county wa)",
    count="How many reports (1–100, default 10; photos:True caps at 25)",
    days="How many days back to search (1–30, default 14)",
    repeats="Allow several reports of the same species (default: most recent of each)",
    confirmed="Only reports eBird reviewers have accepted (default: unconfirmed included)",
    photos="Only reports with a public photo, posted as photo embeds (default: one text digest)",
    rarity="Also show an estimated rarity tier per report (default: off, like eBird's alerts)",
    detail=DETAIL_HELP,
)
@app_commands.choices(detail=DETAIL_CHOICES)
async def rare_command(
    interaction: discord.Interaction,
    region: str,
    count: app_commands.Range[int, 1, 100] = 10,
    days: app_commands.Range[int, 1, 30] = 14,
    repeats: bool = False,
    confirmed: bool = False,
    photos: bool = False,
    rarity: bool = False,
    detail: app_commands.Choice[str] | None = None,
) -> None:
    delivery = await defer_privately(interaction)
    compact_flag = detail_flag(detail)
    if photos:
        count = min(count, MAX_RARE_PHOTO_POSTS)  # one message per report in photo mode
    try:
        region_code, reports = await fetch_rare_reports(
            region, count=count, days=days, unique_species=not repeats,
            require_photo=photos, confirmed_only=confirmed,
            include_exif=photos and compact_flag != COMPACT_FLAG,
        )
    except ChecklistError as error:
        await delivery.notice(str(error))
        return
    if not reports:
        kind = "eBird-confirmed rarities" if confirmed else "rarities"
        qualifier = " with public photos" if photos else ""
        await delivery.notice(
            f"No {kind}{qualifier} in `{region_code}` over the last {days} days."
        )
        return

    if not photos:
        pages = build_rare_digest(region_code, days, reports, show_rarity=rarity)
        if len(pages) == 1:
            await delivery.send(embed=pages[0])
        else:
            pager = EmbedPager(pages, interaction.user.id)
            pager.message = await delivery.send(embed=pages[0], view=pager)
        await delivery.finish(f"Sent the `{region_code}` rarity digest to your DMs.")
        return

    learn_names([(r.details.photo.photographer, r.details.photo.user_id) for r in reports if r.details])
    plural = "ies" if len(reports) != 1 else "y"
    kind = "confirmed " if confirmed else ""
    await delivery.send(
        content=f"**{len(reports)} {kind}rarit{plural} with photos** · `{region_code}` · "
        f"last {days} days · [region page](<https://ebird.org/region/{region_code}>)"
    )
    for report in reports:
        await delivery.send(embed=build_rare_embed(report, compact_flag, show_rarity=rarity))
    await delivery.finish(f"Sent {len(reports)} rarity report(s) to your DMs.")


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
        await interaction.followup.send(str(error), ephemeral=True)
        return
    link_discord(str(interaction.user.id), result.user_id)
    learn_names([(result.display_name, result.user_id)])
    await interaction.followup.send(
        f"Linked! You are **{result.display_name}** (`{result.user_id}`). You can now "
        "run `/top` and `/recent` without naming a user to get your own photos, and "
        "your @mention and display name work anywhere a user is asked for.",
        ephemeral=True,
    )


def build_alert_embed(
    report: RareReport, subscription: Subscription, kind: str
) -> discord.Embed:
    """One rare-bird alert, sized for a DM."""
    confirming = kind == CONFIRMATION
    show_rarity = subscription.show_rarity
    lines = []
    if report.sci_name:
        lines.append(f"*{report.sci_name}*")
    if show_rarity:
        lines.append(report.rarity_display)
    if confirming:
        lines.append("eBird has now reviewed and accepted this record.")
    if confirming:
        title = "✅ Confirmed: " + report.common_name
    elif show_rarity:
        title = f"{report.rarity_emoji} {report.common_name}"
    else:
        title = report.common_name
    embed = discord.Embed(
        title=title,
        url=report.checklist_url,
        description="\n".join(lines),
        color=RARITY_COLORS.get(report.rarity_label, EMBED_COLOR) if show_rarity else EMBED_COLOR,
    )
    embed.add_field(name="Status", value=report.status_display, inline=True)
    if report.obs_dt:
        embed.add_field(name="Observed", value=report.obs_dt, inline=True)
    if report.how_many:
        embed.add_field(name="Count", value=str(report.how_many), inline=True)
    if report.location:
        place = f"[{report.location}]({report.map_url})" if report.map_url else report.location
        context = place_context(subscription.region, report.county, report.state)
        if context:
            place = f"{place} ({context})"
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
    embed.set_footer(text=f"{subscription.display_region} · /unalert to stop")
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
    # max_age=0: alerts must never run on a cached feed, or a poll could miss
    # reports that landed since the last fetch
    observations = await fetch_notable(
        region, days=ALERT_WINDOW_DAYS, session=session, max_age=0
    )
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
            rarity = f"{report.rarity_emoji} {report.rarity_label}".strip()
            kind = subscription.pending_kind(key, report.status)
            if kind is None:
                continue
            if not subscription.wants_rarity(report.rarity_label):
                # record it anyway, or every poll would rebuild the same report
                subscription.mark_seen(
                    key, report.obs_dt, report.status, report.common_name, rarity,
                    place_context(subscription.region, report.county, report.state),
                )
                changed = True
                continue
            if sent >= ALERT_DM_MAX:
                break
            if await deliver_alert(subscription, build_alert_embed(report, subscription, kind)):
                subscription.mark_seen(
                    key, report.obs_dt, report.status, report.common_name, rarity,
                    place_context(subscription.region, report.county, report.state),
                )
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
    show_rarity="Show an estimated rarity tier in each alert (default: off, like eBird's alerts)",
)
@app_commands.choices(
    rarity=[app_commands.Choice(name=label, value=level) for level, label in RARITY_LEVELS]
)
async def alert_command(
    interaction: discord.Interaction,
    region: str,
    rarity: app_commands.Choice[int] | None = None,
    confirmations: bool = False,
    show_rarity: bool = False,
) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        async with aiohttp.ClientSession() as session:
            code = await resolve_region_code(region, session=session)
            label = await region_name(code, session=session)
            # seed with everything already in the window, so subscribing doesn't
            # dump days of backlog into the user's DMs
            backlog = await fetch_notable(code, days=ALERT_WINDOW_DAYS, session=session)
            # tier each seeded species now so /alerts can show it later; one
            # cached baseline fetch, then the per-species lookups are free
            tiers: dict[str, str] = {}
            for species_code in {o.get("speciesCode") for o in backlog}:
                if not species_code:
                    continue
                tier, emoji, share, _ = await species_rarity(code, species_code, session)
                tiers[species_code] = f"{emoji} {tier}".strip() if share is not None else ""
    except ChecklistError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return

    subscription = Subscription(
        user_id=str(interaction.user.id),
        region=code,
        region_label=label,
        min_rarity=rarity.value if rarity else 0,
        confirmations=confirmations,
        show_rarity=show_rarity,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    for obs in backlog:
        subscription.mark_seen(
            notable_key(obs),
            obs.get("obsDt") or "",
            notable_status(obs),
            species=obs.get("comName") or "",
            rarity=tiers.get(obs.get("speciesCode") or "", ""),
            place=place_context(
                code, obs.get("subnational2Name") or "", obs.get("subnational1Name") or ""
            ),
        )
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
    await interaction.followup.send(summary, ephemeral=True)


def seen_line(key: str, value: list[str]) -> str:
    """One remembered report as a digest line for /alerts."""
    obs_dt, status, species, rarity, place = value
    checklist_id, _, species_code = key.partition(":")
    name = species or species_code or checklist_id
    bits = [
        rarity,
        f"**{name}** {STATUS_MARKS.get(status, '')}".strip(),
        place,
        obs_dt,
        f"[{checklist_id}](https://ebird.org/checklist/{checklist_id})",
    ]
    return "> " + " · ".join(bit for bit in bits if bit)


ALERTS_LIST_BUDGET = 4000  # embed descriptions cap at 4096


@bot.tree.command(name="alerts", description="Show your rare bird alert subscriptions")
async def alerts_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    mine = store.for_user(str(interaction.user.id))
    if not mine:
        await interaction.followup.send(
            "No alert subscriptions yet. Start one with `/alert region:king county wa`.",
            ephemeral=True,
        )
        return

    # rows recorded before tiers/places were stored have blanks; fill them in
    # (tiers from the cached season baselines, places from the notable feed,
    # which still covers every row because seen rows prune at the window edge)
    filled = False
    async with aiohttp.ClientSession() as session:
        for subscription in mine:
            feed: dict[str, dict] | None = None
            for key, value in subscription.recent_seen(3):
                species_code = key.partition(":")[2]
                if not value[3] and species_code:
                    tier, emoji, share, _ = await species_rarity(
                        subscription.region, species_code, session
                    )
                    if share is not None:
                        value[3] = f"{emoji} {tier}".strip()
                        filled = True
                if not value[4] and subscription.region.count("-") < 2:
                    if feed is None:
                        try:
                            feed = {
                                notable_key(obs): obs for obs in await fetch_notable(
                                    subscription.region, days=ALERT_WINDOW_DAYS,
                                    session=session,
                                )
                            }
                        except (ChecklistError, aiohttp.ClientError, asyncio.TimeoutError):
                            feed = {}
                    obs = feed.get(key)
                    if obs is not None:
                        value[4] = place_context(
                            subscription.region,
                            obs.get("subnational2Name") or "",
                            obs.get("subnational1Name") or "",
                        )
                        filled = filled or bool(value[4])
    if filled:
        store.save()

    blocks = []
    for subscription in sorted(mine, key=lambda s: s.region):
        bits = [subscription.rarity_label, f"{subscription.alerts_sent} sent"]
        if subscription.confirmations:
            bits.append("confirmations on")
        if subscription.show_rarity:
            bits.append("rarity labels on")
        if subscription.paused:
            bits.append("⚠️ paused (DMs failed; re-run `/alert` to resume)")
        if subscription.last_alert:
            bits.append(f"last {subscription.last_alert[:16].replace('T', ' ')}")
        lines = [
            f"**{subscription.display_region}** (`{subscription.region}`)",
            " · ".join(bits),
        ]
        lines += [seen_line(key, value) for key, value in subscription.recent_seen(3)]
        blocks.append("\n".join(lines))
    kept: list[str] = []
    used = 0
    for block in blocks:
        if used + len(block) + 2 > ALERTS_LIST_BUDGET:
            kept.append(f"…and {len(blocks) - len(kept)} more region(s)")
            break
        kept.append(block)
        used += len(block) + 2
    embed = discord.Embed(
        title="🔔 Your rare bird alerts",
        description="\n\n".join(kept),
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"Checked every {ALERT_INTERVAL_SECONDS // 60} min · /unalert to stop one")
    await interaction.followup.send(embed=embed, ephemeral=True)


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
            else "You had no alert subscriptions.",
            ephemeral=True,
        )
        return
    try:
        code = await resolve_region_code(region)
    except ChecklistError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return
    dropped = store.remove(user_id, code)
    store.save()
    if dropped is None:
        await interaction.followup.send(
            f"You weren't watching `{code}`. Use `/alerts` to see your subscriptions.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Stopped alerts for **{dropped.display_region}** (`{code}`).", ephemeral=True
    )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set — copy .env.example to .env and add your bot token."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
