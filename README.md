# eBird Checklist Photos Discord Bot

One slash command: give it an eBird checklist link or ID, and it posts every
public photo from that checklist as embeds; species name, the image itself,
and a link to each photo's Macaulay Library page.

```
/checklist S378216909
/checklist https://ebird.org/checklist/S378216909
/checklist S378216909 detail:Minimal
```

Photos are grouped by species, one message per species. Discord renders
embeds that share a link as a single card with a grid of images, so all of a
species' photos ride on one card: the title links to the Macaulay Library
asset page, the description repeats the link in copyable form, and the footer
carries the ML catalog number, photographer credit, and the photo count.
Only the first photo of each species contributes metadata; the rest add just
their image, which keeps a 24-photo checklist to 10 messages instead of 24.

A card holds at most four images (Discord's limit), so a species with more
photos continues on further image-only cards. For the full metadata or the
camera EXIF of any individual photo, pass its catalog number to
`/checkmedia ML662698120`.

There is also `/checkmedia` for a single Macaulay Library asset:

```
/checkmedia https://macaulaylibrary.org/asset/662698120
/checkmedia ML662698120
/checkmedia ML662698120 detail:Minimal
```

It posts that photo with *all* its metadata in one embed, plus a link back to
the asset's checklist and the camera EXIF the Macaulay Library displays on
the asset page: camera make/model, lens, focal length, exposure, aperture,
ISO, flash, capture timestamp, and GPS coordinates (when the uploaded file
carried them). Assets with stripped EXIF get a "none available"
note; audio/video assets show metadata and a link but no image.

## Where results appear

Only `/checkmedia` posts to the channel for everyone to see, which makes it
the one to reach for when sharing a photo. Everything else keeps the channel
clean:

| Command | Output |
|---|---|
| `/checkmedia` | posted publicly in the channel |
| `/checklist`, `/top`, `/recent`, `/sp`, `/rare` | DMed to whoever ran it, with a dismissable "sent to your DMs" note |
| `/alert`, `/alerts`, `/unalert`, `/iam` | dismissable replies only you can see |

Results go to DMs rather than dismissable replies because dismissable
(ephemeral) messages cannot be forwarded and vanish when the client reloads;
DMs stay put and can be forwarded one by one. Run any of these commands
*inside* a DM with the bot and the results simply post there in place. If
your DMs are closed, the bot says so once and falls back to dismissable
replies so nothing is lost.

## The `detail` option

`/checklist`, `/checkmedia`, `/top`, `/recent`, `/sp`, and `/rare` all take
the same optional `detail` option, picked from a dropdown. Every level keeps
the photo, species (common + scientific name), the Macaulay Library link, the
checklist link, and the current community rating; the levels differ in what
else they add:

| `detail` | Extra fields shown |
|---|---|
| **Brief** (default) | 📷 Focal length, Observed, Location |
| **Camera** | 📷 Focal length, 📷 Exposure, 📷 Aperture, 📷 ISO, Observed, Location |
| **Full** | everything the asset has, plus all camera EXIF |
| **Minimal** | none |

Leaving it unset gives Brief, so results stay readable; ask for **Full** when
you want the whole record. On `/checklist` the camera rows have nothing to
fill in, because per-photo EXIF would need one extra page fetch per asset, so
Brief and Camera both show just Observed and Location there; use
`/checkmedia ML…` for a single photo's camera data.

Finally, `/top` posts a user's highest-rated photos (Macaulay's
"Best quality" ranking, `sort=rating_rank_desc`), one message per photo,
with the same embeds and `detail` levels as `/checkmedia`. The optional
`count` parameter picks how many (1–50, default 10):

```
/top USER8940126
/top ML662698120          (any asset by that person; resolves the photographer)
/top USER8940126 count:5 detail:Full
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

Linking with `/iam` also lets you drop the `user` option entirely on `/top`
and `/recent` to get your own photos:

```
/iam ML662698120     (once)
/top                 (your own top-rated photos)
/recent count:5      (your own latest uploads)
```

Without a link, those commands reply with a note telling you to name someone
or run `/iam`. `/sp` is deliberately different: leaving `user` blank there
means a global search across all of Macaulay Library, not your own photos.

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
Developer Mode on) to make the commands appear instantly in that server;
comma-separate several IDs to register in multiple servers.

The bot always registers its commands globally as well, which is what makes
them work **in a DM with the bot**, not just in a server. Every command is
available either way and behaves identically; none of them read server state.
Global registration can take up to an hour to appear the first time, so a
fresh bot works in your `GUILD_ID` server immediately but in DMs only once
Discord has propagated it. Inside a `GUILD_ID` server the instant guild copy
takes precedence over the global one, so you should not see duplicates; if
you ever do, clear `GUILD_ID` and rely on the global registration alone.

DM usage is a good fit for `/alert`, `/alerts`, and `/unalert`, since those
reply privately and deliver to your DMs anyway.

### Installing to your account (user install)

The bot registers as **user-installable**, so the commands can travel with
your Discord account instead of only living in servers the bot has joined:
DMs, group DMs, and any server you're in. This needs one setting flipped in
the Developer Portal, which the code cannot do for you:

1. <https://discord.com/developers/applications> → your app → **Installation**
2. Under **Installation Contexts**, tick **User Install** (keep Guild Install on)
3. Under **Default Install Settings** → *User Install*, add the
   `applications.commands` scope
4. Restart the bot, then use the **Install Link** from that page (or the
   app's profile → *Add App* → *Try it yourself*) to add it to your account

If User Install isn't enabled in the portal, Discord rejects the sync; the
bot notices, prints exactly what to change, and automatically falls back to
guild install so servers and DMs keep working. Set `USER_INSTALL=false` in
`.env` to skip user install entirely.

One limit worth knowing: in a server where only *you* have installed the app
(the bot itself isn't a member), Discord treats the app as a guest. Command
replies are visible only to you there, which suits lookups like `/checkmedia`
but means `/checklist` won't post a photo gallery the whole channel can see.
Invite the bot to that server if you want shared output.

## Test the fetcher without Discord

```sh
.venv/bin/python ebird_media.py S378216909
.venv/bin/python ebird_media.py -vvv S378216909   # with per-photo metadata
```

Prints every public photo with its species and Macaulay Library link. The
command-line tester keeps its own `-vvv`, `-c`, `-cc`, and `-ccc` flags; they
were only removed from the Discord commands, which use `detail` instead.

## Rare bird reports

`/rare` posts recent **eBird-confirmed** rarities for a region that have
public photos, newest first:

```
/rare region:US-WA
/rare region:king county wa count:5 days:7
/rare region:US-WA detail:Full  (the detail option works here too)
/rare region:US-WA text:True    (one summary embed, photos not required)
```

- **Region**: an eBird code (`US`, `US-WA`, `US-WA-033`), a state or country
  name, a bare state abbreviation (`WA`), or a county with its state in any
  of these forms: `king county wa`, `king wa`, `King County, WA`,
  `king county washington`. Two-letter input prefers the US state when it
  isn't also a country code, so `WA` means Washington; use `AU-WA` for
  Western Australia. An ambiguous name (`king` on its own) comes back with
  candidates rather than a guess.
- **Confirmed**: only observations a regional reviewer has accepted
  (`obsValid`). Unreviewed reports of the same bird are skipped, so the list
  lags a live rare-bird alert by however long review takes.
- **With photos**: eBird's `hasRichMedia` flag is only a hint (it also covers
  audio, and media can be unindexed), so the bot verifies an actual public
  photo for each report and skips those without one.
- By default it shows the most recent report **per species**; set
  `repeats:True` to allow several reports of the same bird.
- `days` searches 1–30 days back (eBird's own limit).
- `text:True` drops the photo requirement and posts everything as a **single**
  embed: one line per report with rarity, date, place, observer, and a
  checklist link, but no images. It covers more ground, since photo-less
  reports are included and recent finds show up sooner. A 📷 marks reports
  that do have a photo (verified, not just eBird's `hasRichMedia` flag, which
  also covers audio and unindexed media); open the checklist link to see it.
  Long lists are trimmed with an "…and N more" line.

### Alert subscriptions (DMs)

`/alert` watches a region and DMs you when a new rare bird is reported there,
verified or not:

```
/alert region:king county wa
/alert region:US-WA rarity:🟠 Very rare or rarer
/alert region:island county wa confirmations:True
/alerts        (list your subscriptions)
/unalert region:US-WA      (or /unalert with no region to cancel all)
```

- **Cadence**: every watched region is polled every 5 minutes
  (`ALERT_INTERVAL_SECONDS`). Each poll reads eBird's notable feed for the
  last 3 days (`ALERT_WINDOW_DAYS`); that window is deliberately wider than
  the interval because the feed is ordered by *observation* date and
  checklists are often submitted hours or days late. A report is identified
  by checklist + species, so a wide window never re-sends anything.
- **Verified and unverified both alert.** Records eBird reviewers have
  *rejected* never do. Each DM states where review stands.
- **`confirmations:True`** adds a second DM if a report you were alerted to
  while it was pending is later accepted, titled "✅ Confirmed: …".
- **`rarity`** sets a floor using the same tiers as `/rare`, so you can watch
  a whole state for megas only and your county for anything.
- **No backlog on subscribe**: everything already in the window is marked as
  seen, so you only hear about what happens next. Re-subscribing to a region
  you already watch updates the settings and keeps that history.
- **Delivery**: alerts are DMs, so the bot must be able to message you; it
  sends a confirmation DM when you subscribe and warns you in the reply if
  that fails. After 3 consecutive failures a subscription pauses and `/alerts`
  shows it as paused; re-run `/alert` to resume. At most 10 DMs per region per
  poll, with the remainder carried to the next one.
- **Shared polling**: a region is polled once per sweep no matter how many
  people watch it, and the photo and rarity lookups behind those reports are
  shared too, so eBird sees the same load whether one person or fifty watch
  `US-WA-067`. Fetched feeds are also cached for two minutes and reused by
  `/rare`, so a lookup right after a poll costs nothing. The poll itself
  always reads fresh data, never the cache.
- Subscriptions live in `subscriptions.json` beside `bot.py` (gitignored),
  written atomically and reloaded on restart, so a restart never re-sends an
  alert or forgets a subscriber.

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
you can judge for yourself. County queries are scored at **state** scale, as
a single county's counts are too small to rank reliably; the embed cites the
state it used. Two caveats: it measures *photographic*
documentation, not sightings, and most eBird-flagged records are ordinary
species out of range or season; those land in the bottom tiers, which is
the honest answer. Regions with fewer than 500 prior photos fall back to
all-time counts.

## When the service won't start

Run the preflight check on the box, as the service user; it reports the exact
problem rather than leaving you to read a crash loop:

```sh
cd /opt/ebird-discord-bot && sudo -u ebird .venv/bin/python preflight.py
```

It verifies the three modules are present and import cleanly (a common cause
is copying one file but not the others), the dependency versions, that the
directory is writable, and that `DISCORD_TOKEN` is readable. For the raw
error, `journalctl -u ebird-discord-bot -n 40 --no-pager` shows the traceback.

Command registration can no longer take the bot down: if a sync fails, it
logs why and keeps running with whatever commands Discord already has.

## Run as a systemd service

The repo ships [ebird-discord-bot.service](ebird-discord-bot.service), written
for a deployment at `/opt/ebird-discord-bot` running as the `ebird` user;
edit the `WorkingDirectory`, `ExecStart`, and `User` lines to match your
setup. `.env` is read from `WorkingDirectory`, so it must sit in the checkout
alongside `bot.py`, readable by the service user (and ideally `chmod 600`).
The service user also needs **write** access to that directory: the bot saves
`subscriptions.json` (alert subscriptions) and `aliases.json` there, so
`chown -R ebird: /opt/ebird-discord-bot` after deploying.

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
