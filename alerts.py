"""Rare-bird alert subscriptions: who watches which region, and what they've seen.

State lives in the shared SQLite database (see db.py). Each subscription
carries its own `seen` map, so a new subscriber never gets a backlog of the
whole window and a bot restart never re-sends an alert that already went out.
A subscriptions.json from before the database is imported once and renamed
*.migrated.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ebird_media import STATUS_CONFIRMED, STATUS_PENDING, STATUS_REJECTED

# The tiers ebird_media assigns, ranked so a subscription can ask for
# "this rare or rarer". Unknown labels (an eBird region with no photo history)
# rank 0, so an "anything" subscription still sees them.
RARITY_RANK = {
    "Locally notable": 0,
    "Notable sighting": 0,
    "Scarce": 1,
    "Rare": 2,
    "Very rare": 3,
    "Mega rarity": 4,
}
RARITY_LEVELS = (
    (0, "Anything eBird flags"),
    (1, "🟢 Scarce or rarer"),
    (2, "🟡 Rare or rarer"),
    (3, "🟠 Very rare or rarer"),
    (4, "🔴 Mega rarities only"),
)
RARITY_LABELS = dict(RARITY_LEVELS)

NEW_REPORT = "new"       # nobody has been told about this report yet
CONFIRMATION = "confirmed"  # already alerted while pending, now reviewed & accepted

STATUS_MARKS = {STATUS_CONFIRMED: "✅", STATUS_PENDING: "⏳", STATUS_REJECTED: "❌"}

# One remembered report is [obs_dt, status, species, rarity, place]. Everything
# after status is display-only, so /alerts can list recent reports without
# re-fetching anything; fields stay empty on rows written before they existed.
# `place` is county ("King") for a state region, "King, Washington" for a
# country, and empty for a county region, where it would just repeat.
_SEEN_WIDTH = 5

_SUB_COLUMNS = (
    "user_id, region, region_label, min_rarity, confirmations, "
    "created, alerts_sent, last_alert, paused, failures, show_rarity"
)


@dataclass
class Subscription:
    """One user's standing request for alerts in one region."""

    user_id: str
    region: str                 # canonical eBird code, e.g. US-WA-033
    region_label: str = ""      # "King, Washington, United States"
    min_rarity: int = 0
    confirmations: bool = False  # also DM when a pending report gets accepted
    created: str = ""
    alerts_sent: int = 0
    last_alert: str = ""
    paused: bool = False        # DMs kept failing; user must re-subscribe
    failures: int = 0
    show_rarity: bool = False   # opt-in estimated tier labels in alert DMs
    # notable_key -> [obsDt, status] of what has already been delivered
    seen: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> Subscription:
        known = {f.name for f in fields(cls)}
        sub = cls(**{key: value for key, value in data.items() if key in known})
        sub.user_id = str(sub.user_id)
        sub.region = str(sub.region).upper()
        sub.seen = {
            str(key): ([str(part) for part in value] + [""] * _SEEN_WIDTH)[:_SEEN_WIDTH]
            for key, value in (sub.seen or {}).items()
            if isinstance(value, (list, tuple)) and 2 <= len(value) <= _SEEN_WIDTH
        }
        return sub

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> tuple:
        return (
            self.user_id, self.region, self.region_label, self.min_rarity,
            int(self.confirmations), self.created, self.alerts_sent,
            self.last_alert, int(self.paused), self.failures,
            int(self.show_rarity),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Subscription:
        return cls(
            user_id=row["user_id"],
            region=row["region"],
            region_label=row["region_label"],
            min_rarity=row["min_rarity"],
            confirmations=bool(row["confirmations"]),
            created=row["created"],
            alerts_sent=row["alerts_sent"],
            last_alert=row["last_alert"],
            paused=bool(row["paused"]),
            failures=row["failures"],
            show_rarity=bool(row["show_rarity"]),
        )

    @property
    def display_region(self) -> str:
        return self.region_label or self.region

    @property
    def rarity_label(self) -> str:
        return RARITY_LABELS.get(self.min_rarity, RARITY_LABELS[0])

    def pending_kind(self, key: str, status: str) -> str | None:
        """What this subscriber is still owed for a report: new, confirmation, or nothing."""
        prior = self.seen.get(key)
        if prior is None:
            return NEW_REPORT
        if self.confirmations and prior[1] == STATUS_PENDING and status == STATUS_CONFIRMED:
            return CONFIRMATION
        return None

    def wants_rarity(self, label: str) -> bool:
        # At state or country scale, "Locally notable" reports are noise: a
        # common bird tripping one county's filter. Only that exact label is
        # dropped; an unknown tier ("Notable sighting") still alerts, so a
        # rarity-scoring outage can't silently mute a whole subscription.
        if label == "Locally notable" and self.region.count("-") < 2:
            return False
        return RARITY_RANK.get(label, 0) >= self.min_rarity

    def mark_seen(
        self, key: str, obs_dt: str, status: str,
        species: str = "", rarity: str = "", place: str = "",
    ) -> None:
        prior = self.seen.get(key)
        if prior is not None:  # a status update mustn't blank what's already known
            species = species or prior[2]
            rarity = rarity or prior[3]
            place = place or prior[4]
        self.seen[key] = [obs_dt or "", status, species, rarity, place]

    def recent_seen(self, limit: int = 3) -> list[tuple[str, list[str]]]:
        """The newest remembered reports, for showing back in /alerts."""
        newest_first = sorted(
            self.seen.items(), key=lambda item: item[1][0], reverse=True
        )
        return newest_first[:limit]

    def prune(self, cutoff: str) -> bool:
        """Forget reports older than the poll window; they can't come back."""
        kept = {key: value for key, value in self.seen.items() if value[0] >= cutoff}
        if len(kept) == len(self.seen):
            return False
        self.seen = kept
        return True


class AlertStore:
    """The subscription list, persisted in the bot's SQLite database.

    Subscriptions are worked on in memory exactly as before; `save()` writes
    the whole current state in one transaction, so a crash mid-write can
    never leave a half-updated list behind.
    """

    def __init__(self, conn: sqlite3.Connection, legacy_json: Path | None = None) -> None:
        self.conn = conn
        self.subscriptions: list[Subscription] = []
        if legacy_json is not None:
            self._import_legacy(Path(legacy_json))
        self.load()

    def _import_legacy(self, path: Path) -> None:
        """One-time import of a subscriptions.json from before the database."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            return  # no legacy file: nothing to migrate
        except ValueError as error:
            print(f"Ignoring unreadable {path.name}: {error}")
            return
        raw = data.get("subscriptions") if isinstance(data, dict) else None
        imported = 0
        with self.conn:
            for entry in raw or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    sub = Subscription.from_dict(entry)
                except (TypeError, ValueError, AttributeError) as error:
                    print(f"Skipping unreadable subscription in {path.name}: {error}")
                    continue
                cursor = self.conn.execute(
                    f"INSERT OR IGNORE INTO subscriptions ({_SUB_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    sub.to_row(),
                )
                if cursor.rowcount:
                    imported += 1
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO seen"
                        " (user_id, region, key, obs_dt, status, species, rarity, place)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (sub.user_id, sub.region, key, *value)
                            for key, value in sub.seen.items()
                        ],
                    )
        try:
            path.replace(path.with_name(path.name + ".migrated"))
            print(f"Imported {imported} subscription(s) from {path.name}.")
        except OSError as error:
            print(f"Imported {path.name} but couldn't rename it afterwards: {error}")

    def load(self) -> None:
        subscriptions: list[Subscription] = []
        by_key: dict[tuple[str, str], Subscription] = {}
        for row in self.conn.execute("SELECT * FROM subscriptions"):
            sub = Subscription.from_row(row)
            subscriptions.append(sub)
            by_key[(sub.user_id, sub.region)] = sub
        for row in self.conn.execute(
            "SELECT user_id, region, key, obs_dt, status, species, rarity, place FROM seen"
        ):
            sub = by_key.get((row["user_id"], row["region"]))
            if sub is not None:
                sub.seen[row["key"]] = [
                    row["obs_dt"], row["status"], row["species"], row["rarity"],
                    row["place"],
                ]
        self.subscriptions = subscriptions

    def save(self) -> None:
        """Persist the in-memory state: one transaction, all or nothing."""
        with self.conn:
            self.conn.execute("DELETE FROM subscriptions")  # seen rows cascade away
            for sub in self.subscriptions:
                self.conn.execute(
                    f"INSERT INTO subscriptions ({_SUB_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    sub.to_row(),
                )
                self.conn.executemany(
                    "INSERT INTO seen"
                    " (user_id, region, key, obs_dt, status, species, rarity, place)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (sub.user_id, sub.region, key, *value)
                        for key, value in sub.seen.items()
                    ],
                )

    def find(self, user_id: str, region: str) -> Subscription | None:
        region = region.upper()
        return next(
            (s for s in self.subscriptions if s.user_id == user_id and s.region == region),
            None,
        )

    def for_user(self, user_id: str) -> list[Subscription]:
        return [s for s in self.subscriptions if s.user_id == user_id]

    def add(self, subscription: Subscription) -> None:
        """Add or replace; a re-subscribe keeps the old seen set and counters."""
        previous = self.find(subscription.user_id, subscription.region)
        if previous is not None:
            subscription.seen = {**previous.seen, **subscription.seen}
            subscription.alerts_sent = previous.alerts_sent
            subscription.last_alert = previous.last_alert
            subscription.created = previous.created or subscription.created
            self.subscriptions.remove(previous)
        self.subscriptions.append(subscription)

    def remove(self, user_id: str, region: str) -> Subscription | None:
        found = self.find(user_id, region)
        if found is not None:
            self.subscriptions.remove(found)
        return found

    def remove_all(self, user_id: str) -> int:
        mine = self.for_user(user_id)
        self.subscriptions = [s for s in self.subscriptions if s.user_id != user_id]
        return len(mine)

    def active(self) -> list[Subscription]:
        return [s for s in self.subscriptions if not s.paused]

    def regions(self) -> list[str]:
        return sorted({s.region for s in self.active()})
