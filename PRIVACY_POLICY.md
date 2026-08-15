# Privacy Policy

**Last updated: August 15, 2026**

This policy describes what the eBird checklist Discord bot (the "Bot")
collects, why, and what happens to it. The short version: the Bot stores the
minimum it needs to run its features, keeps it in a single database on the
operator's server, and never sells or shares it. There are no analytics, no
advertising, and no tracking.

## 1. Data the Bot stores

The Bot stores three kinds of data, all of it created by you using a command
or already public on eBird:

- **Account link** (`/iam`): your Discord user ID paired with the public
  eBird profile ID you chose, so `/top` and `/recent` can default to your own
  photos and so others can reference you by name or @mention.
- **Learned names**: pairs of publicly displayed eBird photographer names and
  their public eBird profile IDs, gathered from public search results. These
  identify eBird profiles, not Discord accounts.
- **Alert subscriptions** (`/alert`): your Discord user ID, the region you
  watch, your chosen rarity tier and settings, delivery counters, and
  identifiers of rare-bird reports already sent to you so nothing is sent
  twice. For display back to you in `/alerts`, each remembered report also
  keeps its species name and rarity tier.

## 2. Data the Bot does not collect

- No message content. The Bot only receives the slash commands you invoke
  and their parameters, processes them to answer, and does not store them.
- No presence, activity, or server membership tracking.
- No analytics, advertising identifiers, cookies, or fingerprinting.
- No data about anyone who has never used a command (learned names describe
  public eBird profiles, not Discord users).

## 3. How data is used

Stored data is used only to provide the Bot's features: defaulting commands
to your linked eBird profile, resolving names you type to public eBird
profiles, and delivering the rare-bird alerts you subscribed to. It is not
used for anything else.

## 4. Where data lives and how it is protected

Everything lives in one SQLite database file on the operator's server,
readable only by the service account the Bot runs as. Traffic between you and
Discord is protected by Discord; traffic between the Bot and eBird uses
HTTPS. Queries the Bot sends to eBird contain only eBird-side information
(region codes, public profile IDs, checklist IDs); your Discord identity is
never sent to eBird. The Bot's operational logs may record Discord user IDs
alongside delivery failures (for example, when a DM bounces) to keep alerts
working; logs are routine server logs and are not mined for anything.

## 5. Sharing

Stored data is never sold, rented, or shared with third parties. The only
parties that see any of it are Discord (as the platform carrying the
messages) and the operator (as the person running the server).

## 6. Retention

- **Alert subscriptions**: kept until you run `/unalert`, which deletes the
  subscription and everything remembered for it. Remembered report
  identifiers also expire automatically a few days after the observation
  leaves the alert window.
- **Account links and learned names**: kept until you ask for removal.
  Running `/iam` again replaces your link.
- Routine server backups, where they exist, age out on the operator's normal
  schedule.

## 7. Your choices and rights

- Run `/unalert` to delete alert data yourself, immediately.
- Run `/iam` to replace your account link.
- Contact the operator (section 9) to have any stored identifier removed
  completely, or to ask what is stored about you.

## 8. Children

The Bot follows Discord's age requirements: you must be at least 13, or older
where your country requires it.

## 9. Contact

Privacy questions, access requests, or removal requests: contact the operator
at **[YOUR CONTACT: email, Discord username, or repository issues page]**.

## 10. Changes to this policy

This policy may be updated from time to time; the "Last updated" date above
changes when it is. Material changes will be reflected here before they take
effect.
