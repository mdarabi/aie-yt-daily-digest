"""Persistent run state: which videos were already digested, and which are deferred.

A video is marked *seen* only after the digest containing it was sent (or it was
intentionally skipped, e.g. a Short). Videos that aren't ready yet — still live,
or auto-captions not generated — are *deferred*: retried by id on every run until
they're ready or their retention lapses, so they can't fall out of the time window.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class Deferred:
    first_seen: datetime
    published: datetime | None
    title: str
    reason: str


@dataclass
class State:
    last_success: datetime | None = None
    seen: dict[str, datetime] = field(default_factory=dict)
    deferred: dict[str, Deferred] = field(default_factory=dict)

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        deferred = {}
        for vid, d in data.get("deferred", {}).items():
            deferred[vid] = Deferred(
                first_seen=_parse(d["first_seen"]),
                published=_parse(d["published"]) if d.get("published") else None,
                title=d.get("title", ""),
                reason=d.get("reason", ""),
            )
        return cls(
            last_success=_parse(data["last_success"]) if data.get("last_success") else None,
            seen={vid: _parse(ts) for vid, ts in data.get("seen", {}).items()},
            deferred=deferred,
        )

    def save(self, path: Path) -> None:
        data = {
            "last_success": _iso(self.last_success) if self.last_success else None,
            "seen": {vid: _iso(ts) for vid, ts in self.seen.items()},
            "deferred": {
                vid: {
                    "first_seen": _iso(d.first_seen),
                    "published": _iso(d.published) if d.published else None,
                    "title": d.title,
                    "reason": d.reason,
                }
                for vid, d in self.deferred.items()
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    # -- mutations -----------------------------------------------------------

    def mark_seen(self, video_id: str, when: datetime) -> None:
        self.seen[video_id] = when
        self.deferred.pop(video_id, None)

    def defer(self, video_id: str, title: str, reason: str,
              published: datetime | None, now: datetime) -> None:
        existing = self.deferred.get(video_id)
        self.deferred[video_id] = Deferred(
            first_seen=existing.first_seen if existing else now,
            published=published or (existing.published if existing else None),
            title=title,
            reason=reason,
        )

    def prune(self, now: datetime, seen_retention_days: int, deferred_retention_days: int) -> None:
        seen_cutoff = now - timedelta(days=seen_retention_days)
        self.seen = {vid: ts for vid, ts in self.seen.items() if ts >= seen_cutoff}
        deferred_cutoff = now - timedelta(days=deferred_retention_days)
        # A deferral that never resolved (e.g. a video that will never get captions
        # or a cancelled premiere) is dropped after retention; if it ever resurfaces
        # in the feed window it would be re-processed, which the seen-set then dedups.
        self.deferred = {
            vid: d for vid, d in self.deferred.items() if d.first_seen >= deferred_cutoff
        }
