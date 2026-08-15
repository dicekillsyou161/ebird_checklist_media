# Terms of Service

**Last updated: August 15, 2026**

These terms cover your use of the eBird checklist Discord bot (the "Bot"), a
free, personally operated Discord application that surfaces publicly available
birding records and photographs from eBird and the Macaulay Library. By adding
the Bot to a server, installing it to your account, or using any of its
commands, you agree to these terms. If you do not agree, do not use the Bot.

## 1. What the Bot does

The Bot looks up publicly available data (checklists, media, notable-bird
feeds) through eBird's public interfaces and presents it in Discord: photo
embeds, metadata summaries, rarity digests, and optional direct-message alerts
for rare bird reports in regions you choose. It links and embeds media hosted
by the Cornell Lab of Ornithology; it does not copy, store, or rehost
photographs.

## 2. Not affiliated with eBird or Discord

The Bot is an independent hobby project. It is not affiliated with, endorsed
by, or sponsored by the Cornell Lab of Ornithology, eBird, the Macaulay
Library, or Discord Inc. All bird photographs remain the property of their
photographers and are archived by the Macaulay Library. Your use of content
reached through the Bot must comply with the
[Cornell Lab terms of use](https://www.birds.cornell.edu/home/terms-of-use/),
and your use of Discord must comply with the
[Discord Terms of Service](https://discord.com/terms).

## 3. Eligibility

You must meet Discord's minimum age requirement (13, or older where your
country requires it) to use the Bot.

## 4. Acceptable use

You agree not to:

- use the Bot to harass, stalk, or identify the movements of any person,
  including bird observers whose names appear on public checklists;
- use location information surfaced by the Bot in ways that endanger birds,
  such as disturbing sensitive nesting sites; follow the birding community's
  ethics guidelines when acting on rare bird reports;
- flood, script, or automate commands in a way that degrades the Bot or
  places unreasonable load on eBird's services;
- attempt to gain access to data the Bot does not expose, or to its server;
- resell or commercially redistribute output from the Bot.

The operator may block users or servers that violate these rules, without
notice.

## 5. What the Bot stores

The Bot stores the minimum it needs to function, in a database on the
operator's server, readable only by the service account:

- **Account link**: if you run `/iam`, your Discord user ID paired with the
  public eBird profile ID you chose.
- **Learned names**: pairs of publicly displayed eBird photographer names and
  their public profile IDs, gathered from search results so users can be
  looked up by name.
- **Alert subscriptions**: if you run `/alert`, your Discord user ID, the
  watched region, your chosen settings, delivery counters, and identifiers of
  reports already sent to you (so nothing is sent twice). This includes, for
  display back to you, the species name and rarity tier of recent reports.

The Bot does not store message content, does not track your Discord activity,
and does not use analytics, advertising, or tracking of any kind. Stored data
is never sold or shared with third parties. Lookups the Bot performs against
eBird are subject to Cornell's own privacy practices.

To remove your data: `/unalert` deletes a subscription and everything
remembered for it; running `/iam` again replaces your account link. For full
removal of any stored identifier, contact the operator (section 10). The
[Privacy Policy](PRIVACY_POLICY.md) covers storage, retention, and your
choices in more detail.

## 6. Direct messages

Rare-bird alerts arrive as Discord DMs that you explicitly request with
`/alert`. You can stop them at any time with `/unalert`. If your DMs reject
messages repeatedly, the subscription pauses automatically.

## 7. No warranty

The Bot is provided **as is** and **as available**, free of charge, with no
warranty of any kind. Data comes from third-party services and may be
incomplete, delayed, or wrong; rarity tiers are statistical estimates, not
official designations. The Bot may change, break, or shut down at any time
without notice.

## 8. Limitation of liability

To the maximum extent permitted by law, the operator is not liable for any
damages arising from your use of, or inability to use, the Bot. Because the
Bot is free, the total liability for any claim is limited to the amount you
paid to use it: zero.

## 9. Changes to these terms

These terms may be updated from time to time; the "Last updated" date above
changes when they are. Continued use of the Bot after an update means you
accept the revised terms.

## 10. Contact

Questions, data-removal requests, or problems: contact the operator at
**[YOUR CONTACT: email, Discord username, or repository issues page]**.

## 11. Governing law

These terms are governed by the laws of **[YOUR STATE / COUNTRY]**, without
regard to conflict-of-law rules.
