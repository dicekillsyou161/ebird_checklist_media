# eBird Discord Bot

Discord bot for eBird and Macaulay Library media.

| Command | Does |
|---|---|
| `/checklist` | every public photo on a checklist, one message per species |
| `/checkmedia` | one asset with full metadata and camera EXIF |
| `/top` | a user's highest-rated photos |
| `/recent` | a user's latest uploads |
| `/sp` | media search by species, optionally per user or region |
| `/rare` | recent rarity reports for a region |
| `/alert` `/alerts` `/unalert` | rare bird alert DMs for a region |
| `/iam` | link your Discord account to your eBird identity |

## Where results appear

| Command | Output |
|---|---|
| `/checkmedia` | public, in the channel |
| `/checklist`, `/top`, `/recent`, `/sp`, `/rare` | your DMs, plus a dismissable note |
| `/alert`, `/alerts`, `/unalert`, `/iam` | dismissable replies only you can see |

In a DM with the bot, results post in place. If your DMs are closed, the
bot falls back to dismissable replies. Multi-item results (`/top`,
`/recent`, `/sp`, and `/rare` digests) arrive as one message that pages
with ◀ ▶ buttons, active for 14 minutes.

## Photo lookups

```
/checklist S378216909      (or the full checklist URL)
/checkmedia ML662698120    (or the asset URL)
```

`/checklist`: one message per species; up to four images per card
(Discord's limit), more continue on image-only cards; metadata from each
species' first photo only.

`/checkmedia`: all metadata, checklist link, and the asset page's camera
EXIF (camera, lens, focal length, exposure, aperture, ISO, flash,
timestamp, GPS when present). Audio/video show metadata and a link, no
image.

## The `detail` option

All lookup commands take an optional `detail` dropdown. Every level keeps
the photo, species names, Macaulay link, checklist link, and rating:

| `detail` | Extra fields |
|---|---|
| **Brief** (default) | 📷 Focal length, Observed, Location |
| **Camera** | 📷 Focal length, 📷 Exposure, 📷 Aperture, 📷 ISO, Observed, Location |
| **Full** | everything, plus all camera EXIF |
| **Minimal** | none |

`/checklist` leaves the camera rows empty (EXIF costs one fetch per photo);
use `/checkmedia` for one photo's camera data.

## User photo lists

```
/top USER8940126
/top ML662698120               (an asset link resolves its photographer)
/top USER8940126 count:5 detail:Full
/recent USER8940126 obs:True   (sort by observation date, not upload)
```

`/top` uses Macaulay's "Best quality" ranking. `count`: 1 to 50, default
10. Name a user by:

- **`USER…` ID** (the `userId=` in photographer links on media.ebird.org),
  bare digits, or an asset link. Profile URLs don't work (sign-in walled).
- **Display name** or unambiguous fragment. Names are learned from results
  the bot has served; unknown names get a reply saying how to teach it.
- **@mention**, once that person has run `/iam <USER… ID or asset link>`.

After `/iam`, `/top` and `/recent` with no `user` return your own photos.
`/sp` with no user searches all of Macaulay Library instead.

## Species search

```
/sp species:horned puffin                        (global best)
/sp species:black oystercatcher user:USER8940126
/sp species:warbler user:USER8940126 group:True  (all their warblers)
/sp species:warbler region:US-WA group:True      (best warbler media from WA)
```

Fuzzy match on common or scientific name; exact names win, ambiguous ones
return candidates. Audio and video included (no image in the embed).

- **`group:True`**: name-fragment search. Per user, filters their best 400
  items; globally, queries each matching taxon (up to 40) and merges by
  approximate quality rank.
- **`region`** (code or name): restricts name matching and results to that
  region.

## Rare bird reports

```
/rare region:US-WA
/rare region:king county wa count:5 days:7
/rare region:US-WA confirmed:True   (only reviewer-accepted)
/rare region:US-WA photos:True      (only reports with a photo, as embeds)
```

Default output is one digest message, newest first, confirmed and
unconfirmed both included: a line per report with rarity, date, place and
county, observer, and checklist link (county omitted when the region
searched is a county). ⚠️ = awaiting review, 📷 = verified public photo.

- **Region forms**: eBird code (`US`, `US-WA`, `US-WA-033`), state or
  country name, state abbreviation (`WA`), or county + state
  (`king county wa`, `King County, WA`). Ambiguous names return candidates;
  `AU-WA` for Western Australia.
- **`confirmed:True`**: only reviewer-accepted (`obsValid`) records.
  Rejected records never appear either way.
- **`photos:True`**: only reports with a verified photo, one embed each,
  metadata per `detail`.
- **`repeats:True`**: allow several reports per species (default: most
  recent of each). `days`: 1 to 30. `count`: 1 to 100 (photo mode caps at
  25), default 10.

### Alert subscriptions

```
/alert region:king county wa
/alert region:US-WA rarity:🟠 Very rare or rarer
/alert region:island county wa confirmations:True
/alerts                    (subscriptions, each with its 3 latest reports)
/unalert region:US-WA      (no region: cancel all)
```

DMs you when a new rare bird is reported in the region, verified or not.

- Polls every 5 minutes (`ALERT_INTERVAL_SECONDS`) over a 3-day window
  (`ALERT_WINDOW_DAYS`, wide because checklists arrive late); reports are
  keyed by checklist + species, so nothing repeats.
- Rejected records never alert. Each DM shows review status.
- **`confirmations:True`**: follow-up DM when a pending report is accepted.
- **`rarity`**: floor, using the `/rare` tiers.
- Subscribing marks the current window as seen (no backlog dump);
  re-subscribing updates settings, keeps history.
- 3 failed DMs pause a subscription; re-run `/alert` to resume. Max 10 DMs
  per region per poll, remainder carries over.
- Each region is polled once per sweep regardless of subscriber count;
  feeds are cached 2 minutes for `/rare`, the poll always fetches fresh.
- State lives in `bot.db`; restarts never re-send or forget.

### Rarity tiers

Estimated as the species' share of the region's photos from earlier years
(current year excluded so fresh megas don't read common):

| Share of prior photos | Label |
|---|---|
| < 0.005% | 🔴 Mega rarity |
| < 0.03% | 🟠 Very rare |
| < 0.12% | 🟡 Rare |
| < 0.40% | 🟢 Scarce |
| ≥ 0.40% | ⚪ Locally notable |

Embeds cite the basis ("8 prior photos in US-WA"). Counties score at state
scale; under 500 prior photos falls back to all-time counts.

## Setup

1. Create the app at <https://discord.com/developers/applications>;
   **Bot** tab → **Reset Token** (no privileged intents needed).
2. Invite it (`CLIENT_ID` = Application ID; `18432` = Send Messages +
   Embed Links):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=18432
   ```

3. Install and run:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env    # paste DISCORD_TOKEN; optionally GUILD_ID
   .venv/bin/python bot.py
   ```

`GUILD_ID` (comma-separated server IDs) makes commands appear instantly in
those servers. Commands also register globally, which enables DM use;
global registration can take up to an hour the first time.

### User install

Commands can follow your account into DMs, group DMs, and any server.
Required portal setting:

1. Developer Portal → your app → **Installation**
2. **Installation Contexts**: tick **User Install** (keep Guild Install on)
3. **Default Install Settings** → *User Install*: add
   `applications.commands`
4. Restart the bot, then add it via that page's **Install Link**

If the portal setting is missing, the bot logs the fix and falls back to
guild install. `USER_INSTALL=false` in `.env` skips it. In a server where
only you installed the app, replies are visible only to you; invite the bot
for shared output.

Point the portal's *Terms of Service URL* and *Privacy Policy URL* fields
at [TERMS_OF_SERVICE.md](TERMS_OF_SERVICE.md) and
[PRIVACY_POLICY.md](PRIVACY_POLICY.md) (fill their bracketed placeholders
first).

## Command-line tester

```sh
.venv/bin/python ebird_media.py S378216909
.venv/bin/python ebird_media.py -vvv S378216909
.venv/bin/python ebird_media.py rare US-WA 5 7 [photos] [confirmed]
```

The CLI keeps `-vvv`, `-c`, `-cc`, `-ccc` (replaced by `detail` in
Discord).

## Troubleshooting

```sh
cd /opt/ebird-discord-bot && sudo -u ebird .venv/bin/python preflight.py
```

Preflight checks files, imports, dependencies, directory writability, and
the token. Raw traceback: `journalctl -u ebird-discord-bot -n 40
--no-pager`. A failed command sync logs and keeps running.

## systemd

[ebird-discord-bot.service](ebird-discord-bot.service) assumes
`/opt/ebird-discord-bot` and user `ebird`; edit `WorkingDirectory`,
`ExecStart`, `User` to match. `.env` sits beside `bot.py`. The service user
needs write access for `bot.db`: `chown -R ebird: /opt/ebird-discord-bot`.

```sh
sudo useradd --system --shell /usr/sbin/nologin ebird   # once
sudo cp ebird-discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ebird-discord-bot
journalctl -u ebird-discord-bot -f
```

Restarts on crash (10 s delay); backs off 5 minutes after 5 rapid
failures. User-service variant: remove `User=`, set
`WantedBy=default.target`, install to `~/.config/systemd/user/`,
`systemctl --user enable --now ebird-discord-bot`,
`loginctl enable-linger $USER`.

### Storage

One SQLite file, `bot.db`, beside `bot.py`: learned names, `/iam` links,
subscriptions and history. Created owner-readable only; writes are
transactions. First start after upgrading imports old `aliases.json` /
`subscriptions.json` and renames them `*.migrated`. `bot.db-wal` and
`bot.db-shm` are normal SQLite files. Back up with the bot stopped, or
`sqlite3 bot.db ".backup bot-backup.db"` while running.

## Notes

- Data: the public JSON search API behind media.ebird.org; no API key.
  Images embed from Cornell's CDN; nothing is downloaded or rehosted.
- The `User-Agent` is deliberately non-browser
  (`ebird-checklist-discord-bot/1.0`); Cornell challenges browser-like
  clients, so a browser UA breaks the API.
- Max 50 photos posted per command (400 fetched); the checklist link
  covers the rest.
- Public media only. Rarities pending review are included
  (`unconfirmed=incl`) and marked.
- Photos are © their photographers (Macaulay Library); the bot embeds
  rather than copies. See the
  [Cornell Lab terms of use](https://www.birds.cornell.edu/home/terms-of-use/).
