# eBird Discord Bot

A Discord bot for eBird and Macaulay Library media: checklist photo
galleries, asset lookups with camera EXIF, per-user photo rankings, species
searches, regional rarity reports, and rare bird alerts by DM.

| Command | Does |
|---|---|
| `/checklist` | every public photo on a checklist, one message per species |
| `/checkmedia` | one Macaulay Library asset with full metadata and camera EXIF |
| `/top` | a user's highest-rated photos |
| `/recent` | a user's latest uploads |
| `/sp` | media search by species, optionally per user or region |
| `/rare` | recent rarity reports for a region |
| `/alert` `/alerts` `/unalert` | rare bird alert DMs for a region |
| `/iam` | link your Discord account to your eBird identity |

## Where results appear

| Command | Output |
|---|---|
| `/checkmedia` | posted publicly in the channel |
| `/checklist`, `/top`, `/recent`, `/sp`, `/rare` | your DMs, with a dismissable "sent to your DMs" note |
| `/alert`, `/alerts`, `/unalert`, `/iam` | dismissable replies only you can see |

DMs are used because dismissable (ephemeral) replies can't be forwarded and
vanish on reload. Run a command in a DM with the bot and results post there
in place. If your DMs are closed, the bot says so once and falls back to
dismissable replies.

## Photo lookups

```
/checklist S378216909      (or the full checklist URL)
/checkmedia ML662698120    (or the asset URL)
```

`/checklist` groups photos by species, one message each. Embeds that share a
link render as one card with up to four images (Discord's limit); more
photos continue on image-only cards. Metadata comes from each species' first
photo only.

`/checkmedia` posts one asset with all its metadata, a link to its
checklist, and the camera EXIF from the asset page: camera, lens, focal
length, exposure, aperture, ISO, flash, timestamp, and GPS when the file
carried it. Audio and video assets show metadata and a link but no image.

## The `detail` option

`/checklist`, `/checkmedia`, `/top`, `/recent`, `/sp`, and `/rare` take the
same optional `detail` dropdown. Every level keeps the photo, species names,
Macaulay Library link, checklist link, and rating:

| `detail` | Extra fields |
|---|---|
| **Brief** (default) | 📷 Focal length, Observed, Location |
| **Camera** | 📷 Focal length, 📷 Exposure, 📷 Aperture, 📷 ISO, Observed, Location |
| **Full** | everything the asset has, plus all camera EXIF |
| **Minimal** | none |

On `/checklist` the camera rows stay empty (per-photo EXIF would cost one
extra fetch per asset); use `/checkmedia` for a single photo's camera data.

## User photo lists

```
/top USER8940126
/top ML662698120               (any asset link resolves its photographer)
/top USER8940126 count:5 detail:Full
/recent USER8940126            (latest uploads)
/recent USER8940126 obs:True   (sort by observation date instead of upload)
```

`/top` uses Macaulay's "Best quality" ranking. `count` is 1 to 50, default
10. A user can be named by:

- **`USER…` ID** (the `userId=` in any photographer link on
  media.ebird.org), bare digits, or one of their asset links. eBird profile
  URLs don't work; those pages sit behind a sign-in.
- **Display name** or unambiguous fragment (`zorthesosen`). The bot learns
  names from every result it serves; until someone has looked a person up
  by ID or asset link, the name is unknown and the bot says how to teach it.
- **Discord @mention**, once that person has linked themselves with
  `/iam <USER… ID or asset link>`.

After `/iam`, `/top` and `/recent` with no `user` return your own photos.
`/sp` with no user instead searches all of Macaulay Library.

## Species search

```
/sp species:horned puffin                        (global best)
/sp species:black oystercatcher user:USER8940126
/sp species:warbler user:USER8940126 group:True  (all their warblers)
/sp species:warbler region:US-WA group:True      (best warbler media from WA)
```

Matches common or scientific names through eBird's taxonomy (fuzzy). An
exact name wins; an ambiguous one returns candidates instead of a guess.
Audio and video are included (metadata and link, no image).

- **`group:True`** treats the text as a group name. With a user it filters
  their library (best 400 items) for matching names; globally it queries
  every matching taxon (up to 40; broader groups get a "too broad" reply)
  and merges by approximate quality rank.
- **`region`** (code or name) restricts both the name matching and the
  results to that region.

## Rare bird reports

`/rare` posts recent rarities for a region, newest first, as one text
digest; reviewer-accepted and still-unreviewed reports both appear:

```
/rare region:US-WA
/rare region:king county wa count:5 days:7
/rare region:US-WA confirmed:True   (only reviewer-accepted reports)
/rare region:US-WA photos:True      (only reports with a photo, as photo embeds)
/rare region:US-WA photos:True detail:Full
```

- **Region forms**: an eBird code (`US`, `US-WA`, `US-WA-033`), a state or
  country name, a bare state abbreviation (`WA`), or a county with its
  state (`king county wa`, `king wa`, `King County, WA`). Two-letter input
  prefers the US state when it isn't also a country code; use `AU-WA` for
  Western Australia. Ambiguous names return candidates.
- **The digest** is one embed, a line per report: rarity, date, place,
  observer, checklist link. ⚠️ marks reports awaiting review; 📷 marks a
  verified public photo (eBird's `hasRichMedia` flag alone is unreliable).
  Long lists end with "…and N more".
- **`confirmed:True`** keeps only reviewer-accepted (`obsValid`) records.
  Rejected records never appear either way.
- **`photos:True`** keeps only reports with a verified photo and posts each
  as its own embed, with metadata per the `detail` option.
- **`repeats:True`** allows several reports per species (default: most
  recent of each). `days` searches 1 to 30 days back (eBird's limit).

### Alert subscriptions

`/alert` watches a region and DMs you when a new rare bird is reported,
verified or not:

```
/alert region:king county wa
/alert region:US-WA rarity:🟠 Very rare or rarer
/alert region:island county wa confirmations:True
/alerts                    (your subscriptions, each with its 3 latest reports)
/unalert region:US-WA      (no region: cancel all)
```

- Polls every 5 minutes (`ALERT_INTERVAL_SECONDS`) over a 3-day window
  (`ALERT_WINDOW_DAYS`); the window is wider than the interval because
  checklists are often submitted late. Reports are keyed by checklist +
  species, so nothing is ever re-sent.
- Rejected records never alert. Each DM shows the review status.
- **`confirmations:True`** adds a follow-up DM when a report you saw as
  pending is later accepted.
- **`rarity`** sets a floor using the `/rare` tiers.
- Subscribing marks everything already in the window as seen (no backlog
  dump). Re-subscribing updates settings and keeps history.
- `/alerts` lists each subscription with its 3 most recent stored reports:
  species, tier, status, time, checklist link.
- After 3 failed DMs a subscription pauses; re-run `/alert` to resume. At
  most 10 DMs per region per poll; the remainder carries over.
- A region is polled once per sweep regardless of subscriber count; feeds
  are cached 2 minutes and reused by `/rare`. The poll itself always
  fetches fresh.
- State lives in `bot.db` (see **Storage**); a restart never re-sends an
  alert or forgets a subscriber.

### Rarity tiers

eBird publishes no rarity score, so the bot uses the species' share of the
region's photos from earlier years (excluding the current year keeps a
much-photographed mega from reading common):

| Share of the region's prior photos | Label |
|---|---|
| < 0.005% | 🔴 Mega rarity |
| < 0.03% | 🟠 Very rare |
| < 0.12% | 🟡 Rare |
| < 0.40% | 🟢 Scarce |
| ≥ 0.40% | ⚪ Locally notable |

Embeds cite the basis ("8 prior photos in US-WA"). Counties are scored at
state scale (county counts are too small to rank); regions with under 500
prior photos fall back to all-time counts. The measure is photographic
documentation, not sightings.

## Setup

1. Create the app at <https://discord.com/developers/applications>;
   **Bot** tab → **Reset Token** → copy it (no privileged intents needed).
2. Invite it, replacing `CLIENT_ID` with the Application ID from
   *General Information* (`18432` = Send Messages + Embed Links):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=18432
   ```

3. Install and run:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env    # paste your DISCORD_TOKEN; optionally set GUILD_ID
   .venv/bin/python bot.py
   ```

`GUILD_ID` in `.env` (comma-separated server IDs) makes commands appear
instantly in those servers. Commands also register globally, which is what
makes them work in DMs; global registration can take up to an hour the
first time. In a `GUILD_ID` server the guild copy takes precedence, so no
duplicates appear.

### User install

The bot registers as user-installable, so commands can follow your account
into DMs, group DMs, and any server you're in. One portal setting is
required:

1. <https://discord.com/developers/applications> → your app → **Installation**
2. **Installation Contexts**: tick **User Install** (keep Guild Install on)
3. **Default Install Settings** → *User Install*: add the
   `applications.commands` scope
4. Restart the bot, then add it to your account with that page's
   **Install Link**

If User Install isn't enabled in the portal, the bot logs the fix and falls
back to guild install. `USER_INSTALL=false` in `.env` skips it entirely.

In a server where only you have installed the app (the bot isn't a member),
Discord treats it as a guest: replies are visible only to you, and
`/checklist` can't post a gallery the channel can see. Invite the bot for
shared output.

The portal's **General Information** page has *Terms of Service URL* and
*Privacy Policy URL* fields; point them at
[TERMS_OF_SERVICE.md](TERMS_OF_SERVICE.md) and
[PRIVACY_POLICY.md](PRIVACY_POLICY.md), after filling in each file's
bracketed placeholders.

## Command-line tester

```sh
.venv/bin/python ebird_media.py S378216909
.venv/bin/python ebird_media.py -vvv S378216909            # per-photo metadata
.venv/bin/python ebird_media.py rare US-WA 5 7             # rarity digest data
.venv/bin/python ebird_media.py rare US-WA 5 7 photos confirmed
```

The CLI keeps the `-vvv`, `-c`, `-cc`, and `-ccc` flags that the Discord
commands replaced with `detail`.

## Troubleshooting

If the service won't start, run the preflight check on the box as the
service user; it names the exact problem instead of a crash loop:

```sh
cd /opt/ebird-discord-bot && sudo -u ebird .venv/bin/python preflight.py
```

It verifies the project files are present and import cleanly (a common
failure is copying one file but not the others), dependency versions,
directory writability, and `DISCORD_TOKEN`. For the raw traceback:
`journalctl -u ebird-discord-bot -n 40 --no-pager`. A failed command sync
logs the reason and keeps running; it cannot take the bot down.

## Run as a systemd service

[ebird-discord-bot.service](ebird-discord-bot.service) is written for
`/opt/ebird-discord-bot` running as user `ebird`; edit `WorkingDirectory`,
`ExecStart`, and `User` to match. `.env` is read from `WorkingDirectory`
(readable by the service user, ideally `chmod 600`). The service user needs
write access to the directory for `bot.db`:
`chown -R ebird: /opt/ebird-discord-bot`.

```sh
sudo useradd --system --shell /usr/sbin/nologin ebird   # once
sudo cp ebird-discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ebird-discord-bot
journalctl -u ebird-discord-bot -f
```

The unit restarts on crashes (10 s delay) and backs off for 5 minutes after
5 rapid failures. To run it as a user service instead: remove `User=`,
change `WantedBy=` to `default.target`, install to
`~/.config/systemd/user/`, then `systemctl --user enable --now
ebird-discord-bot` and `loginctl enable-linger $USER`.

### Storage

Everything the bot remembers (learned names, `/iam` links, alert
subscriptions and their history) is one SQLite file, `bot.db`, beside
`bot.py`; no extra software needed. It's created readable by the service
user only, and writes are transactions, so a crash can't half-update it.
On the first start after upgrading, old `aliases.json` and
`subscriptions.json` files are imported and renamed `*.migrated`. The
`bot.db-wal` and `bot.db-shm` files are normal SQLite operation. Back up by
copying `bot.db` while stopped, or `sqlite3 bot.db ".backup bot-backup.db"`
while running.

## Notes

- Data comes from the public JSON search API behind media.ebird.org,
  paginated with `initialCursorMark`; no API key.
- Images embed straight from Cornell's CDN; the bot never downloads or
  rehosts photos.
- The bot sends an honest non-browser `User-Agent`
  (`ebird-checklist-discord-bot/1.0`). Cornell challenges browser-like
  clients (Anubis), so switching to a browser UA string breaks the API
  (HTML instead of JSON).
- At most 50 photos post per command (400 fetched via pagination); the
  checklist link covers the rest. Large batches post as Discord's rate
  limits allow.
- Public media only; a hidden checklist comes back as "no public photos".
  Rarities pending review are included (`unconfirmed=incl`) and marked.
- Photos are © their photographers, archived by the Macaulay Library. The
  bot links and embeds rather than copying; keep usage within the
  [Cornell Lab terms of use](https://www.birds.cornell.edu/home/terms-of-use/).
