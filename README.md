# eBird Checklist Photos — Discord Bot

One slash command: give it an eBird checklist link or ID, and it posts every
public photo from that checklist as embeds — species name, the image itself,
and a link to each photo's Macaulay Library page.

```
/checklist S378216909
/checklist https://ebird.org/checklist/S378216909
/checklist -vvv S378216909
```

Each photo is its own embed: the title links to the Macaulay Library asset
page, the description repeats the link in copyable form, and the footer
carries the ML catalog number and photographer credit. Photos are grouped by
species, 10 embeds per message.

Prefix the checklist with `-vvv` to attach each photo's full metadata to its
embed: observed date/time, location, coordinates (linked to a map), age/sex
counts, community rating, pixel dimensions, license ID, eBird species code,
and any notes or tags on the asset. Verbose mode posts 5 embeds per message
to stay inside Discord's per-message size limit.

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

Set `GUILD_ID` in `.env` (Server Settings → copy server ID) to make the
command appear instantly in that server; without it, global registration can
take up to an hour on first run.

## Test the fetcher without Discord

```sh
.venv/bin/python ebird_media.py S378216909
.venv/bin/python ebird_media.py -vvv S378216909   # with per-photo metadata
```

Prints every public photo with its species and Macaulay Library link.

## Run as a systemd service

The repo ships [ebird-discord-bot.service](ebird-discord-bot.service), written
for a deployment at `/opt/ebird-discord-bot` running as the `ebird` user —
edit the `WorkingDirectory`, `ExecStart`, and `User` lines to match your
setup. `.env` is read from `WorkingDirectory`, so it must sit in the checkout
alongside `bot.py`, readable by the service user (and ideally `chmod 600`).

```sh
sudo useradd --system --shell /usr/bin/nologin ebird   # once, if it doesn't exist
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
  all clients — the bot never downloads or rehosts photos.
- Cornell fronts its sites with an anti-bot challenge (Anubis) for
  **browser-like** clients. This bot sends an honest, non-browser
  `User-Agent` (`ebird-checklist-discord-bot/1.0`), which the policy lets
  through. Don't "upgrade" it to a browser UA string — that gets challenged
  and the API will return HTML instead of JSON.

## Limits and notes

- Posts at most 50 photos per command (5 messages of 10 embeds) and links the
  checklist for the rest; fetches at most 400 via pagination.
- Public media only — a hidden checklist comes back as "no public photos".
- Photos are © their photographers, archived by the Macaulay Library. The bot
  links and embeds rather than copying; keep usage within the
  [Cornell Lab terms of use](https://www.birds.cornell.edu/home/terms-of-use/).
