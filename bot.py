"""Discord bot: /checklist <eBird checklist> posts every public Macaulay Library photo."""
from __future__ import annotations

import os
import re

import discord
from discord import app_commands
from dotenv import load_dotenv

from ebird_media import (
    COMPACT_FLAG,
    VERBOSE_FLAG,
    ChecklistError,
    Photo,
    extract_flags,
    fetch_asset_details,
    fetch_checklist_photos,
    parse_asset_id,
    parse_checklist_id,
)

load_dotenv()

EMBEDS_PER_MESSAGE = 10          # Discord's per-message embed limit
EMBEDS_PER_MESSAGE_VERBOSE = 5   # metadata fields add length; stay under the 6000-char/message cap
MAX_PHOTOS_POSTED = 50           # keep one command from flooding a channel
FIELD_VALUE_MAX = 300            # display cap for one metadata value (Discord allows 1024)
EMBED_COLOR = discord.Color.from_str("#4a7628")  # eBird green


class ChecklistBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        raw = os.getenv("GUILD_ID", "")
        tokens = [token for token in re.split(r"[,\s]+", raw) if token]
        if not all(token.isdigit() for token in tokens):
            raise SystemExit(f"GUILD_ID must be numeric server IDs (comma-separated), got: {raw!r}")
        if tokens:
            # Register instantly in these servers; global sync can take up to an hour.
            for guild_id in tokens:
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
        else:
            await self.tree.sync()


bot = ChecklistBot()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} — /checklist is ready")


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
    compact = COMPACT_FLAG in flags
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

    species_count = len({photo.common_name for photo in photos})
    first = photos[0]
    detail_bits = [bit for bit in (first.photographer, first.obs_date, first.location) if bit]
    plural = "s" if len(photos) != 1 else ""
    await interaction.followup.send(
        f"**{len(photos)} public photo{plural}** · "
        f"{species_count} species · {' · '.join(detail_bits)}\n<{checklist_url}>"
    )

    shown = photos[:MAX_PHOTOS_POSTED]
    per_message = EMBEDS_PER_MESSAGE_VERBOSE if verbose else EMBEDS_PER_MESSAGE
    for start in range(0, len(shown), per_message):
        batch = shown[start:start + per_message]
        await interaction.followup.send(
            embeds=[build_embed(photo, verbose=verbose, show_rating=compact) for photo in batch]
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
    media="Macaulay Library asset link or ML number, e.g. ML662698120 — prefix with -ccc for species+links only"
)
async def checkmedia_command(interaction: discord.Interaction, media: str) -> None:
    await interaction.response.defer()
    flags, rest = extract_flags(media.split())
    compact = COMPACT_FLAG in flags
    try:
        asset_id = parse_asset_id(" ".join(rest))
        details = await fetch_asset_details(asset_id)
    except ChecklistError as error:
        await interaction.followup.send(str(error))
        return

    if compact:
        embed = build_embed(details.photo, checklist_id=details.checklist_id, show_rating=True)
        if details.media_type and details.media_type != "photo":
            embed.set_image(url=None)
        await interaction.followup.send(embed=embed)
        return

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
    await interaction.followup.send(embed=embed)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set — copy .env.example to .env and add your bot token."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
