"""Rare-bird alert subscriptions: who watches which region, and what they've seen.

One JSON file holds every subscription. Each carries its own `seen` map, so a
new subscriber never gets a backlog of the whole window and a bot restart never
re-sends an alert that already went out.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ebird_media import STATUS_CONFIRMED, STATUS_PENDING

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
    # notable_key -> [obsDt, status] of what has already been delivered
    seen: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> Subscription:
        known = {f.name for f in fields(cls)}
        sub = cls(**{key: value for key, value in data.items() if key in known})
        sub.user_id = str(sub.user_id)
        sub.region = str(sub.region).upper()
        sub.seen = {
            str(key): [str(value[0]), str(value[1])]
            for key, value in (sub.seen or {}).items()
            if isinstance(value, (list, tuple)) and len(value) == 2
        }
        return sub

    def to_dict(self) -> dict:
        return asdict(self)

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
        return RARITY_RANK.get(label, 0) >= self.min_rarity

    def mark_seen(self, key: str, obs_dt: str, status: str) -> None:
        self.seen[key] = [obs_dt or "", status]

    def prune(self, cutoff: str) -> bool:
        """Forget reports older than the poll window; they can't come back."""
        kept = {key: value for key, value in self.seen.items() if value[0] >= cutoff}
        if len(kept) == len(self.seen):
            return False
        self.seen = kept
        return True


class AlertStore:
    """The subscription list, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.subscriptions: list[Subscription] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        raw = data.get("subscriptions") if isinstance(data, dict) else None
        subscriptions = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            try:
                subscriptions.append(Subscription.from_dict(entry))
            except (TypeError, ValueError, AttributeError) as error:
                print(f"Skipping unreadable subscription in {self.path.name}: {error}")
        self.subscriptions = subscriptions

    def save(self) -> None:
        payload = {"subscriptions": [sub.to_dict() for sub in self.subscriptions]}
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(self.path)  # atomic: a crash mid-write can't truncate the list
        except OSError as error:
            print(f"Couldn't save {self.path.name}: {error}")

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
