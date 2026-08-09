"""Discord bot: /checklist <eBird checklist> posts every public Macaulay Library photo."""
from __future__ import annotations

import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from ebird_media import (
    ChecklistError,
    Photo,
    extract_vvv,
    fetch_checklist_photos,
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
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            # Register instantly in one server; global sync can take up to an hour.
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = ChecklistBot()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} — /checklist is ready")


def build_embed(photo: Photo, *, verbose: bool = False) -> discord.Embed:
    lines = []
    if photo.sci_name:
        lines.append(f"*{photo.sci_name}*")
    lines.append(f"[macaulaylibrary.org/asset/{photo.asset_id}]({photo.asset_url})")
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
    verbose, rest = extract_vvv(checklist.split())
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
            embeds=[build_embed(photo, verbose=verbose) for photo in batch]
        )

    if len(photos) > MAX_PHOTOS_POSTED:
        await interaction.followup.send(
            f"…showing the first {MAX_PHOTOS_POSTED} of {len(photos)} photos — "
            f"see the rest at <{checklist_url}>"
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
