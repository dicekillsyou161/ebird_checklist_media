# eBird Checklist Photos: Discord Bot

One slash command: give it an eBird checklist link or ID, and it posts every
public photo from that checklist as embeds; species name, the image itself,
and a link to each photo's Macaulay Library page.

```
/checklist S378216909
/checklist https://ebird.org/checklist/S378216909
/checklist -vvv S378216909
```

Each photo is its own embed: the title links to the Macaulay Library asset
page, the description repeats the link in copyable form, and the footer
carries the ML catalog number and photographer credit. Photos are grouped by
species and posted one message per photo, so each can be forwarded
individually.

Prefix the checklist with `-vvv` to attach each photo's full metadata to its
embed: observed date/time, location, coordinates (linked to a map), age/sex
counts, community rating, pixel dimensions, license ID, eBird species code,
and any notes or tags on the asset.

There is also `/checkmedia` for a single Macaulay Library asset:

```
/checkmedia https://macaulaylibrary.org/asset/662698120
/checkmedia ML662698120
/checkmedia -ccc ML662698120
```

It posts that photo with *all* its metadata in one embed; everything `-vvv`
shows, plus a link back to the asset's checklist and the camera EXIF the
Macaulay Library displays on the asset page: camera make/model, lens, focal
length, exposure, shutter speed, aperture, ISO, flash, capture timestamp, and
GPS coordinates (when the uploaded file carried them). Assets with stripped
EXIF get a "none available" note; audio/video assets show metadata and a link
but no image.

Compact flags trim the embed down; more c's, more cut. All three keep the
photo, species (common + scientific name), the Macaulay Library link, the
checklist link, and the current community rating:

| Flag   | Extra fields shown                                              |
|--------|-----------------------------------------------------------------|
| `-c`   | 📷 Focal length, 📷 Shutter speed, 📷 Aperture, 📷 ISO, Observed, Location |
| `-cc`  | 📷 Focal length, Observed, Location                             |
| `-ccc` | none                                                            |

If several are given, the most-compact wins. On `/checklist`, any compact
flag produces the default embeds plus the rating line (per-photo camera
fields would need one page fetch per photo, so they're `/checkmedia`-only).

Finally, `/top` posts a user's highest-rated photos (Macaulay's
"Best quality" ranking, `sort=rating_rank_desc`), one message per photo,
with the same embeds and compact flags as `/checkmedia`. The optional
`count` parameter picks how many (1–50, default 10):

```
/top USER8940126
/top ML662698120          (any asset by that person; resolves the photographer)
/top -cc USER8940126 count:5
/recent USER8940126       (same, but most recently uploaded instead of top rated)
/recent USER8940126 obs:True   (sort by observation date/time instead of upload date)
/sp species:black oystercatcher user:USER8940126   (one species, all media types)
/sp species:horned puffin                          (no user: the global top-rated)
```

Omitting `user` on `/sp` returns the highest-rated media of that species
across all of Macaulay Library. `group:True` works globally too: the bot
resolves every taxon matching the name (up to 40; broader groups like
"warbler" get a "too broad" reply), queries each one's global best, and
merges the results. Cross-species ordering in that merge approximates
Macaulay's quality rank (vote-count-weighted rating), so it can differ
slightly from the catalog's exact order.

The optional `region` parameter takes an eBird region code (`US`, `US-WA`,
`US-WA-033`) or a plain name ("washington"). It restricts *both* sides of
the search: species-name matching considers only species recorded in that
region (so "warbler" group-searches fine within a state), and the media
results themselves are limited to that region:

```
/sp species:warbler region:US-WA group:True     (best warbler media from WA)
/sp species:horned puffin region:washington     (WA's best Horned Puffins)
```

`/sp` matches the species by common *or* scientific name (fuzzy, via eBird's
own taxonomy autocomplete) and, unlike the photo-only commands, includes
audio and video assets too; those post with metadata and a link but no
image. Results use the best-quality ranking.

An exact name always wins ("yellow warbler" → that species). An ambiguous
name ("warbler") isn't guessed: the bot replies with the closest matches so
you can retry. To genuinely search a *group*, set `group:True`; the bot
pages through the user's library (their best `MAX_PHOTOS`=400 items) and
keeps everything whose common or scientific name contains your text:

```
/sp species:warbler user:USER8940126 group:True   (all warblers they have)
```

Identify the user by their `USER…` ID (click any photographer name on
media.ebird.org; it's the `userId=` in the URL), bare digits, or one of
their asset links. eBird *profile* URLs can't be used: those pages sit
behind a sign-in.

Two friendlier forms also work everywhere a user is accepted:

- **Display name** (`Mark Zorthesosen`, or any unambiguous fragment like
  `zorthesosen`); there's no public eBird name-search API, so the bot
  *learns* names from every command result it sees (stored in
  `aliases.json`). Once anyone has pulled a person's media by ID or asset
  link, their name resolves; before that, the bot replies telling you how to
  teach it. Ambiguous fragments list the people they match.
- **Discord @mention**; after a person links themselves once with
  `/iam <their USER… ID or asset link>` (private/ephemeral reply), their
  @mention works as a user reference.

## Setup

**1. Create the Discord application**

- Go to <https://discord.com/developers/applications> → **New Application**
- **Bot** tab → **Reset Token** → copy the token (no privileged intents needed)

**2. Invite the bot to your server**

Replace `CLIENT_ID` with the Application ID from *General Information*:

```
https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=18432
```

(`18432` = Send Messages + Embed Links.)

**3. Install and run**

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # paste your DISCORD_TOKEN; optionally set GUILD_ID
.venv/bin/python bot.py
```

Set `GUILD_ID` in `.env` (right-click the server → Copy Server ID, with
Developer Mode on) to make the command appear instantly in that server;
comma-separate several IDs to register in multiple servers. Without it, the
command registers globally in every server the bot has joined, which can take
up to an hour on first run.

## Test the fetcher without Discord

```sh
.venv/bin/python ebird_media.py S378216909
.venv/bin/python ebird_media.py -vvv S378216909   # with per-photo metadata
```

Prints every public photo with its species and Macaulay Library link.

## Rare bird reports

`/rare` posts recent **eBird-confirmed** rarities for a region that have
public photos, newest first:

```
/rare region:US-WA
/rare region:king county count:5 days:7
/rare region:-cc US-WA          (compact flags work here too)
```

- **Region**: an eBird code (`US`, `US-WA`, `US-WA-033`) or a name.
- **Confirmed**: only observations a regional reviewer has accepted
  (`obsValid`). Unreviewed reports of the same bird are skipped, so the list
  lags a live rare-bird alert by however long review takes.
- **With photos**: eBird's `hasRichMedia` flag is only a hint (it also covers
  audio, and media can be unindexed), so the bot verifies an actual public
  photo for each report and skips those without one.
- By default it shows the most recent report **per species**; set
  `repeats:True` to allow several reports of the same bird.
- `days` searches 1–30 days back (eBird's own limit).

### How "level of rarity" is estimated

eBird doesn't publish a rarity score, so the bot derives one: a species'
share of all photos taken in that region **in earlier years**. Excluding the
current year matters; a mega that fifty people just photographed would
otherwise look common. The tiers:

| Share of the region's prior photos | Label |
|---|---|
| < 0.005% | 🔴 Mega rarity |
| < 0.03% | 🟠 Very rare |
| < 0.12% | 🟡 Rare |
| < 0.40% | 🟢 Scarce |
| ≥ 0.40% | ⚪ Locally notable |

The embed always shows the raw basis (e.g. "8 prior photos in US-WA"), so
you can judge for yourself. Two caveats: it measures *photographic*
documentation, not sightings, and most eBird-flagged records are ordinary
species out of range or season; those land in the bottom tiers, which is
the honest answer. Regions with fewer than 500 prior photos fall back to
all-time counts.

## Run as a systemd service

The repo ships [ebird-discord-bot.service](ebird-discord-bot.service), written
for a deployment at `/opt/ebird-discord-bot` running as the `ebird` user;
edit the `WorkingDirectory`, `ExecStart`, and `User` lines to match your
setup. `.env` is read from `WorkingDirectory`, so it must sit in the checkout
alongside `bot.py`, readable by the service user (and ideally `chmod 600`).

```sh
sudo useradd --system --shell /usr/sbin/nologin ebird   # once, if it doesn't exist
sudo cp ebird-discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ebird-discord-bot
journalctl -u ebird-discord-bot -f                          # follow logs
```

The unit restarts the bot on crashes (10 s delay) but backs off for 5 minutes
after 5 rapid failures, so a missing or revoked token won't hot-loop.

To run it as a *user* service instead (no root): remove the `User=` line,
change `WantedBy=multi-user.target` to `default.target`, install the file to
`~/.config/systemd/user/`, then `systemctl --user enable --now
ebird-discord-bot` and `loginctl enable-linger $USER` so it keeps running
without an open session.

## How it works

- Photo metadata comes from the public JSON search API behind
  `media.ebird.org` (Macaulay Library media search), filtered by checklist
  (`subId=…`, `mediaType=photo`) and paginated with `initialCursorMark`.
  No API key required.
- Images embed straight from Cornell's CDN
  (`cdn.download.ams.birds.cornell.edu/api/v2/asset/<id>/1200`), which serves
  all clients; the bot never downloads or rehosts photos.
- Cornell fronts its sites with an anti-bot challenge (Anubis) for
  **browser-like** clients. This bot sends an honest, non-browser
  `User-Agent` (`ebird-checklist-discord-bot/1.0`), which the policy lets
  through. Don't "upgrade" it to a browser UA string; that gets challenged
  and the API will return HTML instead of JSON.

## Limits and notes

- Posts at most 50 photos per command (one message each) and links the
  checklist for the rest; fetches at most 400 via pagination. Large batches
  post gradually as Discord's rate limits allow.
- Public media only; a hidden checklist comes back as "no public photos".
- Rarities still pending eBird regional review are included (`unconfirmed=incl`;
  the search index omits them by default) and marked "⚠️ Unconfirmed".
- Photos are © their photographers, archived by the Macaulay Library. The bot
  links and embeds rather than copying; keep usage within the
  [Cornell Lab terms of use](https://www.birds.cornell.edu/home/terms-of-use/).
